"""The conductor must attach thread context to the event it dispatches (D-3 wiring).

Why the *event* and not ``spawn_instance``: the conductor spawns one session per
identity and reuses it for every round (``sessions`` in :meth:`Conductor.run`),
while the thread grows on every round. Context attached at spawn would be round
one's thread forever — the staleness this thread exists to remove, reintroduced
one layer down. The event is rebuilt per round, so that is where it belongs.
"""

from __future__ import annotations

from typing import Any

import pytest

from spirrow_mindwire.conductor.core import Conductor
from spirrow_mindwire.value_objects import ChatroomEvent, Role, SessionHandle, ThreadRef

_TR = ThreadRef(project_id="p", thread_id="T-x", chatroom_uri="mc://t")
_ROSTER = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER, "Heisenberg": Role.IMPLEMENTER}


class _Mcp:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_get_thread":
            return {"messages": self.messages}
        return {"msg": {"msg_id": "msg-relay"}}


class _Dispatcher:
    def __init__(self) -> None:
        self.events: list[ChatroomEvent] = []

    async def spawn_instance(
        self, thread_ref: ThreadRef, role: Role, instance_id: str
    ) -> SessionHandle:
        from datetime import UTC, datetime

        return SessionHandle(
            session_id="s",
            instance_id=instance_id,
            adapter_id="a",
            thread_ref=thread_ref,
            role=role,
            started_at=datetime.now(UTC),
        )

    async def dispatch(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        self.events.append(event)


@pytest.mark.anyio
async def test_dispatched_event_carries_the_thread_not_just_the_trigger() -> None:
    messages = [
        {"msg_id": "msg-1", "author": "Bohr", "content": "the design", "timestamp": ""},
        {"msg_id": "msg-2", "author": "Einstein", "content": "an objection", "timestamp": ""},
        {"msg_id": "msg-3", "author": "Bohr", "content": "go\n\nNEXT: Einstein", "timestamp": ""},
    ]
    dispatcher = _Dispatcher()
    c = Conductor(
        mcp=_Mcp(messages),
        dispatcher=dispatcher,
        thread_ref=_TR,
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=1,
    )
    await c.run()
    assert dispatcher.events, "the conductor did not dispatch"
    ctx = dispatcher.events[0].thread_context
    assert ctx is not None, "the dispatched turn got the trigger message and nothing else"
    assert ctx.opener is not None and ctx.opener.msg_id == "msg-1"
    assert [m.msg_id for m in ctx.recent] == ["msg-2"]
    assert ctx.total_count == 3


@pytest.mark.anyio
async def test_context_is_rebuilt_each_round_not_frozen_at_spawn() -> None:
    """A session is reused across rounds; a spawn-time snapshot would go stale."""
    messages = [
        {"msg_id": "msg-1", "author": "Bohr", "content": "opener", "timestamp": ""},
        {"msg_id": "msg-2", "author": "Bohr", "content": "one\n\nNEXT: Einstein", "timestamp": ""},
    ]
    mcp = _Mcp(messages)
    dispatcher = _Dispatcher()

    real_dispatch = dispatcher.dispatch

    async def _dispatch(handle: SessionHandle, event: ChatroomEvent) -> None:
        await real_dispatch(handle, event)
        # simulate the dispatched role posting its reply
        mcp.messages = [
            *mcp.messages,
            {
                "msg_id": f"msg-{len(mcp.messages) + 1}",
                "author": "Einstein",
                "content": "review\n\nNEXT: Einstein",
                "timestamp": "",
            },
        ]

    dispatcher.dispatch = _dispatch  # type: ignore[method-assign]
    c = Conductor(
        mcp=mcp,
        dispatcher=dispatcher,
        thread_ref=_TR,
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=3,
    )
    await c.run()
    assert len(dispatcher.events) >= 2
    first = dispatcher.events[0].thread_context
    second = dispatcher.events[1].thread_context
    assert first is not None and second is not None
    assert second.total_count > first.total_count
