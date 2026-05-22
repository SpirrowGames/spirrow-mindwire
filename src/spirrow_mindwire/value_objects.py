"""Phase 1 value objects — ADR-2026-05-21-06 (Accepted v2.1) §2.

These are the in-memory contract types exchanged between the dispatcher
and adapters. Unlike the Phase 0 ``schema/`` package (pydantic models that
validate / serialize on-disk YAML+JSONL, ADR-06 §6 *discard* target),
these are plain ``@dataclass`` value objects per ADR-06 §2 — no on-disk
representation, no pydantic validation at this layer.

Type ↔ ADR-06 §-boundary mapping is kept 1:1 with :mod:`ports` (§3):
this module is §2 only.

ULID-typed fields are plain ``str`` (annotated ``# ULID`` per the ADR);
timestamp fields are ``datetime`` (``# iso_z`` per the ADR). The ULID /
UTC conventions are inherited from Phase 0 (ADR-06 §6 / I6), but the
*validation* of those strings is a dispatcher/adapter concern, not a
property of the value object.

Invariant cross-references (enforced in T13, see ADR-06 Implementation
Notes): I2 (SessionHandle identity equality) is realised here via
``eq=False`` on :class:`SessionHandle`; the remaining invariants (I4 / I5
/ I7 / I8 / I9) constrain *behaviour* in the dispatcher, not these types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, TypeAlias

# --------------------------------------------------------------------------- #
# §2.6 Enums
# --------------------------------------------------------------------------- #


class Role(StrEnum):
    """A thread role an adapter session can be spawned for (ADR-06 §2.6)."""

    PROPOSER = "proposer"
    NAYSAYER = "naysayer"
    IMPLEMENTER = "implementer"


class Capability(StrEnum):
    """What an adapter can do; gates ``AdapterRegistry.qualified_for`` (§2.6).

    ``NAYSAYER_QUALIFIED`` carries the architecture-level naysayer
    independence enforcement inherited from ADR-05 (a session running the
    same model as ``main`` must not be assigned the naysayer slot).
    """

    READ_THREAD = "read_thread"
    POST_REPLY = "post_reply"
    EXECUTE_CODE = "execute_code"
    NAYSAYER_QUALIFIED = "naysayer_qualified"


class SessionState(StrEnum):
    """Adapter session lifecycle state (ADR-06 §2.6).

    The allowed transitions between these states are I8 (ADR-06 §4),
    enforced by the dispatcher FSM in T13 — not by this enum.
    """

    IDLE = "idle"  # spawned, awaiting events
    PROCESSING = "processing"
    HALTING = "halting"
    HALTED = "halted"
    FAILED = "failed"


class EventType(StrEnum):
    """Discriminator for :class:`ChatroomEvent` (ADR-06 §2.3)."""

    NEW_MESSAGE = "new_message"
    THREAD_CLOSED = "thread_closed"
    ROLE_REASSIGNED = "role_reassigned"  # Phase 2+
    PEER_HALTED = "peer_halted"


# --------------------------------------------------------------------------- #
# §2.1 ThreadRef
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ThreadRef:
    """Uniquely identifies a thread in the ChatRoom (ADR-06 §2.1).

    Replaces the discarded Phase 0 ``schema.ThreadMeta`` (filesystem-shaped
    superset with ``awaiting_from`` / ``retry_count`` etc.).
    """

    project_id: str
    thread_id: str  # ULID
    chatroom_uri: str  # resource URI on the magickit chatroom


# --------------------------------------------------------------------------- #
# §2.2 SessionHandle
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, eq=False)
class SessionHandle:
    """Opaque token the dispatcher uses to refer to an adapter session (§2.2).

    Adapter-specific resources (pid / browser tab id / HTTP session token)
    are never leaked through this token; they surface only via
    :attr:`HealthStatus.details` for observability (I2).

    ``eq=False`` is **load-bearing** (ADR-06 §4 I2): equality must be
    *identity* equality (Python ``is``), never value equality. With
    ``eq=False`` the dataclass leaves ``__eq__`` / ``__hash__`` as
    ``object``'s identity-based implementations, so two ``spawn()`` results
    whose fields happen to coincide are still distinct sessions and remain
    usable as ``dict`` keys by identity. ``frozen=True`` keeps the token
    immutable. Do **not** add ``eq=True`` or override ``__eq__`` /
    ``__hash__``.
    """

    session_id: str  # ULID
    adapter_id: str
    thread_ref: ThreadRef
    role: Role
    started_at: datetime  # iso_z


# --------------------------------------------------------------------------- #
# §2.3 ChatroomEvent
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NewMessagePayload:
    """Payload for :attr:`EventType.NEW_MESSAGE` events (ADR-06 §2.3)."""

    msg_id: str  # chatroom-side message ID
    author: str  # "human" / role string
    body: str
    parent_msg_id: str | None


EventPayload: TypeAlias = NewMessagePayload
"""Payload union discriminated by :attr:`ChatroomEvent.event_type` (§2.3).

ADR-06 §2.3 defines only :class:`NewMessagePayload` (the only Phase 1
event carrying a structured payload). ``THREAD_CLOSED`` / ``PEER_HALTED``
carry no structured payload and ``ROLE_REASSIGNED`` is Phase 2+. Additional
payload dataclasses join this union as their ``EventType``s are
implemented.

NOTE (PR-A, for main ADR-verify): the union currently being a single
member is a faithful reading of the ADR (which only specifies
``NewMessagePayload``); flag if a richer Phase 1 payload set is intended.
"""


@dataclass(frozen=True)
class ChatroomEvent:
    """An event the dispatcher delivers to an adapter (ADR-06 §2.3).

    ``event_id`` is the ULID dedup key (I4); ``occurred_at`` is the ULID-
    derived monotonic timestamp ordering used for per-session FIFO (I9).
    """

    event_id: str  # ULID, dedup key
    event_type: EventType
    thread_ref: ThreadRef
    occurred_at: datetime  # iso_z
    payload: EventPayload  # union by event_type


# --------------------------------------------------------------------------- #
# §2.4 ReplyDraft
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReplyDraft:
    """An adapter's reply, before the dispatcher completes it (ADR-06 §2.4).

    The adapter does **not** know about the ChatRoom, so post-API params
    are absent. The dispatcher fills ``author = session.role`` /
    ``thread_ref`` / ``posted_at`` / ``session_id`` / ``adapter_id`` and
    assigns the I5 ``reply_seq`` when the draft arrives (adapter never sets
    these).
    """

    body: str  # markdown
    reply_to_msg_id: str | None
    adapter_metadata: dict[str, Any]  # adapter-specific trace (model_id, tokens, ...)


# --------------------------------------------------------------------------- #
# §2.5 HealthStatus / ErrorInfo
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ErrorInfo:
    """Structured error attached to :class:`HealthStatus` (ADR-06 §2.5).

    ``code`` is the single SOT for an adapter failure's catalog code
    (``adapter.*`` / §3.4 Port exception cross-reference, Option (i) per
    ADR-06 §3.4): exception codes live here, **not** duplicated in
    :attr:`HealthStatus.details` (§3-axis dual-management avoidance).
    """

    code: str  # "adapter.timeout" / "adapter.auth_failed" / ... (catalogued)
    message: str
    raised_at: datetime


@dataclass(frozen=True)
class HealthStatus:
    """Adapter session health snapshot (ADR-06 §2.5).

    ``details`` is an observability-only escape hatch (I2): it may carry
    adapter-specific runtime values (pid / tab_id / http_session_status)
    for logging / dashboards, but the dispatcher must **not** use
    ``details`` values in decision branches, and exception codes are not
    duplicated here (they live in :attr:`ErrorInfo.code`).
    """

    state: SessionState
    last_active_at: datetime
    error: ErrorInfo | None
    details: dict[str, Any]  # adapter-specific, observability only (I2)


# --------------------------------------------------------------------------- #
# Event log entry (§3.1 SpawnContext.on_event_log parameter)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Event:
    """One entry in the flat JSONL Event log (ADR-06 §6 / I6 inherited
    convention: ``event_id`` ULID required, no message body, N+1 allowed).

    Emitted via :attr:`ports.SpawnContext.on_event_log` as an
    *observational* channel only (I7: no control-flow semantics; a raising
    callback is isolated by the dispatcher and never breaks the main flow).

    NOTE (PR-A, for main ADR-verify): ADR-06 §2 does not give ``Event``'s
    field list explicitly — it appears only in the §3.1 ``on_event_log``
    signature. This is a minimal Phase 1 shape honouring the §6 JSONL
    conventions. The concrete ``fields`` key set (e.g. ``author`` /
    ``model_id`` unified per Implementation Notes naming-hygiene anchor #6)
    is firmed up + enforced in T13. Flag if a different ``Event`` shape is
    intended.
    """

    event_id: str  # ULID
    occurred_at: datetime  # iso_z
    kind: str  # event-log entry type/name (no message body — I6)
    fields: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "Capability",
    "ChatroomEvent",
    "ErrorInfo",
    "Event",
    "EventPayload",
    "EventType",
    "HealthStatus",
    "NewMessagePayload",
    "ReplyDraft",
    "Role",
    "SessionHandle",
    "SessionState",
    "ThreadRef",
]
