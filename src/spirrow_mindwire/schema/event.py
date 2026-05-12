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

**Event semantics: occurrence vs snapshot** (docs/feature-2-design.md §3.5):

- *Occurrence* events record that something **happened** at a moment in
  time — they are not state, just trail. Examples: :class:`MessageReceived`,
  :class:`MessageSent`, :class:`ClaudeCodeInvokeStart` /
  :class:`ClaudeCodeInvokeEnd`, :class:`RetryBackoffStarted`. The fields
  describe the occurrence itself (e.g., ``attempt_num`` /
  ``backoff_seconds`` for RetryBackoffStarted).
- *Snapshot* events record the **transition into a new state** — they
  capture meta.yaml-side state alongside the event. Examples:
  :class:`ThreadStatusChanged` (carries ``from_status`` / ``to_status``
  / ``retry_count``), :class:`ThreadCreated`. The state fields mirror
  the post-write meta.yaml so the event log alone is enough to
  reconstruct (or audit) the meta.yaml state at any point.

Phase 0 covers thread / message / claude_code.invoke events. Feature 2
sub-PR 3 adds :class:`RetryBackoffStarted` (occurrence) and extends
:class:`ThreadStatusChanged` with ``retry_count`` (snapshot). Further
``error.*`` sub-types remain deferred until error taxonomy lands.
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
    """Snapshot of a meta.yaml status transition.

    Logged immediately after ``transition_state`` writes meta.yaml
    (= meta is SOT, event is best-effort audit log; see
    docs/feature-2-design.md §3.5 FI-2 resolution). ``retry_count``
    mirrors the post-write meta.yaml value so the event log alone is
    enough to reconstruct the retry trail (= cumulative semantics, see
    docs/feature-2-design.md §3.5).

    Defaults to ``retry_count=0`` for backward-compat with pre-Feature-2
    event log entries that did not carry the field.
    """

    type: Literal["thread.status.changed"] = "thread.status.changed"
    thread_id: UlidStr
    from_status: ThreadStatus
    to_status: ThreadStatus
    retry_count: int = Field(default=0, ge=0)


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


class RetryBackoffStarted(_BaseEvent):
    """Occurrence event: a retry backoff sleep just started.

    Logged by the dispatcher right before sleeping ``backoff_seconds``
    between two retry attempts (after a transient failure, before the
    next ``_invoker`` call). Distinct from :class:`ThreadStatusChanged`:
    this carries *no* lifecycle state, only what just happened
    (occurrence vs snapshot, see module docstring + docs/feature-2-design.md
    §3.5).

    ``attempt_num`` is 1-based and refers to the **upcoming** retry
    attempt (i.e., the attempt that runs *after* this backoff). The
    initial invoke is attempt 0; after its failure ``attempt_num=1`` is
    logged before sleeping; etc. Hence ``attempt_num`` is bounded by
    ``[1, max_retries]`` — exhaustion happens without a final backoff.
    """

    type: Literal["thread.retry.backoff_started"] = "thread.retry.backoff_started"
    thread_id: UlidStr
    attempt_num: int = Field(ge=1)
    backoff_seconds: float = Field(ge=0)


Event = Annotated[
    ThreadCreated
    | ThreadStatusChanged
    | ThreadResolved
    | ThreadArchived
    | MessageReceived
    | MessageSent
    | ClaudeCodeInvokeStart
    | ClaudeCodeInvokeEnd
    | RetryBackoffStarted,
    Field(discriminator="type"),
]


__all__ = [
    "ClaudeCodeInvokeEnd",
    "ClaudeCodeInvokeStart",
    "Event",
    "MessageReceived",
    "MessageSent",
    "RetryBackoffStarted",
    "ThreadArchived",
    "ThreadCreated",
    "ThreadResolved",
    "ThreadStatusChanged",
]
