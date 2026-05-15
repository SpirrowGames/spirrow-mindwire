"""Tests for ``spirrow_mindwire.migration.v1_to_v2``.

Covers the design contracts from chatroom thread
``T-feat3-d1-schema-skeleton`` msg-120 §3 (D1-2):

- meta.yaml only is touched
- in-place rewrite via ``atomic_write_text``
- idempotent (already-v2 skipped, no rewrite)
- v2 pre-flight validation (broken payload → ``failed``)
- ``--dry-run`` plans without writing
- per-thread isolation (one failure does not abort the run)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.migration import MigrationReport, migrate_data_dir
from spirrow_mindwire.schema import ThreadMeta

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ULID_B = "01ARZ3NDEKTSV4RRFFQ69G5FBW"
ULID_C = "01ARZ3NDEKTSV4RRFFQ69G5FCX"
DEFAULT_TS = "2026-05-07T08:43:07Z"


def _v1_meta_payload(thread_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "thread_id": thread_id,
        "title": "",
        "status": "active",
        "awaiting_from": "claude-code",
        "participants": ["claude.ai", "claude-code"],
        "created_at": DEFAULT_TS,
        "updated_at": DEFAULT_TS,
        "tags": [],
        "retry_count": 0,
    }
    payload.update(overrides)
    return payload


def _seed_v1_thread(data_dir: Path, thread_id: str, **overrides: Any) -> Path:
    """Write a v1 meta.yaml at ``<data_dir>/threads/<thread_id>/meta.yaml``."""
    layout = ThreadDirLayout(base_dir=data_dir, thread_id=thread_id)
    layout.thread_dir.mkdir(parents=True, exist_ok=True)
    payload = _v1_meta_payload(thread_id, **overrides)
    layout.meta_path.write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return layout.meta_path


def _seed_v2_thread(data_dir: Path, thread_id: str) -> Path:
    layout = ThreadDirLayout(base_dir=data_dir, thread_id=thread_id)
    layout.thread_dir.mkdir(parents=True, exist_ok=True)
    payload = _v1_meta_payload(thread_id, schema_version=2)
    layout.meta_path.write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return layout.meta_path


# ---------- happy path ---------------------------------------------------


def test_migrate_v1_thread_writes_v2(tmp_path: Path) -> None:
    meta = _seed_v1_thread(tmp_path, ULID_A)

    report = migrate_data_dir(tmp_path)

    assert len(report.migrated) == 1
    assert report.migrated[0].thread_id == ULID_A
    assert report.has_failures is False

    written = yaml.safe_load(meta.read_text(encoding="utf-8"))
    assert written["schema_version"] == 2
    # Other fields are preserved as-is.
    assert written["thread_id"] == ULID_A
    assert written["status"] == "active"
    assert written["awaiting_from"] == "claude-code"


def test_migrated_meta_loads_through_thread_meta(tmp_path: Path) -> None:
    """A successful migration must produce a file the schema can parse."""
    meta = _seed_v1_thread(tmp_path, ULID_A)
    migrate_data_dir(tmp_path)

    parsed = ThreadMeta.model_validate(yaml.safe_load(meta.read_text(encoding="utf-8")))
    assert parsed.schema_version == 2
    assert parsed.thread_id == ULID_A


def test_migrate_multiple_threads(tmp_path: Path) -> None:
    _seed_v1_thread(tmp_path, ULID_A)
    _seed_v1_thread(tmp_path, ULID_B)
    _seed_v1_thread(tmp_path, ULID_C)

    report = migrate_data_dir(tmp_path)

    assert {o.thread_id for o in report.migrated} == {ULID_A, ULID_B, ULID_C}
    assert report.total_scanned == 3


# ---------- idempotency / skip logic -------------------------------------


def test_already_v2_is_skipped_not_rewritten(tmp_path: Path) -> None:
    meta = _seed_v2_thread(tmp_path, ULID_A)
    before_mtime = meta.stat().st_mtime_ns

    report = migrate_data_dir(tmp_path)

    assert len(report.migrated) == 0
    assert len(report.skipped_already_v2) == 1
    assert report.skipped_already_v2[0].thread_id == ULID_A
    # File is untouched (= true skip, not "rewrite identical bytes").
    assert meta.stat().st_mtime_ns == before_mtime


def test_second_run_is_no_op(tmp_path: Path) -> None:
    _seed_v1_thread(tmp_path, ULID_A)

    migrate_data_dir(tmp_path)  # 1st: migrates
    report = migrate_data_dir(tmp_path)  # 2nd: nothing to do

    assert len(report.migrated) == 0
    assert len(report.skipped_already_v2) == 1


def test_unknown_schema_version_is_skipped(tmp_path: Path) -> None:
    _seed_v1_thread(tmp_path, ULID_A, schema_version=99)

    report = migrate_data_dir(tmp_path)

    assert len(report.skipped_unknown_version) == 1
    assert "schema_version=99" in (report.skipped_unknown_version[0].detail or "")
    assert len(report.migrated) == 0
    assert len(report.failed) == 0


def test_missing_schema_version_is_skipped(tmp_path: Path) -> None:
    """`schema_version` absent → classified as ``skipped_unknown_version`` (= ``None``)."""
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    layout.thread_dir.mkdir(parents=True, exist_ok=True)
    payload = _v1_meta_payload(ULID_A)
    payload.pop("schema_version")
    layout.meta_path.write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    report = migrate_data_dir(tmp_path)

    assert len(report.skipped_unknown_version) == 1


# ---------- dry-run ------------------------------------------------------


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    meta = _seed_v1_thread(tmp_path, ULID_A)
    before_mtime = meta.stat().st_mtime_ns

    report = migrate_data_dir(tmp_path, dry_run=True)

    # Plan classifies as migrated (= would be), but file is untouched.
    assert len(report.migrated) == 1
    assert report.migrated[0].detail == "dry-run"
    assert meta.stat().st_mtime_ns == before_mtime
    assert yaml.safe_load(meta.read_text(encoding="utf-8"))["schema_version"] == 1


def test_dry_run_skips_already_v2_too(tmp_path: Path) -> None:
    _seed_v2_thread(tmp_path, ULID_A)
    report = migrate_data_dir(tmp_path, dry_run=True)
    assert len(report.skipped_already_v2) == 1
    assert len(report.migrated) == 0


# ---------- failure modes ------------------------------------------------


def test_invalid_yaml_is_failed(tmp_path: Path) -> None:
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    layout.thread_dir.mkdir(parents=True, exist_ok=True)
    layout.meta_path.write_text("schema_version: 1\nnot: [closed", encoding="utf-8")

    report = migrate_data_dir(tmp_path)

    assert len(report.failed) == 1
    assert "yaml parse error" in (report.failed[0].detail or "")
    assert report.has_failures is True


def test_non_mapping_is_failed(tmp_path: Path) -> None:
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    layout.thread_dir.mkdir(parents=True, exist_ok=True)
    layout.meta_path.write_text("- just\n- a\n- list\n", encoding="utf-8")

    report = migrate_data_dir(tmp_path)

    assert len(report.failed) == 1
    assert "not a mapping" in (report.failed[0].detail or "")


def test_validation_failure_does_not_write(tmp_path: Path) -> None:
    """A v1 payload that breaks ThreadMeta(v2) is recorded as failed and the
    file is left untouched (= no half-migrated state)."""
    meta = _seed_v1_thread(tmp_path, ULID_A, participants=[])
    raw_before = meta.read_text(encoding="utf-8")

    report = migrate_data_dir(tmp_path)

    assert len(report.failed) == 1
    assert "validation failed" in (report.failed[0].detail or "")
    assert meta.read_text(encoding="utf-8") == raw_before


def test_failure_in_one_thread_does_not_abort_others(tmp_path: Path) -> None:
    _seed_v1_thread(tmp_path, ULID_A)
    # broken yaml in the middle thread
    layout_bad = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_B)
    layout_bad.thread_dir.mkdir(parents=True, exist_ok=True)
    layout_bad.meta_path.write_text("schema_version: 1\nbroken: [", encoding="utf-8")
    _seed_v1_thread(tmp_path, ULID_C)

    report = migrate_data_dir(tmp_path)

    assert {o.thread_id for o in report.migrated} == {ULID_A, ULID_C}
    assert {o.thread_id for o in report.failed} == {ULID_B}


# ---------- edge cases ---------------------------------------------------


def test_no_threads_dir_returns_empty_report(tmp_path: Path) -> None:
    report = migrate_data_dir(tmp_path)
    assert isinstance(report, MigrationReport)
    assert report.total_scanned == 0
    assert report.has_failures is False


def test_staging_dir_is_skipped(tmp_path: Path) -> None:
    """A staging dir (`.staging-<ULID>/`) must not be touched even if it
    happens to contain a meta.yaml (e.g. a crashed thread-creation in
    flight). The migration script only operates on settled thread dirs."""
    _seed_v1_thread(tmp_path, ULID_A)
    staging = tmp_path / "threads" / f".staging-{ULID_B}"
    staging.mkdir(parents=True)
    (staging / "meta.yaml").write_text(
        yaml.safe_dump(_v1_meta_payload(ULID_B), default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    report = migrate_data_dir(tmp_path)

    assert [o.thread_id for o in report.migrated] == [ULID_A]
    # Staging meta still v1: untouched.
    parsed = yaml.safe_load((staging / "meta.yaml").read_text(encoding="utf-8"))
    assert parsed["schema_version"] == 1


def test_empty_thread_dir_is_skipped(tmp_path: Path) -> None:
    """A thread dir without a meta.yaml (operator-created stub, etc.) is
    skipped silently — no failure, no entry."""
    (tmp_path / "threads" / ULID_A).mkdir(parents=True)

    report = migrate_data_dir(tmp_path)

    assert report.total_scanned == 0


def test_messages_and_events_are_not_touched(tmp_path: Path) -> None:
    """D1-1 = A scope: only meta.yaml flips. Message frontmatter and event
    log entries keep their independent ``schema_version`` literals."""
    meta = _seed_v1_thread(tmp_path, ULID_A)
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    layout.messages_dir.mkdir(parents=True, exist_ok=True)
    msg_path = layout.message_path(1, "claude.ai")
    msg_path.write_text(
        "---\nschema_version: 1\nmsg_id: x/001\n---\nbody\n",
        encoding="utf-8",
    )
    log_path = layout.event_log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('{"schema_version": 1, "type": "thread.created"}\n', encoding="utf-8")

    migrate_data_dir(tmp_path)

    assert yaml.safe_load(meta.read_text(encoding="utf-8"))["schema_version"] == 2
    assert "schema_version: 1" in msg_path.read_text(encoding="utf-8")
    assert '"schema_version": 1' in log_path.read_text(encoding="utf-8")
