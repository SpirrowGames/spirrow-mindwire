"""Tests for :mod:`spirrow_mindwire.watcher.loader`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from factories import seed_thread_meta, write_message_file

from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.watcher.loader import load_messages, load_thread_meta

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
NOW = datetime(2026, 5, 7, 8, 43, 7, tzinfo=UTC)


def _layout(base: Path) -> ThreadDirLayout:
    return ThreadDirLayout(base_dir=base, thread_id=ULID_A)


def _write_meta(layout: ThreadDirLayout, **overrides: object) -> None:
    """Seed meta with the loader-test default ``title='test'`` (overridable)."""
    payload: dict[str, object] = {"title": "test"}
    payload.update(overrides)
    seed_thread_meta(layout, **payload)


def _write_message(
    layout: ThreadDirLayout, seq: int, sender: str, body: str, **overrides: object
) -> None:
    write_message_file(layout, seq, sender, body, atomic=False, **overrides)


def test_load_thread_meta_round_trip(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_meta(layout, status="active")
    meta = load_thread_meta(layout)
    assert meta.thread_id == ULID_A
    assert meta.status == "active"
    assert meta.created_at == NOW


def test_load_messages_returns_seq_sorted(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_meta(layout)
    _write_message(layout, 2, "claude-code", "second")
    _write_message(layout, 1, "claude.ai", "first")
    msgs = load_messages(layout)
    assert [m.seq for m in msgs] == [1, 2]
    assert msgs[0].body == "first"
    assert msgs[1].body == "second"


def test_load_messages_skips_unknown_filenames(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_meta(layout)
    _write_message(layout, 1, "claude.ai", "real")
    layout.messages_dir.mkdir(parents=True, exist_ok=True)
    (layout.messages_dir / "README.md").write_text("not a message", encoding="utf-8")
    msgs = load_messages(layout)
    assert len(msgs) == 1
    assert msgs[0].seq == 1


def test_load_messages_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_meta(layout)
    # messages dir not created
    assert load_messages(layout) == []


def test_load_message_missing_frontmatter_raises(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write_meta(layout)
    layout.messages_dir.mkdir(parents=True, exist_ok=True)
    (layout.messages_dir / "001-from-cai.md").write_text("no frontmatter here", encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        load_messages(layout)
