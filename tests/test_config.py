"""Tests for ``spirrow_mindwire.config``.

Covers the four resolution paths:
- defaults only (zero-config startup)
- TOML file overrides
- env vars overriding TOML
- strict validation (extra keys, schema_version mismatch)
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from spirrow_mindwire.config import (
    CONFIG_SCHEMA_VERSION,
    DEFAULT_DATA_DIR,
    MindwireSettings,
    load_settings,
)


@pytest.fixture(autouse=True)
def _clear_mindwire_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any ``MINDWIRE_*`` from the test environment."""

    import os

    for key in [k for k in os.environ if k.startswith("MINDWIRE_")]:
        monkeypatch.delenv(key, raising=False)


def test_defaults_only_when_no_toml_and_no_env(tmp_path: Path) -> None:
    """Missing TOML + no env vars yields production-ready defaults."""

    s = load_settings(tmp_path / "absent.toml")

    assert s.schema_version == CONFIG_SCHEMA_VERSION
    assert s.paths.data_dir == DEFAULT_DATA_DIR
    assert s.logging.level == "INFO"
    assert s.logging.format == "json"
    assert s.watcher.dedup_ttl_seconds == 5.0
    assert s.watcher.max_concurrent_threads == 4
    assert s.watcher.polling_mode is False
    assert s.watcher.idle_timeout_seconds == 300.0
    assert s.watcher.absolute_timeout_seconds == 3600.0
    assert s.watcher.retry_backoff_seconds == (5.0, 30.0, 120.0)
    assert s.watcher.max_retries == 3
    assert s.watcher.orphan_tmp_cleanup_age_seconds == 300.0
    assert s.claude_code.allowed_tool_profile == "readonly"
    assert s.claude_code.extra_mcp_servers == {}
    assert s.phanthand.endpoint == "http://localhost:7300"
    assert s.phanthand.api_key_env == "PHANTHAND_API_KEY"


def test_paths_derived_from_data_dir(tmp_path: Path) -> None:
    """Sub-paths should compose deterministically off ``data_dir``."""

    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(f'[paths]\ndata_dir = "{tmp_path.as_posix()}/custom"\n', encoding="utf-8")

    s = load_settings(cfg)
    root = tmp_path / "custom"
    assert s.paths.data_dir == root
    assert s.paths.config_dir == root / "config"
    assert s.paths.new_dir == root / "new"
    assert s.paths.threads_dir == root / "threads"
    assert s.paths.archive_dir == root / "archive"
    assert s.paths.logs_dir == root / "logs"


def test_toml_overrides_defaults(tmp_path: Path) -> None:
    """TOML values override defaults, including nested MCP server entries."""

    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            schema_version = 1

            [watcher]
            max_concurrent_threads = 8
            dedup_ttl_seconds = 10.0

            [claude_code]
            allowed_tool_profile = "minimal"

            [claude_code.extra_mcp_servers.magickit]
            type = "stdio"
            command = "uv"
            args = ["run", "magickit-mcp"]
            allowed_tools = ["mcp__magickit__chatroom_open_thread"]
            """
        ),
        encoding="utf-8",
    )

    s = load_settings(cfg)
    assert s.watcher.max_concurrent_threads == 8
    assert s.watcher.dedup_ttl_seconds == 10.0
    # Untouched watcher fields keep defaults.
    assert s.watcher.idle_timeout_seconds == 300.0
    assert s.claude_code.allowed_tool_profile == "minimal"
    magickit = s.claude_code.extra_mcp_servers["magickit"]
    assert magickit.command == "uv"
    assert magickit.args == ("run", "magickit-mcp")
    assert magickit.allowed_tools == ("mcp__magickit__chatroom_open_thread",)


def test_env_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env vars take precedence over TOML for the same field."""

    cfg = tmp_path / "mindwire.toml"
    cfg.write_text("[watcher]\nmax_concurrent_threads = 8\n", encoding="utf-8")

    monkeypatch.setenv("MINDWIRE_WATCHER__MAX_CONCURRENT_THREADS", "16")

    s = load_settings(cfg)
    assert s.watcher.max_concurrent_threads == 16


def test_env_does_not_clobber_sibling_toml_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An env override on one nested key must not erase neighbouring TOML keys."""

    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            [claude_code]
            allowed_tool_profile = "readonly"

            [claude_code.extra_mcp_servers.magickit]
            type = "stdio"
            command = "uv"
            """
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("MINDWIRE_CLAUDE_CODE__ALLOWED_TOOL_PROFILE", "minimal")

    s = load_settings(cfg)
    assert s.claude_code.allowed_tool_profile == "minimal"
    assert "magickit" in s.claude_code.extra_mcp_servers


def test_strict_extra_key_in_toml_raises(tmp_path: Path) -> None:
    """Unknown keys in TOML fail loud (strict='forbid')."""

    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            [watcher]
            max_concurrent_threads = 4
            bogus_key = "nope"
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_settings(cfg)


def test_unsupported_schema_version_raises(tmp_path: Path) -> None:
    """schema_version must equal CONFIG_SCHEMA_VERSION."""

    cfg = tmp_path / "mindwire.toml"
    cfg.write_text("schema_version = 99\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="schema_version"):
        load_settings(cfg)


def test_direct_construction_skips_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MindwireSettings()`` constructed directly only sees defaults + env."""

    monkeypatch.setenv("MINDWIRE_WATCHER__MAX_CONCURRENT_THREADS", "12")
    s = MindwireSettings()
    assert s.watcher.max_concurrent_threads == 12


# ----- Feature 2 sub-PR 3 O-2 (b): retry_backoff_seconds validators --------


def test_retry_backoff_seconds_monotonic_non_strict_accepts_equal_adjacent(
    tmp_path: Path,
) -> None:
    """Equal-adjacent values are allowed (= flat segments)."""
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            [watcher]
            retry_backoff_seconds = [5.0, 5.0, 30.0]
            """
        ),
        encoding="utf-8",
    )
    s = load_settings(cfg)
    assert s.watcher.retry_backoff_seconds == (5.0, 5.0, 30.0)


def test_retry_backoff_seconds_strict_decrease_rejected(tmp_path: Path) -> None:
    """Strictly decreasing tuple violates non-strict monotonic constraint."""
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            [watcher]
            retry_backoff_seconds = [30.0, 5.0, 1.0]
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="non-strictly monotonic"):
        load_settings(cfg)


def test_retry_backoff_seconds_negative_rejected(tmp_path: Path) -> None:
    """Negative values are rejected."""
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            [watcher]
            retry_backoff_seconds = [-1.0, 5.0, 30.0]
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="non-negative"):
        load_settings(cfg)


def test_retry_backoff_seconds_shorter_than_max_retries_rejected(tmp_path: Path) -> None:
    """``len(retry_backoff_seconds) < max_retries`` is a config bug (cross-field)."""
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            [watcher]
            retry_backoff_seconds = [5.0]
            max_retries = 5
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match=r"retry_backoff_seconds length .* >= max_retries"):
        load_settings(cfg)


def test_retry_backoff_seconds_equal_length_to_max_retries_accepted(tmp_path: Path) -> None:
    """``len(retry_backoff_seconds) == max_retries`` is the canonical config."""
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            [watcher]
            retry_backoff_seconds = [1.0, 2.0]
            max_retries = 2
            """
        ),
        encoding="utf-8",
    )
    s = load_settings(cfg)
    assert s.watcher.retry_backoff_seconds == (1.0, 2.0)
    assert s.watcher.max_retries == 2


def test_retry_backoff_seconds_longer_than_max_retries_accepted(tmp_path: Path) -> None:
    """Longer tuple than ``max_retries`` is allowed (= unused tail elements)."""
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            [watcher]
            retry_backoff_seconds = [1.0, 2.0, 3.0, 4.0, 5.0]
            max_retries = 2
            """
        ),
        encoding="utf-8",
    )
    s = load_settings(cfg)
    assert s.watcher.retry_backoff_seconds == (1.0, 2.0, 3.0, 4.0, 5.0)


def test_retry_backoff_seconds_empty_with_max_retries_zero_accepted() -> None:
    """``max_retries=0`` (= retry disabled) tolerates empty tuple."""
    s = MindwireSettings(watcher={"max_retries": 0, "retry_backoff_seconds": []})  # type: ignore[arg-type]
    assert s.watcher.max_retries == 0
    assert s.watcher.retry_backoff_seconds == ()
