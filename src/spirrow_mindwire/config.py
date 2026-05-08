"""Configuration loading for Spirrow MindWire.

Settings are loaded from (in increasing priority):
1. Production-ready defaults baked into the models below
2. Optional TOML file at ``<data_dir>/config/mindwire.toml`` (silently
   skipped if absent — zero-config startup is supported)
3. Environment variables prefixed ``MINDWIRE_`` with ``__`` as the nested
   delimiter (e.g. ``MINDWIRE_WATCHER__MAX_CONCURRENT_THREADS=8``)

All models forbid unknown keys (``extra='forbid'``) so typos surface
immediately. The ``schema_version`` field is pinned to
``SCHEMA_VERSION``; loading a TOML with a different version is a hard
error.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

SCHEMA_VERSION = 1
DEFAULT_DATA_DIR = Path.home() / "spirrow-mindwire-data"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_StrictModel):
    """Filesystem layout under ``data_dir``.

    All sub-paths are derived from ``data_dir`` so installations only
    need to set one root.
    """

    data_dir: Path = DEFAULT_DATA_DIR

    @property
    def config_dir(self) -> Path:
        return self.data_dir / "config"

    @property
    def new_dir(self) -> Path:
        return self.data_dir / "new"

    @property
    def threads_dir(self) -> Path:
        return self.data_dir / "threads"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


class LoggingConfig(_StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "console"] = "json"


class WatcherConfig(_StrictModel):
    """Watcher runtime tuning. Defaults are the T06-confirmed values."""

    dedup_ttl_seconds: float = Field(default=5.0, gt=0)
    max_concurrent_threads: int = Field(default=4, ge=1)
    polling_mode: bool = False
    idle_timeout_seconds: float = Field(default=300.0, gt=0)
    absolute_timeout_seconds: float = Field(default=3600.0, gt=0)
    retry_backoff_seconds: tuple[float, ...] = (5.0, 30.0, 120.0)
    retry_jitter: float = Field(default=0.2, ge=0, le=1)
    max_retries: int = Field(default=3, ge=0)
    shutdown_grace_seconds: float = Field(default=60.0, gt=0)


class ExtraMCPServerConfig(_StrictModel):
    """One entry under ``[claude_code.extra_mcp_servers.<name>]``.

    Phase 0 supports stdio MCP servers only; HTTP/SSE transports may be
    added later when a real use case appears.
    """

    type: Literal["stdio"] = "stdio"
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()


class ClaudeCodeConfig(_StrictModel):
    allowed_tool_profile: Literal["minimal", "readonly"] = "readonly"
    extra_mcp_servers: dict[str, ExtraMCPServerConfig] = Field(default_factory=dict)


class PhanthandConfig(_StrictModel):
    endpoint: str = "http://localhost:7300"
    api_key_env: str = "PHANTHAND_API_KEY"
    timeout_seconds: float = Field(default=10.0, gt=0)


class MindwireSettings(BaseSettings):
    """Top-level MindWire configuration.

    Construct via :func:`load_settings`; direct ``MindwireSettings()``
    skips TOML loading and only honours defaults + env vars.
    """

    model_config = SettingsConfigDict(
        env_prefix="MINDWIRE_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    schema_version: int = SCHEMA_VERSION
    paths: PathsConfig = Field(default_factory=PathsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
    claude_code: ClaudeCodeConfig = Field(default_factory=ClaudeCodeConfig)
    phanthand: PhanthandConfig = Field(default_factory=PhanthandConfig)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported schema_version={v}; this MindWire build "
                f"requires schema_version={SCHEMA_VERSION}. "
                "Migrate the config or pin a compatible MindWire version."
            )
        return v


def _default_config_path() -> Path:
    """Resolve the default ``mindwire.toml`` path.

    Honours ``MINDWIRE_PATHS__DATA_DIR`` so the data root can be moved
    without editing TOML.
    """

    env_data_dir = os.environ.get("MINDWIRE_PATHS__DATA_DIR")
    data_dir = Path(env_data_dir).expanduser() if env_data_dir else DEFAULT_DATA_DIR
    return data_dir / "config" / "mindwire.toml"


def load_settings(config_path: Path | None = None) -> MindwireSettings:
    """Load :class:`MindwireSettings` with defaults < TOML < env vars.

    A missing TOML file is not an error — defaults plus env vars are
    used (zero-config startup).
    """

    resolved_path = config_path if config_path is not None else _default_config_path()

    if not resolved_path.is_file():
        return MindwireSettings()

    toml_path = resolved_path

    class _ScopedSettings(MindwireSettings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (
                init_settings,
                env_settings,
                TomlConfigSettingsSource(settings_cls, toml_file=toml_path),
                file_secret_settings,
            )

    return _ScopedSettings()


__all__ = [
    "DEFAULT_DATA_DIR",
    "SCHEMA_VERSION",
    "ClaudeCodeConfig",
    "ExtraMCPServerConfig",
    "LoggingConfig",
    "MindwireSettings",
    "PathsConfig",
    "PhanthandConfig",
    "WatcherConfig",
    "load_settings",
]
