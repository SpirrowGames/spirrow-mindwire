"""Tests for EventDedup (ADR-06 §4 I4, T13 PR-D)."""

from __future__ import annotations

import pytest

from spirrow_mindwire.dispatcher.dedup import DEFAULT_DEDUP_SET_SIZE, EventDedup


def test_mark_and_seen() -> None:
    d = EventDedup()
    assert not d.seen("e1")
    d.mark("e1")
    assert d.seen("e1")


def test_default_size_constant() -> None:
    assert DEFAULT_DEDUP_SET_SIZE == 1024


def test_eviction_past_max_size() -> None:
    d = EventDedup(max_size=2)
    d.mark("e1")
    d.mark("e2")
    d.mark("e3")  # evicts e1 (oldest)
    assert not d.seen("e1")
    assert d.seen("e2")
    assert d.seen("e3")


def test_remark_refreshes_recency() -> None:
    d = EventDedup(max_size=2)
    d.mark("e1")
    d.mark("e2")
    d.mark("e1")  # refresh e1 → e2 becomes oldest
    d.mark("e3")  # evicts e2
    assert d.seen("e1")
    assert not d.seen("e2")
    assert d.seen("e3")


def test_invalid_max_size() -> None:
    with pytest.raises(ValueError, match="max_size"):
        EventDedup(max_size=0)
