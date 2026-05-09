"""Tests for :mod:`spirrow_mindwire.watcher.loader`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.watcher.loader import load_messages, load_thread_meta

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
NOW = datetime(2026, 5, 7, 8, 43, 7, tzinfo=UTC)


def _layout(base: Path) -> ThreadDirLayout:
    return ThreadDirLayout(base_dir=base, thread_id=ULID_A)


def _write_meta(layout: ThreadDirLayout, **overrides: object) -> None:
    layout.thread_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": 1,
        "thread_id": ULID_A,
        "title": "test",
        "status": "awaiting-cc",
        "participants": ["claude.ai", "claude-code"],
        "created_at": "2026-05-07T08:43:07Z",
        "updated_at": "2026-05-07T08:43:07Z",
        "tags": [],
    }
    payload.update(overrides)
    layout.meta_path.write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _write_message(
    layout: ThreadDirLayout, seq: int, sender: str, body: str, **overrides: object
) -> None:
    layout.messages_dir.mkdir(parents=True, exist_ok=True)
    fm: dict[str, object] = {
        "schema_version": 1,
        "msg_id": f"{ULID_A}/{seq:03d}",
        "seq": seq,
        "from": sender,
        "to": "claude-code" if sender == "claude.ai" else "claude.ai",
        "created_at": "2026-05-07T08:43:07Z",
    }
    fm.update(overrides)
    target = layout.message_path(seq, sender)  # type: ignore[arg-type]
    target.write_text(
        f"---\n{yaml.safe_dump(fm, default_flow_style=False, sort_keys=False)}---\n\n{body}\n",
        encoding="utf-8",
    )


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
