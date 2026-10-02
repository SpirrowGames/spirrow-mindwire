"""Live snapshot of the tierc-v2 rules bytes (T-decider-tierc-v2-all-escalations).

Design: Bohr msg-4631 (save at hash time, from the same bytes), msg-4633 (a snapshot failure never
stops the conductor; the report re-hashes and excludes ``missing`` / ``mismatch``), msg-5130 (a
corrupt snapshot is moved aside to ``.corrupt-<UTC>`` and restored; fixed ``.tmp`` name unlinked
in ``finally``). Einstein approved the design in msg-5131.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.adapters import decider_lexora
from spirrow_mindwire.adapters.decider_lexora import build_decider, snapshot_tierc_rules
from spirrow_mindwire.config import MindwireSettings, resolve_tierc_rules_snapshot_dir
from spirrow_mindwire.decider.questions import load_tierc_rules, tierc_rules_template_path

TEMPLATE_BYTES = tierc_rules_template_path().read_bytes()
SHA = hashlib.sha256(TEMPLATE_BYTES).hexdigest()
FIXED_NOW = datetime(2026, 10, 1, 12, 34, 56, tzinfo=UTC)


def _rules_file(tmp_path: Path) -> Path:
    p = tmp_path / "tierc_rules.toml"
    p.write_bytes(TEMPLATE_BYTES)
    return p


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def _build(rules_path: Path, snapshot_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("MINDWIRE_DECIDER_BACKEND", raising=False)
    monkeypatch.setenv("MINDWIRE_LEXORA_URL", "http://lexora.test")
    return build_decider(
        config_backend="lexora",
        tierc_mode="shadow",
        rules_path=rules_path,
        snapshot_dir=snapshot_dir,
    )


# --------------------------------------------------------------------------- raw bytes


def test_rules_carry_the_exact_hashed_bytes(tmp_path: Path) -> None:
    rules = load_tierc_rules(_rules_file(tmp_path))
    assert rules.raw == TEMPLATE_BYTES
    assert hashlib.sha256(rules.raw).hexdigest() == rules.sha256 == SHA
    assert "raw" not in repr(rules)


def test_snapshot_writes_the_loaded_bytes_not_a_reread(tmp_path: Path) -> None:
    src = _rules_file(tmp_path)
    rules = load_tierc_rules(src)
    src.write_text("[[rule]]\nid = 'rule_1'\ntext = 'edited after load'\n", encoding="utf-8")
    snap = tmp_path / "snap"
    assert snapshot_tierc_rules(rules, snap) is True
    assert (snap / f"{SHA}.toml").read_bytes() == TEMPLATE_BYTES


# --------------------------------------------------------------------------- build_decider


def test_build_decider_saves_snapshot_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _rules_file(tmp_path)
    snap = tmp_path / "decider" / "rules"  # parents do not exist yet (mkdir -p)
    d = _build(src, snap, monkeypatch)
    assert d is not None and d.rules is not None and d.rules.sha256 == SHA
    dest = snap / f"{SHA}.toml"
    assert dest.read_bytes() == TEMPLATE_BYTES
    assert sorted(p.name for p in snap.iterdir()) == [f"{SHA}.toml"]

    # second start: an intact snapshot is not rewritten — any write would have to replace it.
    def _no_replace(*_a: Any, **_k: Any) -> None:
        raise AssertionError("an intact snapshot must not be rewritten")

    monkeypatch.setattr(os, "replace", _no_replace)
    assert _build(src, snap, monkeypatch) is not None
    assert dest.read_bytes() == TEMPLATE_BYTES


def test_unwritable_snapshot_dir_logs_error_and_still_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    src = _rules_file(tmp_path)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger=decider_lexora.logger.name):
        d = _build(src, blocker / "rules", monkeypatch)
    assert d is not None and d.rules is not None  # the conductor still starts (D20, msg-4633)
    errors = _errors(caplog)
    assert len(errors) == 1
    assert "snapshot failed" in errors[0] and SHA in errors[0]


def test_missing_rules_file_still_refuses_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="not readable"):
        _build(tmp_path / "missing.toml", tmp_path / "snap", monkeypatch)


# --------------------------------------------------------------------------- corrupt snapshot


def test_corrupt_snapshot_is_moved_aside_and_restored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rules = load_tierc_rules(_rules_file(tmp_path))
    snap = tmp_path / "snap"
    snap.mkdir()
    dest = snap / f"{SHA}.toml"
    bad = b"tampered\n"
    dest.write_bytes(bad)
    with caplog.at_level(logging.ERROR, logger=decider_lexora.logger.name):
        assert snapshot_tierc_rules(rules, snap, now=lambda: FIXED_NOW) is True
    evidence = snap / f"{SHA}.toml.corrupt-20261001T123456Z"
    assert evidence.read_bytes() == bad  # original bytes kept as evidence
    assert dest.read_bytes() == rules.raw  # restored from memory
    assert not (snap / f"{SHA}.toml.tmp").exists()
    errors = _errors(caplog)
    assert len(errors) == 1
    assert hashlib.sha256(bad).hexdigest() in errors[0] and str(evidence) in errors[0]


def test_second_corruption_in_the_same_second_keeps_earlier_evidence(tmp_path: Path) -> None:
    rules = load_tierc_rules(_rules_file(tmp_path))
    snap = tmp_path / "snap"
    snap.mkdir()
    dest = snap / f"{SHA}.toml"
    for bad in (b"first\n", b"second\n"):
        dest.write_bytes(bad)
        assert snapshot_tierc_rules(rules, snap, now=lambda: FIXED_NOW) is True
    base = snap / f"{SHA}.toml.corrupt-20261001T123456Z"
    assert base.read_bytes() == b"first\n"
    assert base.with_name(base.name + "-1").read_bytes() == b"second\n"


def test_move_aside_failure_leaves_corrupt_file_and_no_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rules = load_tierc_rules(_rules_file(tmp_path))
    snap = tmp_path / "snap"
    snap.mkdir()
    dest = snap / f"{SHA}.toml"
    dest.write_bytes(b"tampered\n")

    def _deny(*_a: Any, **_k: Any) -> None:
        raise PermissionError("rename denied")

    monkeypatch.setattr(os, "replace", _deny)
    with caplog.at_level(logging.ERROR, logger=decider_lexora.logger.name):
        assert snapshot_tierc_rules(rules, snap, now=lambda: FIXED_NOW) is False
    assert dest.read_bytes() == b"tampered\n"  # evidence never destroyed
    assert sorted(p.name for p in snap.iterdir()) == [f"{SHA}.toml"]  # no tmp, no evidence
    errors = _errors(caplog)
    assert len(errors) == 1 and "could not be moved aside" in errors[0]


# --------------------------------------------------------------------------- tmp lifecycle


def test_write_failure_leaves_no_tmp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rules = load_tierc_rules(_rules_file(tmp_path))
    snap = tmp_path / "snap"

    def _disk_full(_fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", _disk_full)
    with caplog.at_level(logging.ERROR, logger=decider_lexora.logger.name):
        assert snapshot_tierc_rules(rules, snap) is False
    assert list(snap.iterdir()) == []
    assert len(_errors(caplog)) == 1


def test_stale_tmp_from_a_killed_process_is_overwritten(tmp_path: Path) -> None:
    rules = load_tierc_rules(_rules_file(tmp_path))
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / f"{SHA}.toml.tmp").write_bytes(b"partial")
    assert snapshot_tierc_rules(rules, snap) is True
    assert sorted(p.name for p in snap.iterdir()) == [f"{SHA}.toml"]
    assert (snap / f"{SHA}.toml").read_bytes() == TEMPLATE_BYTES


def test_snapshot_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rules = load_tierc_rules(_rules_file(tmp_path))

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(Path, "mkdir", _boom)
    assert snapshot_tierc_rules(rules, tmp_path / "snap") is False


# --------------------------------------------------------------------------- config


def test_snapshot_dir_default_and_override(tmp_path: Path) -> None:
    s = MindwireSettings()
    assert s.decider.tierc.rules_snapshot_dir is None
    assert resolve_tierc_rules_snapshot_dir(s) == s.paths.data_dir / "decider" / "rules"
    s2 = MindwireSettings.model_validate(
        {"decider": {"tierc": {"rules_snapshot_dir": str(tmp_path / "snaps")}}}
    )
    assert resolve_tierc_rules_snapshot_dir(s2) == tmp_path / "snaps"


# --------------------------------------------------------------------------- report step (c)


def _load_report() -> Any:
    script = Path(__file__).resolve().parent.parent / "scripts" / "tierc_eval_report.py"
    spec = importlib.util.spec_from_file_location("tierc_eval_report_snapshot_test", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _lines(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_report_archives_snapshots_and_excludes_missing_and_mismatch(tmp_path: Path) -> None:
    report = _load_report()
    good = SHA
    missing = "0" * 64
    mismatch = "f" * 64
    live = tmp_path / "live"
    live.mkdir()
    (live / f"{good}.toml").write_bytes(TEMPLATE_BYTES)
    (live / f"{mismatch}.toml").write_bytes(b"not the hashed bytes\n")
    (live / f"{missing}.toml.tmp").write_bytes(TEMPLATE_BYTES)  # never read
    shas: dict[int, str | None] = {1: good, 2: missing, 3: mismatch, 4: mismatch, 5: None}
    fx = _lines(
        tmp_path / "fixture.jsonl",
        [{"thread_id": "T", "round_index": i, "eval_set": "body"} for i in shas],
    )
    rp = _lines(
        tmp_path / "replay.jsonl",
        [
            {"thread_id": "T", "round_index": i, "state": {}, "decision": {"rules_sha256": sha}}
            for i, sha in shas.items()
        ],
    )
    lb = _lines(
        tmp_path / "labels.jsonl",
        [{"thread_id": "T", "round_index": i, "label": "spurious"} for i in shas],
    )
    archive = tmp_path / "archive"
    out = tmp_path / "report.md"
    rc = report.main(
        [
            "--replay",
            str(rp),
            "--fixture",
            str(fx),
            "--labels",
            f"a={lb}",
            "--rules-snapshot-dir",
            str(live),
            "--rules-archive-dir",
            str(archive),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    assert sorted(p.name for p in archive.iterdir()) == [f"{good}.toml"]
    assert (archive / f"{good}.toml").read_bytes() == TEMPLATE_BYTES
    text = out.read_text(encoding="utf-8")
    assert "excluded rows: missing=1, mismatch=2" in text
    assert f"missing: `{missing}` — 1 row(s)" in text
    assert f"mismatch: `{mismatch}` — 2 row(s)" in text
    assert "rows replayed: body 2" in text  # the good-hash row and the v1 row (no hash) stay


def test_report_without_snapshot_dir_is_unchanged(tmp_path: Path) -> None:
    report = _load_report()
    fx = _lines(tmp_path / "f.jsonl", [{"thread_id": "T", "round_index": 1, "eval_set": "body"}])
    rp = _lines(
        tmp_path / "r.jsonl",
        [{"thread_id": "T", "round_index": 1, "state": {}, "decision": {"rules_sha256": "0" * 64}}],
    )
    out = tmp_path / "o.md"
    assert report.main(["--replay", str(rp), "--fixture", str(fx), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "rules snapshots" not in text and "rows replayed: body 1" in text
