"""Tests for :mod:`spirrow_mindwire.filesystem`.

Covers atomic writes, on-disk path computation, and JSONL event-log
append behavior.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from spirrow_mindwire.filesystem import (
    EventLogWriter,
    ThreadDirLayout,
    atomic_write_text,
)
from spirrow_mindwire.schema import (
    MessageReceived,
    ThreadCreated,
    ThreadStatusChanged,
)

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ULID_E1 = "01ARZ3NDEKTSV4RRFFQ69G5FB1"
ULID_E2 = "01ARZ3NDEKTSV4RRFFQ69G5FB2"
NOW = datetime(2026, 5, 7, 8, 43, 7, tzinfo=UTC)


# ---------- atomic_write_text -------------------------------------------


def test_atomic_write_creates_file_with_content(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "out.txt"
    atomic_write_text(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "v1")
    atomic_write_text(target, "v2")
    assert target.read_text(encoding="utf-8") == "v2"


def test_atomic_write_leaves_no_tmp_on_success(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "ok")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_atomic_write_cleans_up_tmp_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    with (
        patch(
            "spirrow_mindwire.filesystem.atomic.os.replace",
            side_effect=OSError("boom"),
        ),
        pytest.raises(OSError, match="boom"),
    ):
        atomic_write_text(target, "data")
    assert not target.exists()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_atomic_write_calls_fsync_by_default(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    with patch("spirrow_mindwire.filesystem.atomic.os.fsync") as mock_fsync:
        atomic_write_text(target, "ok")
    mock_fsync.assert_called_once()


def test_atomic_write_skips_fsync_when_disabled(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    with patch("spirrow_mindwire.filesystem.atomic.os.fsync") as mock_fsync:
        atomic_write_text(target, "ok", fsync=False)
    mock_fsync.assert_not_called()
    assert target.read_text(encoding="utf-8") == "ok"


# ---------- ThreadDirLayout ---------------------------------------------


def test_thread_dir_paths(tmp_path: Path) -> None:
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    assert layout.thread_dir == tmp_path / "threads" / ULID_A
    assert layout.staging_dir == tmp_path / "threads" / f".staging-{ULID_A}"
    assert layout.meta_path == tmp_path / "threads" / ULID_A / "meta.yaml"
    assert layout.messages_dir == tmp_path / "threads" / ULID_A / "messages"
    assert (
        layout.event_log_path
        == tmp_path / "logs" / "threads" / f"{ULID_A}.jsonl"
    )


def test_thread_dir_staging_paths_mirror_final(tmp_path: Path) -> None:
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    assert layout.staging_meta_path == layout.staging_dir / "meta.yaml"
    assert layout.staging_messages_dir == layout.staging_dir / "messages"


@pytest.mark.parametrize(
    ("seq", "expected"),
    [
        (1, "001-from-cai.md"),
        (42, "042-from-cai.md"),
        (999, "999-from-cai.md"),
        (1000, "1000-from-cai.md"),
        (12345, "12345-from-cai.md"),
    ],
)
def test_message_filename_padding(seq: int, expected: str, tmp_path: Path) -> None:
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    assert layout.message_path(seq, "claude.ai").name == expected


def test_message_filename_sender_codes(tmp_path: Path) -> None:
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    assert layout.message_path(1, "claude.ai").name == "001-from-cai.md"
    assert layout.message_path(1, "claude-code").name == "001-from-cc.md"


def test_message_path_rejects_seq_below_one(tmp_path: Path) -> None:
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    with pytest.raises(ValueError, match="seq"):
        layout.message_path(0, "claude.ai")


def test_message_path_rejects_unknown_sender(tmp_path: Path) -> None:
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    with pytest.raises(ValueError, match="sender"):
        layout.message_path(1, "gpt")  # type: ignore[arg-type]


def test_thread_dir_rejects_path_traversal_in_thread_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ULID"):
        ThreadDirLayout(base_dir=tmp_path, thread_id="../etc")


def test_thread_dir_rejects_non_ulid_thread_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ULID"):
        ThreadDirLayout(base_dir=tmp_path, thread_id="not-a-ulid")


# ---------- EventLogWriter ----------------------------------------------


def test_event_log_append_writes_one_line(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "threads" / f"{ULID_A}.jsonl"
    writer = EventLogWriter(log_path)
    writer.append(
        ThreadCreated(
            schema_version=1,
            event_id=ULID_E1,
            ts=NOW,
            thread_id=ULID_A,
            title="t",
        )
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["type"] == "thread.created"
    assert parsed["title"] == "t"
    assert parsed["thread_id"] == ULID_A


def test_event_log_append_multiple_events(tmp_path: Path) -> None:
    log_path = tmp_path / f"{ULID_A}.jsonl"
    writer = EventLogWriter(log_path)
    writer.append(
        ThreadCreated(
            schema_version=1,
            event_id=ULID_E1,
            ts=NOW,
            thread_id=ULID_A,
            title="t",
        )
    )
    writer.append(
        ThreadStatusChanged(
            schema_version=1,
            event_id=ULID_E2,
            ts=NOW,
            thread_id=ULID_A,
            from_status="active",
            to_status="awaiting-cc",
        )
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "thread.created"
    assert json.loads(lines[1])["type"] == "thread.status.changed"


def test_event_log_uses_from_alias_for_message_events(tmp_path: Path) -> None:
    log_path = tmp_path / f"{ULID_A}.jsonl"
    writer = EventLogWriter(log_path)
    writer.append(
        MessageReceived.model_validate(
            {
                "schema_version": 1,
                "event_id": ULID_E1,
                "ts": NOW.isoformat(),
                "thread_id": ULID_A,
                "seq": 1,
                "size_bytes": 1234,
                "from": "claude.ai",
            }
        )
    )

    parsed = json.loads(log_path.read_text(encoding="utf-8"))
    assert parsed["from"] == "claude.ai"
    assert "from_" not in parsed


def test_event_log_creates_parent_dir(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "threads" / f"{ULID_A}.jsonl"
    assert not log_path.parent.exists()
    EventLogWriter(log_path).append(
        ThreadCreated(
            schema_version=1, event_id=ULID_E1, ts=NOW, thread_id=ULID_A
        )
    )
    assert log_path.exists()
