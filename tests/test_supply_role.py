"""Role supply — T-dispatched-turn-gets-one-message D-1.

`role` was *receivable but never supplied* through the harness. The chatroom
validates it against the author identity's ``allowed_roles`` before writing
anything, so a recorded non-null role means "this identity was verified able to
claim this role" — the I-6 invariant the loop's gates are named after.

Measured on the live corpus 2026-08-16: 38/38 harness-attested (i.e. definitely
harness-authored) naysayer posts recorded ``role: null``, and
``naysayer-pr-review`` was null 346 times out of 346. The gate role was unarmed
in exactly the posts no human wrote.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from spirrow_mindwire.dispatcher.core import Dispatcher
from spirrow_mindwire.magickit.gateway import MagickitChatroomGateway
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    EventType,
    HealthStatus,
    NewMessagePayload,
    ReplyDraft,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

_TR = ThreadRef(project_id="p", thread_id="T-x", chatroom_uri="mc://t")


class _FakeCaller:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return self.result


# --------------------------------------------------------------------------- #
# D-1 role
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_gateway_sends_the_role_it_was_given() -> None:
    caller = _FakeCaller({"msg": {"msg_id": "msg-1"}})
    gw = MagickitChatroomGateway(caller)
    await gw.post_reply(
        _TR,
        author="Einstein",
        body="b",
        reply_to_msg_id=None,
        idempotency_key="s:1",
        role=Role.NAYSAYER,
    )
    assert caller.calls[0][1]["role"] == "naysayer"


@pytest.mark.anyio
async def test_gateway_omits_role_rather_than_sending_an_empty_one() -> None:
    """An empty string is a *claim* the server would try to validate; absence is not."""
    caller = _FakeCaller({"msg": {"msg_id": "msg-1"}})
    gw = MagickitChatroomGateway(caller)
    await gw.post_reply(
        _TR, author="x", body="b", reply_to_msg_id=None, idempotency_key="s:1", role=None
    )
    assert "role" not in caller.calls[0][1]


class _RecordingGateway:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post_reply(
        self,
        thread_ref: ThreadRef,
        *,
        author: str,
        body: str,
        reply_to_msg_id: str | None,
        idempotency_key: str,
        role: Role | None = None,
    ) -> str:
        self.posts.append({"author": author, "body": body, "role": role})
        return f"msg-{len(self.posts)}"


class _EchoAdapter:
    adapter_id = "echo"
    capabilities = frozenset(
        {Capability.READ_THREAD, Capability.POST_REPLY, Capability.NAYSAYER_QUALIFIED}
    )

    def __init__(self) -> None:
        self.ctx: SpawnContext | None = None

    async def spawn(self, thread_ref: ThreadRef, role: Role, ctx: SpawnContext) -> SessionHandle:
        self.ctx = ctx
        return SessionHandle(
            session_id="s1",
            instance_id=ctx.own_instance_id,
            adapter_id=self.adapter_id,
            thread_ref=thread_ref,
            role=role,
            started_at=datetime.now(UTC),
        )

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        assert self.ctx is not None
        await self.ctx.on_reply(ReplyDraft(body="reply", reply_to_msg_id=None, adapter_metadata={}))

    async def halt(self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)) -> None:
        return None

    async def health(self, handle: SessionHandle) -> HealthStatus:
        return HealthStatus(
            state=SessionState.IDLE, last_active_at=datetime.now(UTC), error=None, details={}
        )


@pytest.mark.anyio
async def test_dispatcher_supplies_the_sessions_role_on_every_post() -> None:
    """D-1: the receiving end was always live; the caller simply never passed a value."""
    from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry

    adapter = _EchoAdapter()
    registry = InMemoryAdapterRegistry()
    registry.register(adapter)
    gateway = _RecordingGateway()
    d = Dispatcher(registry=registry, gateway=gateway)
    handle = await d.spawn_instance(_TR, Role.NAYSAYER, "Einstein")
    await d.dispatch(
        handle,
        ChatroomEvent(
            event_id="e1",
            event_type=EventType.NEW_MESSAGE,
            thread_ref=_TR,
            occurred_at=datetime.now(UTC),
            payload=NewMessagePayload(msg_id="msg-1", author="Bohr", body="b", parent_msg_id=None),
        ),
    )
    assert gateway.posts[0]["author"] == "Einstein"
    assert gateway.posts[0]["role"] is Role.NAYSAYER
