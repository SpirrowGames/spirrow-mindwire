"""Time-bounded deduplication of ``(thread_id, seq)`` events.

watchdog can emit the same event twice (e.g. a write followed by a
metadata update on the same file) within milliseconds. T06 Q4 picks a
short TTL window — 5 seconds by default — over content hashing to keep
the dedup logic O(1) and not introduce read-on-detect latency.

The cache is **not** thread-safe; callers serialize lookups through
the asyncio event loop.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
from typing import NamedTuple


class _Key(NamedTuple):
    thread_id: str
    seq: int


class DedupCache:
    """In-memory ``(thread_id, seq) -> last_seen_at`` with TTL eviction.

    Bounded size keeps long-running watchers from leaking memory if the
    same thread receives huge numbers of events; oldest entries are
    evicted past ``max_size``.
    """

    def __init__(self, ttl: timedelta, *, max_size: int = 4096) -> None:
        if ttl <= timedelta(0):
            raise ValueError(f"ttl must be positive, got {ttl}")
        if max_size < 1:
            raise ValueError(f"max_size must be positive, got {max_size}")
        self._ttl = ttl
        self._max_size = max_size
        self._seen: OrderedDict[_Key, datetime] = OrderedDict()

    def seen_recently(self, thread_id: str, seq: int, now: datetime) -> bool:
        """Return True if ``(thread_id, seq)`` was marked within ``ttl``."""
        key = _Key(thread_id, seq)
        last = self._seen.get(key)
        if last is None:
            return False
        if now - last > self._ttl:
            # expired — drop and report unseen
            del self._seen[key]
            return False
        return True

    def mark(self, thread_id: str, seq: int, now: datetime) -> None:
        """Record ``(thread_id, seq)`` as seen at ``now``."""
        key = _Key(thread_id, seq)
        if key in self._seen:
            self._seen.move_to_end(key)
        self._seen[key] = now
        if len(self._seen) > self._max_size:
            self._seen.popitem(last=False)


__all__ = ["DedupCache"]
