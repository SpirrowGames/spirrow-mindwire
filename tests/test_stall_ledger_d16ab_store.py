"""D-16ab open-record store (T-stalled-pr-has-no-detector msg-4685 §2 + amendments).

Each test names the spec clause whose test list it implements.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.stall_ledger import store as store_mod
from spirrow_mindwire.stall_ledger.model import Unit, UnitKind
from spirrow_mindwire.stall_ledger.store import (
    StoreStatus,
    load_store,
    new_record_json,
    read_clear_requests,
    save_store,
    write_clear_request,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _record(identifier: str = "o/r#1", **over: Any) -> dict[str, Any]:
    unit = Unit(UnitKind.PR, identifier)
    raw = new_record_json(
        unit=unit,
        now=NOW - timedelta(days=1),
        klass="unclassified",
        flags=[],
        motion_at_open=NOW - timedelta(days=2),
    )
    raw.update(over)
    return raw


def _write(path: Path, data: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = (json.dumps(data) if not isinstance(data, str) else data).encode("utf-8")
    path.write_bytes(blob)
    return blob


def _corrupt_files(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


# ── msg-4685 §7-1 store matrix ────────────────────────────────────────────────────────


def test_absent_store(tmp_path: Path) -> None:
    loaded = load_store(tmp_path / "stall-ledger.json", NOW)
    assert loaded.status == StoreStatus.ABSENT
    assert loaded.envelope == {"schema_version": "1.0", "records": {}, "quarantined_records": {}}


def test_broken_json_is_corrupt_and_moved_aside(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.json"
    blob = _write(path, "{not json")
    loaded = load_store(path, NOW)
    assert loaded.status == StoreStatus.CORRUPT
    assert not path.exists()
    [moved] = _corrupt_files(path)
    assert moved.read_bytes() == blob
    assert loaded.records == {}


def test_valid_store(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.json"
    _write(path, {"schema_version": "1.0", "records": {"pr:o/r#1": _record()}})
    loaded = load_store(path, NOW)
    assert loaded.status == StoreStatus.VALID
    assert list(loaded.records) == ["pr:o/r#1"]


def test_interrupted_rename_leaves_previous_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "stall-ledger.json"
    before = _write(path, {"schema_version": "1.0", "records": {}})

    def boom(src: object, dst: object) -> None:
        raise OSError("power cut")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save_store(path, {"schema_version": "1.0", "records": {"x": 1}})
    assert path.read_bytes() == before


# ── msg-4687 §5: preservation ─────────────────────────────────────────────────────────


def test_round_trip_preserves_unknown_record_keys_and_remedy_fields(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.json"
    attempt = {
        "at": (NOW - timedelta(hours=3)).isoformat(),
        "kind": "gate-refire",
        "head_sha": "abc",
        "state": "completed",
        "emitted_event_ids": {"github_review_ids": ["review-X"], "chatroom_msg_ids": []},
        "outcome": "unknown",
        "future_field": {"nested": [1, 2]},
    }
    rec = _record(remedy_attempts=[attempt], ladder_stage="stage-2", phase2_extra="kept")
    data = {"schema_version": "1.0", "records": {"pr:o/r#1": rec}, "future_top": {"a": 1}}
    _write(path, data)
    loaded = load_store(path, NOW)
    assert loaded.envelope is not None
    save_store(path, loaded.envelope)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["records"] == data["records"]
    assert written["future_top"] == {"a": 1}
    assert written["quarantined_records"] == {}


def test_newer_minor_version_is_processed_normally(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.json"
    _write(path, {"schema_version": "1.7", "records": {"pr:o/r#1": _record(new_in_17=True)}})
    loaded = load_store(path, NOW)
    assert loaded.status == StoreStatus.VALID
    assert loaded.envelope is not None and loaded.envelope["schema_version"] == "1.7"


# ── msg-4691 §3-3: load order ─────────────────────────────────────────────────────────


def test_v2_store_without_v1_fields_is_version_ahead_and_untouched(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.json"
    rec = _record()
    del rec["stall_epoch_start"]
    blob = _write(path, {"schema_version": "2.0", "records": {"pr:o/r#1": rec}})
    loaded = load_store(path, NOW)
    assert loaded.status == StoreStatus.VERSION_AHEAD
    assert not loaded.writable
    assert path.read_bytes() == blob
    assert _corrupt_files(path) == []


@pytest.mark.parametrize(
    "data",
    [
        {"records": {}},
        {"schema_version": "one", "records": {}},
        {"schema_version": 1.0, "records": {}},
        {"schema_version": "0.9", "records": {}},
    ],
    ids=["missing", "unparseable", "not-a-string", "major-below-1"],
)
def test_unrecognized_version_halts_without_touching_file(tmp_path: Path, data: object) -> None:
    path = tmp_path / "stall-ledger.json"
    blob = _write(path, data)
    loaded = load_store(path, NOW)
    assert loaded.status == StoreStatus.VERSION_UNRECOGNIZED
    assert not loaded.writable
    assert path.read_bytes() == blob
    assert _corrupt_files(path) == []


def test_v1_field_validation_is_never_called_on_a_non_v1_major(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("v1 validation ran on a non-v1 store")

    monkeypatch.setattr(store_mod, "validate_v1_records", forbidden)
    monkeypatch.setattr(store_mod, "record_from_json", forbidden)
    for version in ("2.0", "0.1", "x"):
        path = tmp_path / f"{version}.json"
        _write(path, {"schema_version": version, "records": {"k": {"broken": True}}})
        assert load_store(path, NOW).status in (
            StoreStatus.VERSION_AHEAD,
            StoreStatus.VERSION_UNRECOGNIZED,
        )


# ── msg-4693 §4: envelope ─────────────────────────────────────────────────────────────


def test_valid_v1_with_schema_version_and_one_record_is_not_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.json"
    _write(path, {"schema_version": "1.0", "records": {"pr:o/r#1": _record()}})
    loaded = load_store(path, NOW)
    assert loaded.status == StoreStatus.VALID
    assert _corrupt_files(path) == []
    assert loaded.quarantine_events == []


def test_empty_ledger_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.json"
    _write(path, {"schema_version": "1.0", "records": {}})
    assert load_store(path, NOW).status == StoreStatus.VALID


@pytest.mark.parametrize("records", [None, [], "x"], ids=["missing", "list", "string"])
def test_records_missing_or_not_an_object_is_corrupt(tmp_path: Path, records: object) -> None:
    path = tmp_path / "stall-ledger.json"
    data: dict[str, Any] = {"schema_version": "1.0"}
    if records is not None:
        data["records"] = records
    _write(path, data)
    assert load_store(path, NOW).status == StoreStatus.CORRUPT
    assert len(_corrupt_files(path)) == 1


def test_unknown_top_level_key_survives(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.json"
    _write(path, {"schema_version": "1.0", "records": {}, "later": [1, {"x": None}]})
    loaded = load_store(path, NOW)
    assert loaded.envelope is not None
    save_store(path, loaded.envelope)
    assert json.loads(path.read_text(encoding="utf-8"))["later"] == [1, {"x": None}]


# ── msg-4695 §5 / msg-4697 §3: record quarantine ──────────────────────────────────────


def test_one_bad_record_of_three_is_quarantined_the_rest_load(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.json"
    bad = _record("o/r#2")
    del bad["evidence"]
    records = {"pr:o/r#1": _record("o/r#1"), "pr:o/r#2": bad, "pr:o/r#3": _record("o/r#3")}
    _write(path, {"schema_version": "1.0", "records": records})
    loaded = load_store(path, NOW)
    assert loaded.status == StoreStatus.VALID
    assert sorted(loaded.records) == ["pr:o/r#1", "pr:o/r#3"]
    assert loaded.quarantined["pr:o/r#2"]["reason"] == "missing_field:evidence"
    assert _corrupt_files(path) == []


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda r: r.pop("stall_epoch_start"), "missing_field:stall_epoch_start"),
        (lambda r: r.__setitem__("flags", "x"), "wrong_type:flags"),
        (lambda r: r["evidence"].pop("motion_at_open"), "missing_field:evidence.motion_at_open"),
        (
            lambda r: r.__setitem__("stall_epoch_start", "2026-01-01T00:00:00"),
            "wrong_type:stall_epoch_start",
        ),
    ],
)
def test_record_level_reasons(tmp_path: Path, mutate: Any, reason: str) -> None:
    path = tmp_path / "stall-ledger.json"
    rec = _record()
    mutate(rec)
    _write(path, {"schema_version": "1.0", "records": {"pr:o/r#1": rec}})
    loaded = load_store(path, NOW)
    assert loaded.quarantined["pr:o/r#1"]["reason"] == reason


def test_key_mismatch_quarantines_with_raw_identical(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.json"
    rec = _record("o/r#9")
    _write(path, {"schema_version": "1.0", "records": {"pr:o/r#1": rec}})
    loaded = load_store(path, NOW)
    entry = loaded.quarantined["pr:o/r#1"]
    assert entry["reason"] == "key_mismatch"
    assert entry["first_raw"] == rec
    assert entry["last_raw"] == rec


def test_same_unit_failing_100_times_is_one_entry(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.json"
    first = _record("o/r#9", marker=0)
    _write(path, {"schema_version": "1.0", "records": {"pr:o/r#1": first}})
    loaded = load_store(path, NOW)
    assert loaded.quarantine_events[0].first is True
    for i in range(1, 100):
        assert loaded.envelope is not None
        env = loaded.envelope
        env["records"]["pr:o/r#1"] = _record("o/r#9", marker=i)  # a buggy other writer
        save_store(path, env)
        loaded = load_store(path, NOW + timedelta(minutes=i))
        assert loaded.quarantine_events[0].first is False
        assert loaded.quarantine_repeats == 1
    entry = loaded.quarantined["pr:o/r#1"]
    assert len(loaded.quarantined) == 1
    assert entry["count"] == 100
    assert entry["first_raw"]["marker"] == 0
    assert entry["last_raw"]["marker"] == 99
    assert entry["first_detected_at"] == NOW.isoformat()


def test_quarantined_records_survive_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.json"
    q = {"pr:o/r#5": {"reason": "key_mismatch", "count": 3, "first_raw": {}, "last_raw": {}}}
    _write(path, {"schema_version": "1.0", "records": {}, "quarantined_records": q})
    loaded = load_store(path, NOW)
    assert loaded.envelope is not None
    save_store(path, loaded.envelope)
    assert json.loads(path.read_text(encoding="utf-8"))["quarantined_records"] == q


def test_quarantining_a_record_never_writes_quarantine_json(tmp_path: Path) -> None:
    qpath = tmp_path / "quarantine.json"
    qblob = _write(qpath, {"p/T-x": {"first_failure_at": NOW.isoformat()}})
    path = tmp_path / "stall-ledger.json"
    rec = _record("o/r#9")
    _write(path, {"schema_version": "1.0", "records": {"pr:o/r#1": rec}})
    loaded = load_store(path, NOW)
    assert loaded.envelope is not None
    save_store(path, loaded.envelope)
    assert qpath.read_bytes() == qblob


# ── msg-4699 §2 / §5: clear requests ──────────────────────────────────────────────────


def test_clear_request_writes_only_a_request_file(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.json"
    blob = _write(path, {"schema_version": "1.0", "records": {}})
    req_dir = tmp_path / "stall-ledger.requests"
    target = write_clear_request(req_dir, "pr:o/r#1", "alice", NOW)
    assert path.read_bytes() == blob
    assert target.parent == req_dir
    requests, invalid = read_clear_requests(req_dir)
    assert invalid == []
    assert [(r.unit_key, r.requested_by) for r in requests] == [("pr:o/r#1", "alice")]
    assert not list(req_dir.glob("*.tmp"))


def test_two_clear_requests_never_overwrite_each_other(tmp_path: Path) -> None:
    req_dir = tmp_path / "r"
    a = write_clear_request(req_dir, "k1", "a", NOW)
    b = write_clear_request(req_dir, "k2", "b", NOW)
    assert a != b
    assert len(read_clear_requests(req_dir)[0]) == 2


def test_save_is_single_file_rename(tmp_path: Path) -> None:
    path = tmp_path / "state" / "stall-ledger.json"
    save_store(path, {"schema_version": "1.0", "records": {}})
    assert os.listdir(path.parent) == ["stall-ledger.json"]
