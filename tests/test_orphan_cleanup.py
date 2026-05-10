"""Tests for spirrow_mindwire.watcher.orphan_cleanup.

Verifies the age-threshold semantics: orphan ``.tmp`` files older than
``orphan_tmp_cleanup_age_seconds`` are deleted, while recent ``.tmp``s
(potentially in-flight writes) are preserved.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from spirrow_mindwire.watcher.orphan_cleanup import cleanup_orphan_tmp

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ULID_B = "01HRZ3NDEKTSV4RRFFQ69G5FAW"


def _set_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def _make_tmp(threads_root: Path, ulid: str, name: str, mtime: float) -> Path:
    msgs = threads_root / ulid / "messages"
    msgs.mkdir(parents=True, exist_ok=True)
    p = msgs / name
    p.write_text("orphan content")
    _set_mtime(p, mtime)
    return p


def test_no_threads_root_returns_zero(tmp_path: Path) -> None:
    threads_root = tmp_path / "threads"
    assert cleanup_orphan_tmp(threads_root, age_threshold_seconds=300.0) == 0


def test_empty_threads_root_returns_zero(tmp_path: Path) -> None:
    threads_root = tmp_path / "threads"
    threads_root.mkdir()
    assert cleanup_orphan_tmp(threads_root, age_threshold_seconds=300.0) == 0


def test_deletes_old_tmp_files(tmp_path: Path) -> None:
    threads_root = tmp_path / "threads"
    now = 10_000.0
    old = _make_tmp(threads_root, ULID_A, "001-from-cai.md.tmp", mtime=now - 1000.0)

    deleted = cleanup_orphan_tmp(threads_root, age_threshold_seconds=300.0, now_seconds=now)
    assert deleted == 1
    assert not old.exists()


def test_preserves_recent_tmp_files(tmp_path: Path) -> None:
    """Age < threshold: in-flight writes must survive."""
    threads_root = tmp_path / "threads"
    now = 10_000.0
    fresh = _make_tmp(threads_root, ULID_A, "001-from-cai.md.tmp", mtime=now - 10.0)

    deleted = cleanup_orphan_tmp(threads_root, age_threshold_seconds=300.0, now_seconds=now)
    assert deleted == 0
    assert fresh.exists()


def test_age_threshold_boundary(tmp_path: Path) -> None:
    """``mtime == cutoff`` is preserved (>=); ``mtime < cutoff`` is deleted."""
    threads_root = tmp_path / "threads"
    now = 10_000.0
    age_threshold = 300.0
    cutoff = now - age_threshold

    at_boundary = _make_tmp(threads_root, ULID_A, "001-from-cai.md.tmp", mtime=cutoff)
    just_under = _make_tmp(threads_root, ULID_B, "001-from-cai.md.tmp", mtime=cutoff - 0.001)

    deleted = cleanup_orphan_tmp(threads_root, age_threshold_seconds=age_threshold, now_seconds=now)
    assert deleted == 1
    assert at_boundary.exists()  # mtime == cutoff → preserved
    assert not just_under.exists()  # mtime < cutoff → deleted


def test_only_tmp_extension_is_targeted(tmp_path: Path) -> None:
    """``.md`` and other extensions are untouched even when old."""
    threads_root = tmp_path / "threads"
    now = 10_000.0
    msgs = threads_root / ULID_A / "messages"
    msgs.mkdir(parents=True)

    md_path = msgs / "001-from-cai.md"
    md_path.write_text("real message")
    _set_mtime(md_path, now - 1000.0)

    tmp_path_orphan = msgs / "001-from-cai.md.tmp"
    tmp_path_orphan.write_text("orphan")
    _set_mtime(tmp_path_orphan, now - 1000.0)

    deleted = cleanup_orphan_tmp(threads_root, age_threshold_seconds=300.0, now_seconds=now)
    assert deleted == 1
    assert md_path.exists()
    assert not tmp_path_orphan.exists()


def test_multiple_threads_swept(tmp_path: Path) -> None:
    threads_root = tmp_path / "threads"
    now = 10_000.0
    p1 = _make_tmp(threads_root, ULID_A, "001-from-cai.md.tmp", mtime=now - 1000.0)
    p2 = _make_tmp(threads_root, ULID_B, "002-from-cc.md.tmp", mtime=now - 1000.0)

    deleted = cleanup_orphan_tmp(threads_root, age_threshold_seconds=300.0, now_seconds=now)
    assert deleted == 2
    assert not p1.exists()
    assert not p2.exists()


def test_logs_deletion_at_info_level(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    threads_root = tmp_path / "threads"
    now = 10_000.0
    _make_tmp(threads_root, ULID_A, "001-from-cai.md.tmp", mtime=now - 1000.0)

    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.watcher.orphan_cleanup"):
        cleanup_orphan_tmp(threads_root, age_threshold_seconds=300.0, now_seconds=now)

    assert any("orphan_cleanup: deleted" in record.message for record in caplog.records)


def test_unrelated_dir_layout_ignored(tmp_path: Path) -> None:
    """``.tmp`` files outside ``<ULID>/messages/`` glob pattern are ignored."""
    threads_root = tmp_path / "threads"
    now = 10_000.0
    threads_root.mkdir()

    # Top-level .tmp (not under messages/)
    top_tmp = threads_root / "stray.tmp"
    top_tmp.write_text("stray")
    _set_mtime(top_tmp, now - 1000.0)

    # Under thread dir but not under messages/
    other = threads_root / ULID_A / "stray.tmp"
    other.parent.mkdir(parents=True)
    other.write_text("stray2")
    _set_mtime(other, now - 1000.0)

    deleted = cleanup_orphan_tmp(threads_root, age_threshold_seconds=300.0, now_seconds=now)
    assert deleted == 0
    assert top_tmp.exists()
    assert other.exists()


def test_unlink_failure_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One bad entry doesn't abort the whole sweep (logs WARNING, returns count of successes)."""
    threads_root = tmp_path / "threads"
    now = 10_000.0
    p1 = _make_tmp(threads_root, ULID_A, "001-from-cai.md.tmp", mtime=now - 1000.0)
    p2 = _make_tmp(threads_root, ULID_B, "002-from-cc.md.tmp", mtime=now - 1000.0)

    real_unlink = Path.unlink

    def fake_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == p1:
            raise PermissionError("simulated lock")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    with caplog.at_level(logging.WARNING, logger="spirrow_mindwire.watcher.orphan_cleanup"):
        deleted = cleanup_orphan_tmp(threads_root, age_threshold_seconds=300.0, now_seconds=now)

    # p1 fails (PermissionError), p2 succeeds; count = 1
    assert deleted == 1
    assert p1.exists()  # failed unlink leaves it
    assert not p2.exists()
    assert any("unlink failed" in record.message for record in caplog.records)


def test_staging_dir_orphan_tmp_is_preserved(tmp_path: Path) -> None:
    """``.staging-<ULID>/messages/*.tmp`` must NOT be touched.

    Staging dirs hold incomplete-thread state being assembled atomically
    (see thread_dir.py). Without the dot-prefix filter, ``Path.glob``
    would match dot-prefix dir names (Python's glob doesn't auto-skip
    dotfiles unlike POSIX shell), and we'd race with the in-progress
    staging logic.
    """
    threads_root = tmp_path / "threads"
    now = 10_000.0
    msgs = threads_root / f".staging-{ULID_A}" / "messages"
    msgs.mkdir(parents=True)
    staging_tmp = msgs / "001-from-cai.md.tmp"
    staging_tmp.write_text("staging-in-progress")
    _set_mtime(staging_tmp, now - 1000.0)  # would be deleted without filter

    deleted = cleanup_orphan_tmp(threads_root, age_threshold_seconds=300.0, now_seconds=now)
    assert deleted == 0
    assert staging_tmp.exists()
