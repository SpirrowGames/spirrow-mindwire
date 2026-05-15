"""Shared ``awaiting_from`` toggle helper (events + meta in one step).

Single source of truth for the post-write_reply / post-send_message
sequence of:

1. read ``meta.yaml`` (= snapshot pre-state)
2. defensive skip if already in terminal state (= ``awaiting_from`` is None)
3. idempotent skip if already at target (= avoids a tautology event)
4. atomic ``set_awaiting_from`` of meta.yaml to the opposite participant
5. append :class:`AwaitingFromChanged` to ``events.jsonl``

Used by:

- :class:`spirrow_mindwire.watcher.dispatcher.ThreadDispatcher` after a
  successful ``write_reply`` lands on disk
  (``from_participant=claude-code``).
- :func:`spirrow_mindwire.mcp_write_server.tools_write.send_message`
  after an external ``send_message`` writes the next message file
  (``from_participant=claude.ai``).

The two callers share this helper per integrator decide
``T-feat3-d2-mcp-server`` msg-127 §4 C1 — the terminal-state guard,
idempotent skip, and snapshot-event semantic are too easy to drift
apart across implementations.

**Cross-process race scope** (msg-127 §1 D2-6 acceptance): the
read-modify-write sequence is *not* transactional across processes. If
the watcher dispatcher and the MCP write server toggle the same
thread concurrently, the loser's meta-write is overwritten and the
audit log records two ``AwaitingFromChanged`` events. The Phase 1 MVP
explicitly accepts this; sub-PR 4 is the planned re-design.

**Module placement**: a top-level module rather than under
``lifecycle/`` because lifecycle is intentionally events-free
(:mod:`spirrow_mindwire.lifecycle.transitions` module docstring). The
inline meta read mirrors the same module's pattern — duplicating one
``yaml.safe_load`` to avoid a watcher dependency.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import yaml

from spirrow_mindwire.filesystem import EventLogWriter, ThreadDirLayout
from spirrow_mindwire.lifecycle import set_awaiting_from
from spirrow_mindwire.schema import (
    AwaitingFromChanged,
    Participant,
    ThreadMeta,
    opposite_of,
)
from spirrow_mindwire.ulid_util import new_ulid

logger = logging.getLogger(__name__)


def toggle_awaiting_from(
    *,
    layout: ThreadDirLayout,
    log: EventLogWriter,
    from_participant: Participant,
) -> None:
    """Toggle ``awaiting_from`` to the opposite of ``from_participant``.

    Reads ``meta.yaml``, writes the toggled value via
    :func:`set_awaiting_from`, then appends an
    :class:`AwaitingFromChanged` event to ``events.jsonl``.

    Args:
        layout: ThreadDirLayout for the affected thread.
        log: open event-log writer for the same thread.
        from_participant: the participant who just authored a message
            (= "whose turn just ended"). The new ``awaiting_from`` is
            :func:`opposite_of` this value.

    **Terminal-state defensive**: if pre-meta's ``awaiting_from`` is
    ``None`` (= the thread is in a terminal state), logs a warning and
    returns without writing. Callers should guard at the tool boundary
    (= reject with an explicit error) instead of relying on this silent
    skip. The skip exists so the watcher dispatcher path — which is
    only ever called from non-terminal control flow — never crashes on
    an operator-edited mid-toggle race.

    **Idempotent skip**: if pre-meta's ``awaiting_from`` already equals
    the target (= operator pre-edit, or replayed call), returns without
    writing meta or appending the event. Prevents tautology
    ``AwaitingFromChanged`` rows where ``from_participant ==
    to_participant``.

    **Snapshot semantic**: :attr:`AwaitingFromChanged.from_participant`
    is the pre-write value, *not* the actor. This mirrors
    :attr:`ThreadStatusChanged.from_status` and keeps the audit log
    faithful when the pre-state was already mid-toggle.
    """
    old_meta_text = layout.meta_path.read_text(encoding="utf-8")
    pre_meta = ThreadMeta.model_validate(yaml.safe_load(old_meta_text))

    if pre_meta.awaiting_from is None:
        logger.warning(
            "thread %s: toggle_awaiting_from called with awaiting_from=None (status=%s); skipping",
            layout.thread_id,
            pre_meta.status,
        )
        return

    to_participant = opposite_of(from_participant)
    if pre_meta.awaiting_from == to_participant:
        # Idempotent: already at target. Skip the redundant write and
        # avoid a tautology event in events.jsonl.
        return

    set_awaiting_from(layout, to_participant)
    log.append(
        AwaitingFromChanged(
            schema_version=1,
            event_id=new_ulid(),
            ts=datetime.now(UTC),
            thread_id=layout.thread_id,
            from_participant=pre_meta.awaiting_from,
            to_participant=to_participant,
        )
    )


__all__ = ["toggle_awaiting_from"]
