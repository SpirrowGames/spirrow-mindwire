"""The conductor's side of a git-sync park / defer (ADR-2026-10-09-23 unit 1, design v1.2).

A park posts ``git.<code>: <thread_id> <branch> <要約>`` into the thread under
``conductor-relay``, ending ``NEXT: human`` with a ``git_sync.park`` stop marker, and stops the run
on ``StopReason.HUMAN`` against that notice. A notice that cannot land exits non-zero (stand-down,
quarantine). A deferral propagates unchanged and records no adapter-error snapshot.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_conductor_core import _ROSTER, _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire.conductor.core import (
    CONDUCTOR_RELAY_AUTHOR,
    Conductor,
    ConductorStopSlot,
    StopReason,
)
from spirrow_mindwire.conductor.stand_down import STAND_DOWN_EXIT_CODE, StandDownError
from spirrow_mindwire.conductor.stop_marker import parse_stop_marker
from spirrow_mindwire.git_sync import (
    GitParkCode,
    GitSyncDeferredError,
    GitSyncParkError,
    GitSyncPhase,
)
from spirrow_mindwire.magickit.client import ThreadResolvedError
from spirrow_mindwire.value_objects import ChatroomEvent, SessionHandle


class _GitSyncDispatcher(_ScriptedDispatcher):
    def __init__(self, mcp: _FakeChatroomMcp, exc: BaseException) -> None:
        super().__init__(mcp, {})
        self._exc = exc

    async def dispatch(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        self.dispatches.append((handle.role, event.payload.msg_id))
        raise self._exc


def _park() -> GitSyncParkError:
    return GitSyncParkError(
        GitParkCode.DIRTY_AFTER_TURN,
        thread_id="T-cond",
        branch="loop/t-cond",
        summary="1 uncommitted change(s) left as is: ?? wip.txt",
        phase=GitSyncPhase.POST_TURN,
    )


def _conductor(
    mcp: _FakeChatroomMcp, dispatcher: _ScriptedDispatcher, slot: ConductorStopSlot | None = None
) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=dispatcher,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        stop_slot=slot,
    )


@pytest.mark.anyio
async def test_park_posts_the_reason_line_and_stops_at_human() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="critique\n\nNEXT: Bohr")
    slot = ConductorStopSlot()
    dispatcher = _GitSyncDispatcher(mcp, _park())
    outcome = await _conductor(mcp, dispatcher, slot).run()

    assert len(dispatcher.dispatches) == 1
    (post,) = mcp.posts
    assert post["author"] == CONDUCTOR_RELAY_AUTHOR
    lines = post["content"].rstrip().splitlines()
    assert lines[0] == (
        "git.dirty_after_turn: T-cond loop/t-cond 1 uncommitted change(s) left as is: ?? wip.txt"
    )
    assert lines[-1] == "NEXT: human"
    marker = parse_stop_marker(post["content"])
    assert marker is not None and marker["kind"] == "git_sync.park"
    assert marker["phase"] == "post_turn"
    assert outcome.stop_reason is StopReason.HUMAN
    assert outcome.last_msg_id == "m2"  # recorded against the notice
    assert slot.snapshot is None  # a park is not an adapter error


@pytest.mark.anyio
async def test_park_notice_that_does_not_land_exits_non_zero() -> None:
    class _ResolvedOnPost(_FakeChatroomMcp):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            if name == "chatroom_post_message":
                raise ThreadResolvedError("thread is resolved")
            return await super().call_tool(name, arguments)

    mcp = _ResolvedOnPost()
    mcp.seed(author="Einstein", content="critique\n\nNEXT: Bohr")
    with pytest.raises(StandDownError) as ei:
        await _conductor(mcp, _GitSyncDispatcher(mcp, _park())).run()
    assert ei.value.code == STAND_DOWN_EXIT_CODE
    assert ei.value.event.kind == "git_sync.park"


@pytest.mark.anyio
async def test_deferral_propagates_without_a_snapshot_or_a_post() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="critique\n\nNEXT: Bohr")
    slot = ConductorStopSlot()
    exc = GitSyncDeferredError(thread_id="T-cond", candidate="loop/t-cond", detail="HTTP 502")
    with pytest.raises(GitSyncDeferredError):
        await _conductor(mcp, _GitSyncDispatcher(mcp, exc), slot).run()
    assert slot.snapshot is None
    assert mcp.posts == []
