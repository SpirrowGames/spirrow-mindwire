"""Tests for spirrow_mindwire.watcher.startup_scan.

Verifies state-based recovery: ``active`` / ``retrying`` threads are
re-queued at startup, terminal states are skipped, and corrupt /
incomplete thread directories are handled defensively.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from factories import seed_thread_meta, write_message_file

from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.schema import Message
from spirrow_mindwire.watcher.events import ThreadEvent
from spirrow_mindwire.watcher.startup_scan import _detect_race_gap, startup_full_scan

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ULID_B = "01HRZ3NDEKTSV4RRFFQ69G5FAW"
ULID_C = "01HRZ3NDEKTSV4RRFFQ69G5FAX"

NOW = datetime(2026, 5, 7, 8, 43, 7, tzinfo=UTC)


def _seed(base_dir: Path, ulid: str, **overrides: object) -> ThreadDirLayout:
    layout = ThreadDirLayout(base_dir=base_dir, thread_id=ulid)
    seed_thread_meta(layout, **overrides)
    return layout


def test_active_thread_is_enqueued(tmp_path: Path) -> None:
    layout = _seed(tmp_path, ULID_A, status="active", awaiting_from="claude-code")
    write_message_file(layout, seq=1, sender="claude.ai", atomic=False)

    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    n = startup_full_scan(tmp_path, queue, now=NOW)
    assert n == 1

    evt = queue.get_nowait()
    assert evt.thread_id == ULID_A
    assert evt.seq == 1
    assert evt.path.name == "001-from-cai.md"
    assert evt.detected_at == NOW


def test_retrying_thread_is_enqueued(tmp_path: Path) -> None:
    layout = _seed(
        tmp_path,
        ULID_A,
        status="retrying",
        awaiting_from="claude-code",
        retry_count=1,
    )
    write_message_file(layout, seq=1, sender="claude.ai", atomic=False)

    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    n = startup_full_scan(tmp_path, queue, now=NOW)
    assert n == 1


def test_terminal_states_are_skipped(tmp_path: Path) -> None:
    """terminated / resolved / archived threads は queue に push されない."""
    layout_a = _seed(
        tmp_path,
        ULID_A,
        status="terminated",
        awaiting_from=None,
        terminated_reason="retry-exhausted",
        terminated_at="2026-05-07T08:43:07Z",
    )
    write_message_file(layout_a, seq=1, sender="claude.ai", atomic=False)

    layout_b = _seed(tmp_path, ULID_B, status="resolved", awaiting_from=None)
    write_message_file(layout_b, seq=1, sender="claude.ai", atomic=False)

    layout_c = _seed(tmp_path, ULID_C, status="archived", awaiting_from=None)
    write_message_file(layout_c, seq=1, sender="claude.ai", atomic=False)

    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    n = startup_full_scan(tmp_path, queue, now=NOW)
    assert n == 0
    assert queue.empty()


def test_no_threads_root_returns_zero(tmp_path: Path) -> None:
    """fresh install: threads/ が存在しない (no-op)."""
    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    n = startup_full_scan(tmp_path, queue)
    assert n == 0


def test_staging_dir_skipped(tmp_path: Path) -> None:
    """``.staging-<ULID>/`` は dot-prefix で skip される."""
    threads_root = tmp_path / "threads"
    staging = threads_root / f".staging-{ULID_A}"
    staging.mkdir(parents=True)
    (staging / "meta.yaml").write_text("incomplete")

    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    n = startup_full_scan(tmp_path, queue)
    assert n == 0


def test_invalid_meta_skipped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    threads_root = tmp_path / "threads"
    bad_dir = threads_root / ULID_A
    bad_dir.mkdir(parents=True)
    (bad_dir / "meta.yaml").write_text("not yaml: {{{")

    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    with caplog.at_level(logging.WARNING, logger="spirrow_mindwire.watcher.startup_scan"):
        n = startup_full_scan(tmp_path, queue)
    assert n == 0
    assert any("failed to load meta.yaml" in r.message for r in caplog.records)


def test_active_thread_no_messages_skipped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """active / retrying で messages が空 (= 通常発生しない) は warning + skip."""
    _seed(tmp_path, ULID_A, status="active", awaiting_from="claude-code")
    # No write_message_file — messages dir is missing entirely.

    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    with caplog.at_level(logging.WARNING, logger="spirrow_mindwire.watcher.startup_scan"):
        n = startup_full_scan(tmp_path, queue)
    assert n == 0
    assert any("no messages but status=" in r.message for r in caplog.records)


def test_non_ulid_dir_skipped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    threads_root = tmp_path / "threads"
    non_ulid = threads_root / "not-a-ulid"
    non_ulid.mkdir(parents=True)

    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    with caplog.at_level(logging.WARNING, logger="spirrow_mindwire.watcher.startup_scan"):
        n = startup_full_scan(tmp_path, queue)
    assert n == 0
    assert any("skipping non-ULID dir" in r.message for r in caplog.records)


def test_mix_of_states_only_active_and_retrying_enqueued(tmp_path: Path) -> None:
    """active + retrying + terminated 混在で 2 件のみ enqueue."""
    layout_a = _seed(tmp_path, ULID_A, status="active", awaiting_from="claude-code")
    write_message_file(layout_a, seq=1, sender="claude.ai", atomic=False)

    layout_b = _seed(tmp_path, ULID_B, status="retrying", awaiting_from="claude-code")
    write_message_file(layout_b, seq=1, sender="claude.ai", atomic=False)

    layout_c = _seed(
        tmp_path,
        ULID_C,
        status="terminated",
        awaiting_from=None,
        terminated_reason="retry-exhausted",
        terminated_at="2026-05-07T08:43:07Z",
    )
    write_message_file(layout_c, seq=1, sender="claude.ai", atomic=False)

    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    n = startup_full_scan(tmp_path, queue, now=NOW)
    assert n == 2

    enqueued_ids = {queue.get_nowait().thread_id for _ in range(2)}
    assert enqueued_ids == {ULID_A, ULID_B}


# ---------------------------------------------------------------------------
# D3-1 option-a: race-gap observation (Feature 3-A sub-PR 3)
# ---------------------------------------------------------------------------


def _msg(seq: int, sender: str) -> Message:
    to = "claude-code" if sender == "claude.ai" else "claude.ai"
    return Message.model_validate(
        {
            "schema_version": 1,
            "msg_id": f"{ULID_A}/{seq:03d}",
            "seq": seq,
            "from": sender,
            "to": to,
            "created_at": "2026-05-07T08:43:07Z",
            "body": "x",
        }
    )


def test_detect_race_gap_none_for_contiguous() -> None:
    msgs = [_msg(1, "claude.ai"), _msg(2, "claude-code"), _msg(3, "claude.ai")]
    assert _detect_race_gap(msgs) is None


def test_detect_race_gap_none_for_empty() -> None:
    assert _detect_race_gap([]) is None


def test_detect_race_gap_duplicate_seq() -> None:
    """Two writers computed the same next_seq → both files survive (race signature)."""
    msgs = sorted(
        [_msg(1, "claude.ai"), _msg(2, "claude-code"), _msg(2, "claude.ai")],
        key=lambda m: m.seq,
    )
    reason = _detect_race_gap(msgs)
    assert reason is not None
    assert "duplicate_seq=[2]" in reason


def test_detect_race_gap_seq_hole() -> None:
    """A missing seq (lost write) shows up as a compact (lo, hi) range."""
    msgs = [_msg(1, "claude.ai"), _msg(3, "claude.ai")]
    reason = _detect_race_gap(msgs)
    assert reason is not None
    assert "seq_hole missing_ranges=[(2, 2)]" in reason


def test_detect_race_gap_wide_hole_is_compact() -> None:
    """A huge gap is reported as one range, never enumerated int-by-int."""
    msgs = [_msg(1, "claude.ai"), _msg(1_000_000, "claude.ai")]
    reason = _detect_race_gap(msgs)
    assert reason == "seq_hole missing_ranges=[(2, 999999)]"


def test_summary_metric_emitted_no_anomaly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    layout = _seed(tmp_path, ULID_A, status="active", awaiting_from="claude-code")
    write_message_file(layout, seq=1, sender="claude.ai", atomic=False)

    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.watcher.startup_scan"):
        startup_full_scan(tmp_path, queue, now=NOW)

    summary = [r.message for r in caplog.records if "race-gap summary" in r.message]
    assert len(summary) == 1
    assert "scanned=1 gap_detected=0 gap_rate=0.000 anomalies=[]" in summary[0]


def test_duplicate_seq_thread_still_enqueued_and_counted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Race-gap is observation-only: the thread is still requeued normally."""
    layout = _seed(tmp_path, ULID_A, status="active", awaiting_from="claude-code")
    # Two files at seq 2 with different `from` suffix = cross-process race trace.
    write_message_file(layout, seq=1, sender="claude.ai", atomic=False)
    write_message_file(layout, seq=2, sender="claude.ai", atomic=False)
    write_message_file(layout, seq=2, sender="claude-code", atomic=False)

    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.watcher.startup_scan"):
        n = startup_full_scan(tmp_path, queue, now=NOW)

    # Still enqueued (observation does not gate recovery).
    assert n == 1
    summary = [r.message for r in caplog.records if "race-gap summary" in r.message]
    assert len(summary) == 1
    assert "scanned=1 gap_detected=1" in summary[0]
    assert any("race-gap anomaly on" in r.message for r in caplog.records)
