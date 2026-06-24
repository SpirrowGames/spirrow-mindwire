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
    DEFAULT_CONDUCTOR_MAX_ROUNDS,
    DEFAULT_DATA_DIR,
    ConductorConfig,
    MCPServerConfig,
    MindwireSettings,
    NaysayerGatingConfig,
    load_settings,
)
from spirrow_mindwire.value_objects import Role


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
    assert s.mcp_server.host == "127.0.0.1"
    assert s.mcp_server.port == 7400
    assert s.mcp_server.api_key_env == "MINDWIRE_MCP_API_KEY"


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


# ----- Feature 3-A sub-PR 2: MCPServerConfig (mindwire-mcp-server) ---------


def test_mcp_server_toml_overrides_defaults(tmp_path: Path) -> None:
    """``[mcp_server]`` TOML block overrides the production-ready defaults."""
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            [mcp_server]
            host = "0.0.0.0"
            port = 9000
            api_key_env = "CUSTOM_KEY_ENV"
            """
        ),
        encoding="utf-8",
    )
    s = load_settings(cfg)
    assert s.mcp_server.host == "0.0.0.0"
    assert s.mcp_server.port == 9000
    assert s.mcp_server.api_key_env == "CUSTOM_KEY_ENV"


def test_mcp_server_env_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``MINDWIRE_MCP_SERVER__PORT`` wins over the TOML value."""
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text("[mcp_server]\nport = 9000\n", encoding="utf-8")
    monkeypatch.setenv("MINDWIRE_MCP_SERVER__PORT", "9100")

    s = load_settings(cfg)
    assert s.mcp_server.port == 9100


def test_mcp_server_port_out_of_range_rejected() -> None:
    """Port must be a TCP port number (1..65535)."""
    with pytest.raises(ValidationError):
        MCPServerConfig(port=70000)
    with pytest.raises(ValidationError):
        MCPServerConfig(port=0)


def test_mcp_server_extra_key_in_toml_raises(tmp_path: Path) -> None:
    """Unknown keys under ``[mcp_server]`` fail loud (strict='forbid')."""
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            [mcp_server]
            host = "127.0.0.1"
            bogus_key = "nope"
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_settings(cfg)


# ----- PR-2a: ConductorConfig (mindwire-loop --mode conductor, msg-523) -----


def test_conductor_config_defaults_are_empty_and_unobtrusive() -> None:
    """Zero-config: the conductor block defaults empty (validated at daemon startup, not load)."""
    s = MindwireSettings()
    assert s.conductor.task_thread_id == ""
    assert s.conductor.roster == {}
    assert s.conductor.naysayer_identity == ""
    assert s.conductor.human_identity == "human"  # carve-out ① default (PR-2b-3 D-1)
    assert s.conductor.max_rounds == DEFAULT_CONDUCTOR_MAX_ROUNDS  # single SOT (D-2)


def test_conductor_config_parses_roster_and_fields_from_toml(tmp_path: Path) -> None:
    """``[conductor]`` parses the identity→role roster (string role → Role) and scalar knobs."""
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            [conductor]
            task_thread_id = "T-cross-thread-relay-conductor"
            naysayer_identity = "Einstein"
            human_identity = "takahito"
            max_rounds = 12

            [conductor.roster]
            Bohr = "proposer"
            Heisenberg = "implementer"
            Einstein = "naysayer"
            """
        ),
        encoding="utf-8",
    )
    s = load_settings(cfg)
    assert s.conductor.task_thread_id == "T-cross-thread-relay-conductor"
    assert s.conductor.naysayer_identity == "Einstein"
    assert s.conductor.human_identity == "takahito"
    assert s.conductor.max_rounds == 12
    assert s.conductor.roster == {
        "Bohr": Role.PROPOSER,
        "Heisenberg": Role.IMPLEMENTER,
        "Einstein": Role.NAYSAYER,
    }


def test_conductor_config_env_overrides_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MINDWIRE_CONDUCTOR__*`` env vars win over TOML for scalar fields."""
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text('[conductor]\ntask_thread_id = "T-from-toml"\n', encoding="utf-8")
    monkeypatch.setenv("MINDWIRE_CONDUCTOR__TASK_THREAD_ID", "T-from-env")

    s = load_settings(cfg)
    assert s.conductor.task_thread_id == "T-from-env"


def test_conductor_config_rejects_unknown_role_in_roster() -> None:
    """A roster value that is not a valid Role fails loud (enum validation)."""
    with pytest.raises(ValidationError):
        ConductorConfig(roster={"Bohr": "wizard"})  # type: ignore[dict-item]


def test_conductor_config_rejects_max_rounds_below_one() -> None:
    """``max_rounds`` must be >= 1 (mirrors the Conductor ctor invariant)."""
    with pytest.raises(ValidationError):
        ConductorConfig(max_rounds=0)


def test_conductor_human_identity_configurable_and_max_rounds_single_sot() -> None:
    """PR-2b-3: ``human_identity`` is configurable (D-1); the round-cap default is one SOT (D-2)."""
    from spirrow_mindwire.conductor import core as _core

    assert ConductorConfig(human_identity="takahito").human_identity == "takahito"
    # D-2: the ConductorConfig default and the Conductor ctor default are the SAME constant.
    assert ConductorConfig().max_rounds == DEFAULT_CONDUCTOR_MAX_ROUNDS
    assert _core._DEFAULT_MAX_ROUNDS == DEFAULT_CONDUCTOR_MAX_ROUNDS


def test_conductor_config_extra_key_in_toml_raises(tmp_path: Path) -> None:
    """Unknown keys under ``[conductor]`` fail loud (strict='forbid')."""
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            [conductor]
            task_thread_id = "T-x"
            bogus_key = "nope"
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_settings(cfg)


# ----- cost levers: conductor forced-consult narrowing + naysayer-gating debounce -----


def test_conductor_force_naysayer_flag_defaults_false() -> None:
    """The forced-consult cost lever is default-off (baseline Obj2 behavior preserved)."""
    assert MindwireSettings().conductor.force_naysayer_only_on_explicit_human is False
    assert ConductorConfig().force_naysayer_only_on_explicit_human is False


def test_conductor_force_naysayer_flag_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MINDWIRE_CONDUCTOR__FORCE_NAYSAYER_ONLY_ON_EXPLICIT_HUMAN`` toggles the lever."""
    monkeypatch.setenv("MINDWIRE_CONDUCTOR__FORCE_NAYSAYER_ONLY_ON_EXPLICIT_HUMAN", "true")
    assert MindwireSettings().conductor.force_naysayer_only_on_explicit_human is True


def test_naysayer_gating_defaults_are_off() -> None:
    """Both PR-review debounce knobs default off (a full review on every fire)."""
    g = MindwireSettings().naysayer_gating
    assert g.skip_if_head_unchanged is False
    assert g.max_review_rounds == 0
    assert g.review_login == "spirrowgames-ops"


def test_naysayer_gating_parses_from_toml(tmp_path: Path) -> None:
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        dedent(
            """
            [naysayer_gating]
            skip_if_head_unchanged = true
            max_review_rounds = 4
            """
        ),
        encoding="utf-8",
    )
    g = load_settings(cfg).naysayer_gating
    assert g.skip_if_head_unchanged is True
    assert g.max_review_rounds == 4


def test_naysayer_gating_env_overrides_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text("[naysayer_gating]\nmax_review_rounds = 2\n", encoding="utf-8")
    monkeypatch.setenv("MINDWIRE_NAYSAYER_GATING__MAX_REVIEW_ROUNDS", "5")
    assert load_settings(cfg).naysayer_gating.max_review_rounds == 5


def test_naysayer_gating_rejects_negative_max_rounds() -> None:
    """``max_review_rounds`` must be >= 0 (0 = disabled)."""
    with pytest.raises(ValidationError):
        NaysayerGatingConfig(max_review_rounds=-1)


def test_naysayer_gating_extra_key_raises() -> None:
    """Unknown keys under ``[naysayer_gating]`` fail loud (strict='forbid')."""
    with pytest.raises(ValidationError):
        NaysayerGatingConfig(bogus=True)  # type: ignore[call-arg]


def test_naysayer_gating_shadow_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shadow (observe-only) flag defaults off and is env-overridable."""
    assert MindwireSettings().naysayer_gating.shadow is False
    monkeypatch.setenv("MINDWIRE_NAYSAYER_GATING__SHADOW", "true")
    assert MindwireSettings().naysayer_gating.shadow is True
