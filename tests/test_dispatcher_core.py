"""Tests for the Dispatcher core (ADR-06 §4, T13 PR-D Step 2)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from spirrow_mindwire.adapters.claude_code_sdk import ClaudeCodeSdkAdapter
from spirrow_mindwire.dispatcher.core import (
    Dispatcher,
    NoQualifiedAdapterError,
    UnknownSessionError,
)
from spirrow_mindwire.dispatcher.event_log import (
    EVENT_FIELD_AUTHOR,
    EVENT_FIELD_MODEL_ID,
    EVENT_KIND_REPLY_SENT,
    reply_sent_event,
)
from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.ulid_util import new_ulid
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    Event,
    EventType,
    HealthStatus,
    NewMessagePayload,
    ReplyDraft,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

_TS = datetime(2026, 5, 22, tzinfo=UTC)


def _thread_ref() -> ThreadRef:
    return ThreadRef(project_id="spirrow-mindwire", thread_id="01JTHREAD", chatroom_uri="mc://t/1")


def _event(
    *, event_id: str = "01JEVENT", author: str = "human", msg_id: str = "m1"
) -> ChatroomEvent:
    return ChatroomEvent(
        event_id=event_id,
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id=msg_id, author=author, body="hi", parent_msg_id=None),
    )


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
    ) -> str:
        self.posts.append(
            {
                "author": author,
                "body": body,
                "reply_to_msg_id": reply_to_msg_id,
                "idempotency_key": idempotency_key,
            }
        )
        return f"posted-{len(self.posts)}"


class _ReplyingAdapter:
    """Fake adapter that emits one reply via ctx.on_reply on each delivery."""

    adapter_id = "fake-replier"
    capabilities = frozenset({Capability.READ_THREAD, Capability.POST_REPLY})

    def __init__(self, *, reply_body: str = "ok") -> None:
        self._reply_body = reply_body
        self._ctx: SpawnContext | None = None
        self.deliver_calls = 0
        self.max_concurrent = 0
        self._active = 0

    async def spawn(self, thread_ref: ThreadRef, role: Role, ctx: SpawnContext) -> SessionHandle:
        self._ctx = ctx
        return SessionHandle(
            session_id=new_ulid(),
            instance_id=ctx.own_instance_id,
            adapter_id=self.adapter_id,
            thread_ref=thread_ref,
            role=role,
            started_at=_TS,
        )

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        self._active += 1
        self.max_concurrent = max(self.max_concurrent, self._active)
        self.deliver_calls += 1
        try:
            await asyncio.sleep(0)  # yield so any interleaving would surface
            assert self._ctx is not None
            await self._ctx.on_reply(
                ReplyDraft(
                    body=self._reply_body,
                    reply_to_msg_id=event.payload.msg_id,
                    adapter_metadata={"model_id": "fake-model"},
                )
            )
        finally:
            self._active -= 1

    async def halt(self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)) -> None:
        return None

    async def health(self, handle: SessionHandle) -> HealthStatus:
        return HealthStatus(state=SessionState.IDLE, last_active_at=_TS, error=None, details={})


def _registry_with(adapter: Any) -> InMemoryAdapterRegistry:
    reg = InMemoryAdapterRegistry()
    reg.register(adapter)
    return reg


@pytest.mark.anyio
async def test_spawn_and_dispatch_posts_reply() -> None:
    adapter = _ReplyingAdapter(reply_body="hello")
    gateway = _FakeGateway()
    disp = Dispatcher(registry=_registry_with(adapter), gateway=gateway)
    handle = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    await disp.dispatch(handle, _event(msg_id="m9"))
    assert len(gateway.posts) == 1
    post = gateway.posts[0]
    assert post["author"] == "proposer-1"  # I3 v2.2 (ADR-06 amendment): author = instance_id
    assert post["body"] == "hello"
    assert post["reply_to_msg_id"] == "m9"
    assert post["idempotency_key"] == f"{handle.session_id}:1"  # I5


@pytest.mark.anyio
async def test_spawn_instance_sets_instance_id_on_handle() -> None:
    # T24 / ADR-08 §2.2: the instance_id handed to spawn_instance lands on the
    # returned SessionHandle (delivered to the adapter via
    # SpawnContext.own_instance_id, since the handle is identity-keyed / I2 and
    # cannot be re-stamped after the adapter returns it).
    adapter = _ReplyingAdapter()
    disp = Dispatcher(registry=_registry_with(adapter), gateway=_FakeGateway())
    handle = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    assert handle.instance_id == "proposer-1"
    assert handle.role is Role.PROPOSER


@pytest.mark.anyio
async def test_spawn_instance_rejects_blank_instance_id() -> None:
    # SHOULD-3 (PR #69 naysayer): instance_id becomes the chatroom reply author
    # (I3 v2.2), so a blank label is rejected at the dispatcher Port boundary.
    disp = Dispatcher(registry=_registry_with(_ReplyingAdapter()), gateway=_FakeGateway())
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="non-empty instance_id"):
            await disp.spawn_instance(_thread_ref(), Role.PROPOSER, bad)


@pytest.mark.anyio
async def test_idempotency_key_increments_per_session() -> None:
    adapter = _ReplyingAdapter()
    gateway = _FakeGateway()
    disp = Dispatcher(registry=_registry_with(adapter), gateway=gateway)
    handle = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    await disp.dispatch(handle, _event(event_id="e1"))
    await disp.dispatch(handle, _event(event_id="e2"))
    keys = [p["idempotency_key"] for p in gateway.posts]
    assert keys == [f"{handle.session_id}:1", f"{handle.session_id}:2"]


@pytest.mark.anyio
async def test_dedup_drops_duplicate_event_id() -> None:
    adapter = _ReplyingAdapter()
    gateway = _FakeGateway()
    disp = Dispatcher(registry=_registry_with(adapter), gateway=gateway)
    handle = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    await disp.dispatch(handle, _event(event_id="dup"))
    await disp.dispatch(handle, _event(event_id="dup"))  # I4: skipped
    assert adapter.deliver_calls == 1
    assert len(gateway.posts) == 1


@pytest.mark.anyio
async def test_per_session_fifo_serializes_delivery() -> None:
    adapter = _ReplyingAdapter()
    gateway = _FakeGateway()
    disp = Dispatcher(registry=_registry_with(adapter), gateway=gateway)
    handle = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    await asyncio.gather(
        disp.dispatch(handle, _event(event_id="e1")),
        disp.dispatch(handle, _event(event_id="e2")),
    )
    # I9: per-session lock prevents deliver_event from interleaving.
    assert adapter.max_concurrent == 1
    assert adapter.deliver_calls == 2


@pytest.mark.anyio
async def test_event_sink_failure_is_isolated_i7() -> None:
    adapter = _ReplyingAdapter()
    gateway = _FakeGateway()

    async def bad_sink(_event: Event) -> None:
        raise RuntimeError("sink boom")

    disp = Dispatcher(registry=_registry_with(adapter), gateway=gateway, event_sink=bad_sink)
    handle = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    # I7: a raising event-log sink must NOT break the main flow.
    await disp.dispatch(handle, _event())
    assert len(gateway.posts) == 1  # reply still posted despite the sink failure


@pytest.mark.anyio
async def test_reply_sent_event_uses_anchor6_keys() -> None:
    adapter = _ReplyingAdapter()
    gateway = _FakeGateway()
    events: list[Event] = []

    async def sink(event: Event) -> None:
        events.append(event)

    disp = Dispatcher(registry=_registry_with(adapter), gateway=gateway, event_sink=sink)
    handle = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    await disp.dispatch(handle, _event())
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == EVENT_KIND_REPLY_SENT
    # anchor #6: unified key names; author = instance_id (I3 v2.2 / T26).
    assert ev.fields[EVENT_FIELD_AUTHOR] == "proposer-1"
    assert ev.fields[EVENT_FIELD_MODEL_ID] == "fake-model"


@pytest.mark.anyio
async def test_spawn_instance_no_qualified_adapter_raises() -> None:
    # _ReplyingAdapter lacks NAYSAYER_QUALIFIED → no naysayer candidate.
    disp = Dispatcher(registry=_registry_with(_ReplyingAdapter()), gateway=_FakeGateway())
    with pytest.raises(NoQualifiedAdapterError):
        await disp.spawn_instance(_thread_ref(), Role.NAYSAYER, "naysayer-1")


@pytest.mark.anyio
async def test_dispatch_unknown_handle_raises() -> None:
    disp = Dispatcher(registry=_registry_with(_ReplyingAdapter()), gateway=_FakeGateway())
    stray = SessionHandle(
        session_id="01JSTRAY",
        instance_id="proposer-1",
        adapter_id="x",
        thread_ref=_thread_ref(),
        role=Role.PROPOSER,
        started_at=_TS,
    )
    with pytest.raises(UnknownSessionError):
        await disp.dispatch(stray, _event())


class _HaltTrackingAdapter(_ReplyingAdapter):
    """Replying adapter that records the handles ``halt`` was called with."""

    def __init__(self) -> None:
        super().__init__()
        self.halted: list[str] = []

    async def halt(self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)) -> None:
        self.halted.append(handle.instance_id)


@pytest.mark.anyio
async def test_aclose_halts_all_spawned_sessions() -> None:
    # The conductor spawns sessions directly through the dispatcher (no watcher records the
    # handles), so dispatcher.aclose() is what tears them down on daemon shutdown — symmetric with
    # ChatroomWatcher.stop(). It must halt every spawned session and be idempotent.
    adapter = _HaltTrackingAdapter()
    disp = Dispatcher(registry=_registry_with(adapter), gateway=_FakeGateway())
    h1 = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    h2 = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-2")
    await disp.aclose()
    assert sorted(adapter.halted) == ["proposer-1", "proposer-2"]
    # Sessions were cleared: a dispatch to a now-closed handle is an unknown session.
    with pytest.raises(UnknownSessionError):
        await disp.dispatch(h1, _event())
    # Idempotent: a second aclose halts nothing more and does not raise.
    await disp.aclose()
    assert sorted(adapter.halted) == ["proposer-1", "proposer-2"]
    assert h2.instance_id == "proposer-2"  # (handle retained for clarity; nothing left to halt)


@pytest.mark.anyio
async def test_aclose_continues_past_a_failing_halt() -> None:
    # A failing halt is logged and the rest still run (best-effort, like watcher.stop()).
    class _BadHalt(_HaltTrackingAdapter):
        async def halt(
            self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)
        ) -> None:
            self.halted.append(handle.instance_id)
            raise RuntimeError("halt boom")

    adapter = _BadHalt()
    disp = Dispatcher(registry=_registry_with(adapter), gateway=_FakeGateway())
    await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-2")
    await disp.aclose()  # must not raise despite both halts failing
    assert sorted(adapter.halted) == ["proposer-1", "proposer-2"]


# ----- live T11 ↔ T13 cross-check (real ClaudeCodeSdkAdapter) -----------------


class _FakeSdkClient:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses

    async def connect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self._responses:
            yield message

    async def interrupt(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


@pytest.mark.anyio
async def test_live_claude_code_adapter_with_dispatcher(tmp_path: Path) -> None:
    def factory(_options: Any) -> Any:
        return _FakeSdkClient(
            [
                AssistantMessage(content=[TextBlock(text="real reply")], model="m"),
                ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id="s",
                    stop_reason="end_turn",
                    result="ok",
                ),
            ]
        )

    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=factory)
    registry = InMemoryAdapterRegistry()
    registry.register(adapter)
    gateway = _FakeGateway()
    disp = Dispatcher(registry=registry, gateway=gateway)

    # ADR-05 §5 architecture-level independence: claude-code (same model family
    # as main, no NAYSAYER_QUALIFIED) is excluded from the naysayer slot.
    with pytest.raises(NoQualifiedAdapterError):
        await disp.spawn_instance(_thread_ref(), Role.NAYSAYER, "naysayer-1")

    # …but it serves the proposer slot end-to-end (real adapter + dispatcher).
    handle = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    await disp.dispatch(handle, _event(msg_id="m1"))
    assert len(gateway.posts) == 1
    assert gateway.posts[0]["author"] == "proposer-1"  # I3 v2.2: author = instance_id
    # The dispatcher appends the harness-derived source marker to the body
    # for every SDK-adapter reply (msg-805 D3 / msg-834 §2). The original
    # agent text is preserved verbatim; the marker is the trailing line.
    posted_body = gateway.posts[0]["body"]
    assert posted_body.startswith("real reply")
    assert posted_body.rstrip().splitlines()[-1].startswith("<!-- source:")


# ----- source marker wiring (msg-805 D3 / msg-834 §2) ------------------------ #


class _MarkerOptionsAdapter(_ReplyingAdapter):
    """Replying adapter that also exposes ``source_marker_options`` (the getter
    the dispatcher looks up via ``getattr`` to derive the harness-side marker).

    Mirrors what the real SDK adapters do — store the options for the session
    and return them through a public getter — without pulling in the SDK. This
    is the direct wiring test for msg-834 §2 (b): the dispatcher, not the
    adapter, produces the marker text.
    """

    def __init__(self, *, options: Any, reply_body: str = "hello") -> None:
        super().__init__(reply_body=reply_body)
        self._options = options

    def source_marker_options(self, handle: SessionHandle) -> Any:
        _ = handle  # single-session fake; port receives the handle for parity
        return self._options


@pytest.mark.anyio
async def test_dispatcher_appends_source_marker_when_adapter_exposes_options() -> None:
    """The dispatcher derives the marker from ``adapter.source_marker_options(handle)``.

    Msg-805 D3 / msg-834 §2 (b): the harness stamps the marker on posting;
    the adapter's reply body itself is never modified in place, and the
    marker's text is a function of the options object only. Here the fake
    adapter yields a ``SimpleNamespace`` with the three SDK-shape attrs and
    the dispatcher stamps the derived marker as the final line of the
    posted body — even though the ``ReplyDraft.body`` the adapter emitted
    contained no marker string.
    """
    from types import SimpleNamespace

    options = SimpleNamespace(tools=[], mcp_servers={}, setting_sources=[])
    adapter = _MarkerOptionsAdapter(options=options, reply_body="agent said this")
    gateway = _FakeGateway()
    disp = Dispatcher(registry=_registry_with(adapter), gateway=gateway)
    handle = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    await disp.dispatch(handle, _event(msg_id="m9"))

    body = gateway.posts[0]["body"]
    # The adapter's body is preserved verbatim (unedited by the harness) …
    assert body.startswith("agent said this")
    # … with the harness-derived marker appended as the final non-empty line.
    tail = body.rstrip().splitlines()[-1]
    assert tail == "<!-- source: tools=0 · mcp=0 · setting_sources=empty -->"


@pytest.mark.anyio
async def test_dispatcher_skips_marker_when_adapter_lacks_options_getter() -> None:
    """An adapter without ``source_marker_options`` posts an unadorned body.

    ``_ReplyingAdapter`` has no options getter, so the dispatcher does not
    append a marker. This is the compatibility guarantee for test fakes and
    for any adapter that is not SDK-backed — the marker is opt-in via the
    getter, not forced by the dispatcher.
    """
    adapter = _ReplyingAdapter(reply_body="hello")
    gateway = _FakeGateway()
    disp = Dispatcher(registry=_registry_with(adapter), gateway=gateway)
    handle = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    await disp.dispatch(handle, _event(msg_id="m1"))
    assert gateway.posts[0]["body"] == "hello"
    assert "<!-- source:" not in gateway.posts[0]["body"]


def test_reply_sent_event_normalizes_missing_model_id() -> None:
    handle = SessionHandle(
        session_id="s",
        instance_id="proposer-1",
        adapter_id="a",
        thread_ref=_thread_ref(),
        role=Role.PROPOSER,
        started_at=_TS,
    )
    # model_id present-but-None must render as "" (not the string "None").
    ev_none = reply_sent_event(
        handle,
        ReplyDraft(body="x", reply_to_msg_id=None, adapter_metadata={"model_id": None}),
        posted_msg_id="p",
        idempotency_key="s:1",
    )
    assert ev_none.fields[EVENT_FIELD_MODEL_ID] == ""
    # missing key also normalizes to "".
    ev_missing = reply_sent_event(
        handle,
        ReplyDraft(body="x", reply_to_msg_id=None, adapter_metadata={}),
        posted_msg_id="p",
        idempotency_key="s:1",
    )
    assert ev_missing.fields[EVENT_FIELD_MODEL_ID] == ""
