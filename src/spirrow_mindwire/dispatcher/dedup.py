"""Event dedup — ADR-2026-05-21-06 §4 I4 (T13 Step 2).

Bounded ULID-keyed dedup set. The dispatcher drops a ChatroomEvent whose
``event_id`` (a ULID) it has already processed, so adapters may assume
exactly-once delivery (I4). The bounded-``OrderedDict`` approach is adapted
from :class:`spirrow_mindwire.watcher.dedup.DedupCache` (main-approved
code-level reuse, msg-181); the key here is the event_id ULID and there is
**no TTL** — eviction is purely size-bounded.

``DEFAULT_DEDUP_SET_SIZE`` is the implementation-side default; ADR-06 §4 I4
deliberately does not fix the number, leaving it tunable from observation.
"""

from __future__ import annotations

from collections import OrderedDict

DEFAULT_DEDUP_SET_SIZE = 1024
"""Default bounded-set size for event dedup (ADR-06 §4 I4; tunable, not ADR-fixed)."""


class EventDedup:
    """Size-bounded set of seen ULID event_ids (oldest evicted past ``max_size``)."""

    def __init__(self, *, max_size: int = DEFAULT_DEDUP_SET_SIZE) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be positive, got {max_size}")
        self._max_size = max_size
        self._seen: OrderedDict[str, None] = OrderedDict()

    def seen(self, event_id: str) -> bool:
        """Return True if ``event_id`` was already marked."""
        return event_id in self._seen

    def mark(self, event_id: str) -> None:
        """Record ``event_id`` as seen, evicting the oldest entry past ``max_size``."""
        if event_id in self._seen:
            self._seen.move_to_end(event_id)
            return
        self._seen[event_id] = None
        if len(self._seen) > self._max_size:
            self._seen.popitem(last=False)


__all__ = ["DEFAULT_DEDUP_SET_SIZE", "EventDedup"]
