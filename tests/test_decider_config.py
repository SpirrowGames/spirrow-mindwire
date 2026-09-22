"""``[decider]`` config セクションの migration / default テスト。

Fermi msg-4066 DECIDED #1 — Tier-C mode 4 値 (`off | shadow | annotate | bounce`)。
既定は全て `off` で読める (既存 TOML を破らない後方互換)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from spirrow_mindwire.config import (
    DeciderConfig,
    DeciderTierCConfig,
    MindwireSettings,
    load_settings,
)


def test_decider_defaults_are_all_off() -> None:
    """default 状態で全 mode が off (D12 の shadow 先行デプロイ前の初期状態)。"""
    cfg = DeciderConfig()
    assert cfg.mode == "off"
    assert cfg.backend == "off"
    assert cfg.tierc.mode == "off"


def test_decider_tierc_mode_accepts_all_four_values() -> None:
    """Fermi DECIDED #1: shadow を含む 4 値 (`off | shadow | annotate | bounce`)。"""
    modes: tuple[Literal["off", "shadow", "annotate", "bounce"], ...] = (
        "off",
        "shadow",
        "annotate",
        "bounce",
    )
    for mode in modes:
        cfg = DeciderTierCConfig(mode=mode)
        assert cfg.mode == mode


def test_decider_tierc_mode_rejects_unknown_value() -> None:
    """typo / 未定義 mode は extra="forbid" ではなく Literal 型で拒否される。"""
    try:
        DeciderTierCConfig(mode="active")  # type: ignore[arg-type]
    except Exception:  # pydantic ValidationError
        return
    raise AssertionError("DeciderTierCConfig should reject unknown mode value")


def test_default_settings_expose_decider_block() -> None:
    """MindwireSettings に ``decider`` field が生えていて、default で off。"""
    settings = MindwireSettings()
    assert isinstance(settings.decider, DeciderConfig)
    assert settings.decider.tierc.mode == "off"
    assert settings.decider.mode == "off"


def test_loading_toml_without_decider_block_keeps_defaults(tmp_path: Path) -> None:
    """既存 TOML (decider 未記述) が壊れず、default で読める後方互換。"""
    toml = tmp_path / "mindwire.toml"
    toml.write_text(
        # 既存の他 section だけ書いた minimal TOML — decider に何も触れない
        "schema_version = 1\n",
        encoding="utf-8",
    )
    settings = load_settings(toml)
    assert settings.decider.mode == "off"
    assert settings.decider.tierc.mode == "off"


def test_loading_toml_with_shadow_tierc(tmp_path: Path) -> None:
    """TOML から `[decider.tierc] mode = "shadow"` を読める。"""
    toml = tmp_path / "mindwire.toml"
    toml.write_text(
        'schema_version = 1\n\n[decider.tierc]\nmode = "shadow"\n',
        encoding="utf-8",
    )
    settings = load_settings(toml)
    assert settings.decider.tierc.mode == "shadow"


def test_default_active_questions_are_track_b_only() -> None:
    """`active_questions` の default は Track B の 2 問 (msg-3404 §2)。

    Tier-C 問いは §3.3.b 別フックの管理下で、`active_questions` (Track B)
    には含めない (D16 disjoint フック運用)。
    """
    cfg = DeciderConfig()
    assert cfg.active_questions == ("handoff_valid", "made_progress")
