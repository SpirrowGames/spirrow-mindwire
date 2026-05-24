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

EVENT_KIND_REPLY_SENT = "reply.sent"
EVENT_KIND_DELIVERY_FAILED = "delivery.failed"


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
    """
    return Event(
        event_id=new_ulid(),
        occurred_at=datetime.now(UTC),
        kind=EVENT_KIND_DELIVERY_FAILED,
        fields={
            EVENT_FIELD_AUTHOR: handle.instance_id,  # I3 v2.2 / T26: author = instance_id
            EVENT_FIELD_SESSION_ID: handle.session_id,
            EVENT_FIELD_ADAPTER_ID: handle.adapter_id,
            EVENT_FIELD_FAILED_EVENT_ID: event.event_id,
            EVENT_FIELD_ERROR: str(error),
        },
    )


__all__ = [
    "EVENT_FIELD_ADAPTER_ID",
    "EVENT_FIELD_AUTHOR",
    "EVENT_FIELD_ERROR",
    "EVENT_FIELD_FAILED_EVENT_ID",
    "EVENT_FIELD_IDEMPOTENCY_KEY",
    "EVENT_FIELD_MODEL_ID",
    "EVENT_FIELD_POSTED_MSG_ID",
    "EVENT_FIELD_SESSION_ID",
    "EVENT_KIND_DELIVERY_FAILED",
    "EVENT_KIND_REPLY_SENT",
    "delivery_failed_event",
    "reply_sent_event",
]
