"""Event-log field names + builders — ADR-06 Implementation Notes (anchor #6).

Naming-hygiene **anchor #6** requires the Event log's "who / which model"
keys to be unified. These constants are the single definition of those key
names; the dispatcher builds Event entries through the helpers here, and a
unit test asserts the keys come from these constants (the enforcement
boundary placed in T13 per PR #56 verify / msg-183 carry-forward).

I3 v2.2 (T25/T26): the ``author`` key carries the session's stable
``instance_id`` (e.g. ``"proposer-1"``), matching the chatroom reply author —
so anchor #6's "chatroom-author == event-log-author" unification holds on a
single identity SOT (``instance_id``). T25 switched the chatroom post; T26
(this) brought the event log into line (the v2.2 amendment had scoped
event_log out; main's PR #69 review decided to unify). Phase 1 is
1-role-1-instance, so this reads as ``"{role}-1"``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..ulid_util import new_ulid
from ..value_objects import ChatroomEvent, Event, ReplyDraft, SessionHandle

EVENT_FIELD_AUTHOR = "author"
EVENT_FIELD_MODEL_ID = "model_id"
EVENT_FIELD_SESSION_ID = "session_id"
EVENT_FIELD_ADAPTER_ID = "adapter_id"
EVENT_FIELD_POSTED_MSG_ID = "posted_msg_id"
EVENT_FIELD_IDEMPOTENCY_KEY = "idempotency_key"
EVENT_FIELD_FAILED_EVENT_ID = "failed_event_id"
EVENT_FIELD_ERROR = "error"
EVENT_FIELD_DENIAL = "denial"
EVENT_FIELD_AFTER_S = "after_s"
EVENT_FIELD_CC_SESSION_UUID = "cc_session_uuid"

EVENT_KIND_REPLY_SENT = "reply.sent"
EVENT_KIND_DELIVERY_FAILED = "delivery.failed"
EVENT_KIND_SPAWN_READY = "spawn.ready"
EVENT_KIND_SESSION_CC_UUID = "session.cc_session_uuid"


def reply_sent_event(
    handle: SessionHandle,
    draft: ReplyDraft,
    *,
    posted_msg_id: str,
    idempotency_key: str,
) -> Event:
    """Build the observational ``reply.sent`` Event log entry (anchor #6 keys).

    ``author`` is the session's ``instance_id`` (I3 v2.2 / T26 — matches the
    chatroom post author). ``author`` / ``model_id`` use the unified key
    constants. ``model_id`` is read from the adapter's ``adapter_metadata``
    (empty string if missing or ``None``).
    """
    return Event(
        event_id=new_ulid(),
        occurred_at=datetime.now(UTC),
        kind=EVENT_KIND_REPLY_SENT,
        fields={
            EVENT_FIELD_AUTHOR: handle.instance_id,  # I3 v2.2 / T26: author = instance_id
            EVENT_FIELD_MODEL_ID: str(draft.adapter_metadata.get("model_id") or ""),
            EVENT_FIELD_SESSION_ID: handle.session_id,
            EVENT_FIELD_ADAPTER_ID: handle.adapter_id,
            EVENT_FIELD_POSTED_MSG_ID: posted_msg_id,
            EVENT_FIELD_IDEMPOTENCY_KEY: idempotency_key,
        },
    )


def delivery_failed_event(
    handle: SessionHandle,
    event: ChatroomEvent,
    error: BaseException,
) -> Event:
    """Build the observational ``delivery.failed`` Event log entry (ADR-06 §8).

    Logged by the dispatcher when ``deliver_event`` raises, before the error
    is re-raised (fail-loud). ``author`` is the session's ``instance_id``
    (I3 v2.2 / T26, anchor #6 key constant); ``failed_event_id`` is the failed
    :class:`ChatroomEvent`'s id (distinct from this log entry's own
    ``event_id``).

    ``denial`` is present only when the error carries a structured description of
    the denied act (``spec/design/T-denial-detail-and-overdeny.md``). It is read by
    duck-typing rather than by importing the adapter's exception class: the
    dispatcher must not depend on an adapter, and any future error type that wants
    to explain itself here only has to expose the same attribute.
    """
    fields = {
        EVENT_FIELD_AUTHOR: handle.instance_id,  # I3 v2.2 / T26: author = instance_id
        EVENT_FIELD_SESSION_ID: handle.session_id,
        EVENT_FIELD_ADAPTER_ID: handle.adapter_id,
        EVENT_FIELD_FAILED_EVENT_ID: event.event_id,
        EVENT_FIELD_ERROR: str(error),
    }
    record = getattr(error, "denial_record", None)
    if record:
        fields[EVENT_FIELD_DENIAL] = record
    return Event(
        event_id=new_ulid(),
        occurred_at=datetime.now(UTC),
        kind=EVENT_KIND_DELIVERY_FAILED,
        fields=fields,
    )


def spawn_ready_event(handle: SessionHandle, *, after_s: float) -> Event:
    """Build the observational ``spawn.ready`` entry (T43, Bohr msg-4888 / msg-4957).

    Emitted by an adapter at the moment its SDK ``connect()`` has returned — the
    point after which the returned handle is usable. ``after_s`` is the wall time
    that connect took. No other readiness word exists: ``launched_unconfirmed``
    was dropped because no current adapter would emit it (Einstein msg-4887).

    The "connect done ⇒ ready" reading holds only while no out-of-process MCP
    server is attached; ``tests/test_role_tool_surface.py`` pins that premise and
    goes red if it stops being true.

    The adapter is recorded under the anchor #6 key ``adapter_id`` rather than a
    bare ``adapter`` so the same fact is not spelled two ways in one log.
    """
    return Event(
        event_id=new_ulid(),
        occurred_at=datetime.now(UTC),
        kind=EVENT_KIND_SPAWN_READY,
        fields={
            EVENT_FIELD_AUTHOR: handle.instance_id,
            EVENT_FIELD_SESSION_ID: handle.session_id,
            EVENT_FIELD_ADAPTER_ID: handle.adapter_id,
            EVENT_FIELD_AFTER_S: round(after_s, 3),
        },
    )


def cc_session_uuid_event(handle: SessionHandle, *, cc_session_uuid: str) -> Event:
    """Build the observational ``session.cc_session_uuid`` entry (T45 loop side).

    Links mindwire's per-spawn ULID (``session_id``) to the Claude Code session
    UUID the SDK reports in its ``SystemMessage(subtype="init")``. The two are
    different identifiers and are never stored under the same name (Bohr
    msg-4884 §2). Emitted when the adapter first observes the UUID for a session
    and again only if it changes; pushing it to Conclair waits on the schema
    decided with T47 (msg-4957 §3).
    """
    return Event(
        event_id=new_ulid(),
        occurred_at=datetime.now(UTC),
        kind=EVENT_KIND_SESSION_CC_UUID,
        fields={
            EVENT_FIELD_AUTHOR: handle.instance_id,
            EVENT_FIELD_SESSION_ID: handle.session_id,
            EVENT_FIELD_ADAPTER_ID: handle.adapter_id,
            EVENT_FIELD_CC_SESSION_UUID: cc_session_uuid,
        },
    )


__all__ = [
    "EVENT_FIELD_ADAPTER_ID",
    "EVENT_FIELD_AFTER_S",
    "EVENT_FIELD_AUTHOR",
    "EVENT_FIELD_CC_SESSION_UUID",
    "EVENT_FIELD_DENIAL",
    "EVENT_FIELD_ERROR",
    "EVENT_FIELD_FAILED_EVENT_ID",
    "EVENT_FIELD_IDEMPOTENCY_KEY",
    "EVENT_FIELD_MODEL_ID",
    "EVENT_FIELD_POSTED_MSG_ID",
    "EVENT_FIELD_SESSION_ID",
    "EVENT_KIND_DELIVERY_FAILED",
    "EVENT_KIND_REPLY_SENT",
    "EVENT_KIND_SESSION_CC_UUID",
    "EVENT_KIND_SPAWN_READY",
    "cc_session_uuid_event",
    "delivery_failed_event",
    "reply_sent_event",
    "spawn_ready_event",
]
