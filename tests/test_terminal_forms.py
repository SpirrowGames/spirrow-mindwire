"""Terminal forms ``STOP: done`` / ``BLOCKED-ON:`` — the reading side (boundary 2).

T-done-disposition-auto-close, design v2.4 (Bohr msg-8185, approved by Einstein msg-8186). The
grammar is v1 §1 with the v1.1 / v2 / v2.1 / v2.2 corrections: the final control line, the n-1
rule for ``BLOCKED-ON``, the msg-8175 §2 field/body table, and the CommonMark fence rule of
msg-8179 §B (its seven cases are pinned here one by one). Boundary 3 (Bohr msg-8328 §2 3-A) turned
``handoff.ACCEPT_NEW_FORMS`` on; the OFF column is still pinned through the ``rejecting`` fixture,
where every well-formed new form must be MALFORMED with a notice. Nothing about ``NEXT: none`` may
change in either column (its retirement is boundary 5).
"""

from __future__ import annotations

import re

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
    malformed_notice,
    resolve_handoff,
    settles_by_terminal_form,
)
from spirrow_mindwire.conductor.head_skip import (
    TERMINAL_STOP_REASONS,
    parse_head_token,
    stage1_skips,
)
from spirrow_mindwire.park_wake.decide import PARK_WAKE_RELAY_AUTHOR
from spirrow_mindwire.park_wake.runner import run_tick
from spirrow_mindwire.pr_event_advance.auto_gate.decide import (
    ROSTER_UNAVAILABLE,
    AutoGateNoop,
    settled_tail_refusal,
    tail_decision,
)
from spirrow_mindwire.pr_event_advance.decide import Post

F3 = "`" * 3
F4 = "`" * 4

DONE = "x\n\nSTOP: done\n<!-- source: tools=3 -->\n"
BLOCKED = "x\n\nBLOCKED-ON: pr SpirrowGames/spirrow-lexora#104\nNEXT: Bohr"


@pytest.fixture
def accepting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The forms on, explicitly (the shipped default since boundary 3; kept so a test says it)."""
    monkeypatch.setattr(handoff_mod, "ACCEPT_NEW_FORMS", True)


@pytest.fixture
def rejecting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The forms off: the pre-boundary-3 column, which a revert of boundary 3 returns to."""
    monkeypatch.setattr(handoff_mod, "ACCEPT_NEW_FORMS", False)


def test_the_forms_ship_switched_on() -> None:
    """Boundary 3 (msg-8328 §2 3-A): the one commit that turns every reader on at once."""
    assert handoff_mod.ACCEPT_NEW_FORMS is True


@pytest.mark.parametrize("body", [DONE, BLOCKED])
def test_the_shipped_default_accepts_the_forms(body: str) -> None:
    """No fixture: the module default itself settles both forms, roster or not."""
    assert settles_by_terminal_form(body)
    handoff = resolve_handoff(body, _ROSTER)
    assert handoff.kind is HandoffKind.NONE and handoff.malformed is None


# --------------------------------------------------------------------------- grammar


@pytest.mark.parametrize("body", [DONE, BLOCKED])
def test_a_well_formed_new_form_is_malformed_while_off(rejecting: None, body: str) -> None:
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


# --------------------------------------------------------------------------- indentation (Slice I)
#
# msg-8455 §1 I-2: at a control-line position an indented line that is a well-formed terminal form
# once dedented is MALFORMED (``indented_terminal_form``), never ROLE and never ABSENT. Before
# Slice I the first cell was ABSENT with no notice and the next three routed to the wake at once,
# silently dropping the wait.

_INDENTED = MalformedHandoff.INDENTED_TERMINAL_FORM


@pytest.mark.parametrize(
    "body",
    [
        "x\n\n    STOP: done",
        "x\n\n    BLOCKED-ON: pr o/r#1\n    NEXT: Bohr",
        "x\n\n    BLOCKED-ON: pr o/r#1\nNEXT: Bohr",  # only BLOCKED-ON indented (msg-8455 §1)
        "x\n\n\tSTOP: done",
        "x\n\n\tBLOCKED-ON: pr o/r#1\nNEXT: Bohr",
        "x\n\n  \tBLOCKED-ON: thread T-a\nNEXT: Einstein",
        "x\n\n    STOP: done\n<!-- source: tools=3 -->\n",  # footer skipped, as for the plain form
        "x\n\n    STOP: done\n\n<!-- source: tools=3 -->\n<!-- attest: y -->\n",
        # A wake the plain form would refuse, or a reserved word: still the indentation reason.
        "x\n\n    BLOCKED-ON: pr o/r#1\nNEXT: none",
        "x\n\n    BLOCKED-ON: pr o/r#1\nNEXT: Heisenberg",
        "x\n\n    BLOCKED-ON: self job-1\nNEXT: Bohr",
    ],
)
def test_an_indented_terminal_form_at_a_control_position_is_malformed(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    for on in (False, True):
        monkeypatch.setattr(handoff_mod, "ACCEPT_NEW_FORMS", on)
        handoff = resolve_handoff(body, _ROSTER)
        assert (handoff.kind, handoff.malformed) == (HandoffKind.ABSENT, _INDENTED), on
        assert handoff_mod.refused_by_terminal_form(body)
        # head_skip LAUNCHes it (UNRESOLVED), so the conductor posts the notice — the same path
        # as every other MALFORMED form; its NEXT token is never read.
        assert parse_head_token(body) == ""
        assert not stage1_skips(body)


def test_the_indented_notice_says_to_write_it_unindented() -> None:
    notice = malformed_notice(_INDENTED)
    assert "`indented_terminal_form`" in notice
    assert "字下げせず" in notice


@pytest.mark.parametrize(
    "body",
    [
        "x\n\n   STOP: done",  # three spaces is not indented code
        "x\n\n   BLOCKED-ON: pr o/r#1\nNEXT: Bohr",
        "x\n\n   BLOCKED-ON: pr o/r#1\n   NEXT: Bohr",
    ],
)
def test_three_spaces_is_not_indented_code(accepting: None, body: str) -> None:
    handoff = resolve_handoff(body, _ROSTER)
    assert handoff.malformed is None
    assert handoff.kind is HandoffKind.NONE
    assert handoff.stop_line is not None and handoff.stop_line.terminal


@pytest.mark.parametrize(
    ("body", "identity"),
    [
        # An indented example in the middle of the body is still an example.
        ("x\n\n    STOP: done\n\nmore prose\n\nNEXT: Bohr", "Bohr"),
        ("x\n\n    BLOCKED-ON: pr o/r#1\n    NEXT: Einstein\n\nprose\nNEXT: Bohr", "Bohr"),
        # Not the line directly above the final NEXT: not a control position.
        ("x\n\n    BLOCKED-ON: pr o/r#1\n\nNEXT: Bohr", "Bohr"),
        ("x\n    BLOCKED-ON: pr o/r#1\nprose\nNEXT: Bohr", "Bohr"),
    ],
)
def test_an_indented_form_elsewhere_is_still_an_example(body: str, identity: str) -> None:
    handoff = resolve_handoff(body, _ROSTER)
    assert handoff.malformed is None
    assert (handoff.kind, handoff.identity) == (HandoffKind.ROLE, identity)


@pytest.mark.parametrize(
    "body",
    [
        f"x\n{F3}\n    STOP: done\n{F3}\n",
        f"x\n{F3}\n    STOP: done",
        f"x\n{F3}\n    BLOCKED-ON: pr o/r#1\n    NEXT: Bohr\n{F3}\n",
    ],
)
def test_an_indented_form_inside_a_fence_is_not_a_control_line(body: str) -> None:
    handoff = resolve_handoff(body, _ROSTER)
    assert handoff.malformed is None
    assert handoff.kind is HandoffKind.ABSENT


@pytest.mark.parametrize(
    "body",
    [
        "x\n\n    STOP: finished",  # not a well-formed form once dedented
        "x\n\n    stop: done",
        "x\n\n    BLOCKED-ON: soon\nNEXT: Bohr",
    ],
)
def test_an_indented_line_that_is_not_a_form_keeps_its_reading(body: str) -> None:
    # Only a line that is a well-formed form once dedented is refused for its indentation; any
    # other indented line is code, as before.
    handoff = resolve_handoff(body, _ROSTER)
    assert handoff.malformed is None


def test_an_indented_stop_above_a_next_keeps_its_reading() -> None:
    # Not in Slice I's scope (msg-8455 §1 lists the final line and BLOCKED-ON above NEXT only):
    # the legacy reading of a line above ``NEXT: none`` and the ROLE route are unchanged.
    assert resolve_handoff("x\n    STOP: done\nNEXT: Bohr", _ROSTER).identity == "Bohr"
    legacy = resolve_handoff("x\n    STOP: done\nNEXT: none", _ROSTER)
    assert legacy.kind is HandoffKind.NONE and legacy.malformed is None


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


def test_the_next_line_regex_is_anchored_at_line_start() -> None:
    # msg-8210 advisory / msg-8218 §2b-1. Not what fence exclusion depends on any more (the next
    # test), but the NEXT grammar itself is line-anchored, and a change to that is a grammar
    # change that must be made on purpose.
    regex = handoff_mod._NEXT_LINE_RE
    assert regex.pattern.startswith("^"), (
        "_NEXT_LINE_RE must stay anchored at the start of a line: the NEXT grammar is a "
        "whole-line control line, and the corpus is written against that"
    )
    assert regex.flags & re.MULTILINE, "_NEXT_LINE_RE must be MULTILINE so ^ means line start"


def test_fence_exclusion_survives_an_unanchored_next_regex(monkeypatch: pytest.MonkeyPatch) -> None:
    # _fenced_line_starts holds line-start offsets, and _control_next_matches normalises each
    # match to its line's start before comparing. With an unanchored regex a fenced match starts
    # mid-line; without the normalisation it would not be found in the set and the fenced
    # example would become the control line.
    anchored = handoff_mod._NEXT_LINE_RE
    unanchored = re.compile(anchored.pattern.removeprefix("^"), anchored.flags)
    monkeypatch.setattr(handoff_mod, "_NEXT_LINE_RE", unanchored)
    body = f"x\n{F3}\nsee NEXT: Bohr\n{F3}\n"
    (match,) = unanchored.finditer(body)
    line_start = body.rfind("\n", 0, match.start()) + 1
    assert match.start() != line_start  # the match really is mid-line
    assert handoff_mod._control_next_matches(body) == []
    # And a mid-line match outside any fence is kept.
    outside = f"x\n{F3}\ncode\n{F3}\nsee NEXT: Bohr"
    assert len(handoff_mod._control_next_matches(outside)) == 1


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


def test_head_skip_launches_new_forms_while_off(rejecting: None) -> None:
    # LAUNCH is what gets the MALFORMED notice posted (the notice moves the head).
    assert not stage1_skips(DONE)
    assert not stage1_skips(BLOCKED)
    # UNRESOLVED, not the wake's token: a refused form's NEXT says nothing (PR #531 msg-8201).
    assert parse_head_token(BLOCKED) == ""


def test_head_skip_reads_accepted_forms_as_none(accepting: None) -> None:
    for body in (DONE, BLOCKED):
        assert parse_head_token(body) == NONE_TOKEN
        assert stage1_skips(body)
        # The head cache replays the token alone; it must SKIP again.
        assert stage1_skips(f"NEXT: {parse_head_token(body)}")


def test_malformed_handoff_is_a_terminal_stop_reason() -> None:
    assert StopReason.MALFORMED_HANDOFF.value in TERMINAL_STOP_REASONS


def _gate(body: str, roster: object = _ROSTER, field: str | None = None) -> object:
    return tail_decision(
        body,
        repo="a/b",
        number=1,
        roster=roster,  # type: ignore[arg-type]
        next_participant=field,
    )


def test_auto_gate_tail_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handoff_mod, "ACCEPT_NEW_FORMS", False)
    assert _gate(DONE) is AutoGateNoop.QUEUED_BEHIND
    monkeypatch.setattr(handoff_mod, "ACCEPT_NEW_FORMS", True)
    assert isinstance(_gate(DONE), Post)
    assert isinstance(_gate(BLOCKED), Post)


@pytest.mark.parametrize(
    ("body", "field", "reason"),
    [
        ("x\nBLOCKED-ON: pr a/b#1\nNEXT: Heisenberg", None, "blocked_on_bad_wake"),
        ("x\nBLOCKED-ON: pr a/b#1\nNEXT: Nobody", None, "blocked_on_bad_wake"),
        ("x\nBLOCKED-ON: pr a/b#1\nNEXT: Bohr", "Einstein", "field_conflict"),
        (DONE, "Bohr", "field_conflict"),
    ],
)
def test_auto_gate_holds_a_settled_tail_the_roster_refuses(
    accepting: None, body: str, field: str | None, reason: str
) -> None:
    # msg-8218 §2b-2: the sweep parks this head as settled, and its notice is the park-wake
    # tick's. If 1c fired the gate first, the head would move and the refused form would never
    # be reported. So 1c reads it with the roster and the field, and holds it.
    assert stage1_skips(body)
    assert _gate(body, field=field) is AutoGateNoop.QUEUED_BEHIND
    assert settled_tail_refusal(body, roster=_ROSTER, next_participant=field) == (
        f"terminal-form-refused: {reason}"
    )


@pytest.mark.parametrize("roster", [None, {}])
@pytest.mark.parametrize("body", [DONE, BLOCKED])
def test_auto_gate_without_a_roster_holds_every_settled_tail(
    accepting: None, body: str, roster: object
) -> None:
    # Firing without the check could bury a refused head, so no roster means no fire on a
    # settled tail; the detail says why.
    assert _gate(body, roster=roster) is AutoGateNoop.QUEUED_BEHIND
    assert settled_tail_refusal(body, roster=roster) == ROSTER_UNAVAILABLE  # type: ignore[arg-type]


@pytest.mark.parametrize("roster", [None, {}])
def test_auto_gate_without_a_roster_still_fires_on_a_legacy_idle_tail(roster: object) -> None:
    # The roster is read for terminal forms only; ``NEXT: none`` keeps its pre-change reading.
    assert isinstance(_gate("x\n\nNEXT: none", roster=roster), Post)
    assert settled_tail_refusal("x\n\nNEXT: none", roster=roster) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- gateway disposition


def test_no_disposition_is_sent_for_a_new_form_while_off(rejecting: None) -> None:
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
async def test_conductor_stops_on_a_refused_form_with_a_notice(rejecting: None, body: str) -> None:
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
@pytest.mark.parametrize(
    ("body", "field", "reason"),
    [
        # A wake outside STOP_WAKE_ROLES (an implementer).
        ("x\nBLOCKED-ON: pr a/b#1\nNEXT: Heisenberg", None, "blocked_on_bad_wake"),
        # A structured field that contradicts the form.
        ("x\nBLOCKED-ON: pr a/b#1\nNEXT: Bohr", "Einstein", "field_conflict"),
        (DONE, "Bohr", "field_conflict"),
    ],
)
async def test_tick_posts_the_notice_for_a_form_the_sweep_parks_but_the_roster_refuses(
    accepting: None, body: str, field: str | None, reason: str
) -> None:
    # head_skip has no roster (nor the field), so it parks this head as settled and the conductor
    # is never launched to post the notice. A refused stop must never be silent (PR #531 gate
    # msg-8190): the tick, which has the roster, posts the conductor's notice itself.
    assert stage1_skips(body)
    mcp = _TickMcp()
    mcp.thread("p", "T-bad", content=body, msg_id="msg-7")
    if field is not None:
        mcp.threads[("p", "T-bad")]["messages"][0]["next_participant"] = field
    report = (await run_tick(mcp=mcp, gh=_Gh({}), project="p", roster=_ROSTER)).as_dict()
    assert report["unclassified"] == ["T-bad"]
    assert report["errors"] == []
    (post,) = mcp.posts
    assert post["thread_id"] == "T-bad"
    assert post["author"] == PARK_WAKE_RELAY_AUTHOR
    assert post["content"] == malformed_notice(MalformedHandoff(reason))
    assert "role" not in post
    # The notice moves the head to a NEXT: human the sweep parks on, and is not itself a form.
    assert stage1_skips(post["content"]) and not settles_by_terminal_form(post["content"])
    (notice,) = report["notices"]
    assert notice["refused_msg"] == "msg-7" and notice["malformed"] == reason
    assert notice["action"] == "posted"
    assert report["posted_heads"] == {"T-bad": notice["msg_id"]}


@pytest.mark.anyio
async def test_a_notice_that_fails_to_land_is_an_error_row(accepting: None) -> None:
    mcp = _TickMcp()
    mcp.thread("p", "T-bad", content="x\nBLOCKED-ON: pr a/b#1\nNEXT: Heisenberg")
    mcp.resolved_on_post.add("T-bad")
    report = (await run_tick(mcp=mcp, gh=_Gh({}), project="p", roster=_ROSTER)).as_dict()
    assert report["notices"][0]["action"] == "skipped"
    assert report["errors"] == [
        {
            "thread_id": "T-bad",
            "reason": "terminal-form-refused: blocked_on_bad_wake: thread-resolved",
        }
    ]
    assert report["posted_heads"] == {}


def test_the_conductor_and_the_tick_post_the_same_notice() -> None:
    # One text, owned by handoff.py; the conductor no longer keeps its own copy.
    from spirrow_mindwire.conductor import core

    assert not hasattr(core, "_malformed_notice")
    assert vars(core)["malformed_notice"] is malformed_notice


# --------------------------------------------------------------------------- the whole matrix
#
# PR #531 gate msg-8190 / msg-8201: each round found one cell of (accepted / malformed) x (final
# NEXT token) where a refused form was parked silently. This enumerates every cell and checks the
# class, not an instance: for each head, with the forms off and on, and with and without a
# contradicting ``next_participant`` field —
#
# - refused (``resolve_handoff`` with the roster sets ``malformed``): the MALFORMED notice is
#   posted EXACTLY ONCE between the two readers that can say it — the conductor (when the sweep
#   LAUNCHes) and the park-wake tick (when the sweep SKIPs) — and 1c fires on NEITHER share, with
#   or without a roster (msg-8218 §2b-2: on the tick's share a fire would move the head before a
#   tick that failed could retry, and the refusal would never be said);
# - accepted: the sweep SKIPs (also on the cached token), the tick posts nothing, and nothing is
#   launched — a valid wait never relaunches. 1c fires on it (it is idle) only with a roster.

_TOKENS: dict[str, str | None] = {
    "participant": "Bohr",
    "human": HUMAN_TOKEN,
    "none": NONE_TOKEN,
    "operator": "operator",
    "pr-review": "pr-review a/b#1",
    "absent": None,
}


def _matrix_bodies() -> list[tuple[str, str]]:
    """``(cell id, body)`` for every terminal-form shape under every final ``NEXT:`` token."""
    cells: list[tuple[str, str]] = []
    for name, token in _TOKENS.items():
        # A ``NEXT:`` line earlier in the body (quoted, or a stale one) above a final STOP line.
        lead = f"x\nNEXT: {token}\n\n" if token is not None else "x\n\n"
        cells.append((f"stop-done/{name}", f"{lead}STOP: done"))
        cells.append((f"stop-not-done/{name}", f"{lead}STOP: nope"))
        cells.append((f"stop-spaced/{name}", f"{lead}STOP : done"))
        cells.append((f"stop-done-indented/{name}", f"{lead}    STOP: done"))  # Slice I
        if token is None:
            continue
        # Slice I (msg-8455 §1): BLOCKED-ON indented, with and without its NEXT indented too.
        cells.append(
            (f"blocked-on-indented/{name}", f"x\n    BLOCKED-ON: pr a/b#1\n    NEXT: {token}")
        )
        cells.append(
            (f"blocked-on-indented-above/{name}", f"x\n    BLOCKED-ON: pr a/b#1\nNEXT: {token}")
        )
        cells.append((f"blocked-on-bad-form/{name}", f"x\nBLOCKED-ON: soon\nNEXT: {token}"))
        cells.append((f"blocked-on-self/{name}", f"x\nBLOCKED-ON: self job-1\nNEXT: {token}"))
        cells.append((f"blocked-on-lowercase/{name}", f"x\nblocked-on: pr a/b#1\nNEXT: {token}"))
        cells.append((f"blocked-on-pr/{name}", f"x\nBLOCKED-ON: pr a/b#1\nNEXT: {token}"))
        cells.append((f"blocked-on-thread/{name}", f"x\nBLOCKED-ON: thread T-other\nNEXT: {token}"))
    # Participant-only shapes: a roster-refused wake, and STOP above a participant.
    cells.append(
        ("blocked-on-implementer/participant", "x\nBLOCKED-ON: pr a/b#1\nNEXT: Heisenberg")
    )
    cells.append(("blocked-on-unknown/participant", "x\nBLOCKED-ON: pr a/b#1\nNEXT: Nobody"))
    cells.append(("stop-above-next/participant", "x\nSTOP: done\nNEXT: Bohr"))
    return cells


_MATRIX = [
    pytest.param(body, field, on, id=f"{cell}/field={field}/accept={on}")
    for cell, body in _matrix_bodies()
    for field in (None, "Einstein")
    for on in (False, True)
]


@pytest.mark.anyio
@pytest.mark.parametrize(("body", "field", "on"), _MATRIX)
async def test_no_cell_of_the_matrix_is_silent(
    monkeypatch: pytest.MonkeyPatch, body: str, field: str | None, on: bool
) -> None:
    monkeypatch.setattr(handoff_mod, "ACCEPT_NEW_FORMS", on)
    # Every cell is a terminal form; a legacy body would test nothing here.
    assert handoff_mod.terminal_form_view(body) is not handoff_mod.TerminalView.LEGACY
    refused = resolve_handoff(body, _ROSTER, next_participant=field).malformed

    # Sweep (roster-free, field-blind), on the fetched body and on the cached token.
    sweep_skips = stage1_skips(body)
    assert stage1_skips(f"NEXT: {parse_head_token(body)}") is sweep_skips

    # Park-wake tick (roster + field).
    tick = _TickMcp()
    tick.thread("p", "T-other", content="x\n\nNEXT: Bohr")
    tick.thread("p", "T-x", content=body, msg_id="msg-7")
    if field is not None:
        tick.threads[("p", "T-x")]["messages"][0]["next_participant"] = field
    report = (await run_tick(mcp=tick, gh=_Gh({}), project="p", roster=_ROSTER)).as_dict()
    tick_notices = [p["content"] for p in tick.posts if p["thread_id"] == "T-x"]

    # Conductor — launched only when the sweep does not skip.
    conductor_notices: list[str] = []
    if not sweep_skips:
        mcp = _FakeChatroomMcp()
        mcp.seed(author="Bohr", content=body, next_participant=field)
        disp = _ScriptedDispatcher(mcp, {})
        outcome = await _conductor_with(mcp, disp, None).run()
        assert disp.spawns == []
        if refused is not None:
            assert outcome.stop_reason is StopReason.MALFORMED_HANDOFF
        conductor_notices = [p["content"] for p in mcp.posts]

    # 1c, with the roster the pass loads and with none (an empty or unloadable roster).
    gate_with = _gate(body, field=field)
    gate_without = _gate(body, roster=None, field=field)

    if refused is not None:
        notices = tick_notices + conductor_notices
        assert notices == [malformed_notice(refused)], "a refused form must be said exactly once"
        # Neither share is an idle tail for 1c: the conductor is answering it, or the tick owns
        # its notice (and a fire would bury the head before a failed tick could retry).
        assert gate_with is AutoGateNoop.QUEUED_BEHIND
        assert gate_without is AutoGateNoop.QUEUED_BEHIND
        # The notice moves the head to one the sweep parks and that is not itself a form.
        assert stage1_skips(notices[0])
        assert handoff_mod.terminal_form_view(notices[0]) is handoff_mod.TerminalView.LEGACY
    else:
        assert on, "nothing is accepted while the forms are off"
        assert sweep_skips and parse_head_token(body) == NONE_TOKEN
        assert tick_notices == [] and report["notices"] == []
        assert conductor_notices == []
        # Accepted and idle: 1c fires, but only once the roster has confirmed it.
        assert isinstance(gate_with, Post)
        assert gate_without is AutoGateNoop.QUEUED_BEHIND


def test_the_matrix_covers_every_token_and_both_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    # The matrix is only as good as its cells: every NEXT token appears, and with the forms on it
    # holds both accepted heads and heads refused only by the roster / the field (the tick's
    # share) as well as heads refused without one (the conductor's share).
    monkeypatch.setattr(handoff_mod, "ACCEPT_NEW_FORMS", True)
    names = {cell.split("/")[1] for cell, _ in _matrix_bodies()}
    assert names == set(_TOKENS)
    shares = set()
    for _, body in _matrix_bodies():
        for field in (None, "Einstein"):
            refused = resolve_handoff(body, _ROSTER, next_participant=field).malformed
            skips = stage1_skips(body)
            shares.add(("accepted" if refused is None else "refused", skips))
    assert shares == {("accepted", True), ("refused", True), ("refused", False)}


@pytest.mark.parametrize("token", ["none", "human", "operator"])
def test_a_refused_form_ending_on_a_stop_token_launches(token: str) -> None:
    # The msg-8201 instance, pinned by name: the token of a refused form is never read.
    body = f"x\nBLOCKED-ON: pr a/b#1\nNEXT: {token}"
    assert handoff_mod.refused_by_terminal_form(body)
    assert parse_head_token(body) == ""
    assert not stage1_skips(body)
