"""Phase 1 §8 integration tests — ADR-2026-05-21-06 (T13 PR-E / Step 3).

Exercises the assembled stack — :class:`Dispatcher` + the real
:class:`ClaudeCodeSdkAdapter` (with a fake SDK client) + a fake
:class:`ChatroomGateway` — against the ADR-06 §8 acceptance criteria,
without spinning up a real subprocess (that's the ``-m manual`` smoke
test in PR-F).
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
)
from spirrow_mindwire.dispatcher.core import Dispatcher
from spirrow_mindwire.dispatcher.event_log import (
    EVENT_FIELD_AUTHOR,
    EVENT_KIND_DELIVERY_FAILED,
    EVENT_KIND_REPLY_SENT,
)
from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
from spirrow_mindwire.value_objects import (
    ChatroomEvent,
    Event,
    EventType,
    NewMessagePayload,
    Role,
    SessionState,
    ThreadRef,
)

_TS = datetime(2026, 5, 22, tzinfo=UTC)


def _ok_responses() -> list[Any]:
    return [
        AssistantMessage(content=[TextBlock(text="reply text")], model="m"),
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


class _SdkClient:
    """Configurable fake claude_agent_sdk.ClaudeSDKClient."""

    def __init__(self, *, responses: list[Any] | None = None, fail_on: str | None = None) -> None:
        self._responses = responses or []
        self._fail_on = fail_on
        self.disconnected = False
        self.interrupt_count = 0

    async def connect(self) -> None:
        if self._fail_on == "connect":
            raise RuntimeError("connect boom")

    async def query(self, prompt: str) -> None:
        if self._fail_on == "query":
            raise RuntimeError("query boom")

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self._responses:
            yield message

    async def interrupt(self) -> None:
        self.interrupt_count += 1

    async def disconnect(self) -> None:
        self.disconnected = True


class _FakeGateway:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post_reply(
        self,
        thread_ref: ThreadRef,
        *,
        author: Role,
        body: str,
        reply_to_msg_id: str | None,
        idempotency_key: str,
    ) -> str:
        self.posts.append({"author": author, "body": body, "idempotency_key": idempotency_key})
        return f"posted-{len(self.posts)}"


def _factory(client: _SdkClient) -> Callable[[Any], Any]:
    def make(_options: Any) -> _SdkClient:
        return client

    return make


def _thread_ref() -> ThreadRef:
    return ThreadRef(project_id="spirrow-mindwire", thread_id="01JTHREAD", chatroom_uri="mc://t/1")


def _event(*, event_id: str = "01JEVENT", msg_id: str = "m1") -> ChatroomEvent:
    return ChatroomEvent(
        event_id=event_id,
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id=msg_id, author="human", body="hi", parent_msg_id=None),
    )


def _build(
    client: _SdkClient, tmp_path: Path
) -> tuple[Dispatcher, ClaudeCodeSdkAdapter, _FakeGateway, list[Event]]:
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    registry = InMemoryAdapterRegistry()
    registry.register(adapter)
    gateway = _FakeGateway()
    events: list[Event] = []

    async def sink(event: Event) -> None:
        events.append(event)

    return Dispatcher(registry=registry, gateway=gateway, event_sink=sink), adapter, gateway, events


@pytest.mark.anyio
async def test_smoke_proposer_round_trip(tmp_path: Path) -> None:
    # §8 acceptance: 1 thread / 1 role / 1 round-trip → author=proposer reply.
    disp, _adapter, gateway, events = _build(_SdkClient(responses=_ok_responses()), tmp_path)
    handle = await disp.spawn_role(_thread_ref(), Role.PROPOSER)
    await disp.dispatch(handle, _event(msg_id="m9"))

    assert len(gateway.posts) == 1
    assert gateway.posts[0]["author"] is Role.PROPOSER
    assert gateway.posts[0]["body"] == "reply text"
    assert gateway.posts[0]["idempotency_key"] == f"{handle.session_id}:1"
    assert [e.kind for e in events] == [EVENT_KIND_REPLY_SENT]
    assert events[0].fields[EVENT_FIELD_AUTHOR] == "proposer"


@pytest.mark.anyio
async def test_delivery_failure_logs_failed_event(tmp_path: Path) -> None:
    # §8: a failure leaves a FAILED entry in the Event log.
    disp, adapter, gateway, events = _build(_SdkClient(fail_on="query"), tmp_path)
    handle = await disp.spawn_role(_thread_ref(), Role.PROPOSER)

    with pytest.raises(ClaudeCodeSdkDeliveryError):
        await disp.dispatch(handle, _event())

    assert gateway.posts == []  # no reply posted on failure
    assert [e.kind for e in events] == [EVENT_KIND_DELIVERY_FAILED]
    health = await adapter.health(handle)
    assert health.state is SessionState.FAILED
    assert health.error is not None
    assert health.error.code == "adapter.delivery_failed"


@pytest.mark.anyio
async def test_halt_releases_resources_and_blocks_delivery(tmp_path: Path) -> None:
    # §8: halt releases resources (client disconnected) + the session stops.
    client = _SdkClient(responses=_ok_responses())
    disp, adapter, _gateway, _events = _build(client, tmp_path)
    handle = await disp.spawn_role(_thread_ref(), Role.PROPOSER)
    await disp.dispatch(handle, _event(event_id="e1"))

    await disp.halt(handle)
    assert client.disconnected is True  # resource release
    assert (await adapter.health(handle)).state is SessionState.HALTED

    # Delivery to a halted session is rejected (§8 session stopped).
    with pytest.raises(ClaudeCodeSdkDeliveryError):
        await disp.dispatch(handle, _event(event_id="e2"))

    # halt is idempotent (I8): a second halt does nothing extra.
    await disp.halt(handle)
    assert client.interrupt_count == 1


@pytest.mark.anyio
async def test_consecutive_messages_preserve_fifo_and_seq(tmp_path: Path) -> None:
    # §8 integration: consecutive NEW_MESSAGE keep order + monotonic reply_seq (I5/I9).
    disp, _adapter, gateway, _events = _build(_SdkClient(responses=_ok_responses()), tmp_path)
    handle = await disp.spawn_role(_thread_ref(), Role.PROPOSER)
    await disp.dispatch(handle, _event(event_id="e1", msg_id="m1"))
    await disp.dispatch(handle, _event(event_id="e2", msg_id="m2"))

    keys = [p["idempotency_key"] for p in gateway.posts]
    assert keys == [f"{handle.session_id}:1", f"{handle.session_id}:2"]
