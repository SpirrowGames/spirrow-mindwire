"""Event-log field names + builders — ADR-06 Implementation Notes (anchor #6).

Naming-hygiene **anchor #6** requires the Event log's "who / which model"
keys to be unified. These constants are the single definition of those key
names; the dispatcher builds Event entries through the helpers here, and a
unit test asserts the keys come from these constants (the enforcement
boundary placed in T13 per PR #56 verify / msg-183 carry-forward).

NOTE (I3 v2.2 / T26): the chatroom reply *author* switched to ``instance_id``
(T25), but the event-log ``author`` below intentionally still uses the bare
role — the v2.2 amendment scoped ``event_log`` out. This temporarily diverges
the identity SOT that anchor #6 unified (chatroom-author == event-log-author);
unifying ``event_log`` author → ``instance_id`` is tracked as **T26**
(blocked_by T25). No functional harm in Phase 1 (1-role-1-instance: role and
instance_id are 1:1). PR #69 review (main) confirmed this is a follow-up, not
an expansion of T25.
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

    ``author`` / ``model_id`` use the unified key constants. ``model_id`` is
    read from the adapter's ``adapter_metadata`` (empty string if missing or
    ``None``).
    """
    return Event(
        event_id=new_ulid(),
        occurred_at=datetime.now(UTC),
        kind=EVENT_KIND_REPLY_SENT,
        fields={
            EVENT_FIELD_AUTHOR: handle.role.value,  # T26: still role (see module note)
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
    is re-raised (fail-loud). ``author`` uses the anchor #6 key constant;
    ``failed_event_id`` is the failed :class:`ChatroomEvent`'s id (distinct
    from this log entry's own ``event_id``).
    """
    return Event(
        event_id=new_ulid(),
        occurred_at=datetime.now(UTC),
        kind=EVENT_KIND_DELIVERY_FAILED,
        fields={
            EVENT_FIELD_AUTHOR: handle.role.value,  # T26: still role (see module note)
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
