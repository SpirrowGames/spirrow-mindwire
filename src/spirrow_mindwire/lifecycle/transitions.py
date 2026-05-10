"""Lifecycle state transition logic for Spirrow MindWire (Feature 2).

Single entry point for ThreadMeta status transitions, ensuring atomicity
of ``status`` / ``awaiting_from`` / terminated fields / ``updated_at``
updates via :func:`atomic_write_text`. Forbidden transitions raise
:class:`InvalidTransitionError`.

See ``docs/feature-2-design.md`` §3.5 for the design rationale and the
table-driven enforcement pattern.

NOTE: events.jsonl ``ThreadStatusChanged`` append is intentionally NOT
included in this module. The 2-phase commit semantics (meta.yaml ↔
events.jsonl write order, failure detection / rollback) is FI-2,
formal-decided in sub-PR 3 (retry). Until then, callers must append
the event log entry separately in their own atomic block.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import yaml

from spirrow_mindwire.filesystem.atomic import atomic_write_text
from spirrow_mindwire.filesystem.thread_dir import ThreadDirLayout
from spirrow_mindwire.schema import (
    Participant,
    TerminatedReason,
    ThreadMeta,
    ThreadStatus,
)

_ALLOWED_TRANSITIONS: dict[ThreadStatus, set[ThreadStatus]] = {
    "active": {"retrying", "terminated", "resolved"},
    "retrying": {"active", "terminated"},
    "terminated": {"resolved", "archived"},
    "resolved": {"archived"},
    "archived": set(),  # immutable terminal
}
"""Allowed ThreadStatus transitions (Decide #3b-1).

See ``docs/feature-2-design.md`` §3.3 for the transition table and the
prohibited transitions discussion (e.g. ``terminated → active``
automatic is NOT allowed; operator must edit meta.yaml + start a new
thread). ``retrying → resolved`` is intentionally not allowed in
Phase 0 (Naysayer reversal of an early claude.ai proposal).
"""

TERMINAL_STATES: frozenset[ThreadStatus] = frozenset({"terminated", "resolved", "archived"})
"""States that cannot transition further automatically.

- Operator manual transitions only (see ``docs/feature-2-design.md`` §3.6).
- ``awaiting_from`` MUST be ``None`` while in any of these states
  (``transition_state`` enforces this; see also ``ThreadMeta.awaiting_from``).
- ``startup_full_scan`` skips threads in these states (no auto-revival).
- ``ThreadDispatcher._run_thread`` short-circuits on these states.

Note: distinct from "terminal in transition graph" — ``archived`` is the
only strict graph terminal (no outgoing transitions in
:data:`_ALLOWED_TRANSITIONS`).
"""

REQUEUE_STATES: frozenset[ThreadStatus] = frozenset({"active", "retrying"})
"""States that ``startup_full_scan`` re-queues for dispatcher resumption.

The complement of :data:`TERMINAL_STATES` within :data:`ThreadStatus`.
"""


class InvalidTransitionError(ValueError):
    """Raised when a ThreadStatus transition violates :data:`_ALLOWED_TRANSITIONS`."""

    def __init__(self, old: ThreadStatus, new: ThreadStatus) -> None:
        allowed = sorted(_ALLOWED_TRANSITIONS[old])
        super().__init__(
            f"transition {old!r} -> {new!r} is not allowed; allowed from {old!r}: {allowed}"
        )
        self.old = old
        self.new = new


def _validate_transition(old: ThreadStatus, new: ThreadStatus) -> None:
    """Raise :class:`InvalidTransitionError` if ``(old, new)`` is forbidden."""
    if new not in _ALLOWED_TRANSITIONS[old]:
        raise InvalidTransitionError(old, new)


def transition_state(
    layout: ThreadDirLayout,
    new_status: ThreadStatus,
    *,
    awaiting_from: Participant | None,
    terminated_reason: TerminatedReason | None = None,
    terminated_at: datetime | None = None,
    retry_count: int | None = None,
) -> ThreadMeta:
    """Atomically transition a thread to ``new_status``.

    Loads the current ThreadMeta, validates the transition against
    :data:`_ALLOWED_TRANSITIONS`, applies the field updates, and writes
    the updated meta.yaml via :func:`atomic_write_text`. ``updated_at``
    is set to ``datetime.now(UTC)``.

    NOTE: "Atomically" here refers to the meta.yaml WRITE step
    (``atomic_write_text`` = tmp + os.replace). The full read-modify-
    write is NOT serializable: an operator manual edit between the
    read (line 1) and the write (last line) may be silently overwritten.
    This is the Phase 0 race-acceptance contract documented in
    ``docs/feature-2-design.md`` §3.6 ("operator should stop the
    watcher before manual edits"). Multi-process / transactional write
    is queued for a later Phase (FI-2).

    Args:
        layout: ThreadDirLayout pointing to the thread's directory.
        new_status: target ThreadStatus.
        awaiting_from: new ``awaiting_from`` value. **Must be ``None``**
            for states in :data:`TERMINAL_STATES` (``terminated`` /
            ``resolved`` / ``archived``); **must be a non-``None``
            Participant** otherwise. Mismatches raise ``ValueError``.
        terminated_reason: only meaningful when
            ``new_status == 'terminated'`` (where it is required).
            For non-terminated transitions, any value passed here is
            silently discarded; the existing ``terminated_reason``
            from old meta (if any, audit trail) is preserved.
        terminated_at: only meaningful when ``new_status == 'terminated'``;
            defaults to the same ``now`` instant used for ``updated_at``
            if not explicitly provided. For non-terminated transitions,
            any value passed here is silently discarded; the existing
            ``terminated_at`` from old meta (audit trail) is preserved.
        retry_count: optional new ``retry_count`` value. ``None``
            preserves the existing value.

    Returns:
        The new ThreadMeta after the write.

    Raises:
        InvalidTransitionError: if ``(old.status, new_status)`` violates
            :data:`_ALLOWED_TRANSITIONS`.
        ValueError: if ``new_status='terminated'`` but ``terminated_reason``
            is ``None``; or if ``awaiting_from`` does not match
            ``new_status`` (terminal states require ``None``;
            non-terminal states require a non-``None`` Participant).
    """
    if new_status in TERMINAL_STATES:
        if awaiting_from is not None:
            raise ValueError(
                f"awaiting_from must be None for terminal state {new_status!r}, "
                f"got {awaiting_from!r}"
            )
    else:
        if awaiting_from is None:
            raise ValueError(
                f"awaiting_from must be a Participant for non-terminal state "
                f"{new_status!r}, got None"
            )

    # Inline meta read (semantically equivalent to
    # ``spirrow_mindwire.watcher.loader.load_thread_meta``). Duplicated
    # here to keep the lifecycle layer free of a watcher dependency
    # (lifecycle is lower in the layering than watcher). Promoting
    # ``load_thread_meta`` into ``filesystem/`` so both layers can share
    # it is a follow-up clean-up tracked separately, not in scope here.
    old_meta_text = layout.meta_path.read_text(encoding="utf-8")
    old_meta = ThreadMeta.model_validate(yaml.safe_load(old_meta_text))

    _validate_transition(old_meta.status, new_status)

    # Single ``now`` shared between ``updated_at`` and (if applicable)
    # ``terminated_at`` so a single atomic transition produces a single
    # consistent timestamp on disk (= same instant for the meta write
    # and the terminated marker, no second call drift).
    now = datetime.now(UTC)

    update: dict[str, Any] = {
        "status": new_status,
        "awaiting_from": awaiting_from,
        "updated_at": now,
    }
    if retry_count is not None:
        update["retry_count"] = retry_count

    if new_status == "terminated":
        if terminated_reason is None:
            raise ValueError("terminated_reason is required when transitioning to 'terminated'")
        update["terminated_reason"] = terminated_reason
        update["terminated_at"] = terminated_at if terminated_at is not None else now
    # Otherwise preserve existing terminated_reason / terminated_at as
    # audit trail (Decide #3b-2、 docs §3.4 terminal-out preservation).

    new_meta = old_meta.model_copy(update=update)
    atomic_write_text(
        layout.meta_path,
        yaml.safe_dump(
            new_meta.model_dump(mode="json"),
            default_flow_style=False,
            sort_keys=False,
        ),
    )
    return new_meta


__all__ = [
    "InvalidTransitionError",
    "transition_state",
]
