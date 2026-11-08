"""clone_slot_trust: slot trust registration (Bohr msg-8234 判断 3, measured 2026-11-08)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spirrow_mindwire.clone_slot_trust import (
    TrustRegistrationError,
    claude_config_file,
    ensure_trusted,
    is_trusted,
    project_key,
    trust_preparer,
)


def _cfg(tmp_path: Path, data: object) -> Path:
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_project_key_is_absolute_with_forward_slashes(tmp_path: Path) -> None:
    key = project_key(tmp_path / "slots" / "demo" / "abc")
    assert "\\" not in key and key.endswith("/slots/demo/abc")
    assert Path(key).is_absolute()


def test_ensure_trusted_adds_entry_and_keeps_every_other_key(tmp_path: Path) -> None:
    slot = tmp_path / "slot"
    other = {"hasTrustDialogAccepted": False, "lastCost": 1.5}
    cfg = _cfg(tmp_path, {"hasCompletedOnboarding": True, "projects": {"C:/x": other}})
    assert ensure_trusted(slot, cfg) is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["hasCompletedOnboarding"] is True
    assert data["projects"]["C:/x"] == other
    assert data["projects"][project_key(slot)] == {"hasTrustDialogAccepted": True}
    assert is_trusted(slot, cfg)
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_ensure_trusted_is_idempotent_and_keeps_existing_entry_fields(tmp_path: Path) -> None:
    slot = tmp_path / "slot"
    entry = {"hasTrustDialogAccepted": True, "allowedTools": ["x"]}
    cfg = _cfg(tmp_path, {"projects": {project_key(slot): entry}})
    before = cfg.read_text(encoding="utf-8")
    assert ensure_trusted(slot, cfg) is False
    assert cfg.read_text(encoding="utf-8") == before  # not rewritten


def test_untrusted_existing_entry_is_flipped_keeping_its_fields(tmp_path: Path) -> None:
    slot = tmp_path / "slot"
    cfg = _cfg(
        tmp_path, {"projects": {project_key(slot): {"hasTrustDialogAccepted": False, "k": 1}}}
    )
    assert ensure_trusted(slot, cfg) is True
    entry = json.loads(cfg.read_text(encoding="utf-8"))["projects"][project_key(slot)]
    assert entry == {"hasTrustDialogAccepted": True, "k": 1}


def test_missing_config_raises_and_creates_nothing(tmp_path: Path) -> None:
    cfg = tmp_path / ".claude.json"
    with pytest.raises(TrustRegistrationError, match="cannot read"):
        ensure_trusted(tmp_path / "slot", cfg)
    assert not cfg.exists()


@pytest.mark.parametrize("text", ["{not json", "[]", '{"projects": []}'])
def test_unusable_config_raises_and_is_left_unchanged(tmp_path: Path, text: str) -> None:
    cfg = tmp_path / ".claude.json"
    cfg.write_text(text, encoding="utf-8")
    with pytest.raises(TrustRegistrationError):
        ensure_trusted(tmp_path / "slot", cfg)
    assert cfg.read_text(encoding="utf-8") == text


def test_config_file_follows_claude_config_dir(tmp_path: Path) -> None:
    assert claude_config_file({"CLAUDE_CONFIG_DIR": str(tmp_path)}) == tmp_path / ".claude.json"
    assert claude_config_file({}) == Path.home() / ".claude.json"
    assert claude_config_file({"CLAUDE_CONFIG_DIR": "  "}) == Path.home() / ".claude.json"


def test_trust_preparer_resolves_config_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, {"projects": {}})
    prepare = trust_preparer()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    prepare(tmp_path / "slot")
    assert is_trusted(tmp_path / "slot", cfg)
