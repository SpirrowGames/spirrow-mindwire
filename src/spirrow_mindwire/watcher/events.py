"""Watcher-internal events (not the on-disk JSONL event log).

A :class:`ThreadEvent` is emitted by the filesystem observer for every
new ``*.md`` file that lands under ``threads/<ULID>/messages/``. The
dispatcher consumes these and (subject to dedup) triggers a Claude
Code invocation for the corresponding thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ThreadEvent:
    """A filesystem signal that a thread has new state to process."""

    thread_id: str
    """ULID of the thread (parsed from the path)."""
    seq: int
    """Sequence number of the message that triggered the event."""
    path: Path
    """Absolute path of the message file that was created / modified."""
    detected_at: datetime
    """UTC timestamp the observer captured the event at."""


__all__ = ["ThreadEvent"]
