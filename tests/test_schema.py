"""Schema validation tests for ``spirrow_mindwire.schema``.

Covers ``docs/architecture.md`` §3.1-§3.3 contracts:
- field defaults, strict-extras rejection, schema_version pin
- ULID format, UTC-only datetime, literal enums
- Message ``from`` alias + msg_id ↔ seq consistency (zero-padded)
- Event discriminated union dispatch
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from spirrow_mindwire.schema import (
    SCHEMA_VERSION,
    ClaudeCodeInvokeEnd,
    ClaudeCodeInvokeStart,
    Event,
    Message,
    MessageReceived,
    MessageSent,
    ThreadArchived,
    ThreadCreated,
    ThreadMeta,
    ThreadResolved,
    ThreadStatusChanged,
)

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ULID_B = "01ARZ3NDEKTSV4RRFFQ69G5FBW"
NOW = datetime(2026, 5, 7, 8, 43, 7, tzinfo=UTC)


def _meta(**overrides: Any) -> ThreadMeta:
    base: dict[str, Any] = {
        "schema_version": 1,
        "thread_id": ULID_A,
        "status": "active",
        "participants": ("claude.ai", "claude-code"),
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return ThreadMeta(**base)


def _msg(**overrides: Any) -> Message:
    base: dict[str, Any] = {
        "schema_version": 1,
        "msg_id": f"{ULID_A}/001",
        "seq": 1,
        "from": "claude.ai",
        "to": "claude-code",
        "created_at": NOW,
        "body": "hello",
    }
    base.update(overrides)
    return Message.model_validate(base)


# ---------- ThreadMeta ----------------------------------------------------


def test_thread_meta_defaults() -> None:
    m = _meta()
    assert m.schema_version == SCHEMA_VERSION
    assert m.title == ""
    assert m.tags == ()


def test_thread_meta_requires_schema_version() -> None:
    base: dict[str, Any] = {
        "thread_id": ULID_A,
        "status": "active",
        "participants": ("claude.ai", "claude-code"),
        "created_at": NOW,
        "updated_at": NOW,
    }
    with pytest.raises(ValidationError, match="schema_version"):
        ThreadMeta(**base)


def test_thread_meta_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match="extra"):
        _meta(related="external-system")


def test_thread_meta_rejects_empty_participants() -> None:
    with pytest.raises(ValidationError, match="participants"):
        _meta(participants=())


def test_thread_meta_rejects_bad_participant() -> None:
    with pytest.raises(ValidationError):
        _meta(participants=("claude.ai", "gpt"))


def test_thread_meta_rejects_bad_status() -> None:
    with pytest.raises(ValidationError):
        _meta(status="open")


def test_thread_meta_rejects_naive_datetime() -> None:
    naive = datetime(2026, 5, 7, 8, 43, 7)
    with pytest.raises(ValidationError, match="timezone-aware"):
        _meta(created_at=naive)


def test_thread_meta_normalizes_non_utc_offset_to_utc() -> None:
    jst = timezone(timedelta(hours=9))
    m = _meta(created_at=NOW.astimezone(jst))
    assert m.created_at == NOW
    assert m.created_at.utcoffset() == timedelta(0)


def test_thread_meta_rejects_invalid_ulid() -> None:
    with pytest.raises(ValidationError):
        _meta(thread_id="not-a-ulid")


def test_thread_meta_rejects_wrong_schema_version() -> None:
    with pytest.raises(ValidationError):
        _meta(schema_version=99)


def test_thread_meta_is_frozen() -> None:
    m = _meta()
    with pytest.raises(ValidationError):
        m.title = "mutated"


# ---------- Message -------------------------------------------------------


def test_message_from_alias_round_trip() -> None:
    m = _msg()
    assert m.from_ == "claude.ai"
    dumped = m.model_dump(by_alias=True)
    assert dumped["from"] == "claude.ai"
    assert "from_" not in dumped


def test_message_rejects_from_equal_to() -> None:
    with pytest.raises(ValidationError, match="differ"):
        _msg(to="claude.ai")  # from_ defaults to "claude.ai"


def test_message_rejects_seq_zero() -> None:
    with pytest.raises(ValidationError):
        _msg(msg_id=f"{ULID_A}/000", seq=0)


def test_message_rejects_msg_id_seq_mismatch() -> None:
    with pytest.raises(ValidationError, match="msg_id"):
        _msg(msg_id=f"{ULID_A}/002", seq=1)


def test_message_rejects_msg_id_without_thread_prefix() -> None:
    with pytest.raises(ValidationError, match="msg_id"):
        _msg(msg_id="/001")


def test_message_rejects_non_ulid_thread_prefix() -> None:
    with pytest.raises(ValidationError, match="ULID"):
        _msg(msg_id="not-a-ulid/001")


def test_message_msg_id_three_digit_padding() -> None:
    m = _msg(seq=42, msg_id=f"{ULID_A}/042")
    assert m.seq == 42


def test_message_msg_id_four_digit_at_overflow() -> None:
    m = _msg(seq=1000, msg_id=f"{ULID_A}/1000")
    assert m.seq == 1000


def test_message_rejects_unpadded_seq() -> None:
    with pytest.raises(ValidationError, match="msg_id"):
        _msg(seq=42, msg_id=f"{ULID_A}/42")


def test_message_reply_to_optional() -> None:
    m = _msg()
    assert m.reply_to is None
    m2 = _msg(seq=2, msg_id=f"{ULID_A}/002", reply_to=1)
    assert m2.reply_to == 1


def test_message_rejects_reply_to_zero() -> None:
    with pytest.raises(ValidationError):
        _msg(reply_to=0)


def test_message_rejects_reply_to_at_or_after_seq() -> None:
    """``reply_to`` must reference an *earlier* seq."""
    with pytest.raises(ValidationError, match="reply_to"):
        _msg(seq=2, msg_id=f"{ULID_A}/002", reply_to=2)
    with pytest.raises(ValidationError, match="reply_to"):
        _msg(seq=2, msg_id=f"{ULID_A}/002", reply_to=3)


# ---------- Event discriminated union ------------------------------------

EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def _event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "event_id": ULID_B,
        "ts": NOW.isoformat(),
    }
    base.update(overrides)
    return base


def test_event_requires_schema_version() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        EVENT_ADAPTER.validate_python(
            {
                "type": "thread.created",
                "event_id": ULID_B,
                "ts": NOW.isoformat(),
                "thread_id": ULID_A,
            }
        )


def test_event_dispatches_thread_created() -> None:
    e = EVENT_ADAPTER.validate_python(_event(type="thread.created", thread_id=ULID_A, title="t"))
    assert isinstance(e, ThreadCreated)
    assert e.title == "t"


def test_event_dispatches_thread_status_changed() -> None:
    e = EVENT_ADAPTER.validate_python(
        _event(
            type="thread.status.changed",
            thread_id=ULID_A,
            from_status="awaiting-cc",
            to_status="awaiting-cai",
        )
    )
    assert isinstance(e, ThreadStatusChanged)
    assert e.from_status == "awaiting-cc"


def test_event_dispatches_thread_resolved_and_archived() -> None:
    r = EVENT_ADAPTER.validate_python(_event(type="thread.resolved", thread_id=ULID_A))
    a = EVENT_ADAPTER.validate_python(_event(type="thread.archived", thread_id=ULID_A))
    assert isinstance(r, ThreadResolved)
    assert isinstance(a, ThreadArchived)


def test_event_dispatches_message_received_with_from_alias() -> None:
    e = EVENT_ADAPTER.validate_python(
        _event(
            type="message.received",
            thread_id=ULID_A,
            seq=1,
            size_bytes=1234,
            **{"from": "claude.ai"},
        )
    )
    assert isinstance(e, MessageReceived)
    assert e.from_ == "claude.ai"


def test_event_dispatches_message_sent() -> None:
    e = EVENT_ADAPTER.validate_python(
        _event(
            type="message.sent",
            thread_id=ULID_A,
            seq=2,
            size_bytes=4567,
            **{"from": "claude-code"},
        )
    )
    assert isinstance(e, MessageSent)


def test_event_dispatches_invoke_start_and_end() -> None:
    s = EVENT_ADAPTER.validate_python(
        _event(type="claude_code.invoke.start", thread_id=ULID_A, msg_seq=1)
    )
    assert isinstance(s, ClaudeCodeInvokeStart)
    e = EVENT_ADAPTER.validate_python(
        _event(
            type="claude_code.invoke.end",
            thread_id=ULID_A,
            msg_seq=1,
            duration_ms=33000,
            exit_code=0,
        )
    )
    assert isinstance(e, ClaudeCodeInvokeEnd)
    assert e.duration_ms == 33000


def test_event_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(_event(type="thread.deleted", thread_id=ULID_A))


def test_event_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        EVENT_ADAPTER.validate_python(
            _event(
                type="thread.created",
                thread_id=ULID_A,
                title="t",
                metadata={"trace_id": "abc"},
            )
        )


def test_event_rejects_negative_size_bytes() -> None:
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(
            _event(
                type="message.received",
                thread_id=ULID_A,
                seq=1,
                size_bytes=-1,
                **{"from": "claude.ai"},
            )
        )


def test_event_round_trip_json() -> None:
    e = EVENT_ADAPTER.validate_python(_event(type="thread.created", thread_id=ULID_A, title="t"))
    payload = e.model_dump_json(by_alias=True)
    parsed = EVENT_ADAPTER.validate_json(payload)
    assert parsed == e
