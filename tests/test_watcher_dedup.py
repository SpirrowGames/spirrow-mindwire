"""Tests for :class:`spirrow_mindwire.watcher.dedup.DedupCache`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spirrow_mindwire.watcher.dedup import DedupCache

T0 = datetime(2026, 5, 7, 8, 43, 7, tzinfo=UTC)


def test_unmarked_event_is_not_seen() -> None:
    cache = DedupCache(ttl=timedelta(seconds=5))
    assert cache.seen_recently("t1", 1, T0) is False


def test_marked_event_is_seen_inside_ttl() -> None:
    cache = DedupCache(ttl=timedelta(seconds=5))
    cache.mark("t1", 1, T0)
    assert cache.seen_recently("t1", 1, T0 + timedelta(seconds=2)) is True


def test_marked_event_expires_after_ttl() -> None:
    cache = DedupCache(ttl=timedelta(seconds=5))
    cache.mark("t1", 1, T0)
    assert cache.seen_recently("t1", 1, T0 + timedelta(seconds=10)) is False


def test_remarking_event_extends_freshness() -> None:
    cache = DedupCache(ttl=timedelta(seconds=5))
    cache.mark("t1", 1, T0)
    cache.mark("t1", 1, T0 + timedelta(seconds=4))
    assert cache.seen_recently("t1", 1, T0 + timedelta(seconds=8)) is True


def test_distinct_threads_are_independent() -> None:
    cache = DedupCache(ttl=timedelta(seconds=5))
    cache.mark("t1", 1, T0)
    assert cache.seen_recently("t2", 1, T0) is False
    assert cache.seen_recently("t1", 2, T0) is False


def test_max_size_evicts_oldest() -> None:
    cache = DedupCache(ttl=timedelta(seconds=60), max_size=3)
    for i in range(5):
        cache.mark(f"t{i}", 1, T0 + timedelta(seconds=i))
    # The oldest two (t0, t1) should have been evicted.
    assert cache.seen_recently("t0", 1, T0 + timedelta(seconds=5)) is False
    assert cache.seen_recently("t1", 1, T0 + timedelta(seconds=5)) is False
    assert cache.seen_recently("t4", 1, T0 + timedelta(seconds=5)) is True


def test_zero_ttl_is_rejected() -> None:
    with pytest.raises(ValueError, match="ttl"):
        DedupCache(ttl=timedelta(seconds=0))
