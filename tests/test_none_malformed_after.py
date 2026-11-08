"""Boundary 5-A: the ``NEXT: none`` cutoff (``handoff.NONE_MALFORMED_AFTER``), shipped as ``None``.

T-done-disposition-auto-close, Bohr msg-8455 §3 / msg-8556 §3. The rule: with a cutoff set, a
legacy ``NEXT: none`` (with or without the old ``STOP:`` line above it) posted at or after the
cutoff -- or at an unknown time, or at a time with no timezone -- is MALFORMED
(``legacy_none_after_cutoff``) and stops with the notice; one posted before it is read as it always
was. The new forms and every other ``NEXT:`` token are not affected. 5-A ships no cutoff, so every
reader answers exactly as before; the cells below set one through the explicit argument (or by
monkeypatching the constant for readers that do not take the argument), as the 5-B commit will.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from test_conductor_core import _FakeChatroomMcp, _ScriptedDispatcher
from test_decider_step2 import _conductor_with
from test_stop_disposition_slice3 import _ROSTER, _Gh, _TickMcp
from test_terminal_forms import _matrix_bodies

from spirrow_mindwire.conductor import handoff as handoff_mod
from spirrow_mindwire.conductor.core import StopReason
from spirrow_mindwire.conductor.disposition import body_disposition, stop_line_of
from spirrow_mindwire.conductor.handoff import (
    HandoffKind,
    MalformedHandoff,
    StopStatus,
    malformed_notice,
    none_refused_after_cutoff,
    posted_at_of,
    resolve_handoff,
)
from spirrow_mindwire.conductor.head_skip import (
    CACHED_TOKEN_POSTED_AT,
    Decision,
    decide,
    parse_head_token,
    stage1_skips,
)
from spirrow_mindwire.park_wake.runner import run_tick
from spirrow_mindwire.pr_event_advance.auto_gate.decide import AutoGateNoop, tail_decision
from spirrow_mindwire.pr_event_advance.decide import Post
from spirrow_mindwire.tail_cache import SCHEMA_VERSION, CachedTail

CUTOFF = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)
BEFORE = CUTOFF - timedelta(seconds=1)
AFTER = CUTOFF + timedelta(hours=1)
NAIVE = datetime(2026, 11, 13, 0, 0)  # the timezone-less cell

BARE_NONE = "x\n\nNEXT: none"
STOP_NONE = "x\n\nSTOP: done\nNEXT: none"
BLOCKED_NONE = "x\n\nSTOP: blocked-on pr:a/b#1 wake:Bohr\nNEXT: none"
LEGACY_BODIES = [BARE_NONE, STOP_NONE, BLOCKED_NONE]

# (cell id, posted_at, refused?) with the cutoff set.
TIME_CELLS = [
    ("before", BEFORE, False),
    ("exactly", CUTOFF, True),
    ("after", AFTER, True),
    ("unknown", None, True),
    ("naive", NAIVE, True),
]

# Heads whose NEXT token is not ``none``: the readers that do not pass ``posted_at`` look only
# at these kinds (ROLE / HUMAN / operator / PR_REVIEW), so they must not move.
OTHER_BODIES = [
    "x\n\nNEXT: Bohr",
    "x\n\nNEXT: human",
    "x\n\nTIER-C: scope\nNEXT: human",
    "x\nOPERATOR-TASK: restart the web service\nTIER-C-CHECK: none\nNEXT: operator",
    "x\n\nNEXT: operator",
    "x\n\nNEXT: pr-review SpirrowGames/spirrow-mindwire#1",
    "x\n\nNEXT: Nobody",
    "x\n\nno handoff",
]


def test_the_cutoff_ships_unset() -> None:
    """5-A is code only: no cutoff, so no reader changes (msg-8556 §3 5A-1)."""
    assert handoff_mod.NONE_MALFORMED_AFTER is None


@pytest.mark.parametrize("body", LEGACY_BODIES + OTHER_BODIES)
@pytest.mark.parametrize("posted_at", [None, BEFORE, CUTOFF, AFTER, NAIVE])
def test_no_cutoff_changes_nothing(body: str, posted_at: datetime | None) -> None:
    baseline = resolve_handoff(body, _ROSTER)
    assert resolve_handoff(body, _ROSTER, posted_at=posted_at) == baseline
    assert not none_refused_after_cutoff(body, posted_at=posted_at)
    assert parse_head_token(body, posted_at=posted_at) == parse_head_token(body)
    assert stage1_skips(body, posted_at=posted_at) is stage1_skips(body)


@pytest.mark.parametrize("body", LEGACY_BODIES)
@pytest.mark.parametrize(("cell", "posted_at", "refused"), TIME_CELLS)
def test_legacy_none_with_a_cutoff(
    body: str, cell: str, posted_at: datetime | None, refused: bool
) -> None:
    got = resolve_handoff(body, _ROSTER, posted_at=posted_at, none_malformed_after=CUTOFF)
    if refused:
        assert got.kind is HandoffKind.ABSENT, cell
        assert got.malformed is MalformedHandoff.LEGACY_NONE_AFTER_CUTOFF
        assert got.stop_line is None
    else:
        assert got == resolve_handoff(body, _ROSTER), cell
        assert got.kind is HandoffKind.NONE
    # The roster-free reading agrees with the roster's.
    assert (
        none_refused_after_cutoff(body, posted_at=posted_at, none_malformed_after=CUTOFF) is refused
    )


def test_a_field_none_is_the_legacy_none_too() -> None:
    # ``next_participant: none`` resolves to the same legacy NONE; the cutoff retires it as well.
    for body in ("x\n\nno handoff", BARE_NONE):
        got = resolve_handoff(
            body, _ROSTER, next_participant="none", posted_at=AFTER, none_malformed_after=CUTOFF
        )
        assert got.malformed is MalformedHandoff.LEGACY_NONE_AFTER_CUTOFF


def test_a_non_utc_offset_is_compared_as_an_instant() -> None:
    jst = timezone(timedelta(hours=9))
    just_before = (CUTOFF - timedelta(seconds=1)).astimezone(jst)
    assert not none_refused_after_cutoff(
        BARE_NONE, posted_at=just_before, none_malformed_after=CUTOFF
    )
    assert none_refused_after_cutoff(
        BARE_NONE, posted_at=CUTOFF.astimezone(jst), none_malformed_after=CUTOFF
    )


@pytest.mark.parametrize("body", OTHER_BODIES)
@pytest.mark.parametrize(("cell", "posted_at", "refused"), TIME_CELLS)
def test_other_tokens_are_not_affected(
    body: str, cell: str, posted_at: datetime | None, refused: bool
) -> None:
    """The readers that do not pass ``posted_at`` (head_skip's operator check, the operator
    lane, parked_lane, design_conditions, stall_ledger's operator row, 1c's manual-fire check,
    the gate-only slice) read only these kinds. None of them moves under a cutoff."""
    del cell, refused
    with_cutoff = resolve_handoff(body, _ROSTER, posted_at=posted_at, none_malformed_after=CUTOFF)
    assert with_cutoff == resolve_handoff(body, _ROSTER)
    assert resolve_handoff(
        body, {}, posted_at=posted_at, none_malformed_after=CUTOFF
    ) == resolve_handoff(body, {})


@pytest.mark.parametrize(("cell", "body"), _matrix_bodies())
def test_the_new_forms_are_not_affected(cell: str, body: str) -> None:
    # Every cell of the #531 terminal-form matrix, with and without a field, at an unknown time.
    for field in (None, "Einstein"):
        got = resolve_handoff(body, _ROSTER, next_participant=field, none_malformed_after=CUTOFF)
        assert got == resolve_handoff(body, _ROSTER, next_participant=field), cell
        assert got.malformed is not MalformedHandoff.LEGACY_NONE_AFTER_CUTOFF
    assert not none_refused_after_cutoff(body, posted_at=None, none_malformed_after=CUTOFF)


def test_posted_at_of() -> None:
    assert posted_at_of({"timestamp": "2026-11-08T22:30:41.569592Z"}) == datetime(
        2026, 11, 8, 22, 30, 41, 569592, tzinfo=UTC
    )
    assert posted_at_of({"timestamp": "2026-11-08T22:30:41+09:00"}) is not None
    assert posted_at_of({"timestamp": "2026-11-08T22:30:41"}) is None  # no timezone
    assert posted_at_of({"timestamp": "garbage"}) is None
    assert posted_at_of({"timestamp": ""}) is None
    assert posted_at_of({}) is None
    assert posted_at_of({"timestamp": AFTER}) == AFTER
    assert posted_at_of({"timestamp": NAIVE}) is None


# ----------------------------------------------------------------------------- readers, cut on


@pytest.fixture
def cut(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 5-B state: the constant set (readers that take no explicit cutoff argument)."""
    monkeypatch.setattr(handoff_mod, "NONE_MALFORMED_AFTER", CUTOFF)


@pytest.mark.usefixtures("cut")
@pytest.mark.parametrize("body", LEGACY_BODIES)
def test_head_skip_launches_a_refused_legacy_none(body: str) -> None:
    now = AFTER + timedelta(minutes=5)
    assert parse_head_token(body, posted_at=AFTER) == ""
    assert not stage1_skips(body, posted_at=AFTER)
    verdict = decide(
        now=now,
        head_msg_id="msg-9",
        head_body=body,
        control_state="run",
        record=None,
        posted_at=AFTER,
    )
    assert verdict.decision is Decision.LAUNCH
    # Before the cutoff it still parks on the stop token, as today.
    assert parse_head_token(body, posted_at=BEFORE) == "none"
    before = decide(
        now=now,
        head_msg_id="msg-9",
        head_body=body,
        control_state="run",
        record=None,
        posted_at=BEFORE,
    )
    assert before.decision is Decision.SKIP and before.reason == "stop-token"


@pytest.mark.usefixtures("cut")
def test_a_cached_token_is_not_cut_twice() -> None:
    # A cached ``none`` (a pre-cutoff legacy settle, or an accepted STOP: done) replays as
    # ``NEXT: none`` with no time of its own; it must keep parking. A refused head is cached as
    # ``""`` and replays UNRESOLVED.
    assert stage1_skips("NEXT: none", posted_at=CACHED_TOKEN_POSTED_AT)
    assert parse_head_token("NEXT: ", posted_at=CACHED_TOKEN_POSTED_AT) == ""
    assert parse_head_token("x\n\nSTOP: done", posted_at=AFTER) == "none"


@pytest.mark.anyio
@pytest.mark.usefixtures("cut")
@pytest.mark.parametrize("body", LEGACY_BODIES)
async def test_the_conductor_posts_the_notice(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    # The fake chatroom stamps every message 2026-06-07; put the cutoff before it.
    monkeypatch.setattr(handoff_mod, "NONE_MALFORMED_AFTER", datetime(2026, 6, 1, tzinfo=UTC))
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content=body)
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor_with(mcp, disp, None).run()
    assert disp.spawns == []
    assert outcome.stop_reason is StopReason.MALFORMED_HANDOFF
    notice = malformed_notice(MalformedHandoff.LEGACY_NONE_AFTER_CUTOFF)
    assert [p["content"] for p in mcp.posts] == [notice]
    # The notice names the two new forms and NEXT: human, and the sweep parks on it.
    assert "STOP: done" in notice and "BLOCKED-ON" in notice and "NEXT: human" in notice
    assert stage1_skips(notice, posted_at=AFTER)


@pytest.mark.anyio
@pytest.mark.usefixtures("cut")
async def test_the_conductor_settles_a_pre_cutoff_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handoff_mod, "NONE_MALFORMED_AFTER", datetime(2026, 7, 1, tzinfo=UTC))
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content=STOP_NONE)
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor_with(mcp, disp, None).run()
    assert outcome.stop_reason is StopReason.SETTLED
    assert mcp.posts == []


@pytest.mark.usefixtures("cut")
def test_1c_does_not_fire_on_a_refused_legacy_none() -> None:
    def gate(posted_at: datetime | None) -> object:
        return tail_decision(BARE_NONE, repo="a/b", number=1, roster=_ROSTER, posted_at=posted_at)

    assert gate(AFTER) is AutoGateNoop.QUEUED_BEHIND
    assert gate(None) is AutoGateNoop.QUEUED_BEHIND
    assert isinstance(gate(BEFORE), Post)


@pytest.mark.usefixtures("cut")
def test_the_gateway_sends_no_disposition_for_a_refused_legacy_none() -> None:
    assert body_disposition(STOP_NONE, _ROSTER, posted_at=BEFORE) == {"kind": "done"}
    assert body_disposition(STOP_NONE, _ROSTER, posted_at=AFTER) is None
    # The new form is untouched.
    assert body_disposition("x\n\nSTOP: done", _ROSTER, posted_at=AFTER) == {"kind": "done"}


@pytest.mark.usefixtures("cut")
def test_stop_line_of_keeps_history() -> None:
    stop = stop_line_of(BLOCKED_NONE, _ROSTER, posted_at=BEFORE)
    assert stop is not None and stop.status is StopStatus.BLOCKED_ON
    assert stop_line_of(BLOCKED_NONE, _ROSTER, posted_at=AFTER) is None


@pytest.mark.anyio
@pytest.mark.usefixtures("cut")
async def test_park_wake_reads_the_tail_timestamp() -> None:
    # A pre-cutoff legacy wait is still woken; a post-cutoff one is neither woken nor answered by
    # the tick (head_skip LAUNCHes it and the conductor posts the notice: said once).
    for stamp, wakes in (("2026-11-11T00:00:00Z", True), ("2026-11-12T01:00:00Z", False)):
        tick = _TickMcp()
        tick.thread("p", "T-a", status="resolved")
        tick.thread("p", "T-x", content="w\n\nSTOP: blocked-on thread:T-a wake:Bohr\nNEXT: none")
        tick.threads[("p", "T-x")]["messages"][0]["timestamp"] = stamp
        report = (await run_tick(mcp=tick, gh=_Gh({}), project="p", roster=_ROSTER)).as_dict()
        woken = [w["thread_id"] for w in report["woken"]]
        assert (woken == ["T-x"]) is wakes, stamp
        assert report["notices"] == []
        if not wakes:
            assert tick.posts == []


def test_the_tail_cache_carries_the_timestamp() -> None:
    assert SCHEMA_VERSION == 2
    tail = CachedTail.from_message(
        {"msg_id": "m1", "author": "a", "content": "c", "timestamp": "2026-11-08T22:30:41Z"}
    )
    assert posted_at_of(tail.as_message()) == datetime(2026, 11, 8, 22, 30, 41, tzinfo=UTC)
    assert CachedTail.from_message(tail.as_message()) == tail


def test_the_tail_cache_keeps_a_datetime_timestamp() -> None:
    """PR-gate on #548: a datetime ``timestamp`` is kept, not dropped to "after the cutoff"."""
    stamp = datetime(2026, 11, 8, 22, 30, 41, tzinfo=UTC)
    tail = CachedTail.from_message(
        {"msg_id": "m1", "author": "a", "content": "c", "timestamp": stamp}
    )
    assert tail.timestamp is not None
    assert posted_at_of(tail.as_message()) == stamp
    assert CachedTail.from_message(tail.as_message()) == tail
    naive = CachedTail.from_message(
        {"msg_id": "m1", "author": "a", "content": "c", "timestamp": NAIVE}
    )
    assert posted_at_of(naive.as_message()) is None  # no timezone stays unknown, as posted_at_of
