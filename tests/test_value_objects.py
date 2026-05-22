"""Tests for ADR-2026-05-21-06 §2 value objects (PR-A / Step 0)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    ErrorInfo,
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
    return ThreadRef(
        project_id="spirrow-mindwire",
        thread_id="01JTHREAD",
        chatroom_uri="mc://thread/1",
    )


def _handle(ref: ThreadRef) -> SessionHandle:
    return SessionHandle(
        session_id="01JSESSION",
        adapter_id="claude-code-sdk",
        thread_ref=ref,
        role=Role.PROPOSER,
        started_at=_TS,
    )


def test_enum_values() -> None:
    assert Role.PROPOSER.value == "proposer"
    assert Capability.NAYSAYER_QUALIFIED.value == "naysayer_qualified"
    assert SessionState.IDLE.value == "idle"
    assert EventType.NEW_MESSAGE.value == "new_message"


def test_threadref_value_equality_and_frozen() -> None:
    a = _thread_ref()
    b = _thread_ref()
    assert a == b  # plain value objects compare by value
    with pytest.raises(FrozenInstanceError):
        a.project_id = "x"  # type: ignore[misc]


def test_sessionhandle_identity_equality_i2() -> None:
    ref = _thread_ref()
    h1 = _handle(ref)
    h2 = _handle(ref)  # identical field values
    # I2 (ADR-06 §4): identity equality only — coincident fields stay distinct.
    assert h1 != h2
    assert h1 == h1
    # hashable by identity, usable as dict keys despite equal fields
    bucket = {h1: "a", h2: "b"}
    assert len(bucket) == 2


def test_sessionhandle_frozen() -> None:
    h = _handle(_thread_ref())
    with pytest.raises(FrozenInstanceError):
        h.adapter_id = "x"  # type: ignore[misc]


def test_chatroom_event_carries_payload() -> None:
    payload = NewMessagePayload(msg_id="m1", author="human", body="hi", parent_msg_id=None)
    ev = ChatroomEvent(
        event_id="01JEVENT",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=payload,
    )
    assert ev.payload.author == "human"
    assert ev.event_type is EventType.NEW_MESSAGE


def test_reply_draft_metadata() -> None:
    r = ReplyDraft(body="ok", reply_to_msg_id="m1", adapter_metadata={"model_id": "x"})
    assert r.adapter_metadata["model_id"] == "x"


def test_health_status_error_code_is_single_sot() -> None:
    err = ErrorInfo(code="adapter.timeout", message="boom", raised_at=_TS)
    hs = HealthStatus(
        state=SessionState.FAILED,
        last_active_at=_TS,
        error=err,
        details={"pid": 123},
    )
    assert hs.error is not None
    assert hs.error.code == "adapter.timeout"
    # §3.4 Option (i): exception code lives in error.code, not duplicated in details.
    assert "error_code" not in hs.details


def test_event_defaults_to_empty_fields() -> None:
    e = Event(event_id="01JE", occurred_at=_TS, kind="reply.sent")
    assert e.fields == {}
