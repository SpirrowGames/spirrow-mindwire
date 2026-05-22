"""Tests for the Stage 2 ``NaysayerLexoraAdapter`` (ADR-06 §3.1, ADR-05 §5).

The Lexora gateway is replaced by a fake :class:`_FakeLexora` injected via
the ``client`` parameter (the ``client_factory`` pattern used by
``test_claude_code_sdk_adapter.py``), exercising the adapter's lifecycle /
reply wiring / exception mapping / fail-loud behaviour without touching the
network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from spirrow_mindwire.adapters.naysayer_lexora import (
    NaysayerLexoraAdapter,
    NaysayerLexoraDeliveryError,
    NaysayerLexoraHealthError,
    NaysayerLexoraSpawnError,
)
from spirrow_mindwire.lexora.client import ChatCompletion, ChatMessage
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


class _FakeLexora:
    """Structural stand-in for spirrow_mindwire.lexora.client.LexoraClient."""

    def __init__(
        self,
        *,
        completion: ChatCompletion | None = None,
        chat_error: Exception | None = None,
        health_error: Exception | None = None,
        health_body: dict[str, Any] | None = None,
    ) -> None:
        self._completion = completion or ChatCompletion(
            content="You wrote: 'X'. That assumes Y, which is unproven.",
            reasoning_content="Let me think about whether X holds...",
            finish_reason="stop",
            model="DeepSeek-V4-Flash.gguf",
            usage={"completion_tokens": 20},
        )
        self._chat_error = chat_error
        self._health_error = health_error
        self._health_body = health_body or {"status": "ok", "backends": {"naysayer": "healthy"}}
        self.calls: list[dict[str, Any]] = []
        self.health_calls = 0
        self.closed = False

    async def chat_completion(
        self, *, model: str, messages: list[ChatMessage], max_tokens: int
    ) -> ChatCompletion:
        self.calls.append({"model": model, "messages": messages, "max_tokens": max_tokens})
        if self._chat_error is not None:
            raise self._chat_error
        return self._completion

    async def health(self) -> dict[str, Any]:
        self.health_calls += 1
        if self._health_error is not None:
            raise self._health_error
        return self._health_body

    async def aclose(self) -> None:
        self.closed = True


def _ctx(captured: list[ReplyDraft], *, own_role: Role = Role.NAYSAYER) -> SpawnContext:
    async def on_reply(draft: ReplyDraft) -> None:
        captured.append(draft)

    async def on_event_log(_event: Event) -> None:
        return None

    return SpawnContext(on_reply=on_reply, on_event_log=on_event_log, own_role=own_role)


def _thread_ref() -> ThreadRef:
    return ThreadRef(project_id="spirrow-mindwire", thread_id="01JTHREAD", chatroom_uri="mc://t/1")


def _event(
    *,
    author: str = "proposer",
    body: str = "We should store passwords in plaintext for speed.",
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
        adapter_id="naysayer-lexora",
        thread_ref=_thread_ref(),
        role=Role.NAYSAYER,
        started_at=_TS,
    )


# ---------- capabilities / protocol -------------------------------------


def test_capabilities_include_naysayer_qualified_exclude_execute_code() -> None:
    caps = NaysayerLexoraAdapter.capabilities
    # Independent model → may fill the naysayer slot (ADR-05 §5).
    assert Capability.NAYSAYER_QUALIFIED in caps
    assert Capability.READ_THREAD in caps
    assert Capability.POST_REPLY in caps
    # Stage 2 is advice-only / side-effect-free (fork-2 staged release).
    assert Capability.EXECUTE_CODE not in caps


def test_satisfies_roleadapter_protocol() -> None:
    # Static structural conformance (mypy) is the real assertion.
    adapter: RoleAdapter = NaysayerLexoraAdapter(client=_FakeLexora())
    assert adapter.adapter_id == "naysayer-lexora"


# ---------- spawn --------------------------------------------------------


@pytest.mark.anyio
async def test_spawn_returns_idle_handle() -> None:
    adapter = NaysayerLexoraAdapter(client=_FakeLexora())
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    assert handle.adapter_id == "naysayer-lexora"
    assert handle.role is Role.NAYSAYER
    hs = await adapter.health(handle)
    assert hs.state is SessionState.IDLE
    assert hs.error is None


@pytest.mark.anyio
async def test_spawn_health_check_failure_raises_spawn_error() -> None:
    fake = _FakeLexora(health_error=RuntimeError("gateway down"))
    adapter = NaysayerLexoraAdapter(client=fake, health_check_on_spawn=True)
    with pytest.raises(NaysayerLexoraSpawnError):
        await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))


@pytest.mark.anyio
async def test_spawn_without_health_check_does_not_ping() -> None:
    fake = _FakeLexora()
    adapter = NaysayerLexoraAdapter(client=fake)  # health_check_on_spawn defaults False
    await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    assert fake.health_calls == 0


# ---------- deliver_event ------------------------------------------------


@pytest.mark.anyio
async def test_deliver_emits_reply_from_content() -> None:
    fake = _FakeLexora()
    captured: list[ReplyDraft] = []
    adapter = NaysayerLexoraAdapter(client=fake, max_tokens=4096)
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx(captured))
    await adapter.deliver_event(handle, _event(author="proposer", msg_id="m7"))
    assert len(captured) == 1
    # reply is content, not reasoning_content
    assert captured[0].body == "You wrote: 'X'. That assumes Y, which is unproven."
    assert captured[0].reply_to_msg_id == "m7"
    assert captured[0].adapter_metadata["adapter_id"] == "naysayer-lexora"
    assert captured[0].adapter_metadata["finish_reason"] == "stop"
    # the tier name + max_tokens were passed through
    assert fake.calls[0]["model"] == "naysayer"
    assert fake.calls[0]["max_tokens"] == 4096
    hs = await adapter.health(handle)
    assert hs.state is SessionState.IDLE


@pytest.mark.anyio
async def test_deliver_does_not_post_reasoning_content() -> None:
    fake = _FakeLexora(
        completion=ChatCompletion(
            content="real critique",
            reasoning_content="SECRET DELIBERATION should never be posted",
            finish_reason="stop",
            model="m",
        )
    )
    captured: list[ReplyDraft] = []
    adapter = NaysayerLexoraAdapter(client=fake)
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx(captured))
    await adapter.deliver_event(handle, _event())
    assert captured[0].body == "real critique"
    assert "SECRET DELIBERATION" not in captured[0].body


@pytest.mark.anyio
async def test_system_prompt_demands_quoting() -> None:
    fake = _FakeLexora()
    adapter = NaysayerLexoraAdapter(client=fake)
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    await adapter.deliver_event(handle, _event())
    system_msg = fake.calls[0]["messages"][0]
    assert system_msg.role == "system"
    # main order #2: disagree by default + cite the specific passage.
    assert "quote" in system_msg.content.lower()
    assert "do not agree by default" in system_msg.content.lower()


@pytest.mark.anyio
async def test_empty_content_is_fail_loud_delivery_error() -> None:
    # finish_reason="length" with the whole budget on reasoning → no answer.
    fake = _FakeLexora(
        completion=ChatCompletion(
            content="",
            reasoning_content="ran out of room while thinking",
            finish_reason="length",
            model="m",
        )
    )
    captured: list[ReplyDraft] = []
    adapter = NaysayerLexoraAdapter(client=fake)
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx(captured))
    with pytest.raises(NaysayerLexoraDeliveryError):
        await adapter.deliver_event(handle, _event())
    assert captured == []  # no empty post
    hs = await adapter.health(handle)
    assert hs.state is SessionState.FAILED
    assert hs.error is not None
    assert hs.error.code == "adapter.delivery_failed"


@pytest.mark.anyio
async def test_whitespace_only_content_is_fail_loud() -> None:
    fake = _FakeLexora(
        completion=ChatCompletion(
            content="   \n  ", reasoning_content=None, finish_reason="stop", model="m"
        )
    )
    adapter = NaysayerLexoraAdapter(client=fake)
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    with pytest.raises(NaysayerLexoraDeliveryError):
        await adapter.deliver_event(handle, _event())


@pytest.mark.anyio
async def test_chat_failure_sets_failed_and_raises() -> None:
    # A gateway error (e.g. unknown tier 502) must surface, not silently fall back.
    fake = _FakeLexora(chat_error=RuntimeError("502 unknown tier"))
    adapter = NaysayerLexoraAdapter(client=fake)
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    with pytest.raises(NaysayerLexoraDeliveryError):
        await adapter.deliver_event(handle, _event())
    hs = await adapter.health(handle)
    assert hs.state is SessionState.FAILED
    assert hs.error is not None
    assert hs.error.code == "adapter.delivery_failed"
    # §3.4 / I2: failure code lives in error.code, not duplicated in details.
    assert "error_code" not in hs.details


@pytest.mark.anyio
async def test_own_role_self_filter_skips() -> None:
    fake = _FakeLexora()
    captured: list[ReplyDraft] = []
    adapter = NaysayerLexoraAdapter(client=fake)
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx(captured))
    # author == own_role ("naysayer") → our own post echoed back, filtered out.
    await adapter.deliver_event(handle, _event(author="naysayer"))
    assert captured == []
    assert fake.calls == []


@pytest.mark.anyio
async def test_non_new_message_event_is_noop() -> None:
    fake = _FakeLexora()
    captured: list[ReplyDraft] = []
    adapter = NaysayerLexoraAdapter(client=fake)
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx(captured))
    await adapter.deliver_event(handle, _event(event_type=EventType.THREAD_CLOSED))
    assert captured == []
    assert fake.calls == []


@pytest.mark.anyio
async def test_deliver_unknown_session_raises() -> None:
    adapter = NaysayerLexoraAdapter(client=_FakeLexora())
    with pytest.raises(NaysayerLexoraDeliveryError):
        await adapter.deliver_event(_stray_handle(), _event())


@pytest.mark.anyio
async def test_deliver_on_reply_failure_marks_failed() -> None:
    fake = _FakeLexora()

    async def on_reply(_draft: ReplyDraft) -> None:
        raise RuntimeError("downstream boom")

    async def on_event_log(_event: Event) -> None:
        return None

    ctx = SpawnContext(on_reply=on_reply, on_event_log=on_event_log, own_role=Role.NAYSAYER)
    adapter = NaysayerLexoraAdapter(client=fake)
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, ctx)
    with pytest.raises(NaysayerLexoraDeliveryError):
        await adapter.deliver_event(handle, _event())
    hs = await adapter.health(handle)
    assert hs.state is SessionState.FAILED
    assert hs.error is not None
    assert hs.error.code == "adapter.delivery_failed"


# ---------- halt ---------------------------------------------------------


@pytest.mark.anyio
async def test_halt_marks_halted_and_is_idempotent() -> None:
    fake = _FakeLexora()
    adapter = NaysayerLexoraAdapter(client=fake)
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    await adapter.halt(handle)
    assert (await adapter.health(handle)).state is SessionState.HALTED
    # Idempotent (I8): second halt does nothing, no raise.
    await adapter.halt(handle)
    assert (await adapter.health(handle)).state is SessionState.HALTED
    # halt does NOT close the shared client (closed once via aclose).
    assert fake.closed is False


@pytest.mark.anyio
async def test_deliver_on_halted_session_raises() -> None:
    adapter = NaysayerLexoraAdapter(client=_FakeLexora())
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    await adapter.halt(handle)
    with pytest.raises(NaysayerLexoraDeliveryError):
        await adapter.deliver_event(handle, _event())


@pytest.mark.anyio
async def test_halt_unknown_handle_is_noop() -> None:
    adapter = NaysayerLexoraAdapter(client=_FakeLexora())
    await adapter.halt(_stray_handle())  # no raise


# ---------- health -------------------------------------------------------


@pytest.mark.anyio
async def test_health_unknown_handle_raises() -> None:
    adapter = NaysayerLexoraAdapter(client=_FakeLexora())
    with pytest.raises(NaysayerLexoraHealthError):
        await adapter.health(_stray_handle())


@pytest.mark.anyio
async def test_health_includes_lexora_status() -> None:
    fake = _FakeLexora(health_body={"status": "degraded", "backends": {}})
    adapter = NaysayerLexoraAdapter(client=fake)
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    hs = await adapter.health(handle)
    assert hs.details["lexora_health"] == "degraded"


@pytest.mark.anyio
async def test_health_survives_lexora_outage() -> None:
    # A Lexora outage is recorded in details (observability), not raised:
    # the session state is still determinable.
    fake = _FakeLexora(health_error=RuntimeError("connection refused"))
    adapter = NaysayerLexoraAdapter(client=fake)
    handle = await adapter.spawn(_thread_ref(), Role.NAYSAYER, _ctx([]))
    hs = await adapter.health(handle)
    assert hs.state is SessionState.IDLE
    assert "unreachable" in hs.details["lexora_health"]


# ---------- aclose -------------------------------------------------------


@pytest.mark.anyio
async def test_aclose_closes_shared_client() -> None:
    fake = _FakeLexora()
    adapter = NaysayerLexoraAdapter(client=fake)
    await adapter.aclose()
    assert fake.closed is True
