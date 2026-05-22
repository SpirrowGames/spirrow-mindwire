"""Tests for T11 ``ClaudeCodeSdkAdapter`` (ADR-2026-05-21-06 §3.1).

The Claude Agent SDK session is replaced by a fake client injected via
``client_factory``, exercising the adapter's lifecycle / reply wiring /
exception mapping without spinning up the real CLI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from spirrow_mindwire.adapters.claude_code_sdk import (
    ClaudeCodeSdkAdapter,
    ClaudeCodeSdkDeliveryError,
    ClaudeCodeSdkHaltError,
    ClaudeCodeSdkHealthError,
    ClaudeCodeSdkSpawnError,
)
from spirrow_mindwire.ports import RoleAdapter, SpawnContext
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    Event,
    EventType,
    NewMessagePayload,
    ReplyDraft,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

_TS = datetime(2026, 5, 22, tzinfo=UTC)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="test-model")


def _result(*, is_error: bool = False, result: str | None = "ok") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=10,
        is_error=is_error,
        num_turns=1,
        session_id="test",
        stop_reason="end_turn",
        result=result,
    )


class _FakeClient:
    """Structural stand-in for claude_agent_sdk.ClaudeSDKClient."""

    def __init__(self, responses: list[Any], *, fail_on: str | None = None) -> None:
        self._responses = responses
        self._fail_on = fail_on
        self.connected = False
        self.disconnected = False
        self.interrupt_count = 0
        self.queries: list[str] = []

    async def connect(self) -> None:
        if self._fail_on == "connect":
            raise RuntimeError("connect boom")
        self.connected = True

    async def query(self, prompt: str) -> None:
        if self._fail_on == "query":
            raise RuntimeError("query boom")
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self._responses:
            yield message

    async def interrupt(self) -> None:
        if self._fail_on == "interrupt":
            raise RuntimeError("interrupt boom")
        self.interrupt_count += 1

    async def disconnect(self) -> None:
        self.disconnected = True


def _factory(client: _FakeClient) -> Callable[[Any], Any]:
    def make(_options: Any) -> _FakeClient:
        return client

    return make


def _ctx(captured: list[ReplyDraft], *, own_role: Role = Role.PROPOSER) -> SpawnContext:
    async def on_reply(draft: ReplyDraft) -> None:
        captured.append(draft)

    async def on_event_log(_event: Event) -> None:
        return None

    return SpawnContext(on_reply=on_reply, on_event_log=on_event_log, own_role=own_role)


def _thread_ref() -> ThreadRef:
    return ThreadRef(project_id="spirrow-mindwire", thread_id="01JTHREAD", chatroom_uri="mc://t/1")


def _event(
    *,
    author: str = "human",
    body: str = "hi",
    msg_id: str = "m1",
    event_type: EventType = EventType.NEW_MESSAGE,
) -> ChatroomEvent:
    return ChatroomEvent(
        event_id="01JEVENT",
        event_type=event_type,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id=msg_id, author=author, body=body, parent_msg_id=None),
    )


def _stray_handle() -> SessionHandle:
    return SessionHandle(
        session_id="01JSTRAY",
        adapter_id="claude-code-sdk",
        thread_ref=_thread_ref(),
        role=Role.PROPOSER,
        started_at=_TS,
    )


@pytest.mark.anyio
async def test_spawn_connects_and_returns_idle_handle(tmp_path: Path) -> None:
    client = _FakeClient([])
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    assert client.connected is True
    assert handle.adapter_id == "claude-code-sdk"
    assert handle.role is Role.PROPOSER
    hs = await adapter.health(handle)
    assert hs.state is SessionState.IDLE
    assert hs.error is None


@pytest.mark.anyio
async def test_spawn_failure_raises_spawn_error(tmp_path: Path) -> None:
    client = _FakeClient([], fail_on="connect")
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    with pytest.raises(ClaudeCodeSdkSpawnError):
        await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))


@pytest.mark.anyio
async def test_deliver_new_message_emits_reply(tmp_path: Path) -> None:
    client = _FakeClient([_assistant("hi "), _assistant("there"), _result()])
    captured: list[ReplyDraft] = []
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx(captured))
    await adapter.deliver_event(handle, _event(author="human", msg_id="m7"))
    assert len(captured) == 1
    assert captured[0].body == "hi there"
    assert captured[0].reply_to_msg_id == "m7"
    assert captured[0].adapter_metadata["adapter_id"] == "claude-code-sdk"
    assert len(client.queries) == 1
    hs = await adapter.health(handle)
    assert hs.state is SessionState.IDLE


@pytest.mark.anyio
async def test_own_role_self_filter_skips(tmp_path: Path) -> None:
    client = _FakeClient([_assistant("x"), _result()])
    captured: list[ReplyDraft] = []
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx(captured))
    # author == own_role ("proposer") → our own post echoed back, filtered out.
    await adapter.deliver_event(handle, _event(author="proposer"))
    assert captured == []
    assert client.queries == []


@pytest.mark.anyio
async def test_non_new_message_event_is_noop(tmp_path: Path) -> None:
    client = _FakeClient([_assistant("x"), _result()])
    captured: list[ReplyDraft] = []
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx(captured))
    await adapter.deliver_event(handle, _event(event_type=EventType.THREAD_CLOSED))
    assert captured == []
    assert client.queries == []


@pytest.mark.anyio
async def test_deliver_failure_sets_failed_and_raises(tmp_path: Path) -> None:
    client = _FakeClient([], fail_on="query")
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    with pytest.raises(ClaudeCodeSdkDeliveryError):
        await adapter.deliver_event(handle, _event())
    hs = await adapter.health(handle)
    assert hs.state is SessionState.FAILED
    assert hs.error is not None
    assert hs.error.code == "adapter.delivery_failed"
    # §3.4 / I2: the failure code is in error.code, not duplicated in details.
    assert "error_code" not in hs.details


@pytest.mark.anyio
async def test_deliver_on_terminal_session_raises(tmp_path: Path) -> None:
    client = _FakeClient([])
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    await adapter.halt(handle)
    with pytest.raises(ClaudeCodeSdkDeliveryError):
        await adapter.deliver_event(handle, _event())


@pytest.mark.anyio
async def test_halt_disconnects_and_is_idempotent(tmp_path: Path) -> None:
    client = _FakeClient([])
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    await adapter.halt(handle)
    assert client.disconnected is True
    assert client.interrupt_count == 1
    assert (await adapter.health(handle)).state is SessionState.HALTED
    # Idempotent no-op (I8): second halt does nothing, no extra interrupt, no raise.
    await adapter.halt(handle)
    assert client.interrupt_count == 1
    assert (await adapter.health(handle)).state is SessionState.HALTED


@pytest.mark.anyio
async def test_halt_failure_sets_failed_and_raises(tmp_path: Path) -> None:
    client = _FakeClient([], fail_on="interrupt")
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    with pytest.raises(ClaudeCodeSdkHaltError):
        await adapter.halt(handle)
    hs = await adapter.health(handle)
    assert hs.state is SessionState.FAILED
    assert hs.error is not None
    assert hs.error.code == "adapter.halt_failed"


@pytest.mark.anyio
async def test_halt_unknown_handle_is_noop(tmp_path: Path) -> None:
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(_FakeClient([])))
    await adapter.halt(_stray_handle())  # no raise


@pytest.mark.anyio
async def test_health_unknown_handle_raises(tmp_path: Path) -> None:
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(_FakeClient([])))
    with pytest.raises(ClaudeCodeSdkHealthError):
        await adapter.health(_stray_handle())


def test_capabilities_exclude_naysayer_qualified() -> None:
    caps = ClaudeCodeSdkAdapter.capabilities
    # claude-code is the same model family as main → must not be naysayer-qualified.
    assert Capability.NAYSAYER_QUALIFIED not in caps
    assert Capability.EXECUTE_CODE in caps
    assert Capability.POST_REPLY in caps


def test_satisfies_roleadapter_protocol(tmp_path: Path) -> None:
    # Static structural conformance (mypy) is the real assertion.
    adapter: RoleAdapter = ClaudeCodeSdkAdapter(cwd=tmp_path)
    assert adapter.adapter_id == "claude-code-sdk"
