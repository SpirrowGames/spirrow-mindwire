"""MindWire data schemas (pydantic).

Mirrors ``docs/architecture.md`` §3.

- :class:`ThreadMeta` — ``threads/<ULID>/meta.yaml``
- :class:`Message` — ``threads/<ULID>/messages/NNN-from-{cai|cc}.md``
- :data:`Event` — ``logs/threads/<ULID>.jsonl`` (discriminated union)
"""

from __future__ import annotations

from ._common import (
    SCHEMA_VERSION,
    Participant,
    StrictModel,
    TerminatedReason,
    ThreadStatus,
    UlidStr,
    UTCDatetime,
)
from .event import (
    ClaudeCodeInvokeEnd,
    ClaudeCodeInvokeStart,
    Event,
    MessageReceived,
    MessageSent,
    RetryBackoffStarted,
    ThreadArchived,
    ThreadCreated,
    ThreadResolved,
    ThreadStatusChanged,
)
from .message import Message
from .meta import ThreadMeta

__all__ = [
    "SCHEMA_VERSION",
    "ClaudeCodeInvokeEnd",
    "ClaudeCodeInvokeStart",
    "Event",
    "Message",
    "MessageReceived",
    "MessageSent",
    "Participant",
    "RetryBackoffStarted",
    "StrictModel",
    "TerminatedReason",
    "ThreadArchived",
    "ThreadCreated",
    "ThreadMeta",
    "ThreadResolved",
    "ThreadStatus",
    "ThreadStatusChanged",
    "UTCDatetime",
    "UlidStr",
]
