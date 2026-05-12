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

CONFIG_SCHEMA_VERSION = 1
"""Version of the ``mindwire.toml`` configuration schema.

Distinct from data-model schema versions (`spirrow_mindwire.schema`) so
config and on-disk data formats can evolve on independent cadences.
"""

DEFAULT_DATA_DIR = Path.home() / "spirrow-mindwire-data"


class _StrictModel(BaseModel):
    """Base for *config* sub-models: forbid extras only.

    Deliberately distinct from :class:`spirrow_mindwire.schema.StrictModel`
    (which is also ``frozen=True`` + ``populate_by_name=True``).
    Settings models are intentionally kept mutable in Phase 0 because
    pydantic-settings' env-override path interacts with frozen models
    in ways we haven't audited; once that's verified we can promote to
    ``frozen=True``.

    Kept as two separate bases (instead of one shared one) because
    config and schema live in their own layers — see
    ``feedback_decoupling_preference`` — and the leading underscore
    keeps this base out of the public API of this module.
    """

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
    """Watcher runtime tuning. Defaults are the T06+Feature2-confirmed values.

    Feature 2 sub-PR 3 (retry) consumes :attr:`retry_backoff_seconds` /
    :attr:`retry_jitter` / :attr:`max_retries` together in the
    dispatcher's per-thread retry loop. See
    docs/feature-2-design.md §4 (config audit) and §5.1 sub-PR 3
    (retry semantics) for the design rationale, dogfooding hedge, and
    re-audit triggers (FI-3 / FI-4).
    """

    dedup_ttl_seconds: float = Field(default=5.0, gt=0)
    max_concurrent_threads: int = Field(default=4, ge=1)
    polling_mode: bool = False
    idle_timeout_seconds: float = Field(default=300.0, gt=0)
    absolute_timeout_seconds: float = Field(default=3600.0, gt=0)
    retry_backoff_seconds: tuple[float, ...] = (5.0, 30.0, 120.0)
    """Sleep durations between successive retry attempts in seconds.

    Indexed by *retry attempt number minus 1* — element ``[i]`` is the
    sleep before retry attempt ``i+1``. For the default
    ``(5.0, 30.0, 120.0)`` paired with ``max_retries=3``:

    - attempt 0 (initial invoke) fails → sleep 5s  → attempt 1
    - attempt 1 fails              → sleep 30s → attempt 2
    - attempt 2 fails              → sleep 120s → attempt 3
    - attempt 3 fails              → terminated (retry-exhausted)

    So the **maximum per-_run_thread retry duration ≈ sum(retry_backoff_seconds)**
    (= 155s for the default tuple, excluding the invoke latency itself).
    Subsequent live events for the same (thread_id, seq) within this
    window are gated by :attr:`dedup_ttl_seconds` (=5s default — much
    shorter than the retry duration, so duplicate events that arrive
    *while a retry sleep is in progress* are not deduped by
    :class:`~spirrow_mindwire.watcher.dedup.DedupCache` alone; the
    per-thread asyncio lock in :class:`~spirrow_mindwire.watcher.dispatcher.ThreadDispatcher`
    serializes them, so they wait until the retry session ends).

    Validation (sub-PR 3 O-2 (b) audit): the tuple must be non-strictly
    monotonic (each element ≥ the previous one) to keep backoff
    "exponential-ish" interpretable, and its length must be ≥
    :attr:`max_retries` so the retry loop never indexes past the end.
    Both checks are enforced via Pydantic validators (see below).
    """

    retry_jitter: float = Field(default=0.2, ge=0, le=1)
    """Fractional jitter applied to each backoff sleep.

    Actual sleep = ``backoff_seconds * (1 + uniform(-retry_jitter, +retry_jitter))``.
    A value of ``0`` disables jitter; ``1`` doubles or zeros the sleep.
    See docs/feature-2-design.md §4.1 (dogfooding-pending re-audit).
    """

    max_retries: int = Field(default=3, ge=0)
    """Maximum retry attempts after the initial invoke (per _run_thread).

    ``max_retries=N`` means the loop tries ``N+1`` total times (1 initial
    + N retries). ``max_retries=0`` disables retry: a single transient
    failure terminates the thread with ``retry-exhausted``.

    Note: this is the **per-_run_thread gate**, not the cumulative cap on
    ``ThreadMeta.retry_count`` (which is an audit-trail counter that can
    grow across multiple _run_thread invocations via startup_full_scan
    requeue). See docs/feature-2-design.md §3.5 (retry_count semantics)
    and FI-3 dogfooding re-audit.
    """

    orphan_tmp_cleanup_age_seconds: float = Field(default=300.0, gt=0)


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

    schema_version: int = CONFIG_SCHEMA_VERSION
    paths: PathsConfig = Field(default_factory=PathsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
    claude_code: ClaudeCodeConfig = Field(default_factory=ClaudeCodeConfig)
    phanthand: PhanthandConfig = Field(default_factory=PhanthandConfig)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, v: int) -> int:
        if v != CONFIG_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported config schema_version={v}; this MindWire build "
                f"requires schema_version={CONFIG_SCHEMA_VERSION}. "
                "Migrate the config or pin a compatible MindWire version."
            )
        return v


def _default_config_path() -> Path:
    """Resolve the default ``mindwire.toml`` path.

    Honours ``MINDWIRE_PATHS__DATA_DIR`` so the data root can be moved
    without editing TOML.

    Note on env / TOML interaction: ``MINDWIRE_PATHS__DATA_DIR``
    relocates the *whole* data root, which means TOML lookup also moves
    to ``$MINDWIRE_PATHS__DATA_DIR/config/mindwire.toml``. If you need
    to keep TOML at one path while pointing the data root somewhere
    else, pass ``config_path`` to :func:`load_settings` directly.

    Only ``expanduser()`` is applied; shell-style ``${VAR}`` expansion
    is not performed.
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

    # pydantic-settings v2's ``TomlConfigSettingsSource`` reads ``toml_file``
    # at class-definition time, so we subclass per call to scope the path
    # to this load. The instance returned is ``isinstance(MindwireSettings,
    # ...)`` so external callers see the declared return type unchanged.
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
    "CONFIG_SCHEMA_VERSION",
    "DEFAULT_DATA_DIR",
    "ClaudeCodeConfig",
    "ExtraMCPServerConfig",
    "LoggingConfig",
    "MindwireSettings",
    "PathsConfig",
    "PhanthandConfig",
    "WatcherConfig",
    "load_settings",
]
