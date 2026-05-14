"""Thread metadata model (`threads/<ULID>/meta.yaml`).

See ``docs/architecture.md`` §3.1 for the Phase 0 baseline and
``docs/feature-2-design.md`` §3.1 / §3.4 for the Feature 2 fields
(``awaiting_from`` / ``retry_count`` / ``terminated_reason`` /
``terminated_at``) extending the lifecycle state machine.

The ``schema_version`` literal was bumped from 1 to 2 in Feature 3-A
sub-PR 1 (skeleton bump, see ``docs/feature-3-design.md`` §3.1). No
field shape changes accompanied that bump; the literal change marks
the entry into Phase 1 era for ``meta.yaml`` writers. Pre-existing v1
files must be migrated via ``uv run mindwire-migrate-v1-to-v2`` before
the watcher loads them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from ._common import (
    Participant,
    StrictModel,
    TerminatedReason,
    ThreadStatus,
    UlidStr,
    UTCDatetime,
)


class ThreadMeta(StrictModel):
    """Thread metadata persisted as ``meta.yaml``.

    ``title`` defaults to ``""`` — untitled threads are valid in Phase 0
    because the watcher creates them before any human / agent has reason
    to title them. ``tags`` likewise defaults to an empty tuple; per §3.1,
    only the participants themselves may write tags (Connectors must not).

    Feature 2 fields (``awaiting_from`` / ``retry_count`` /
    ``terminated_reason`` / ``terminated_at``) extend the lifecycle
    state machine; see ``docs/feature-2-design.md`` §3.1 / §3.4.
    """

    schema_version: Literal[2]
    thread_id: UlidStr
    title: str = ""
    status: ThreadStatus
    awaiting_from: Participant | None = None
    """Next participant expected to respond.

    ``None`` for terminal states (``terminated`` / ``resolved`` /
    ``archived``). For ``active`` / ``retrying`` states, points to the
    participant whose turn it is to act. Updated by ``transition_state``
    on ``write_reply`` success completion. See
    ``docs/feature-2-design.md`` §3.1.
    """
    participants: tuple[Participant, ...]
    created_at: UTCDatetime
    updated_at: UTCDatetime
    """Last write time of this meta.yaml, regardless of trigger.

    Updated whenever ThreadMeta is persisted, including:
    - status transition (``transition_state`` function)
    - ``awaiting_from`` update without status change
    - any other meta.yaml write

    Distinct from event log timestamps (``events.jsonl``), which record
    when each event was appended.
    """
    tags: tuple[str, ...] = Field(default_factory=tuple)
    retry_count: int = Field(default=0, ge=0)
    """Cumulative retry attempts for this thread (Feature 2 sub-PR 3)."""
    terminated_reason: TerminatedReason | None = None
    """Reason for entering ``terminated`` state.

    ``None`` until the thread has ever entered ``terminated``. Once set,
    **preserved across terminal-out transitions** (``terminated →
    resolved`` / ``terminated → archived`` / and onward) as an audit
    trail. So in ``resolved`` / ``archived`` states this field is
    ``None`` if the thread reached the terminal directly (e.g.
    ``active → resolved``) and non-``None`` if it transited via
    ``terminated``. See ``docs/feature-2-design.md`` §3.4.
    """
    terminated_at: UTCDatetime | None = None
    """Timestamp of transition into ``terminated`` state.

    Same invariant as :attr:`terminated_reason`: ``None`` until first
    entry into ``terminated``, then preserved across terminal-out
    transitions as audit trail. See ``docs/feature-2-design.md`` §3.4.
    """

    @field_validator("participants")
    @classmethod
    def _participants_non_empty(cls, v: tuple[Participant, ...]) -> tuple[Participant, ...]:
        if len(v) == 0:
            raise ValueError("participants must not be empty")
        return v


__all__ = ["ThreadMeta"]
