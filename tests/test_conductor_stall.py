"""T42 — the generic stall watchdog.

Thread T-silent-stops-need-a-generic-watchdog-and-a-loud-stand-down (Fermi msg-3098 §4 DoD, design
Bohr msg-4532 §1 / msg-4569, build instructions Bohr msg-4907, symmetry ruling Einstein on the
same thread). What is pinned here:

1. The sweep's count (``launches_same_head``): +1 per committed LAUNCH on the same
   ``head_msg_id_at_launch``, back to 1 on a new head, untouched by DEFER / SKIP / observation /
   outcome, 0 for a record written before the field existed.
2. ``stalled_to_human`` is in ``TERMINAL_STOP_REASONS`` with the other two "this head goes
   nowhere" reasons.
3. The DoD, end to end, with a fault-injection adapter: a delivery that never posts and never
   says why, on a path where no terminal stop reason fires. The 3rd launch on that head posts
   STALLED ending ``NEXT: human`` and stops with exit 0; a notice that does not land exits 4.
4. The flags reach the Conductor from ``mindwire-loop``'s command line.
5. ``repo_dir = ""`` reads as unset (PR #357 gate advisory), so T44 reports it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import _ROSTER, _FakeChatroomMcp, _thread_ref

from spirrow_mindwire import loop_runner
from spirrow_mindwire.conductor.core import Conductor, ConductorOutcome, StopReason
from spirrow_mindwire.conductor.head_skip import (
    TERMINAL_STOP_REASONS,
    Decision,
    Record,
    commit_launch,
    commit_observation,
    commit_terminal,
    decide,
    record_from_json,
    record_to_json,
)
from spirrow_mindwire.conductor.stall import (
    EVENT_KIND_STALLED,
    STALL_THRESHOLD,
    STALLED_EXIT_CODE,
    StalledError,
    is_stalled,
)
from spirrow_mindwire.conductor.stand_down import STAND_DOWN_EXIT_CODE
from spirrow_mindwire.config import Stage3LoopConfig
from spirrow_mindwire.magickit.client import ThreadResolvedError
from spirrow_mindwire.ulid_util import new_ulid
from spirrow_mindwire.value_objects import (
    ChatroomEvent,
    Role,
    SessionHandle,
    ThreadRef,
)

_STALL_LOGGER = "spirrow_mindwire.conductor.stall"
_T0 = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)


def _stall_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _STALL_LOGGER and r.getMessage().startswith(EVENT_KIND_STALLED)
    ]


def _launch(record: Record | None, head: str, now: datetime, body: str = "NEXT: Bohr") -> Record:
    """One sweep LAUNCH on ``head``: decide (must say LAUNCH), then commit, as the wrapper does."""
    v = decide(now=now, head_msg_id=head, head_body=body, control_state="run", record=record)
    assert v.decision is Decision.LAUNCH, v
    return commit_launch(
        now=now, head_msg_id=head, verdict=v, control_state="run", prior_record=record
    )


# --------------------------------------------------------------------------- #
# 1. the count
# --------------------------------------------------------------------------- #


def test_count_grows_only_on_launches_of_the_same_head() -> None:
    rec = _launch(None, "m7", _T0)
    assert rec.launches_same_head == 1
    rec = _launch(rec, "m7", _T0 + timedelta(minutes=15))
    assert rec.launches_same_head == 2
    rec = _launch(rec, "m7", _T0 + timedelta(minutes=45))
    assert rec.launches_same_head == 3


def test_count_resets_to_one_when_the_head_moves() -> None:
    rec = _launch(None, "m7", _T0)
    rec = _launch(rec, "m7", _T0 + timedelta(minutes=15))
    # Someone posted: a new head, with a new nomination so the launch is not deferred.
    rec = _launch(rec, "m9", _T0 + timedelta(minutes=16), body="NEXT: Heisenberg")
    assert rec.launches_same_head == 1


def test_defer_and_observation_ticks_do_not_move_the_count() -> None:
    # Bohr msg-4907 instruction 1: a DEFER tick must not count, or backoff moves the threshold.
    rec = _launch(None, "m7", _T0)
    rec = _launch(rec, "m7", _T0 + timedelta(minutes=15))
    before = rec.launches_same_head
    now = _T0 + timedelta(minutes=20)  # inside the 30-minute backoff
    v = decide(now=now, head_msg_id="m7", head_body="NEXT: Bohr", control_state="run", record=rec)
    assert v.decision is Decision.DEFER
    rec = commit_observation(now=now, head_msg_id="m7", token="bohr", record=rec)
    assert rec.launches_same_head == before
    rec = commit_terminal(reason="human", head_msg_id="m7", record=rec)
    assert rec.launches_same_head == before


def test_count_keys_on_the_launch_head_not_the_observed_head() -> None:
    # An observation of a different head (a SKIP or DEFER on it) must not reset or advance the
    # count; only the next LAUNCH decides, against head_msg_id_at_launch.
    rec = _launch(None, "m7", _T0)
    rec = commit_observation(
        now=_T0 + timedelta(minutes=1), head_msg_id="m8", token="bohr", record=rec
    )
    assert rec.last_observed_head_msg_id == "m8"
    assert rec.launches_same_head == 1


def test_an_unknown_head_never_accumulates() -> None:
    rec = _launch(None, "", _T0)
    rec = _launch(rec, "", _T0 + timedelta(minutes=15))
    assert rec.launches_same_head == 1


def test_count_round_trips_and_a_legacy_record_reads_zero() -> None:
    rec = _launch(None, "m7", _T0)
    rec = _launch(rec, "m7", _T0 + timedelta(minutes=15))
    assert record_from_json(record_to_json(rec)) == rec
    legacy = record_to_json(rec)
    del legacy["launches_same_head"]
    old = record_from_json(legacy)
    assert old is not None and old.launches_same_head == 0
    # The first launch after an upgrade counts from 1, never from a guess.
    assert _launch(old, "m7", _T0 + timedelta(minutes=45)).launches_same_head == 1


# --------------------------------------------------------------------------- #
# 2. terminal-set symmetry (Einstein, this thread)
# --------------------------------------------------------------------------- #


def test_stalled_is_terminal_like_the_other_two_dead_head_reasons() -> None:
    assert {
        StopReason.NO_PROGRESS.value,
        StopReason.SELF_HANDOFF.value,
        StopReason.STALLED.value,
    } == TERMINAL_STOP_REASONS
    # And it parks the posted notice's id like the other two.
    rec = commit_terminal(reason=StopReason.STALLED.value, head_msg_id="m10", record=None)
    assert rec.terminal_stop_reason == "stalled_to_human"
    assert rec.terminal_head_msg_id == "m10"


def test_no_progress_still_needs_the_registry() -> None:
    # Why the symmetry goes this way and not "drop the other two": a NO_PROGRESS stop posts
    # nothing, so its head still says ``NEXT: Bohr`` and only the terminal record parks it.
    rec = commit_terminal(reason=StopReason.NO_PROGRESS.value, head_msg_id="m7", record=None)
    v = decide(now=_T0, head_msg_id="m7", head_body="NEXT: Bohr", control_state="run", record=rec)
    assert v.decision is Decision.SKIP and v.reason.startswith("terminal-stop:")


# --------------------------------------------------------------------------- #
# is_stalled
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("count", "pinned", "head", "expected"),
    [
        (STALL_THRESHOLD - 1, "m7", "m7", False),
        (STALL_THRESHOLD, "m7", "m7", True),
        (STALL_THRESHOLD + 5, "m7", "m7", True),
        (STALL_THRESHOLD, "m7", "m8", False),  # head moved since the sweep counted: progress
        (STALL_THRESHOLD, None, "m7", False),  # an unpinned count is not evidence
        (STALL_THRESHOLD, "", "m7", False),
    ],
)
def test_is_stalled(count: int, pinned: str | None, head: str, expected: bool) -> None:
    assert (
        is_stalled(launches_same_head=count, launch_head_msg_id=pinned, head_msg_id=head)
        is expected
    )


# --------------------------------------------------------------------------- #
# 3. DoD — fault injection, through three sweep launches
# --------------------------------------------------------------------------- #


class _SilentHangDispatcher:
    """Fault-injection adapter (Bohr msg-4532 §0 (i)): delivery that never posts and never says why.

    Not the self-filter (that is SELF_HANDOFF, caught in ``_route``) and not a clean return (that
    ends on NO_PROGRESS, which TERMINAL_STOP_REASONS already parks). This models the unknown path:
    the session never comes back, the wrapper's timeout kills it, and so no stop reason — terminal
    or otherwise — is ever recorded. Only the head not moving is observable.
    """

    def __init__(self) -> None:
        self.spawns: list[tuple[Role, str]] = []
        self.dispatches = 0

    async def spawn_instance(
        self, thread_ref: ThreadRef, role: Role, instance_id: str
    ) -> SessionHandle:
        self.spawns.append((role, instance_id))
        return SessionHandle(
            session_id=new_ulid(),
            instance_id=instance_id,
            adapter_id="fault-injection",
            thread_ref=thread_ref,
            role=role,
            started_at=_T0,
        )

    async def dispatch(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        self.dispatches += 1
        await asyncio.Event().wait()  # never returns, never posts


class _RefusingMcp(_FakeChatroomMcp):
    """The thread is readable but was resolved underneath the run: every post is refused."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_post_message":
            raise ThreadResolvedError("thread resolved")
        return await super().call_tool(name, arguments)


def _conductor(mcp: _FakeChatroomMcp, dispatcher: Any, record: Record) -> Conductor:
    # Exactly what the wrapper hands over: the committed count and the head it was counted on.
    return Conductor(
        mcp=mcp,
        dispatcher=dispatcher,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        launches_same_head=record.launches_same_head,
        launch_head_msg_id=record.head_msg_id_at_launch,
    )


def _seed_stuck_thread(mcp: _FakeChatroomMcp) -> str:
    # Implementer → proposer: an ordinary AI-addressed handoff that routes straight to a spawn.
    mcp.seed(author="Heisenberg", content="PR is up.\n\nNEXT: Bohr")
    return "m1"


async def _sweep_until_third_launch(
    mcp: _FakeChatroomMcp, head: str
) -> tuple[Record, _SilentHangDispatcher, datetime]:
    """Launches 1 and 2 on ``head``: each spawns, hangs, and is killed with no outcome recorded."""
    dispatcher = _SilentHangDispatcher()
    record: Record | None = None
    now = _T0
    for n in (1, 2):
        record = _launch(record, head, now)
        assert record.launches_same_head == n
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_conductor(mcp, dispatcher, record).run(), timeout=0.05)
        now += timedelta(minutes=60)  # past any backoff
    assert record is not None
    assert dispatcher.dispatches == 2  # two real sessions ran and posted nothing
    return _launch(record, head, now), dispatcher, now


@pytest.mark.anyio
async def test_third_launch_on_a_silent_head_posts_stalled_and_stops_at_human(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    mcp = _FakeChatroomMcp()
    head = _seed_stuck_thread(mcp)
    record, dispatcher, now = await _sweep_until_third_launch(mcp, head)
    assert record.launches_same_head == STALL_THRESHOLD == 3

    outcome = await _conductor(mcp, dispatcher, record).run()

    # Not spawned on the stalled launch.
    assert len(dispatcher.spawns) == 2 and dispatcher.dispatches == 2
    assert outcome.stop_reason is StopReason.STALLED
    (post,) = mcp.posts
    assert (post["project"], post["thread_id"]) == ("spirrow-mindwire", "T-cond")
    assert "STALLED" in post["content"]
    assert post["content"].rstrip().splitlines()[-1] == "NEXT: human"
    assert outcome.last_msg_id == "m2"  # recorded against the notice a human will open
    # event_log
    (line,) = _stall_lines(caplog)
    assert "thread=T-cond" in line and f"head_msg_id={head}" in line
    assert "launches_same_head=3" in line and "target=Bohr" in line
    # The next tick: the wrapper records the outcome, and the thread parks on the notice.
    record = commit_terminal(
        reason=outcome.stop_reason.value, head_msg_id=outcome.last_msg_id or "", record=record
    )
    v = decide(
        now=now + timedelta(minutes=60),
        head_msg_id="m2",
        head_body=post["content"],
        control_state="run",
        record=record,
    )
    assert v.decision is Decision.SKIP


@pytest.mark.anyio
async def test_stalled_notice_that_does_not_land_exits_non_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    mcp = _RefusingMcp()
    head = _seed_stuck_thread(mcp)
    record, dispatcher, _ = await _sweep_until_third_launch(mcp, head)

    with pytest.raises(StalledError) as excinfo:
        await _conductor(mcp, dispatcher, record).run()

    assert excinfo.value.code == STALLED_EXIT_CODE
    assert STALLED_EXIT_CODE not in (0, 1, 2, STAND_DOWN_EXIT_CODE)
    assert len(dispatcher.spawns) == 2  # still no spawn
    assert len(_stall_lines(caplog)) == 1  # the reason reaches the quarantine log tail


@pytest.mark.anyio
async def test_below_threshold_or_moved_head_spawns_normally() -> None:
    mcp = _FakeChatroomMcp()
    head = _seed_stuck_thread(mcp)
    dispatcher = _SilentHangDispatcher()
    rec = Record(head_msg_id_at_launch=head, launches_same_head=STALL_THRESHOLD - 1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_conductor(mcp, dispatcher, rec).run(), timeout=0.05)
    moved = Record(head_msg_id_at_launch="m0", launches_same_head=STALL_THRESHOLD)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_conductor(mcp, dispatcher, moved).run(), timeout=0.05)
    assert dispatcher.dispatches == 2
    assert mcp.posts == []


@pytest.mark.anyio
async def test_a_non_ai_handoff_is_never_reported_as_stalled() -> None:
    # A head that routes to no spawn (here a Fermi handoff) keeps its own stop; the watchdog only
    # looks at launches that would spawn a roster participant.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="critique\n\nNEXT: Fermi")
    dispatcher = _SilentHangDispatcher()
    rec = Record(head_msg_id_at_launch="m1", launches_same_head=STALL_THRESHOLD + 2)
    outcome = await _conductor(mcp, dispatcher, rec).run()
    assert outcome.stop_reason is not StopReason.STALLED
    assert all("STALLED" not in p["content"] for p in mcp.posts)


# --------------------------------------------------------------------------- #
# 4. exit codes and flag plumbing through ``mindwire-loop``
# --------------------------------------------------------------------------- #


def _patch_main(monkeypatch: pytest.MonkeyPatch, argv: list[str], run: Any) -> None:
    monkeypatch.setattr(loop_runner, "load_settings", lambda: None)
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor", *argv])
    monkeypatch.setattr(loop_runner, "run_conductor", run)


def test_flags_reach_run_conductor_and_stalled_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _run(_settings: Any, **kwargs: Any) -> ConductorOutcome:
        seen.update(kwargs)
        return ConductorOutcome(
            rounds=0, stop_reason=StopReason.STALLED, last_msg_id="m2", forced_naysayer_turns=0
        )

    _patch_main(monkeypatch, ["--launches-same-head", "3", "--launch-head-msg-id", "msg-77"], _run)
    loop_runner.main()  # returns normally → process exit 0
    assert seen["launches_same_head"] == 3
    assert seen["launch_head_msg_id"] == "msg-77"


def test_no_flags_means_no_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _run(_settings: Any, **kwargs: Any) -> None:
        seen.update(kwargs)

    _patch_main(monkeypatch, [], _run)
    loop_runner.main()
    assert seen["launches_same_head"] == 0
    assert seen["launch_head_msg_id"] is None


def test_unlanded_stalled_notice_exits_4_through_main(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run(_settings: Any, **_kwargs: Any) -> None:
        from spirrow_mindwire.conductor.stall import stalled_event

        raise StalledError(
            stalled_event(
                project="p", thread="t", head_msg_id="m1", launches_same_head=3, target="Bohr"
            )
        )

    _patch_main(monkeypatch, ["--launches-same-head", "3", "--launch-head-msg-id", "m1"], _run)
    with pytest.raises(SystemExit) as excinfo:
        loop_runner.main()
    assert excinfo.value.code == STALLED_EXIT_CODE


# --------------------------------------------------------------------------- #
# 5. repo_dir = "" is unset (PR #357 gate advisory)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_repo_dir_reads_as_unset(blank: str) -> None:
    # Path("") is Path(".") — the current directory, which passes an is_dir() check.
    assert Path(blank.strip()) == Path(".")
    assert Stage3LoopConfig.model_validate({"repo_dir": blank}).repo_dir is None
