"""Tests for ChatroomWatcher (T14, ADR-06 §7, PR-G)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from spirrow_mindwire.dispatcher.core import Dispatcher
from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
from spirrow_mindwire.magickit.watcher import ChatroomWatcher, WatchSpec
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.ulid_util import new_ulid
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    HealthStatus,
    ReplyDraft,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

_TS = datetime(2026, 5, 22, tzinfo=UTC)


def _thread_ref() -> ThreadRef:
    return ThreadRef(project_id="spirrow-mindwire", thread_id="T-x", chatroom_uri="mc://t")


def _msg(msg_id: str, *, author: str = "human", content: str = "hi") -> dict[str, Any]:
    return {
        "msg_id": msg_id,
        "author": author,
        "content": content,
        "timestamp": "2026-05-22T08:00:00Z",
    }


class _FakeMcp:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return {"messages": self.messages, "mode": "full"}


class _FakeGateway:
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
        self.posts.append({"author": author, "reply_to_msg_id": reply_to_msg_id})
        return f"posted-{len(self.posts)}"


class _RecordingReplyAdapter:
    adapter_id = "rec"
    capabilities = frozenset({Capability.READ_THREAD, Capability.POST_REPLY})

    def __init__(self) -> None:
        self.ctx: SpawnContext | None = None
        self.delivered: list[ChatroomEvent] = []
        self.halted: list[SessionHandle] = []

    async def spawn(self, thread_ref: ThreadRef, role: Role, ctx: SpawnContext) -> SessionHandle:
        self.ctx = ctx
        return SessionHandle(
            session_id=new_ulid(),
            instance_id=ctx.own_instance_id,
            adapter_id=self.adapter_id,
            thread_ref=thread_ref,
            role=role,
            started_at=_TS,
        )

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        self.delivered.append(event)
        assert self.ctx is not None
        await self.ctx.on_reply(
            ReplyDraft(body="ok", reply_to_msg_id=event.payload.msg_id, adapter_metadata={})
        )

    async def halt(self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)) -> None:
        self.halted.append(handle)

    async def health(self, handle: SessionHandle) -> HealthStatus:
        return HealthStatus(state=SessionState.IDLE, last_active_at=_TS, error=None, details={})


def _dispatcher_with(adapter: _RecordingReplyAdapter, gateway: _FakeGateway) -> Dispatcher:
    registry = InMemoryAdapterRegistry()
    registry.register(adapter)
    return Dispatcher(registry=registry, gateway=gateway)


@pytest.mark.anyio
async def test_start_spawns_session() -> None:
    adapter = _RecordingReplyAdapter()
    watcher = ChatroomWatcher(
        _FakeMcp([]),
        _dispatcher_with(adapter, _FakeGateway()),
        [WatchSpec(_thread_ref(), Role.PROPOSER)],
    )
    await watcher.start(baseline=False)
    assert adapter.ctx is not None  # session spawned


def test_watchspec_defaults_instance_id_to_phase1_mint() -> None:
    # T24 / ADR-08 §2.2: a single-instance WatchSpec needs no explicit
    # instance_id — it defaults to mint_instance_id(role) ("{role}-1").
    assert WatchSpec(_thread_ref(), Role.PROPOSER).instance_id == "proposer-1"
    assert WatchSpec(_thread_ref(), Role.NAYSAYER).instance_id == "naysayer-1"


@pytest.mark.anyio
async def test_watchspec_separates_instances_of_same_thread_role() -> None:
    # T24 / ADR-08 §2.2: two instances of the same (thread, role) are distinct
    # WatchSpec keys (instance_id is part of identity), so _handles no longer
    # collides — the v2.1 dict-key collision (add_watch no-op'ing a 2nd
    # same-(thread, role) instance) is structurally resolved.
    ref = _thread_ref()
    adapter = _RecordingReplyAdapter()
    watcher = ChatroomWatcher(
        _FakeMcp([]),
        _dispatcher_with(adapter, _FakeGateway()),
        [
            WatchSpec(ref, Role.PROPOSER, instance_id="proposer-1"),
            WatchSpec(ref, Role.PROPOSER, instance_id="proposer-2"),
        ],
    )
    await watcher.start(baseline=False)
    assert len(watcher._handles) == 2
    assert {h.instance_id for h in watcher._handles.values()} == {"proposer-1", "proposer-2"}


@pytest.mark.anyio
async def test_poll_dispatches_new_messages_in_order() -> None:
    adapter = _RecordingReplyAdapter()
    gateway = _FakeGateway()
    watcher = ChatroomWatcher(
        _FakeMcp([_msg("msg-1"), _msg("msg-2")]),
        _dispatcher_with(adapter, gateway),
        [WatchSpec(_thread_ref(), Role.PROPOSER)],
    )
    await watcher.start(baseline=False)
    assert await watcher.poll_once() == 2
    # fork 3 (msg-198): thread-namespaced stable event_id, in occurred_at order.
    assert [e.event_id for e in adapter.delivered] == ["T-x:msg-1", "T-x:msg-2"]
    assert [p["reply_to_msg_id"] for p in gateway.posts] == ["msg-1", "msg-2"]


@pytest.mark.anyio
async def test_repoll_dedups_seen_messages() -> None:
    adapter = _RecordingReplyAdapter()
    gateway = _FakeGateway()
    watcher = ChatroomWatcher(
        _FakeMcp([_msg("msg-1")]),
        _dispatcher_with(adapter, gateway),
        [WatchSpec(_thread_ref(), Role.PROPOSER)],
    )
    await watcher.start(baseline=False)
    assert await watcher.poll_once() == 1
    assert await watcher.poll_once() == 0  # already seen
    assert len(adapter.delivered) == 1
    assert len(gateway.posts) == 1


@pytest.mark.anyio
async def test_fresh_watcher_dedups_via_stable_event_id() -> None:
    # Restart-safe: a fresh watcher (empty seen-set) on the SAME dispatcher
    # re-dispatches the same stable event_id, but the dispatcher's I4 set
    # drops it — so the adapter is not delivered the message twice.
    adapter = _RecordingReplyAdapter()
    gateway = _FakeGateway()
    dispatcher = _dispatcher_with(adapter, gateway)
    messages = [_msg("msg-1")]

    w1 = ChatroomWatcher(_FakeMcp(messages), dispatcher, [WatchSpec(_thread_ref(), Role.PROPOSER)])
    await w1.start(baseline=False)
    assert await w1.poll_once() == 1
    assert len(adapter.delivered) == 1

    w2 = ChatroomWatcher(_FakeMcp(messages), dispatcher, [WatchSpec(_thread_ref(), Role.PROPOSER)])
    await w2.start(baseline=False)
    await w2.poll_once()  # dispatches the same stable event_id again...
    assert len(adapter.delivered) == 1  # ...dispatcher dedup → no second delivery
    assert len(gateway.posts) == 1


@pytest.mark.anyio
async def test_multi_watch_same_msg_id_not_cross_deduped() -> None:
    # The watcher seen-set is thread-namespaced: the same msg_id in two watched
    # threads must NOT be cross-deduped (both dispatch as distinct event_ids).
    adapter = _RecordingReplyAdapter()
    gateway = _FakeGateway()
    dispatcher = _dispatcher_with(adapter, gateway)
    tr_x = ThreadRef(project_id="p", thread_id="T-x", chatroom_uri="mc://x")
    tr_y = ThreadRef(project_id="p", thread_id="T-y", chatroom_uri="mc://y")
    watcher = ChatroomWatcher(
        _FakeMcp([_msg("msg-1")]),  # same msg_id surfaced for every watched thread
        dispatcher,
        [WatchSpec(tr_x, Role.PROPOSER), WatchSpec(tr_y, Role.PROPOSER)],
    )
    await watcher.start(baseline=False)
    assert await watcher.poll_once() == 2  # both dispatched (not cross-deduped)
    assert {e.event_id for e in adapter.delivered} == {"T-x:msg-1", "T-y:msg-1"}


@pytest.mark.anyio
async def test_baseline_skips_existing() -> None:
    # Default baseline=True marks the thread's current messages seen without
    # dispatching → the first poll dispatches nothing (no backlog reply).
    adapter = _RecordingReplyAdapter()
    gateway = _FakeGateway()
    watcher = ChatroomWatcher(
        _FakeMcp([_msg("msg-1"), _msg("msg-2")]),
        _dispatcher_with(adapter, gateway),
        [WatchSpec(_thread_ref(), Role.PROPOSER)],
    )
    await watcher.start()  # baseline=True (default)
    assert await watcher.poll_once() == 0
    assert adapter.delivered == []
    assert gateway.posts == []


@pytest.mark.anyio
async def test_baseline_then_new_message() -> None:
    # After baseline, only messages arriving afterwards are dispatched.
    adapter = _RecordingReplyAdapter()
    gateway = _FakeGateway()
    fake = _FakeMcp([_msg("msg-1")])
    watcher = ChatroomWatcher(
        fake, _dispatcher_with(adapter, gateway), [WatchSpec(_thread_ref(), Role.PROPOSER)]
    )
    await watcher.start()  # baselines msg-1
    fake.messages.append(_msg("msg-2"))  # new arrival after start
    assert await watcher.poll_once() == 1
    assert [e.event_id for e in adapter.delivered] == ["T-x:msg-2"]


@pytest.mark.anyio
async def test_stop_halts_sessions() -> None:
    adapter = _RecordingReplyAdapter()
    watcher = ChatroomWatcher(
        _FakeMcp([]),
        _dispatcher_with(adapter, _FakeGateway()),
        [WatchSpec(_thread_ref(), Role.PROPOSER)],
    )
    await watcher.start(baseline=False)
    await watcher.stop()
    assert len(adapter.halted) == 1
    assert await watcher.poll_once() == 0  # handles cleared → nothing to poll
