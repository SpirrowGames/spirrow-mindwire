"""``[turn.isolation]`` config and ``mindwire turn-isolation-check`` (unit 2a, D1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from spirrow_mindwire.cli import main
from spirrow_mindwire.config import MindwireSettings, load_settings


def test_isolation_is_off_by_default() -> None:
    assert MindwireSettings().turn.isolation.user is None


def test_isolation_user_is_read_from_toml(tmp_path: Path) -> None:
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text('schema_version = 1\n[turn.isolation]\nuser = "mindwire-turn"\n', "utf-8")
    assert load_settings(cfg).turn.isolation.user == "mindwire-turn"


def test_empty_isolation_user_is_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text('schema_version = 1\n[turn.isolation]\nuser = ""\n', "utf-8")
    with pytest.raises(ValueError):
        load_settings(cfg)


def test_cli_check_with_isolation_off_is_a_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text("schema_version = 1\n", "utf-8")
    with pytest.raises(SystemExit) as info:
        main(["turn-isolation-check", "--config", str(cfg), "--clone", str(tmp_path)])
    assert info.value.code == 0
    assert "turn-isolation: off" in capsys.readouterr().out
