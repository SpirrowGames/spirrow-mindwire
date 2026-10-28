"""U3' (T-tier-c-admission-gate, Bohr msg-4774 / msg-4776, Einstein go): verdict-relay routing.

The route after a PR-gate verdict is decided once, by
:func:`~spirrow_mindwire.conductor.gate_records.decide_relay_route`, called once from
``PrReviewOrchestrator._post_design_relay``. These tests pin the msg-4774 table (tests 1-6),
the implementer instruction (test 8) and the pure function (test 9). Tests 7 and 10 — the
conductor's fail-safe and its obedience to the relay's ``route`` — live next to the other
PR-gate conductor tests in ``tests/test_conductor_core.py``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from spirrow_mindwire.conductor.gate_records import (
    ADVISORY_SELF_TRIAGE_INSTRUCTION,
    AUTO_MERGE_BASES,
    MERGE_SELF_INSTRUCTION,
    MERGE_SELF_MARK,
    MERGE_WAIT_LINE,
    RELAY_AUTHOR,
    RelayRoute,
    decide_relay_route,
    is_merge_self_relay,
    is_merge_wait_relay,
    prior_advisory_approvals,
    render_merge_self_line,
    render_relay_heading,
)
from spirrow_mindwire.conductor.handoff import parse_next_token
from spirrow_mindwire.github.client import CiState, PrRef, ReviewEvent
from spirrow_mindwire.github.reviews import parse_verdict_footer
from spirrow_mindwire.magickit.client import MagickitMcpError
from spirrow_mindwire.naysayer.pr_review import (
    ObjectionParse,
    PrReviewOutcome,
    parse_objections,
)
from spirrow_mindwire.naysayer.principles import objection_classes
from spirrow_mindwire.orchestrator import PrReviewOrchestrator
from spirrow_mindwire.pr_event_advance.decide import read_relay_tail
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


def _outcome(
    event: ReviewEvent,
    objections: str | None,
    head: str | None,
    *,
    base_ref: str | None = "main",
    ci: CiState = CiState.SUCCESS,
) -> PrReviewOutcome:
    """A critique shaped like production's: prose, objection block, verdict, verdict footer.

    ``base_ref`` defaults to ``main``, so the msg-4774 tests below stay the main-bound regression
    they were written as (T-gate-relay-routes-develop-approvals-to-human msg-7584 #6)."""
    parts = ["critique prose"]
    if objections is not None:
        parts.append(f"{_SENTINEL}\n{objections}")
    parts.append(f"VERDICT: {event.value}")
    parts.append(f"<!-- mindwire:verdict head_sha={head} event={event.value} -->")
    return PrReviewOutcome(
        verdict=event, body="\n\n".join(parts), ci_state=ci, head_sha=head, base_ref=base_ref
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
async def test_1_approve_without_advisory_stops_at_human_as_merge_wait() -> None:
    mcp = _ThreadMcp()
    relay = await _relay(mcp, _outcome(ReviewEvent.APPROVE, "[]", "aaaaaaa1"))
    assert relay["route"] is RelayRoute.HUMAN
    assert relay["content"].endswith(f"{MERGE_WAIT_LINE}\nNEXT: human")
    assert is_merge_wait_relay(relay["content"])
    # msg-4361: a merge is not a Tier-C escalation, so the relay carries no TIER-C line.
    assert not any(ln.startswith("TIER-C:") for ln in relay["content"].splitlines())
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
    assert second["content"].endswith(f"{MERGE_WAIT_LINE}\nNEXT: human")
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
    assert not is_merge_wait_relay(rc["content"])  # not a merge wait


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


def test_merge_is_not_a_tier_c_label_and_no_relay_text_teaches_it() -> None:
    """msg-4361 (restated 2026-10-03): a merge is requested by the PR, not escalated."""
    assert "merge-protected" not in ADMIT_LABELS
    assert "merge-protected" not in MERGE_WAIT_LINE
    assert "merge-protected" not in ADVISORY_SELF_TRIAGE_INSTRUCTION
    assert "NEXT: pr-review" in ADVISORY_SELF_TRIAGE_INSTRUCTION
    assert not any(ln.startswith("TIER-C:") for ln in MERGE_WAIT_LINE.splitlines())


def test_require_admitted_fails_loudly_on_an_unadmitted_label() -> None:
    assert require_admitted("goal", where="t") == "goal"
    with pytest.raises(RuntimeError, match=r"gate_records: .*'merge-guarded'"):
        require_admitted("merge-guarded", where="gate_records")


# ---------------------------------------------------------------------------
# T-gate-relay-routes-develop-approvals-to-human (Bohr msg-7574 / msg-7582 v0.4.1, Einstein go):
# an APPROVE on an auto-merge base (develop) with green CI is merged by the loop (route MERGE),
# not parked at the human as a merge wait.
# ---------------------------------------------------------------------------

_HEAD = "abcdef1"


class _BaseDriver:
    """The one driver method the relay router may call: ``fetch_pr_base_ref`` (D2)."""

    def __init__(self, base: str | Exception) -> None:
        self._base = base
        self.base_reads: list[PrRef] = []

    async def fetch_pr_base_ref(self, pr: PrRef) -> str:
        self.base_reads.append(pr)
        if isinstance(self._base, Exception):
            raise self._base
        return self._base

    async def aclose(self) -> None:  # pragma: no cover - never reached
        return None


async def _relay_with(
    mcp: _ThreadMcp, outcome: PrReviewOutcome, driver: _BaseDriver, implementer: str | None = _IMPL
) -> dict[str, Any]:
    orch = PrReviewOrchestrator(mcp, driver=driver)  # type: ignore[arg-type]
    return await orch._post_design_relay(
        project="p", design_thread=_DESIGN, pr_ref=_PR, outcome=outcome, implementer=implementer
    )


_E, _M, _OK = ObjectionParse.EMPTY, ObjectionParse.MISSING, ObjectionParse.OK
_S = CiState.SUCCESS
_APPROVE, _RC, _COMMENT = ReviewEvent.APPROVE, ReviewEvent.REQUEST_CHANGES, ReviewEvent.COMMENT


@pytest.mark.parametrize(
    ("verdict", "objections", "prior", "base", "ci", "expected"),
    [
        # row 1: REQUEST_CHANGES -> implementer, whatever the base
        (_RC, _OK, 0, "develop", _S, RelayRoute.IMPLEMENTER),
        (_RC, _OK, 0, "main", _S, RelayRoute.IMPLEMENTER),
        # row 2: clean APPROVE on an auto base with green CI -> merge
        (_APPROVE, _E, 0, "develop", _S, RelayRoute.MERGE),
        (_APPROVE, _M, 0, "develop", _S, RelayRoute.MERGE),
        # row 3: clean APPROVE anywhere else -> human (merge wait)
        (_APPROVE, _E, 0, "main", _S, RelayRoute.HUMAN),
        (_APPROVE, _E, 0, "release/1.2", _S, RelayRoute.HUMAN),
        (_APPROVE, _E, 0, "feature/x", _S, RelayRoute.HUMAN),
        (_APPROVE, _E, 0, None, _S, RelayRoute.HUMAN),
        (_APPROVE, _E, 0, "Develop", _S, RelayRoute.HUMAN),  # exact match only
        (_APPROVE, _E, 0, "develop", CiState.PENDING, RelayRoute.HUMAN),
        (_APPROVE, _E, 0, "develop", CiState.FAILURE, RelayRoute.HUMAN),
        (_APPROVE, _E, 0, "develop", CiState.UNKNOWN, RelayRoute.HUMAN),
        (_APPROVE, _E, 0, "develop", None, RelayRoute.HUMAN),
        # row 4: first advisory APPROVE -> implementer triage, whatever the base
        (_APPROVE, _OK, 0, "develop", _S, RelayRoute.IMPLEMENTER),
        (_APPROVE, _OK, 0, "main", _S, RelayRoute.IMPLEMENTER),
        # row 5: later advisory APPROVE on an auto base with green CI -> merge
        (_APPROVE, _OK, 1, "develop", _S, RelayRoute.MERGE),
        (_APPROVE, _OK, None, "develop", _S, RelayRoute.MERGE),
        # row 6: later advisory APPROVE anywhere else -> human
        (_APPROVE, _OK, 1, "main", _S, RelayRoute.HUMAN),
        (_APPROVE, _OK, None, None, _S, RelayRoute.HUMAN),
        (_APPROVE, _OK, 2, "develop", CiState.PENDING, RelayRoute.HUMAN),
        # row 7: COMMENT -> human, even on develop
        (_COMMENT, _E, 0, "develop", _S, RelayRoute.HUMAN),
        (_COMMENT, _OK, 0, "develop", _S, RelayRoute.HUMAN),
    ],
)
def test_d1_seven_row_table_with_base_and_ci(
    verdict: ReviewEvent,
    objections: ObjectionParse,
    prior: int | None,
    base: str | None,
    ci: CiState | None,
    expected: RelayRoute,
) -> None:
    assert decide_relay_route(verdict, objections, prior, _IMPL, base, ci) is expected


def test_d1_no_implementer_never_merges() -> None:
    for impl in (None, ""):
        assert decide_relay_route(_APPROVE, _E, 0, impl, "develop", _S) is RelayRoute.HUMAN


def test_d1_allow_list_is_develop_only_and_overridable() -> None:
    assert frozenset({"develop"}) == AUTO_MERGE_BASES
    custom = frozenset({"staging"})
    staging = decide_relay_route(_APPROVE, _E, 0, _IMPL, "staging", _S, auto_merge_bases=custom)
    develop = decide_relay_route(_APPROVE, _E, 0, _IMPL, "develop", _S, auto_merge_bases=custom)
    assert staging is RelayRoute.MERGE
    assert develop is RelayRoute.HUMAN


@pytest.mark.anyio
async def test_d3_develop_approve_relay_wakes_the_implementer_to_merge() -> None:
    driver = _BaseDriver(AssertionError("base already known; must not be read"))
    outcome = _outcome(_APPROVE, "[]", _HEAD, base_ref="develop")
    relay = await _relay_with(_ThreadMcp(), outcome, driver)
    body: str = relay["content"]
    assert relay["route"] is RelayRoute.MERGE
    assert driver.base_reads == []  # the content path already carried the base (D2)
    assert body.endswith(
        f"{render_merge_self_line(_PR, _HEAD)}\n\n{MERGE_SELF_INSTRUCTION}\n\nNEXT: {_IMPL}"
    )
    assert parse_next_token(body) == _IMPL
    assert is_merge_self_relay(body)
    assert not is_merge_wait_relay(body)
    assert MERGE_WAIT_LINE not in body
    assert not any(ln.startswith("TIER-C:") for ln in body.splitlines())
    assert ADVISORY_SELF_TRIAGE_INSTRUCTION not in body
    # Exactly one column-zero MERGE-SELF line, and it names the PR and the reviewed head.
    marks = [ln for ln in body.splitlines() if ln.startswith(MERGE_SELF_MARK)]
    assert marks == [render_merge_self_line(_PR, _HEAD)]
    assert f"pr={_PR}" in marks[0]
    assert f"head={_HEAD}" in marks[0]


@pytest.mark.anyio
@pytest.mark.parametrize("base", ["main", "release/2.0", None])
async def test_d3_non_auto_base_keeps_the_merge_wait(base: str | None) -> None:
    driver = _BaseDriver(base if base is not None else MagickitMcpError("down"))
    outcome = _outcome(_APPROVE, "[]", _HEAD, base_ref=base)
    relay = await _relay_with(_ThreadMcp(), outcome, driver)
    assert relay["route"] is RelayRoute.HUMAN
    assert relay["content"].endswith(f"{MERGE_WAIT_LINE}\nNEXT: human")
    assert not is_merge_self_relay(relay["content"])
    assert MERGE_SELF_INSTRUCTION not in relay["content"]


@pytest.mark.anyio
@pytest.mark.parametrize("ci", [CiState.PENDING, CiState.FAILURE, CiState.UNKNOWN])
async def test_d3_develop_without_green_ci_keeps_the_merge_wait(ci: CiState) -> None:
    driver = _BaseDriver(AssertionError("must not be read"))
    outcome = _outcome(_APPROVE, "[]", _HEAD, base_ref="develop", ci=ci)
    relay = await _relay_with(_ThreadMcp(), outcome, driver)
    assert relay["route"] is RelayRoute.HUMAN
    assert is_merge_wait_relay(relay["content"])
    assert driver.base_reads == []


@pytest.mark.anyio
async def test_d3_advisory_chain_on_develop_triages_once_then_merges() -> None:
    mcp = _ThreadMcp()
    driver = _BaseDriver(AssertionError("must not be read"))
    first_outcome = _outcome(_APPROVE, _ADVISORY, "aaaaaaa1", base_ref="develop")
    first = await _relay_with(mcp, first_outcome, driver)
    assert first["route"] is RelayRoute.IMPLEMENTER
    assert ADVISORY_SELF_TRIAGE_INSTRUCTION in first["content"]
    second_outcome = _outcome(_APPROVE, _ADVISORY, "bbbbbbb2", base_ref="develop")
    second = await _relay_with(mcp, second_outcome, driver)
    assert second["route"] is RelayRoute.MERGE
    assert MERGE_SELF_INSTRUCTION in second["content"]
    # The MERGE relay is still counted as an advisory APPROVE: the instruction does not change
    # how prior_advisory_approvals parses the body.
    pairs = [(m["author"], m["content"]) for m in mcp.messages]
    assert prior_advisory_approvals(pairs, _PR) == 2


# ---- D2: the replay paths carry no base; the relay reads it once, only when it matters ------- #


@pytest.mark.anyio
async def test_d2_replay_approve_reads_the_base_once_and_merges_on_develop() -> None:
    driver = _BaseDriver("develop")
    outcome = _outcome(_APPROVE, "[]", _HEAD, base_ref=None)
    relay = await _relay_with(_ThreadMcp(), outcome, driver)
    assert relay["route"] is RelayRoute.MERGE
    assert driver.base_reads == [PrRef("o", "r", 7)]


@pytest.mark.anyio
async def test_d2_replay_approve_on_main_reads_once_and_waits() -> None:
    driver = _BaseDriver("main")
    outcome = _outcome(_APPROVE, "[]", _HEAD, base_ref=None)
    relay = await _relay_with(_ThreadMcp(), outcome, driver)
    assert relay["route"] is RelayRoute.HUMAN
    assert is_merge_wait_relay(relay["content"])
    assert len(driver.base_reads) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "exc", [MagickitMcpError("x"), RuntimeError("github down"), KeyError("base")]
)
async def test_d2_base_read_failure_fails_to_the_human(exc: Exception) -> None:
    driver = _BaseDriver(exc)
    outcome = _outcome(_APPROVE, "[]", _HEAD, base_ref=None)
    relay = await _relay_with(_ThreadMcp(), outcome, driver)
    assert relay["route"] is RelayRoute.HUMAN
    assert is_merge_wait_relay(relay["content"])
    assert len(driver.base_reads) == 1


@pytest.mark.anyio
async def test_d2_base_is_not_read_when_it_cannot_change_the_answer() -> None:
    pending = CiState.PENDING
    cases: list[tuple[PrReviewOutcome, str | None]] = [
        (_outcome(_RC, _BLOCKING, _HEAD, base_ref=None), _IMPL),  # RC: implementer anyway
        (_outcome(_COMMENT, "[]", _HEAD, base_ref=None), _IMPL),  # COMMENT: human anyway
        (_outcome(_APPROVE, _ADVISORY, _HEAD, base_ref=None), _IMPL),  # first advisory: triage
        (_outcome(_APPROVE, "[]", _HEAD, base_ref=None), None),  # no implementer
        (_outcome(_APPROVE, "[]", None, base_ref=None), _IMPL),  # no reviewed head to bind
        (_outcome(_APPROVE, "[]", _HEAD, base_ref=None, ci=pending), _IMPL),  # CI not green
    ]
    for outcome, impl in cases:
        driver = _BaseDriver(AssertionError("must not be read"))
        relay = await _relay_with(_ThreadMcp(), outcome, driver, implementer=impl)
        assert driver.base_reads == [], outcome
        assert relay["route"] is not RelayRoute.MERGE


@pytest.mark.anyio
async def test_d2_no_reviewed_head_never_merges_even_on_develop() -> None:
    driver = _BaseDriver(AssertionError("must not be read"))
    outcome = _outcome(_APPROVE, "[]", None, base_ref="develop")
    relay = await _relay_with(_ThreadMcp(), outcome, driver)
    assert relay["route"] is RelayRoute.HUMAN


# ---- the instruction does not change what any relay reader parses --------------------------- #


def test_merge_self_instruction_is_inert_to_every_relay_reader() -> None:
    instr = MERGE_SELF_INSTRUCTION
    assert parse_next_token(instr) is None
    assert not is_merge_wait_relay(instr)
    assert not is_merge_self_relay(instr)
    assert parse_verdict_footer(instr) is None
    assert parse_objections(instr).status is ObjectionParse.MISSING
    assert not any(ln.startswith("TIER-C:") for ln in instr.splitlines())
    assert _SENTINEL not in instr
    # The same holds for the reworded advisory instruction (D3).
    assert parse_next_token(ADVISORY_SELF_TRIAGE_INSTRUCTION) is None
    assert not is_merge_self_relay(ADVISORY_SELF_TRIAGE_INSTRUCTION)


@pytest.mark.anyio
@pytest.mark.parametrize("objections", [_ADVISORY, "[]", None])
async def test_merge_relay_parses_as_its_critique(objections: str | None) -> None:
    mcp = _ThreadMcp()
    driver = _BaseDriver("develop")
    if objections == _ADVISORY:  # spend the triage turn so the advisory APPROVE reaches MERGE
        await _relay_with(
            mcp, _outcome(_APPROVE, _ADVISORY, "aaaaaaa1", base_ref="develop"), driver
        )
    outcome = _outcome(_APPROVE, objections, _HEAD, base_ref="develop")
    relay = await _relay_with(mcp, outcome, driver)
    assert relay["route"] is RelayRoute.MERGE
    body: str = relay["content"]
    assert parse_objections(body).status is parse_objections(outcome.body).status
    assert parse_verdict_footer(body) == parse_verdict_footer(outcome.body)
    tail = read_relay_tail(RELAY_AUTHOR, body)
    assert tail is not None
    assert tail.pr_ref == _PR
    assert tail.token == _IMPL
    assert tail.hold_head is None
    assert tail.merge_self is True


def test_merge_self_instruction_snapshot() -> None:
    """D3 wording (msg-7580 steps 1-4 and table rows 1-5, msg-7582 Observation 1). The branch is
    taken by the implementer's model, so the text is the contract: change it on purpose, here."""
    instr = MERGE_SELF_INSTRUCTION
    for needle in (
        # steps
        "1. `gh pr view <n> --repo <owner/repo> --json state,headRefOid,mergeCommit`",
        "`gh pr view` が失敗した、または結果を読めなかった場合は merge を実行せず、5 行目に従う",
        "2. 1〜3 行目のどれにも当たらない",
        "`git merge --no-ff <レビューした sha>`",
        "`git push origin develop` (force は付けない)",
        "3. merge か push が失敗したら",
        "もう一度だけ `gh pr view` で状態を読み直し、1〜3 行目で分類し直す",
        "読み直しに失敗した場合も 5 行目に従う",
        "4. 読み直しても 1〜3 行目のどれにも当たらない場合だけ、5 行目に従う",
        "状態を読み直すのは 1 回だけ。merge の再試行はしない",
        # table
        "| 1. merge 済み | 既存の merge commit を報告し、proposer に `NEXT:` で渡す |",
        "| 2. merge されずに close 済み |",
        "このスレッドの作業はこの PR では landing していない",
        "| 3. open で、head がレビューした sha と違う |",
        "報告し、`NEXT: pr-review <PR>` で gate に戻す |",
        "| 4. merge に成功 | merge commit を報告し、proposer に `NEXT:` で渡す |",
        "| 5. 読み直しても open で head が一致したまま merge が失敗した",
        "または状態が読めない | エラーの本文をそのまま報告し、proposer に `NEXT:` で渡す |",
        # never the human, never gh pr merge
        "`TIER-C:` 行や `NEXT: human` は書かないこと",
        "`gh pr merge` は使わない",
    ):
        assert needle in instr, needle
    digest = hashlib.sha256(instr.encode("utf-8")).hexdigest()
    assert digest == "ef793789b25ad1cbf68ee0d21a74cf93ad12f52a63189201cb0eff0a7ecf04bb"
