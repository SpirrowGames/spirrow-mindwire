"""clone_slot_trust: read-only trust check (msg-9385 option A, msg-9386 D-1 改訂 7)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from spirrow_mindwire import clone_slot_trust as cst
from spirrow_mindwire.clone_slot_trust import (
    TrustReadError,
    claude_config_file,
    is_trusted,
    project_key,
)


def _cfg(tmp_path: Path, data: object) -> Path:
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return p


def _has_drive(key: str) -> bool:
    return len(key) >= 2 and key[1] == ":"


def test_project_key_is_absolute_with_forward_slashes(tmp_path: Path) -> None:
    key = project_key(tmp_path / "slots" / "demo" / "slot-01")
    assert "\\" not in key and key.endswith("/slots/demo/slot-01")
    assert Path(key).is_absolute()


def test_project_key_upper_cases_the_drive_letter(tmp_path: Path) -> None:
    key = project_key(tmp_path)
    if not _has_drive(key):
        pytest.skip("drive letters exist on Windows paths only")
    lower = Path(key[0].lower() + key[1:])
    assert project_key(lower) == key and key[0].isupper()


def test_trusted_entry_is_trusted(tmp_path: Path) -> None:
    slot = tmp_path / "slot-01"
    cfg = _cfg(tmp_path, {"projects": {project_key(slot): {"hasTrustDialogAccepted": True}}})
    assert is_trusted(slot, cfg) is True


@pytest.mark.parametrize(
    "entry",
    [None, {"hasTrustDialogAccepted": False}, {"hasTrustDialogAccepted": "true"}, ["x"]],
)
def test_anything_but_a_true_entry_is_untrusted(tmp_path: Path, entry: object) -> None:
    slot = tmp_path / "slot-01"
    projects = {} if entry is None else {project_key(slot): entry}
    assert is_trusted(slot, _cfg(tmp_path, {"projects": projects})) is False


@pytest.mark.parametrize("data", [{}, {"projects": []}, {"projects": "x"}])
def test_no_projects_object_is_untrusted(tmp_path: Path, data: dict[str, object]) -> None:
    assert is_trusted(tmp_path / "slot-01", _cfg(tmp_path, data)) is False


def test_trust_is_not_inherited_from_an_ancestor(tmp_path: Path) -> None:
    """Measured: a trusted parent does not trust a git root below it; the slot is its own root."""
    slot = tmp_path / "repo" / "slot-01"
    trusted = {"hasTrustDialogAccepted": True}
    cfg = _cfg(
        tmp_path,
        {"projects": {project_key(tmp_path): trusted, project_key(tmp_path / "repo"): trusted}},
    )
    assert is_trusted(slot, cfg) is False


def test_lower_case_drive_key_is_not_accepted(tmp_path: Path) -> None:
    """Only the measured key shape counts (upper-case drive); an unmeasured shape is untrusted."""
    slot = tmp_path / "slot-01"
    key = project_key(slot)
    if not _has_drive(key):
        pytest.skip("drive letters exist on Windows paths only")
    lower = key[0].lower() + key[1:]
    cfg = _cfg(tmp_path, {"projects": {lower: {"hasTrustDialogAccepted": True}}})
    assert is_trusted(slot, cfg) is False


def test_missing_config_raises_and_is_not_created(tmp_path: Path) -> None:
    cfg = tmp_path / ".claude.json"
    with pytest.raises(TrustReadError, match="cannot read"):
        is_trusted(tmp_path / "slot-01", cfg)
    assert not cfg.exists()


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b"\xff\xfe"])
def test_unparseable_config_raises_and_is_left_alone(tmp_path: Path, raw: bytes) -> None:
    cfg = tmp_path / ".claude.json"
    cfg.write_bytes(raw)
    with pytest.raises(TrustReadError):
        is_trusted(tmp_path / "slot-01", cfg)
    assert cfg.read_bytes() == raw


@pytest.mark.parametrize("trusted", [True, False])
def test_claude_json_bytes_never_change_on_any_path(tmp_path: Path, trusted: bool) -> None:
    """msg-9386 A1: whatever the outcome, the config bytes and its directory are unchanged."""
    slot = tmp_path / "slot-01"
    # Compact with a trailing newline: a rewriter would normalize this shape.
    body = {"projects": {project_key(slot): {"hasTrustDialogAccepted": trusted}}, "x": 1}
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n"
    cfg = tmp_path / ".claude.json"
    cfg.write_bytes(raw)
    before = sorted(p.name for p in tmp_path.iterdir())
    mtime = cfg.stat().st_mtime_ns
    assert is_trusted(slot, cfg) is trusted
    assert cfg.read_bytes() == raw
    assert cfg.stat().st_mtime_ns == mtime
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_module_has_no_write_path() -> None:
    """The write / remove paths of the earlier A1 are gone, not merely unused."""
    for gone in ("ensure_trusted", "remove_project_entries", "trust_preparer", "_edit"):
        assert not hasattr(cst, gone)
    src = inspect.getsource(cst)
    for writer in ("os.replace", "write_text", "write_bytes", "mkstemp", ".open(", "unlink"):
        assert writer not in src


def test_config_file_location(tmp_path: Path) -> None:
    assert claude_config_file({"CLAUDE_CONFIG_DIR": str(tmp_path)}) == tmp_path / ".claude.json"
    assert claude_config_file({"CLAUDE_CONFIG_DIR": "  "}) == Path.home() / ".claude.json"
