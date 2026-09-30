"""U3' (T-tier-c-admission-gate, Bohr msg-4774 / msg-4776, Einstein go): verdict-relay routing.

The route after a PR-gate verdict is decided once, by
:func:`~spirrow_mindwire.conductor.gate_records.decide_relay_route`, called once from
``PrReviewOrchestrator._post_design_relay``. These tests pin the msg-4774 table (tests 1-6),
the implementer instruction (test 8) and the pure function (test 9). Tests 7 and 10 — the
conductor's fail-safe and its obedience to the relay's ``route`` — live next to the other
PR-gate conductor tests in ``tests/test_conductor_core.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from spirrow_mindwire.conductor.gate_records import (
    ADVISORY_SELF_TRIAGE_INSTRUCTION,
    MERGE_REQUEST_TIER_C_LABEL,
    MERGE_REQUEST_TIER_C_LINE,
    RELAY_AUTHOR,
    RelayRoute,
    decide_relay_route,
    prior_advisory_approvals,
    render_relay_heading,
)
from spirrow_mindwire.conductor.handoff import parse_next_token
from spirrow_mindwire.github.client import CiState, ReviewEvent
from spirrow_mindwire.magickit.client import MagickitMcpError
from spirrow_mindwire.naysayer.pr_review import (
    ObjectionParse,
    PrReviewOutcome,
    parse_objections,
)
from spirrow_mindwire.naysayer.principles import objection_classes
from spirrow_mindwire.orchestrator import PrReviewOrchestrator
from spirrow_mindwire.tier_c_admission_gate import ADMIT_LABELS, require_admitted

_PR = "o/r#7"
_DESIGN = "T-some-design-thread"
_IMPL = "Heisenberg"
_SENTINEL = "<!-- mindwire:objections v1 -->"


def _advisory_class() -> str:
    return next(name for name, entry in objection_classes().items() if not entry.blocks)


def _blocking_class() -> str:
    return next(name for name, entry in objection_classes().items() if entry.blocks)


_ADVISORY = json.dumps(
    [{"class": _advisory_class(), "where": "src/y.py:7", "evidence": "reads oddly"}]
)
_BLOCKING = json.dumps([{"class": _blocking_class(), "where": "src/x.py:4", "evidence": "n is 0"}])


def _outcome(event: ReviewEvent, objections: str | None, head: str) -> PrReviewOutcome:
    """A critique shaped like production's: prose, objection block, verdict, verdict footer."""
    parts = ["critique prose"]
    if objections is not None:
        parts.append(f"{_SENTINEL}\n{objections}")
    parts.append(f"VERDICT: {event.value}")
    parts.append(f"<!-- mindwire:verdict head_sha={head} event={event.value} -->")
    return PrReviewOutcome(
        verdict=event, body="\n\n".join(parts), ci_state=CiState.SUCCESS, head_sha=head
    )


class _ThreadMcp:
    """A design thread that remembers what is posted into it, so relays accumulate history."""

    def __init__(self, *, fail_read: bool = False) -> None:
        self.messages: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self._fail_read = fail_read

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(name)
        if name == "chatroom_get_thread":
            if self._fail_read:
                raise MagickitMcpError("transport down")
            return {"thread": {"title": "design"}, "messages": list(self.messages)}
        if name == "chatroom_post_message":
            msg_id = f"msg-{len(self.messages) + 1}"
            self.messages.append(
                {"msg_id": msg_id, "author": arguments["author"], "content": arguments["content"]}
            )
            return {"msg": {"msg_id": msg_id}}
        raise AssertionError(f"unexpected tool {name}")


class _NoDriver:
    async def aclose(self) -> None:  # pragma: no cover - never reached
        return None


async def _relay(
    mcp: _ThreadMcp, outcome: PrReviewOutcome, implementer: str | None = _IMPL
) -> dict[str, Any]:
    orch = PrReviewOrchestrator(mcp, driver=_NoDriver())  # type: ignore[arg-type]
    return await orch._post_design_relay(
        project="p",
        design_thread=_DESIGN,
        pr_ref=_PR,
        outcome=outcome,
        implementer=implementer,
    )


# ---- tests 1-6: the msg-4774 table through the production relay writer ----------------------- #


@pytest.mark.anyio
async def test_1_approve_without_advisory_stops_at_human_as_merge_request() -> None:
    mcp = _ThreadMcp()
    relay = await _relay(mcp, _outcome(ReviewEvent.APPROVE, "[]", "aaaaaaa1"))
    assert relay["route"] is RelayRoute.HUMAN
    assert relay["content"].endswith(f"{MERGE_REQUEST_TIER_C_LINE}\nNEXT: human")
    assert ADVISORY_SELF_TRIAGE_INSTRUCTION not in relay["content"]
    # No history read: it could not change the answer.
    assert "chatroom_get_thread" not in mcp.calls


@pytest.mark.anyio
async def test_2_first_advisory_approve_goes_to_implementer() -> None:
    mcp = _ThreadMcp()
    relay = await _relay(mcp, _outcome(ReviewEvent.APPROVE, _ADVISORY, "aaaaaaa1"))
    assert relay["route"] is RelayRoute.IMPLEMENTER
    assert parse_next_token(relay["content"]) == _IMPL


@pytest.mark.anyio
async def test_3_request_changes_then_fix_then_advisory_goes_to_implementer() -> None:
    # Einstein msg-4773: a prior REQUEST_CHANGES relay must not count as a prior advisory.
    mcp = _ThreadMcp()
    first = await _relay(mcp, _outcome(ReviewEvent.REQUEST_CHANGES, _BLOCKING, "aaaaaaa1"))
    assert first["route"] is RelayRoute.IMPLEMENTER
    second = await _relay(mcp, _outcome(ReviewEvent.APPROVE, _ADVISORY, "bbbbbbb2"))
    assert second["route"] is RelayRoute.IMPLEMENTER
    assert parse_next_token(second["content"]) == _IMPL


@pytest.mark.anyio
async def test_4_second_advisory_approve_stops_at_human() -> None:
    mcp = _ThreadMcp()
    first = await _relay(mcp, _outcome(ReviewEvent.APPROVE, _ADVISORY, "aaaaaaa1"))
    assert first["route"] is RelayRoute.IMPLEMENTER
    second = await _relay(mcp, _outcome(ReviewEvent.APPROVE, _ADVISORY, "bbbbbbb2"))
    assert second["route"] is RelayRoute.HUMAN
    assert second["content"].endswith(f"{MERGE_REQUEST_TIER_C_LINE}\nNEXT: human")
    assert ADVISORY_SELF_TRIAGE_INSTRUCTION not in second["content"]


@pytest.mark.anyio
async def test_5_advisory_then_request_changes_then_advisory_stops_at_human() -> None:
    # The count stays at 1 across an intervening REQUEST_CHANGES.
    mcp = _ThreadMcp()
    await _relay(mcp, _outcome(ReviewEvent.APPROVE, _ADVISORY, "aaaaaaa1"))
    rc = await _relay(mcp, _outcome(ReviewEvent.REQUEST_CHANGES, _BLOCKING, "bbbbbbb2"))
    assert rc["route"] is RelayRoute.IMPLEMENTER
    third = await _relay(mcp, _outcome(ReviewEvent.APPROVE, _ADVISORY, "ccccccc3"))
    assert third["route"] is RelayRoute.HUMAN


@pytest.mark.anyio
async def test_6_missing_objections_stops_at_human() -> None:
    mcp = _ThreadMcp()
    relay = await _relay(mcp, _outcome(ReviewEvent.APPROVE, None, "aaaaaaa1"))
    assert relay["route"] is RelayRoute.HUMAN
    assert parse_objections(relay["content"]).status is ObjectionParse.MISSING


# ---- test 8: the implementer instruction appears on exactly one route ------------------------ #


@pytest.mark.anyio
async def test_8_instruction_only_on_first_advisory_implementer_relay() -> None:
    mcp = _ThreadMcp()
    adv = await _relay(mcp, _outcome(ReviewEvent.APPROVE, _ADVISORY, "aaaaaaa1"))
    body: str = adv["content"]
    assert ADVISORY_SELF_TRIAGE_INSTRUCTION in body
    # Placed directly above the NEXT: line.
    assert body.endswith(f"{ADVISORY_SELF_TRIAGE_INSTRUCTION}\n\nNEXT: {_IMPL}")
    for other in (
        await _relay(_ThreadMcp(), _outcome(ReviewEvent.REQUEST_CHANGES, _BLOCKING, "bbbbbbb2")),
        await _relay(_ThreadMcp(), _outcome(ReviewEvent.APPROVE, "[]", "ccccccc3")),
        await _relay(_ThreadMcp(), _outcome(ReviewEvent.APPROVE, None, "ddddddd4")),
        await _relay(mcp, _outcome(ReviewEvent.APPROVE, _ADVISORY, "eeeeeee5")),  # 2nd
    ):
        assert ADVISORY_SELF_TRIAGE_INSTRUCTION not in other["content"]


@pytest.mark.anyio
async def test_relay_framing_does_not_change_what_the_body_parses_as() -> None:
    # prior_advisory_approvals re-parses relay BODIES; the heading, VERDICT line, instruction and
    # NEXT line must not change the objection parse relative to the critique itself.
    for objections in (_ADVISORY, "[]", None):
        outcome = _outcome(ReviewEvent.APPROVE, objections, "aaaaaaa1")
        relay = await _relay(_ThreadMcp(), outcome)
        assert parse_objections(relay["content"]).status is parse_objections(outcome.body).status


@pytest.mark.anyio
async def test_unreadable_history_fails_to_human() -> None:
    mcp = _ThreadMcp(fail_read=True)
    relay = await _relay(mcp, _outcome(ReviewEvent.APPROVE, _ADVISORY, "aaaaaaa1"))
    assert relay["route"] is RelayRoute.HUMAN


@pytest.mark.anyio
async def test_no_implementer_fails_to_human() -> None:
    relay = await _relay(
        _ThreadMcp(), _outcome(ReviewEvent.APPROVE, _ADVISORY, "aaaaaaa1"), implementer=None
    )
    assert relay["route"] is RelayRoute.HUMAN
    rc = await _relay(
        _ThreadMcp(),
        _outcome(ReviewEvent.REQUEST_CHANGES, _BLOCKING, "aaaaaaa1"),
        implementer=None,
    )
    assert rc["route"] is RelayRoute.HUMAN
    assert MERGE_REQUEST_TIER_C_LINE not in rc["content"]  # not a merge request


# ---- test 9: the pure function, every row ---------------------------------------------------- #


@pytest.mark.parametrize(
    ("verdict", "objections", "prior", "implementer", "expected"),
    [
        (ReviewEvent.REQUEST_CHANGES, ObjectionParse.OK, 0, _IMPL, RelayRoute.IMPLEMENTER),
        (ReviewEvent.REQUEST_CHANGES, ObjectionParse.MISSING, 3, _IMPL, RelayRoute.IMPLEMENTER),
        (ReviewEvent.REQUEST_CHANGES, ObjectionParse.OK, 0, None, RelayRoute.HUMAN),
        (ReviewEvent.APPROVE, ObjectionParse.EMPTY, 0, _IMPL, RelayRoute.HUMAN),
        (ReviewEvent.APPROVE, ObjectionParse.MISSING, 0, _IMPL, RelayRoute.HUMAN),
        (ReviewEvent.APPROVE, ObjectionParse.OK, 0, _IMPL, RelayRoute.IMPLEMENTER),
        (ReviewEvent.APPROVE, ObjectionParse.UNKNOWN, 0, _IMPL, RelayRoute.IMPLEMENTER),
        (ReviewEvent.APPROVE, ObjectionParse.NO_EVIDENCE, 0, _IMPL, RelayRoute.IMPLEMENTER),
        (ReviewEvent.APPROVE, ObjectionParse.OK, 1, _IMPL, RelayRoute.HUMAN),
        (ReviewEvent.APPROVE, ObjectionParse.OK, 5, _IMPL, RelayRoute.HUMAN),
        (ReviewEvent.APPROVE, ObjectionParse.OK, None, _IMPL, RelayRoute.HUMAN),
        (ReviewEvent.APPROVE, ObjectionParse.OK, 0, None, RelayRoute.HUMAN),
        (ReviewEvent.APPROVE, ObjectionParse.OK, 0, "", RelayRoute.HUMAN),
        (ReviewEvent.COMMENT, ObjectionParse.OK, 0, _IMPL, RelayRoute.HUMAN),
        (ReviewEvent.COMMENT, ObjectionParse.EMPTY, 0, _IMPL, RelayRoute.HUMAN),
    ],
)
def test_9_decide_relay_route_table(
    verdict: ReviewEvent,
    objections: ObjectionParse,
    prior: int | None,
    implementer: str | None,
    expected: RelayRoute,
) -> None:
    assert decide_relay_route(verdict, objections, prior, implementer) is expected


# ---- the counter ---------------------------------------------------------------------------- #


def _relay_body(pr_ref: str, event: ReviewEvent, objections: str | None, head: str) -> str:
    outcome = _outcome(event, objections, head)
    return f"{render_relay_heading(pr_ref, head)}\n\nVERDICT: {event.value}\n\n{outcome.body}"


def test_counter_counts_only_this_prs_advisory_approvals_by_the_relay_author() -> None:
    msgs = [
        (RELAY_AUTHOR, _relay_body(_PR, ReviewEvent.APPROVE, _ADVISORY, "aaaaaaa1")),  # counted
        (RELAY_AUTHOR, _relay_body(_PR, ReviewEvent.APPROVE, "[]", "aaaaaaa2")),  # clean
        (RELAY_AUTHOR, _relay_body(_PR, ReviewEvent.APPROVE, None, "aaaaaaa3")),  # missing
        (RELAY_AUTHOR, _relay_body(_PR, ReviewEvent.REQUEST_CHANGES, _BLOCKING, "aaaaaaa4")),
        (RELAY_AUTHOR, _relay_body("o/r#70", ReviewEvent.APPROVE, _ADVISORY, "aaaaaaa5")),
        (RELAY_AUTHOR, _relay_body("o/other#7", ReviewEvent.APPROVE, _ADVISORY, "aaaaaaa6")),
        ("Einstein", _relay_body(_PR, ReviewEvent.APPROVE, _ADVISORY, "aaaaaaa7")),  # quoted
    ]
    assert prior_advisory_approvals(msgs, _PR) == 1


def test_counter_reads_a_heading_without_a_head_sha() -> None:
    body = (
        f"{render_relay_heading(_PR, None)}\n\nVERDICT: APPROVE\n\n"
        f"{_outcome(ReviewEvent.APPROVE, _ADVISORY, 'aaaaaaa1').body}"
    )
    assert prior_advisory_approvals([(RELAY_AUTHOR, body)], _PR) == 1


def test_counter_is_zero_on_empty_history() -> None:
    assert prior_advisory_approvals([], _PR) == 0


# ---------------------------------------------------------------------------
# PR #365 PR-gate advisory (msg-4850), human-chosen B: the merge-request label is resolved
# against the gate's admitted set, not hardcoded beside it.
# ---------------------------------------------------------------------------


def test_merge_request_label_is_admitted_and_drives_both_texts() -> None:
    assert MERGE_REQUEST_TIER_C_LABEL in ADMIT_LABELS
    assert f"TIER-C: {MERGE_REQUEST_TIER_C_LABEL}" == MERGE_REQUEST_TIER_C_LINE
    # The implementer instruction teaches the same line, not a second copy of the label.
    assert f"`{MERGE_REQUEST_TIER_C_LINE}`" in ADVISORY_SELF_TRIAGE_INSTRUCTION


def test_require_admitted_fails_loudly_on_an_unadmitted_label() -> None:
    assert require_admitted("merge-protected", where="t") == "merge-protected"
    with pytest.raises(RuntimeError, match=r"gate_records: .*'merge-guarded'"):
        require_admitted("merge-guarded", where="gate_records")


def test_gate_records_import_fails_if_gate_drops_merge_protected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate that stops admitting the label makes gate_records unimportable (no silent drift)."""
    # Local imports on purpose: this test re-imports gate_records via sys.modules, so its
    # module handles stay scoped to the test rather than joining the file-level constants.
    import importlib
    import sys

    import spirrow_mindwire.tier_c_admission_gate as gate

    monkeypatch.setattr(gate, "ADMIT_LABELS", frozenset({"goal", "cost", "irreversible"}))
    saved = sys.modules.pop("spirrow_mindwire.conductor.gate_records")
    try:
        with pytest.raises(RuntimeError, match="merge-protected"):
            importlib.import_module("spirrow_mindwire.conductor.gate_records")
    finally:
        sys.modules["spirrow_mindwire.conductor.gate_records"] = saved
