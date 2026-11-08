"""Terminal forms ``STOP: done`` / ``BLOCKED-ON:`` — the reading side (boundary 2).

T-done-disposition-auto-close, design v2.4 (Bohr msg-8185, approved by Einstein msg-8186). The
grammar is v1 §1 with the v1.1 / v2 / v2.1 / v2.2 corrections: the final control line, the n-1
rule for ``BLOCKED-ON``, the msg-8175 §2 field/body table, and the CommonMark fence rule of
msg-8179 §B (its seven cases are pinned here one by one). ``handoff.ACCEPT_NEW_FORMS`` is off in
this boundary, so every well-formed new form must be MALFORMED with a notice, and nothing about
``NEXT: none`` may change.
"""

from __future__ import annotations

import pytest
from test_conductor_core import _FakeChatroomMcp, _ScriptedDispatcher
from test_decider_step2 import _conductor_with
from test_stop_disposition_slice3 import _ROSTER, _Gh, _TickMcp

from spirrow_mindwire.conductor import handoff as handoff_mod
from spirrow_mindwire.conductor.core import StopReason
from spirrow_mindwire.conductor.disposition import body_disposition
from spirrow_mindwire.conductor.handoff import (
    HUMAN_TOKEN,
    NONE_TOKEN,
    HandoffKind,
    MalformedHandoff,
    StopStatus,
    resolve_handoff,
    settles_by_terminal_form,
)
from spirrow_mindwire.conductor.head_skip import (
    TERMINAL_STOP_REASONS,
    parse_head_token,
    stage1_skips,
)
from spirrow_mindwire.park_wake.runner import run_tick
from spirrow_mindwire.pr_event_advance.auto_gate.decide import AutoGateNoop, tail_decision
from spirrow_mindwire.pr_event_advance.decide import Post

F3 = "`" * 3
F4 = "`" * 4

DONE = "x\n\nSTOP: done\n<!-- source: tools=3 -->\n"
BLOCKED = "x\n\nBLOCKED-ON: pr SpirrowGames/spirrow-lexora#104\nNEXT: Bohr"


@pytest.fixture
def accepting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the forms on for one test (boundary 3 flips the constant itself)."""
    monkeypatch.setattr(handoff_mod, "ACCEPT_NEW_FORMS", True)


def test_the_forms_ship_switched_off() -> None:
    assert handoff_mod.ACCEPT_NEW_FORMS is False


# --------------------------------------------------------------------------- grammar


@pytest.mark.parametrize("body", [DONE, BLOCKED])
def test_a_well_formed_new_form_is_malformed_while_off(body: str) -> None:
    handoff = resolve_handoff(body, _ROSTER)
    assert handoff.kind is HandoffKind.ABSENT
    assert handoff.malformed is MalformedHandoff.NEW_FORM_NOT_ACCEPTED
    assert not settles_by_terminal_form(body)


def test_done_is_a_settle_when_accepted(accepting: None) -> None:
    handoff = resolve_handoff(DONE, _ROSTER)
    assert handoff.kind is HandoffKind.NONE
    assert handoff.stop_line is not None
    assert handoff.stop_line.status is StopStatus.DONE
    assert handoff.stop_line.terminal


def test_blocked_on_is_a_park_on_the_next_line_when_accepted(accepting: None) -> None:
    handoff = resolve_handoff(BLOCKED, _ROSTER)
    assert handoff.kind is HandoffKind.NONE
    stop = handoff.stop_line
    assert stop is not None and stop.terminal
    assert (stop.status, stop.trigger_arm, stop.trigger_operand, stop.wake) == (
        StopStatus.BLOCKED_ON,
        "pr",
        "SpirrowGames/spirrow-lexora#104",
        "Bohr",
    )


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("x\n\nSTOP: finished", MalformedHandoff.STOP_NOT_DONE),
        ("x\n\nstop: done", MalformedHandoff.STOP_NOT_DONE),
        ("x\n\n**STOP**: done", MalformedHandoff.STOP_NOT_DONE),
        ("x\n\nSTOP: blocked-on pr:a/b#1 wake:Bohr", MalformedHandoff.STOP_NOT_DONE),
        ("x\nSTOP: done\nNEXT: Bohr", MalformedHandoff.STOP_ABOVE_NEXT),
        ("x\nSTOP: done\nNEXT: Einstein", MalformedHandoff.STOP_ABOVE_NEXT),
        ("x\nBLOCKED-ON: pr\nNEXT: Bohr", MalformedHandoff.BLOCKED_ON_MALFORMED),
        ("x\nBLOCKED-ON: human T-a\nNEXT: Bohr", MalformedHandoff.BLOCKED_ON_MALFORMED),
        ("x\nblocked-on: pr a/b#1\nNEXT: Bohr", MalformedHandoff.BLOCKED_ON_MALFORMED),
        ("x\nBLOCKED-ON: self job-1\nNEXT: Bohr", MalformedHandoff.BLOCKED_ON_SELF_UNSUPPORTED),
        ("x\nBLOCKED-ON: pr a/b#1\nNEXT: none", MalformedHandoff.BLOCKED_ON_BAD_WAKE),
        ("x\nBLOCKED-ON: pr a/b#1\nNEXT: human", MalformedHandoff.BLOCKED_ON_BAD_WAKE),
        ("x\nBLOCKED-ON: pr a/b#1\nNEXT: operator", MalformedHandoff.BLOCKED_ON_BAD_WAKE),
        ("x\nBLOCKED-ON: pr a/b#1\nNEXT: pr-review a/b#1", MalformedHandoff.BLOCKED_ON_BAD_WAKE),
        ("x\nBLOCKED-ON: pr a/b#1\nNEXT: Heisenberg", MalformedHandoff.BLOCKED_ON_BAD_WAKE),
        ("x\nBLOCKED-ON: pr a/b#1\nNEXT: Nobody", MalformedHandoff.BLOCKED_ON_BAD_WAKE),
    ],
)
def test_broken_forms_are_malformed_whether_or_not_accepted(
    monkeypatch: pytest.MonkeyPatch, body: str, reason: MalformedHandoff
) -> None:
    for on in (False, True):
        monkeypatch.setattr(handoff_mod, "ACCEPT_NEW_FORMS", on)
        handoff = resolve_handoff(body, _ROSTER)
        assert (handoff.kind, handoff.malformed) == (HandoffKind.ABSENT, reason), on


@pytest.mark.parametrize(
    "body",
    [
        "x\n\nSTOP: done\nNEXT: none",
        "x\n\nSTOP: blocked-on thread:T-a wake:Einstein\nNEXT: none",
        "x\n\nNEXT: none",
        "x\n\nNEXT: Bohr",
        "x\n\nNEXT: human",
        # msg-4718 §1-3: a STOP line above NEXT: human leaves the Decider entry unchanged.
        "x\n\nSTOP: done\nNEXT: human",
    ],
)
def test_the_existing_forms_are_untouched(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    monkeypatch.setattr(handoff_mod, "ACCEPT_NEW_FORMS", False)
    off = resolve_handoff(body, _ROSTER)
    monkeypatch.setattr(handoff_mod, "ACCEPT_NEW_FORMS", True)
    assert resolve_handoff(body, _ROSTER) == off
    assert off.malformed is None


def test_a_field_contradicting_the_form_is_malformed(accepting: None) -> None:
    assert (
        resolve_handoff(DONE, _ROSTER, next_participant="Bohr").malformed
        is MalformedHandoff.FIELD_CONFLICT
    )
    assert (
        resolve_handoff(BLOCKED, _ROSTER, next_participant="Einstein").malformed
        is MalformedHandoff.FIELD_CONFLICT
    )
    agreeing = resolve_handoff(BLOCKED, _ROSTER, next_participant="Bohr")
    assert agreeing.kind is HandoffKind.NONE and agreeing.malformed is None


def test_indented_code_is_not_a_control_line(accepting: None) -> None:
    assert resolve_handoff("x\n\n    STOP: done", _ROSTER).kind is HandoffKind.ABSENT
    assert resolve_handoff("x\n\n    STOP: done", _ROSTER).malformed is None


# --------------------------------------------------------------------------- fences (msg-8179 §B)


@pytest.mark.parametrize(
    "body",
    [
        f"x\n{F3}\nSTOP: done\n{F3}\n",  # v2.1 §C: a fenced STOP alone
        f"x\n{F3}\nSTOP: done",  # v2.1 §C: an unclosed fence
        f"x\n{F4}\n{F3}\nSTOP: done\n{F4}",  # 1: a shorter fence inside does not close it
        "x\n~~~~\n~~~\nSTOP: done",  # 2: the same with tildes
        f"x\n{F3}\n~~~\nSTOP: done",  # 3: the other character does not close it
        f"x\n{F3}\n{F3}x\nSTOP: done",  # 4: a closer with text after it is not a closer
    ],
)
def test_fenced_examples_are_not_control_lines(accepting: None, body: str) -> None:
    handoff = resolve_handoff(body, _ROSTER)
    assert handoff.kind is HandoffKind.ABSENT
    assert handoff.malformed is None


def test_a_fenced_next_is_not_the_handoff() -> None:
    assert resolve_handoff(f"x\n{F3}\nNEXT: Bohr\n{F3}\n", _ROSTER).kind is HandoffKind.ABSENT


def test_a_real_next_after_a_closed_fence_is_the_handoff() -> None:
    # v2.1 §C and 6.
    for body in (
        f"x\n{F3}\nNEXT: Einstein\n{F3}\nNEXT: Bohr",
        f"x\n{F4}\n{F3}\nSTOP: done\n{F4}\nNEXT: Bohr",
    ):
        handoff = resolve_handoff(body, _ROSTER)
        assert (handoff.kind, handoff.identity) == (HandoffKind.ROLE, "Bohr")


def test_four_space_indented_backticks_are_not_a_fence() -> None:
    # 5: an indented code line, so the NEXT after it is still read.
    handoff = resolve_handoff(f"x\n    {F3}\nNEXT: Bohr", _ROSTER)
    assert handoff.identity == "Bohr"


def test_a_backtick_info_string_with_a_backtick_is_not_a_fence() -> None:
    # 7: "``` a`b" opens nothing, so the reading is the pre-change one.
    handoff = resolve_handoff(f"x\n{F3} a`b\nNEXT: Bohr", _ROSTER)
    assert handoff.identity == "Bohr"


# --------------------------------------------------------------------------- sweep / 1b / 1c


def test_head_skip_launches_new_forms_while_off() -> None:
    # LAUNCH is what gets the MALFORMED notice posted (the notice moves the head).
    assert not stage1_skips(DONE)
    assert not stage1_skips(BLOCKED)
    assert parse_head_token(BLOCKED) == "bohr"


def test_head_skip_reads_accepted_forms_as_none(accepting: None) -> None:
    for body in (DONE, BLOCKED):
        assert parse_head_token(body) == NONE_TOKEN
        assert stage1_skips(body)
        # The head cache replays the token alone; it must SKIP again.
        assert stage1_skips(f"NEXT: {parse_head_token(body)}")


def test_malformed_handoff_is_a_terminal_stop_reason() -> None:
    assert StopReason.MALFORMED_HANDOFF.value in TERMINAL_STOP_REASONS


def test_auto_gate_tail_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    assert tail_decision(DONE, repo="a/b", number=1) is AutoGateNoop.QUEUED_BEHIND
    monkeypatch.setattr(handoff_mod, "ACCEPT_NEW_FORMS", True)
    assert isinstance(tail_decision(DONE, repo="a/b", number=1), Post)
    assert isinstance(tail_decision(BLOCKED, repo="a/b", number=1), Post)


# --------------------------------------------------------------------------- gateway disposition


def test_no_disposition_is_sent_for_a_new_form_while_off() -> None:
    assert body_disposition(DONE, _ROSTER) is None
    assert body_disposition(BLOCKED, _ROSTER) is None


def test_the_disposition_of_an_accepted_form(accepting: None) -> None:
    assert body_disposition(DONE, _ROSTER) == {"kind": "done"}
    assert body_disposition(BLOCKED, _ROSTER) == {
        "kind": "blocked_on",
        "trigger": {"arm": "pr", "ref": "SpirrowGames/spirrow-lexora#104"},
        "wake": "Bohr",
    }


# --------------------------------------------------------------------------- conductor


@pytest.mark.anyio
@pytest.mark.parametrize("body", [DONE, BLOCKED, "x\nSTOP: done\nNEXT: Bohr"])
async def test_conductor_stops_on_a_refused_form_with_a_notice(body: str) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content=body)
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor_with(mcp, disp, None).run()
    assert outcome.stop_reason is StopReason.MALFORMED_HANDOFF
    assert disp.spawns == []  # neither the wake nor a forced naysayer consult
    (notice,) = mcp.posts
    assert notice["content"].rstrip().endswith(f"NEXT: {HUMAN_TOKEN}")
    # The notice is not itself a terminal form, and it parks the sweep.
    assert resolve_handoff(notice["content"], _ROSTER).kind is HandoffKind.HUMAN
    assert stage1_skips(notice["content"])
    assert outcome.last_msg_id == "m2"  # the stop is recorded against the notice


@pytest.mark.anyio
@pytest.mark.parametrize("body", [DONE, BLOCKED])
async def test_conductor_settles_an_accepted_form_silently(accepting: None, body: str) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content=body)
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor_with(mcp, disp, None).run()
    assert outcome.stop_reason is StopReason.SETTLED
    assert disp.spawns == [] and mcp.posts == []


# --------------------------------------------------------------------------- park-wake tick


@pytest.mark.anyio
async def test_tick_classifies_accepted_forms(accepting: None) -> None:
    mcp = _TickMcp()
    mcp.thread("p", "T-done", content=DONE)
    mcp.thread("p", "T-wait", content="x\nBLOCKED-ON: thread T-other\nNEXT: Einstein")
    mcp.thread("p", "T-other", content="x\n\nNEXT: Bohr")
    report = (await run_tick(mcp=mcp, gh=_Gh({}), project="p", roster=_ROSTER)).as_dict()
    assert report["counts"] == {"done": 1, "blocked_on": 1, "unclassified": 0, "human_close": 0}
    assert mcp.posts == []


@pytest.mark.anyio
async def test_tick_reports_a_form_the_sweep_parks_but_the_roster_refuses(
    accepting: None,
) -> None:
    # head_skip has no roster, so it parks this head as settled; the conductor never runs to post
    # the notice. The tick, which has the roster, must not let it disappear.
    body = "x\nBLOCKED-ON: pr a/b#1\nNEXT: Heisenberg"
    assert stage1_skips(body)
    mcp = _TickMcp()
    mcp.thread("p", "T-bad", content=body)
    report = (await run_tick(mcp=mcp, gh=_Gh({}), project="p", roster=_ROSTER)).as_dict()
    assert report["unclassified"] == ["T-bad"]
    assert report["errors"] == [
        {"thread_id": "T-bad", "reason": "terminal-form-refused: blocked_on_bad_wake"}
    ]
