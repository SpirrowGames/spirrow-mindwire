"""T43 ``spawn.ready`` and T45 (loop side) ``session.cc_session_uuid`` observational events.

Thread ``T-agmsg-transport-lessons-readiness-session-claim-board``; final shapes in Bohr msg-4888
(T43: connect done → ``spawn.ready {adapter, after_s}``, nothing else) and msg-4957 §3 (T45: the
adapter records the Claude Code session UUID from ``SystemMessage(init)`` under a name distinct
from the handle's ``session_id``; the Conclair push waits on T47).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from spirrow_mindwire.adapters.claude_code_sdk import ClaudeCodeSdkAdapter, _drain_reply
from spirrow_mindwire.adapters.implementer import (
    ImplementerSdkAdapter,
    ImplementerSdkDeliveryError,
)
from spirrow_mindwire.dispatcher.event_log import (
    EVENT_FIELD_ADAPTER_ID,
    EVENT_FIELD_AFTER_S,
    EVENT_FIELD_AUTHOR,
    EVENT_FIELD_CC_SESSION_UUID,
    EVENT_FIELD_SESSION_ID,
    EVENT_KIND_SESSION_CC_UUID,
    EVENT_KIND_SPAWN_READY,
)
from spirrow_mindwire.obligations import load_manifest
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.value_objects import (
    ChatroomEvent,
    Event,
    EventType,
    NewMessagePayload,
    ReplyDraft,
    Role,
    ThreadRef,
)

_OBLIGATIONS = load_manifest()
_TS = datetime(2026, 9, 30, tzinfo=UTC)
_UUID_A = "11111111-2222-3333-4444-555555555555"
_UUID_B = "66666666-7777-8888-9999-000000000000"


def _init(uuid: str | None) -> SystemMessage:
    data: dict[str, Any] = {"type": "system", "subtype": "init"}
    if uuid is not None:
        data["session_id"] = uuid
    return SystemMessage(subtype="init", data=data)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m")


def _result(*, is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="t",
        stop_reason="end_turn",
        result="ok",
    )


class _Client:
    """Fake SDK client; each ``query`` consumes the next scripted turn."""

    def __init__(self, turns: list[list[Any]], *, fail_connect: bool = False) -> None:
        self._turns = list(turns)
        self._current: list[Any] = []
        self._fail_connect = fail_connect

    async def connect(self) -> None:
        if self._fail_connect:
            raise RuntimeError("connect boom")

    async def query(self, prompt: str) -> None:
        self._current = self._turns.pop(0)

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self._current:
            yield message

    async def interrupt(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


def _factory(client: _Client) -> Callable[[Any], _Client]:
    return lambda _options: client


def _thread_ref() -> ThreadRef:
    return ThreadRef(project_id="p", thread_id="T", chatroom_uri="mc://t/1")


def _ctx(role: Role, log: list[Event], *, log_raises: bool = False) -> SpawnContext:
    async def on_reply(_draft: ReplyDraft) -> None:
        return None

    async def on_event_log(event: Event) -> None:
        log.append(event)
        if log_raises:
            raise RuntimeError("sink boom")

    return SpawnContext(
        on_reply=on_reply,
        on_event_log=on_event_log,
        own_role=role,
        own_instance_id=f"{role.value}-1",
    )


def _event() -> ChatroomEvent:
    return ChatroomEvent(
        event_id="01JEVENT",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id="m1", author="human", body="go", parent_msg_id=None),
    )


def _implementer(tmp_path: Path, client: _Client) -> ImplementerSdkAdapter:
    return ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(client),
    )


def _proposer(tmp_path: Path, client: _Client) -> ClaudeCodeSdkAdapter:
    return ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))


def _kinds(log: list[Event], kind: str) -> list[Event]:
    return [e for e in log if e.kind == kind]


# ── T43: spawn.ready ────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "build, role", [(_implementer, Role.IMPLEMENTER), (_proposer, Role.PROPOSER)]
)
async def test_spawn_ready_emitted_once_after_connect(
    tmp_path: Path, build: Any, role: Role
) -> None:
    log: list[Event] = []
    adapter = build(tmp_path, _Client([]))
    handle = await adapter.spawn(_thread_ref(), role, _ctx(role, log))
    [ready] = _kinds(log, EVENT_KIND_SPAWN_READY)
    assert ready.fields[EVENT_FIELD_ADAPTER_ID] == adapter.adapter_id
    assert ready.fields[EVENT_FIELD_SESSION_ID] == handle.session_id
    assert ready.fields[EVENT_FIELD_AUTHOR] == handle.instance_id
    after_s = ready.fields[EVENT_FIELD_AFTER_S]
    assert isinstance(after_s, float) and after_s >= 0.0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "build, role", [(_implementer, Role.IMPLEMENTER), (_proposer, Role.PROPOSER)]
)
async def test_no_spawn_ready_when_connect_fails(tmp_path: Path, build: Any, role: Role) -> None:
    log: list[Event] = []
    adapter = build(tmp_path, _Client([], fail_connect=True))
    with pytest.raises(Exception, match="connect boom"):
        await adapter.spawn(_thread_ref(), role, _ctx(role, log))
    assert _kinds(log, EVENT_KIND_SPAWN_READY) == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "build, role", [(_implementer, Role.IMPLEMENTER), (_proposer, Role.PROPOSER)]
)
async def test_raising_event_log_does_not_fail_the_spawn(
    tmp_path: Path, build: Any, role: Role
) -> None:
    """I7: the log is observational — a raising sink must not lose a connected session."""
    log: list[Event] = []
    adapter = build(tmp_path, _Client([]))
    handle = await adapter.spawn(_thread_ref(), role, _ctx(role, log, log_raises=True))
    assert handle is not None
    assert len(_kinds(log, EVENT_KIND_SPAWN_READY)) == 1


def test_no_launched_unconfirmed_vocabulary() -> None:
    """msg-4888: a word no adapter emits is not added to the vocabulary."""
    from spirrow_mindwire.dispatcher import event_log

    kinds = {v for k, v in vars(event_log).items() if k.startswith("EVENT_KIND_")}
    assert not any("unconfirmed" in k or "mcp_unavailable" in k for k in kinds)


# ── T45: cc_session_uuid from SystemMessage(init) ──────────────────────────


@pytest.mark.anyio
async def test_drain_reply_reports_init_uuid_and_ignores_others() -> None:
    seen: list[str] = []
    client = _Client(
        [
            [
                _init(_UUID_A),
                SystemMessage(subtype="other", data={"session_id": "x"}),
                _assistant("hi"),
                _result(),
            ]
        ]
    )
    await client.query("q")
    body = await _drain_reply(client, on_init=seen.append)
    assert body == "hi"
    assert seen == [_UUID_A]


@pytest.mark.anyio
async def test_drain_reply_init_without_uuid_reports_nothing() -> None:
    seen: list[str] = []
    client = _Client([[_init(None), _assistant("hi"), _result()]])
    await client.query("q")
    await _drain_reply(client, on_init=seen.append)
    assert seen == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "build, role", [(_implementer, Role.IMPLEMENTER), (_proposer, Role.PROPOSER)]
)
async def test_cc_session_uuid_logged_once_per_distinct_value(
    tmp_path: Path, build: Any, role: Role
) -> None:
    log: list[Event] = []
    turns = [
        [_init(_UUID_A), _assistant("1"), _result()],
        [_init(_UUID_A), _assistant("2"), _result()],  # same session: no second entry
        [_init(_UUID_B), _assistant("3"), _result()],  # changed: logged again
    ]
    adapter = build(tmp_path, _Client(turns))
    handle = await adapter.spawn(_thread_ref(), role, _ctx(role, log))
    for _ in turns:
        await adapter.deliver_event(handle, _event())
    entries = _kinds(log, EVENT_KIND_SESSION_CC_UUID)
    assert [e.fields[EVENT_FIELD_CC_SESSION_UUID] for e in entries] == [_UUID_A, _UUID_B]
    for e in entries:
        # Two different identifiers under two different names (msg-4884 §2).
        assert e.fields[EVENT_FIELD_SESSION_ID] == handle.session_id
        assert e.fields[EVENT_FIELD_SESSION_ID] != e.fields[EVENT_FIELD_CC_SESSION_UUID]
    assert adapter._sessions[handle].cc_session_uuid == _UUID_B


@pytest.mark.anyio
async def test_cc_session_uuid_recorded_even_when_the_turn_fails(tmp_path: Path) -> None:
    """The failed turn is the one whose transcript someone goes looking for."""
    log: list[Event] = []
    adapter = _implementer(tmp_path, _Client([[_init(_UUID_A), _result(is_error=True)]]))
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx(Role.IMPLEMENTER, log))
    with pytest.raises(ImplementerSdkDeliveryError):
        await adapter.deliver_event(handle, _event())
    [entry] = _kinds(log, EVENT_KIND_SESSION_CC_UUID)
    assert entry.fields[EVENT_FIELD_CC_SESSION_UUID] == _UUID_A


def test_session_handle_is_unchanged() -> None:
    """T43/T45 add no mutable state to the frozen, identity-keyed handle (msg-4884 §1)."""
    import dataclasses

    from spirrow_mindwire.value_objects import SessionHandle

    names = {f.name for f in dataclasses.fields(SessionHandle)}
    assert "cc_session_uuid" not in names
    assert "readiness" not in names and "ready_at" not in names
