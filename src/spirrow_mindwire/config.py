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
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from .value_objects import Role

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

    @field_validator("retry_backoff_seconds")
    @classmethod
    def _retry_backoff_monotonic(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        """Enforce non-strict monotonic increase of retry_backoff_seconds.

        Equal-adjacent values are allowed (= flat segments such as
        ``(5.0, 5.0, 30.0)``); strict decrease is rejected. Empty
        tuples are allowed at this layer — the cross-field
        ``len(retry_backoff_seconds) >= max_retries`` check lives on
        the model validator below so the error message can name both
        fields.
        """
        for i in range(1, len(v)):
            if v[i] < v[i - 1]:
                raise ValueError(
                    "retry_backoff_seconds must be non-strictly monotonic "
                    "(each element ≥ the previous one); "
                    f"got {v} (index {i}: {v[i]} < index {i - 1}: {v[i - 1]})"
                )
            if v[i] < 0:
                raise ValueError(f"retry_backoff_seconds must contain non-negative values; got {v}")
        if v and v[0] < 0:
            raise ValueError(f"retry_backoff_seconds must contain non-negative values; got {v}")
        return v

    @model_validator(mode="after")
    def _retry_backoff_length_covers_max_retries(self) -> Self:
        """Cross-field: ``len(retry_backoff_seconds) >= max_retries``.

        The dispatcher's retry loop indexes ``retry_backoff_seconds[attempt]``
        for ``attempt in range(max_retries)`` (see dispatcher module
        docstring). A shorter tuple would raise ``IndexError`` mid-retry,
        which is a configuration bug we want to surface at load time.
        """
        if len(self.retry_backoff_seconds) < self.max_retries:
            raise ValueError(
                f"retry_backoff_seconds length ({len(self.retry_backoff_seconds)}) "
                f"must be >= max_retries ({self.max_retries}) — the retry loop "
                f"indexes retry_backoff_seconds[attempt] for "
                f"attempt in [0, max_retries-1]; configure either a longer "
                f"retry_backoff_seconds tuple or a smaller max_retries"
            )
        return self


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


class MCPServerConfig(_StrictModel):
    """HTTP MCP write server (``mindwire-mcp-server``) runtime settings.

    The write server is a Phase 1 Feature 3-A addition (sub-PR 2). It is
    a *separate* process from the in-process MCP server used by
    claude-code during watcher dispatch — the `[claude_code]` /
    ``extra_mcp_servers`` tree is unrelated. ``mindwire-mcp`` (the
    read-only stub at :mod:`spirrow_mindwire.mcp_server`) is also
    unrelated and unaffected by this config block; see
    docs/feature-3-design.md §2.1 for the 3-layer separation rationale.

    Defaults are production-ready (see [[feedback_config_defaults_first]]):

    - ``host = "127.0.0.1"`` matches the Phanthand precedent — the write
      server exposes file-system-mutating tools, so binding to localhost
      is the safe-by-default surface. Operators who run the write server
      on a separate host must override this explicitly.
    - ``port = 7400`` sits one decade above phanthand's ``7300`` so the
      two Spirrow services form an adjacent range without colliding.
    - ``api_key_env`` names the environment variable holding the bearer
      token (= the *name*, not the value — secrets stay out of TOML and
      the value is read at process start).
    """

    host: str = "127.0.0.1"
    port: int = Field(default=7400, ge=1, le=65535)
    api_key_env: str = "MINDWIRE_MCP_API_KEY"


class LoopWatchConfig(_StrictModel):
    """One ``(thread, role)`` the Stage 3 loop daemon watches (ADR-2026-05-21-06 §7).

    Mirrors :class:`spirrow_mindwire.magickit.watcher.WatchSpec` in config form:
    the daemon turns each entry into a ``WatchSpec`` at startup. ``instance_id``
    is optional (defaults to ``mint_instance_id(role)`` = ``"{role}-1"`` in the
    watcher); ``baseline`` follows the watcher default (mark current messages
    seen without dispatching, so the daemon acts only on messages arriving after
    start).

    Topology invariant (T-stage3-loop-wiring msg-385 §5(a)): each *thread* must
    carry exactly **one** auto-replying role — the watcher dedups per
    ``(thread, msg_id)`` and the SDK adapters reply to every message, so two
    auto-reply roles on one thread would steal each other's messages / ping-pong.
    The operator is responsible for not listing two auto-reply roles for the same
    ``thread_id`` (the naysayer's PR-review thread is wired separately via
    :class:`spirrow_mindwire.orchestrator.PrReviewOrchestrator`).
    """

    thread_id: str
    role: Role
    instance_id: str = ""
    baseline: bool = True


class Stage3LoopConfig(_StrictModel):
    """Stage 3 autonomous-loop daemon (``mindwire-loop``) settings.

    Distinct from the Phase 0 relay watcher (:class:`WatcherConfig`) — this
    configures the ``ChatroomWatcher``-backed RoleAdapter loop (proposer /
    implementer / naysayer over the magickit chatroom), not the file-relay
    ``ThreadDispatcher``.

    Secrets and inference endpoints stay **out** of this config (and out of
    TOML): the implementer's inference base URL
    (``MINDWIRE_IMPLEMENTER_BASE_URL``), the design-time naysayer's base URL
    (``MINDWIRE_NAYSAYER_BASE_URL`` → Lexora's ``naysayer`` / Gemini tier; the
    naysayer adapter refuses to spawn without it, so leaving it unset silently
    disables the loop's independent-review leg), the Lexora URL
    (``MINDWIRE_LEXORA_URL``) and the naysayer's GitHub token
    (``MINDWIRE_NAYSAYER_GITHUB_TOKEN``) are resolved from the environment by the
    adapters themselves at spawn (the same "env name, not value, in config"
    principle as :class:`PhanthandConfig`). The magickit chatroom MCP URL
    (``MINDWIRE_MAGICKIT_MCP_URL``) likewise comes from the env / its in-code
    default. See ``docs/deploy.md`` for the unattended-daemon runbook (ADR-18).
    """

    project: str = "spirrow-mindwire"
    poll_interval_seconds: float = Field(default=5.0, gt=0)
    repo_dir: Path | None = None
    """Working directory the proposer + implementer SDK sessions operate in.

    Required to run the loop (the implementer edits / builds / commits here).
    ``None`` is a config error surfaced at daemon startup, not load time, so a
    process that only reads other settings is unaffected.
    """

    watches: tuple[LoopWatchConfig, ...] = ()


class ConductorConfig(_StrictModel):
    """NEXT-driven design-loop conductor (``mindwire-loop --mode conductor``) settings.

    The conductor (``T-cross-thread-relay-conductor`` Tier-C decide msg-523) drives **one** design
    task thread serially: it reads the latest message's ``NEXT: <participant>`` handoff and
    dispatches exactly that one participant, replacing the design-loop ``ChatroomWatcher``
    auto-reply intake (Obj1). The adapters, inference endpoints, ``project`` and ``repo_dir`` are
    shared with :class:`Stage3LoopConfig` (``[loop]``) — this block only adds the conductor knobs:

    - ``task_thread_id`` — the single design thread the conductor drives (1 task = 1 thread).
    - ``roster`` — the chatroom identity→role map (e.g. ``{"Bohr": "proposer", ...}``). The
      ``NEXT:`` vocabulary is the persona name, and the conductor authors each reply under that
      name; the reserved ``human`` / ``none`` sentinels are not roster entries.
    - ``naysayer_identity`` — the roster persona that fills the independent naysayer slot; Obj2's
      forced-consultation recognises a naysayer turn by this mapping, so it **must** map to the
      naysayer role in ``roster`` (the :class:`~spirrow_mindwire.conductor.core.Conductor` ctor
      enforces this fail-loud — Tier B msg-529).

    All three required fields default empty and are validated at **daemon startup** (not load time,
    mirroring :attr:`Stage3LoopConfig.repo_dir`) so a process that only reads other settings is
    unaffected.
    """

    task_thread_id: str = ""
    roster: dict[str, Role] = Field(default_factory=dict)
    naysayer_identity: str = ""
    max_rounds: int = Field(default=40, ge=1)
    """Runaway backstop: the maximum number of serial turns before the conductor stops.

    Mirrors the :class:`~spirrow_mindwire.conductor.core.Conductor` default (40); a turn that does
    not converge to ``NEXT: human`` / ``none`` within this many rounds stops at the round cap.
    """


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
    mcp_server: MCPServerConfig = Field(default_factory=MCPServerConfig)
    loop: Stage3LoopConfig = Field(default_factory=Stage3LoopConfig)
    conductor: ConductorConfig = Field(default_factory=ConductorConfig)

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
    "ConductorConfig",
    "ExtraMCPServerConfig",
    "LoggingConfig",
    "LoopWatchConfig",
    "MCPServerConfig",
    "MindwireSettings",
    "PathsConfig",
    "PhanthandConfig",
    "Stage3LoopConfig",
    "WatcherConfig",
    "load_settings",
]
