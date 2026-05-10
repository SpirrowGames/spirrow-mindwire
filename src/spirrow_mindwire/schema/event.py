"""Event log entries (`logs/threads/<ULID>.jsonl`).

See ``docs/architecture.md`` §3.3. Events form a discriminated union on
the ``type`` field. New event types are added by defining a model with
the corresponding ``type`` literal and extending :data:`Event`.

Naming convention:
- ``message.received`` / ``message.sent`` carry ``seq`` — the seq of
  the message itself.
- ``claude_code.invoke.start`` / ``invoke.end`` carry ``msg_seq`` — the
  seq of the *triggering* message, not a seq of invokes. The different
  field name disambiguates "message under invoke" from "the invoke's
  own seq" (which doesn't exist as a concept in Phase 0).

Each ``type`` field has its literal as a default so models can be built
in tests / generators without restating the discriminator. ``TypeAdapter[Event]``
parsing of on-disk JSONL still requires ``type`` to be present
(discriminator dispatch happens before defaults apply).

Phase 0 covers thread / message / claude_code.invoke events. ``error.*``
sub-types are intentionally deferred to Feature 2 (robustness) where
the taxonomy is co-designed with the dead-letter / retry layer.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ._common import (
    Participant,
    StrictModel,
    ThreadStatus,
    UlidStr,
    UTCDatetime,
)


class _BaseEvent(StrictModel):
    schema_version: Literal[1]
    """Schema version for individual event log entries.

    Independent from :data:`spirrow_mindwire.schema._common.SCHEMA_VERSION`
    (which versions ``ThreadMeta`` on-disk YAML). Bumping one does not
    require bumping the other.
    """

    event_id: UlidStr
    ts: UTCDatetime


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
