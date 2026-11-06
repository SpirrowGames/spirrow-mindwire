"""Conditional proceed, the exit side (PR 2 of T-naysayer-conditional-proceed-on-small-fixmin).

PR 1 (#520) put the PR gate's verification in place; this pins the rest of msg-7979's scope:

* the naysayer is offered the conditional exit only while the project's setting is ``issuing``
  — absent, ``verify_only`` and an unknown value offer nothing, and the prompt is then unchanged;
* what the naysayer and the implementer are taught is the format the PR gate reads (one spelling);
* T1 — the implementer's assembled prompt carries ``OBL-DESIGN-CONDITIONS``, and a conditional
  go at the head reaches the implementer's turn with all k condition lines;
* T3 — a ``not applied`` hand-back goes to the naysayer, never reads as a go;
* T4 — the ``naysayer.round`` log says ``conditional_proceed=0|1``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_conductor_retry_notice import _Dispatcher, _Mcp
from test_loop_runner import (
    _conductor_settings,
    _exec_caps,
    _FakeChatroom,
    _FakeMcp,
    _naysayer_caps,
    _proposer_caps,
    _StubAdapter,
)

from spirrow_mindwire import design_conditions
from spirrow_mindwire.adapters.implementer import ImplementerSdkAdapter
from spirrow_mindwire.adapters.naysayer_lexora import _build_messages as lexora_messages
from spirrow_mindwire.conductor.core import Conductor
from spirrow_mindwire.conductor.handoff import (
    NAYSAYER_CONDITIONAL_PROCEED_GUIDANCE,
    HandoffKind,
    build_handoff_protocol_block,
    conditional_proceed_notice,
    resolve_handoff,
)
from spirrow_mindwire.config import NaysayerGatingConfig
from spirrow_mindwire.design_conditions import (
    ConditionalProceedMode,
    ThreadMessage,
    ThreadPolicy,
    canonical_conditions,
    check_pr_body,
    evaluate_thread,
    mode_for,
)
from spirrow_mindwire.loop_runner import _conditional_proceed_notice, build_conductor
from spirrow_mindwire.naysayer.conditions_check import ConditionsOutcome
from spirrow_mindwire.naysayer.review_rounds import log_design_round, log_round
from spirrow_mindwire.obligations import load_manifest
from spirrow_mindwire.thread_context import build_thread_context, build_turn_prompt
from spirrow_mindwire.value_objects import (
    ChatroomEvent,
    EventType,
    NewMessagePayload,
    Role,
    ThreadRef,
)

_ROSTER = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER, "Heisenberg": Role.IMPLEMENTER}
_POLICY = ThreadPolicy(roster=_ROSTER, human_identity="human")
_TR = ThreadRef(project_id="p", thread_id="T-x", chatroom_uri="mc://t")
_M = "<!-- mindwire:objections v1 -->"


def _objection(fix_min: str) -> dict[str, str]:
    return {
        "class": "correctness",
        "where": "D4",
        "evidence": "the case is not handled",
        "fix_min": fix_min,
        "fix_cut": "drop the case",
        "fix_weight": "small",
    }


_FIXES = ("Validate every entry before evaluating.", "Add the missing boundary test.")


def _conditional_go(round_number: int = 3) -> str:
    """A go written exactly in the form NAYSAYER_CONDITIONAL_PROCEED_GUIDANCE teaches."""
    block = json.dumps([_objection(f) for f in _FIXES], ensure_ascii=False)
    ids = "\n".join(f"Cond-{round_number}-{n}" for n in range(1, len(_FIXES) + 1))
    return (
        "Two small objections remain; both fixes are fully determined.\n\n"
        f"{_M}\n```json\n{block}\n```\n\n"
        f"Conditions ({len(_FIXES)}):\n{ids}\n\n"
        "TIER-C-CHECK: none\nNEXT: Heisenberg"
    )


# ─── the exit is offered under `issuing` only ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("modes", "offered"),
    [
        ({}, False),  # key absent: the project never used it
        ({"p": "issuing"}, True),
        ({"p": "ISSUING "}, True),  # mode_for folds case and whitespace
        ({"p": "verify_only"}, False),  # issuance stopped, verification continues
        ({"p": "enabled"}, False),  # unknown value: verify, never issue
        ({"other": "issuing"}, False),  # another project's setting
    ],
)
def test_the_notice_follows_the_projects_setting(modes: dict[str, str], offered: bool) -> None:
    settings = _conductor_settings().model_copy(
        update={"naysayer_gating": NaysayerGatingConfig(conditional_proceed=modes)}
    )
    notice = _conditional_proceed_notice(settings, "p")
    assert (notice == NAYSAYER_CONDITIONAL_PROCEED_GUIDANCE) is offered
    assert (notice is None) is not offered
    # The PR gate's side of the same key: everything but absent keeps verifying.
    assert mode_for(modes, "p").verifies is ("p" in modes)


def test_only_issuing_issues() -> None:
    assert [m for m in ConditionalProceedMode if m.issues] == [ConditionalProceedMode.ISSUING]
    assert conditional_proceed_notice(False) is None


def test_build_conductor_wires_the_notice_from_the_loop_project(tmp_path: Path) -> None:
    def build(modes: dict[str, str]) -> Any:
        settings = _conductor_settings(tmp_path)
        settings = settings.model_copy(
            update={"naysayer_gating": NaysayerGatingConfig(conditional_proceed=modes)}
        )
        return build_conductor(
            settings,
            mcp=_FakeMcp(_FakeChatroom()),
            proposer=_StubAdapter("fake-proposer", _proposer_caps()),
            implementer=_StubAdapter("fake-implementer", _exec_caps()),
            naysayer=_StubAdapter("fake-naysayer", _naysayer_caps()),
        ).conductor

    project = _conductor_settings(tmp_path).loop.project
    assert build({project: "issuing"})._conditional_proceed_notice == (
        NAYSAYER_CONDITIONAL_PROCEED_GUIDANCE
    )
    assert build({project: "verify_only"})._conditional_proceed_notice is None
    assert build({})._conditional_proceed_notice is None


def _thread(head_author: str, head_body: str) -> list[dict[str, Any]]:
    return [
        {"msg_id": "msg-1", "author": "Bohr", "content": "the design", "timestamp": ""},
        {"msg_id": "msg-2", "author": head_author, "content": head_body, "timestamp": ""},
    ]


async def _first_dispatch(
    messages: list[dict[str, Any]], notice: str | None
) -> tuple[Role, ChatroomEvent]:
    dispatcher = _Dispatcher()
    conductor = Conductor(
        mcp=_Mcp(messages),
        dispatcher=dispatcher,
        thread_ref=_TR,
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=1,
        conditional_proceed_notice=notice,
    )
    await conductor.run()
    assert dispatcher.dispatched, "the conductor did not dispatch"
    return dispatcher.dispatched[0]


def _dispatch(messages: list[dict[str, Any]], notice: str | None) -> tuple[Role, ChatroomEvent]:
    return asyncio.run(_first_dispatch(messages, notice))


def test_the_conductor_attaches_the_notice_to_a_naysayer_dispatch_only() -> None:
    notice = NAYSAYER_CONDITIONAL_PROCEED_GUIDANCE
    role, event = _dispatch(_thread("Bohr", "design\n\nNEXT: Einstein"), notice)
    assert role is Role.NAYSAYER
    assert event.conditional_proceed_notice == notice
    assert notice in build_turn_prompt(event, Role.NAYSAYER, "closing")

    role, event = _dispatch(_thread("Einstein", "critique\n\nNEXT: Bohr"), notice)
    assert role is Role.PROPOSER
    assert event.conditional_proceed_notice is None


_BASELINE_DIR = Path(__file__).parent / "data" / "conditional_proceed_baseline"


def _baseline(name: str) -> str:
    """A naysayer prompt rendered by the pre-change code (origin/main df82e82)."""
    return (_BASELINE_DIR / name).read_bytes().decode("utf-8")


def test_without_the_notice_the_naysayer_prompt_is_unchanged() -> None:
    role, event = _dispatch(_thread("Bohr", "design\n\nNEXT: Einstein"), None)
    assert role is Role.NAYSAYER
    assert event.conditional_proceed_notice is None
    prompt = build_turn_prompt(event, Role.NAYSAYER, "closing")
    assert "CONDITIONAL PROCEED" not in prompt
    # Independent baseline: the same event rendered by the pre-change code on ``main``
    # (df82e82, before the field existed), checked in as a golden file. A change to
    # prompt assembly reds this even if it affects both sides of an in-process compare.
    assert build_turn_prompt(
        _event(None, "design\n\nNEXT: Einstein"), Role.NAYSAYER, "closing"
    ) == _baseline("naysayer_sdk_prompt.txt")
    # The static system prompt never carries the exit: it is a per-turn notice only.
    assert "CONDITIONAL PROCEED" not in build_handoff_protocol_block(Role.NAYSAYER)


def _event(notice: str | None, body: str = "b") -> ChatroomEvent:
    return ChatroomEvent(
        event_id="T:msg-2",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_TR,
        occurred_at=datetime(2026, 11, 6, tzinfo=UTC),
        payload=NewMessagePayload(msg_id="msg-2", author="Bohr", body=body, parent_msg_id=None),
        conditional_proceed_notice=notice,
    )


def test_the_lexora_naysayer_renders_the_notice_and_is_unchanged_without_it() -> None:
    notice = NAYSAYER_CONDITIONAL_PROCEED_GUIDANCE
    with_notice = lexora_messages(_event(notice), Role.NAYSAYER, "sys")[1].content
    without = lexora_messages(_event(None), Role.NAYSAYER, "sys")[1].content
    assert notice in with_notice
    assert with_notice.replace(f"{notice}\n\n", "") == without
    # Independent baseline rendered by the pre-change code on ``main`` (df82e82).
    baseline_event = _event(None, "design\n\nNEXT: Einstein")
    rendered = lexora_messages(baseline_event, Role.NAYSAYER, "sys")[1].content
    assert rendered == _baseline("naysayer_lexora_user.txt")
    assert "CONDITIONAL PROCEED" not in without


# ─── what the naysayer is taught ──────────────────────────────────────────────────────────────


def test_the_guidance_carries_the_five_conditions_and_the_once_per_thread_rule() -> None:
    g = NAYSAYER_CONDITIONAL_PROCEED_GUIDANCE
    for n in range(1, 6):
        assert f"({n}) " in g
    assert "`fix_weight: small`" in g and "non-empty" in g  # (1)
    assert "exactly one way" in g  # (2)
    assert "acceptance criteria, scope, a test boundary or a public interface" in g  # (3)
    assert "`採用=cut` or `採用=own`" in g  # (4)
    assert "TIER-C-CHECK" in g  # (5)
    assert "At most one conditional go per thread" in g
    assert "including after an abort" in g
    assert "already carries a go to the implementer" in g


def test_the_guidance_gives_exactly_two_choices_after_not_applied() -> None:
    g = NAYSAYER_CONDITIONAL_PROCEED_GUIDANCE
    assert "exactly two choices" in g
    assert "(c) hand back to the proposer" in g
    assert "(d) if it is a Tier-C type" in g
    # The removed choices (msg-7956 Obj-3-1) are excluded in so many words.
    assert (
        "may not drop a condition, replace it with another, or turn it into an unconditional" in (g)
    )
    assert "`Conditions-aborted: <msg-id of the conditional go>`" in g


def test_a_go_written_as_taught_is_the_one_the_pr_gate_verifies() -> None:
    """One spelling: the taught form produces a VERIFY with the go's `fix_min` as canonical."""
    go = _conditional_go()
    # The guidance's own example lines are the gate's patterns.
    assert "    Conditions (2):\n    Cond-3-1\n    Cond-3-2\n" in (
        NAYSAYER_CONDITIONAL_PROCEED_GUIDANCE
    )
    conditions, problem = canonical_conditions(go)
    assert problem is None
    assert [c.cond_id for c in conditions] == ["Cond-3-1", "Cond-3-2"]
    assert [c.fix_min for c in conditions] == list(_FIXES)
    verdict = evaluate_thread(
        [ThreadMessage("msg-1", "Bohr", "design"), ThreadMessage("msg-2", "Einstein", go)],
        _POLICY,
    )
    assert verdict.outcome is ConditionsOutcome.VERIFY


def test_an_abort_written_as_taught_stops_the_conditional_go() -> None:
    abort = (
        "The implementer could not apply Cond-3-1; handing back.\n\n"
        "Conditions-aborted: msg-2\n\nNEXT: Bohr"
    )
    verdict = evaluate_thread(
        [
            ThreadMessage("msg-1", "Bohr", "design"),
            ThreadMessage("msg-2", "Einstein", _conditional_go()),
            ThreadMessage("msg-3", "Heisenberg", "Cond-3-1: not applied — x\n\nNEXT: Einstein"),
            ThreadMessage("msg-4", "Einstein", abort),
        ],
        _POLICY,
    )
    assert verdict.outcome is ConditionsOutcome.HOLD  # row 3: aborted, no restart go yet


# ─── T1: what the implementer is taught, and what reaches its turn ────────────────────────────


def _obligation() -> str:
    (entry,) = [o for o in load_manifest().obligations if o.id == "OBL-DESIGN-CONDITIONS"]
    assert entry.role is Role.IMPLEMENTER
    return entry.body


def test_t1_the_implementer_prompt_carries_the_obligation(tmp_path: Path) -> None:
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=load_manifest(), inference_base_url="http://lx"
    )
    assert _obligation() in adapter._system_prompt
    assert "[OBL-DESIGN-CONDITIONS]" in adapter._system_prompt


def test_t1_the_obligation_teaches_the_rules_of_msg_7956() -> None:
    body = _obligation()
    assert "`## Design conditions`" in body
    assert "one pull request" in body and "do not split the conditions across PRs" in body
    assert "`Cond-<r>-<n>: not applied — <reason>`" in body
    assert "`Cond-<r>-<n>: not applied — split`" in body
    assert "`NEXT: <naysayer persona>`" in body
    assert "keep the `Thread:` line" in body


def test_t1_a_pr_body_written_as_the_obligation_teaches_passes_the_gate_check() -> None:
    """The row form the obligation teaches is the one ``check_pr_body`` accepts."""
    assert (
        "`Cond-<r>-<n>: <its fix_min, copied verbatim from the go's objections block> — "
        "applied: <file, place or test name>`"
    ) in _obligation()
    rows = "\n".join(
        f"Cond-3-{n}: {fix} — applied: src/x.py" for n, fix in enumerate(_FIXES, start=1)
    )
    body = f"Thread: T-x\n\n## Design conditions\n{rows}\n\n## Tests\nall green\n"
    verdict = evaluate_thread(
        [
            ThreadMessage("msg-1", "Bohr", "d"),
            ThreadMessage("msg-2", "Einstein", _conditional_go()),
        ],
        _POLICY,
    )
    check = check_pr_body(verdict, body, thread_id="T-x")
    assert check.outcome is ConditionsOutcome.VERIFY, check.reasons


def test_t1_a_conditional_go_at_the_head_reaches_the_implementer_with_every_condition() -> None:
    go = _conditional_go()
    messages = [
        {"msg_id": "msg-1", "author": "Bohr", "content": "the design", "timestamp": ""},
        {"msg_id": "msg-2", "author": "Einstein", "content": go, "timestamp": ""},
    ]
    context = build_thread_context(messages, trigger_msg_id="msg-2")
    event = ChatroomEvent(
        event_id="T:msg-2",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_TR,
        occurred_at=datetime(2026, 11, 6, tzinfo=UTC),
        payload=NewMessagePayload(msg_id="msg-2", author="Einstein", body=go, parent_msg_id=None),
        thread_context=context,
    )
    prompt = build_turn_prompt(event, Role.IMPLEMENTER, "Carry out the work.")
    assert "Conditions (2):" in prompt
    for n, fix in enumerate(_FIXES, start=1):
        assert f"Cond-3-{n}" in prompt
        assert fix in prompt


# ─── T3: `not applied` goes back to the naysayer ──────────────────────────────────────────────

_NOT_APPLIED = (
    "Cond-3-1: not applied — it narrows the acceptance range the human fixed in msg-1.\n\n"
    "NEXT: Einstein"
)


def test_t3_not_applied_resolves_to_the_naysayer_and_is_not_a_go() -> None:
    handoff = resolve_handoff(_NOT_APPLIED, _ROSTER)
    assert handoff.kind is HandoffKind.ROLE and handoff.role is Role.NAYSAYER
    assert not _POLICY.is_valid_go("Heisenberg", _NOT_APPLIED)


def test_t3_the_conductor_delivers_not_applied_to_the_naysayer() -> None:
    notice = NAYSAYER_CONDITIONAL_PROCEED_GUIDANCE
    role, event = _dispatch(_thread("Heisenberg", _NOT_APPLIED), notice)
    assert role is Role.NAYSAYER
    assert event.payload.body == _NOT_APPLIED
    # The naysayer receiving it is told its two choices (under `issuing`).
    assert "exactly two choices" in build_turn_prompt(event, Role.NAYSAYER, "closing")


def test_t3_not_applied_after_a_conditional_go_does_not_unlock_the_gate() -> None:
    verdict = evaluate_thread(
        [
            ThreadMessage("msg-1", "Bohr", "design"),
            ThreadMessage("msg-2", "Einstein", _conditional_go()),
            ThreadMessage("msg-3", "Heisenberg", _NOT_APPLIED),
        ],
        _POLICY,
    )
    # Still row 6: the conditions stand until the naysayer aborts them.
    assert verdict.outcome is ConditionsOutcome.VERIFY


# ─── T4: the round log ────────────────────────────────────────────────────────────────────────


def _log(caplog: pytest.LogCaptureFixture, body: str) -> str:
    logger = logging.getLogger("test.conditional")
    with caplog.at_level(logging.INFO, logger="test.conditional"):
        log_design_round(
            logger,
            body=body,
            thread="T-x",
            backend="codex",
            model="naysayer",
            round_number=3,
            out_tokens=5,
        )
    return caplog.messages[-1]


def test_t4_a_conditional_go_logs_conditional_proceed_1(caplog: pytest.LogCaptureFixture) -> None:
    line = _log(caplog, _conditional_go())
    assert line.endswith(" conditional_proceed=1")
    assert " blocking=2 " in line


def test_t4_an_ordinary_post_logs_conditional_proceed_0(caplog: pytest.LogCaptureFixture) -> None:
    line = _log(caplog, f"critique\n\n{_M}\n[]\n\nNEXT: Bohr")
    assert line.endswith(" conditional_proceed=0")
    # Prose that only mentions the format is not a conditional go.
    line = _log(caplog, "We could write `Conditions (2):` here.\n\nNEXT: Bohr")
    assert line.endswith(" conditional_proceed=0")


def test_t4_a_pr_round_has_no_conditional_proceed_value(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.conditional.pr")
    with caplog.at_level(logging.INFO, logger="test.conditional.pr"):
        log_round(
            logger,
            thread="",
            pr="o/r#1",
            backend="codex",
            model="naysayer",
            round_number=1,
            scope="full",
            counts=None,
            out_tokens=1,
        )
    assert caplog.messages[-1].endswith(" conditional_proceed=-")


def test_the_heading_has_one_spelling() -> None:
    """The gate and the round log read the same compiled pattern (no second copy)."""
    from spirrow_mindwire.naysayer import review_rounds

    assert vars(design_conditions)["CONDITIONS_HEADING_RE"] is review_rounds.CONDITIONS_HEADING_RE
