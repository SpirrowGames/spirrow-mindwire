"""Event-log field names + builders — ADR-06 Implementation Notes (anchor #6).

Naming-hygiene **anchor #6** requires the Event log's "who / which model"
keys to be unified. These constants are the single definition of those key
names; the dispatcher builds Event entries through the helpers here, and a
unit test asserts the keys come from these constants (the enforcement
boundary placed in T13 per PR #56 verify / msg-183 carry-forward).
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..ulid_util import new_ulid
from ..value_objects import Event, ReplyDraft, SessionHandle

EVENT_FIELD_AUTHOR = "author"
EVENT_FIELD_MODEL_ID = "model_id"
EVENT_FIELD_SESSION_ID = "session_id"
EVENT_FIELD_ADAPTER_ID = "adapter_id"
EVENT_FIELD_POSTED_MSG_ID = "posted_msg_id"
EVENT_FIELD_IDEMPOTENCY_KEY = "idempotency_key"

EVENT_KIND_REPLY_SENT = "reply.sent"


def reply_sent_event(
    handle: SessionHandle,
    draft: ReplyDraft,
    *,
    posted_msg_id: str,
    idempotency_key: str,
) -> Event:
    """Build the observational ``reply.sent`` Event log entry (anchor #6 keys).

    ``author`` / ``model_id`` use the unified key constants. ``model_id`` is
    read from the adapter's ``adapter_metadata`` (empty string if the adapter
    did not report one).
    """
    return Event(
        event_id=new_ulid(),
        occurred_at=datetime.now(UTC),
        kind=EVENT_KIND_REPLY_SENT,
        fields={
            EVENT_FIELD_AUTHOR: handle.role.value,
            EVENT_FIELD_MODEL_ID: str(draft.adapter_metadata.get("model_id", "")),
            EVENT_FIELD_SESSION_ID: handle.session_id,
            EVENT_FIELD_ADAPTER_ID: handle.adapter_id,
            EVENT_FIELD_POSTED_MSG_ID: posted_msg_id,
            EVENT_FIELD_IDEMPOTENCY_KEY: idempotency_key,
        },
    )


__all__ = [
    "EVENT_FIELD_ADAPTER_ID",
    "EVENT_FIELD_AUTHOR",
    "EVENT_FIELD_IDEMPOTENCY_KEY",
    "EVENT_FIELD_MODEL_ID",
    "EVENT_FIELD_POSTED_MSG_ID",
    "EVENT_FIELD_SESSION_ID",
    "EVENT_KIND_REPLY_SENT",
    "reply_sent_event",
]
