"""Event log entries (`logs/threads/<ULID>.jsonl`).

See ``docs/architecture.md`` §3.3. Events form a discriminated union on
the ``type`` field. New event types are added by defining a model with
the corresponding ``type`` literal and extending :data:`Event`.

Phase 0 covers thread / message / claude_code.invoke events. ``error.*``
sub-types are intentionally deferred to Feature 2 (robustness) where
the taxonomy is co-designed with the dead-letter / retry layer.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator

from ._common import (
    SCHEMA_VERSION,
    AwareDatetime,
    Participant,
    StrictModel,
    ThreadStatus,
    UlidStr,
)


class _BaseEvent(StrictModel):
    schema_version: int = SCHEMA_VERSION
    event_id: UlidStr
    ts: AwareDatetime

    @field_validator("schema_version")
    @classmethod
    def _pin_schema_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported schema_version={v}; events require "
                f"schema_version={SCHEMA_VERSION}"
            )
        return v


class ThreadCreated(_BaseEvent):
    type: Literal["thread.created"] = "thread.created"
    thread_id: UlidStr
    title: str = ""


class ThreadStatusChanged(_BaseEvent):
    type: Literal["thread.status.changed"] = "thread.status.changed"
    thread_id: UlidStr
    from_status: ThreadStatus
    to_status: ThreadStatus


class ThreadResolved(_BaseEvent):
    type: Literal["thread.resolved"] = "thread.resolved"
    thread_id: UlidStr


class ThreadArchived(_BaseEvent):
    type: Literal["thread.archived"] = "thread.archived"
    thread_id: UlidStr


class MessageReceived(_BaseEvent):
    type: Literal["message.received"] = "message.received"
    thread_id: UlidStr
    seq: int = Field(ge=1)
    from_: Participant = Field(alias="from")
    size_bytes: int = Field(ge=0)


class MessageSent(_BaseEvent):
    type: Literal["message.sent"] = "message.sent"
    thread_id: UlidStr
    seq: int = Field(ge=1)
    from_: Participant = Field(alias="from")
    size_bytes: int = Field(ge=0)


class ClaudeCodeInvokeStart(_BaseEvent):
    type: Literal["claude_code.invoke.start"] = "claude_code.invoke.start"
    thread_id: UlidStr
    msg_seq: int = Field(ge=1)


class ClaudeCodeInvokeEnd(_BaseEvent):
    type: Literal["claude_code.invoke.end"] = "claude_code.invoke.end"
    thread_id: UlidStr
    msg_seq: int = Field(ge=1)
    duration_ms: int = Field(ge=0)
    exit_code: int


Event = Annotated[
    ThreadCreated
    | ThreadStatusChanged
    | ThreadResolved
    | ThreadArchived
    | MessageReceived
    | MessageSent
    | ClaudeCodeInvokeStart
    | ClaudeCodeInvokeEnd,
    Field(discriminator="type"),
]


__all__ = [
    "ClaudeCodeInvokeEnd",
    "ClaudeCodeInvokeStart",
    "Event",
    "MessageReceived",
    "MessageSent",
    "ThreadArchived",
    "ThreadCreated",
    "ThreadResolved",
    "ThreadStatusChanged",
]
