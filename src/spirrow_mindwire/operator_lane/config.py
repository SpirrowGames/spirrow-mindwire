"""``<data_dir>\\config\\operator_lane.json`` — the lane's switch and its settings.

The lane is OFF unless this file says ``"enabled": true`` (D1': ``operator_lane.enabled``). A guard
that finds the isolation open turns it off again by rewriting ``enabled`` to ``false`` with the
reason (D2'' (b-3) / D2⁗); only the operator turns it back on, by hand, after fixing the cause.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .state import atomic_write_bytes, atomic_write_text, iso

DEFAULT_PROJECTS = ("spirrow-mindwire",)


@dataclass(frozen=True)
class LaneConfig:
    enabled: bool = False
    #: D5: the projects whose operator heads the lane takes. Initial value: mindwire only.
    projects: tuple[str, ...] = DEFAULT_PROJECTS
    #: The chatroom author the lane posts as, and whose markers ``posted()`` looks for.
    operator_author: str = "operator"
    claude_path: str = "claude"
    #: D2'' (b-2): the OS user the session runs as, and where its password lives.
    run_as_user: str = "mw-operator"
    credential_target: str = "MindWire-Operator-Lane/mw-operator"
    #: Working directory of the session (a directory ``run_as_user`` can write to).
    session_workdir: str = ""
    #: D2⁗: the firewall rule audited before every launch, and the magickit addresses it must block.
    firewall_rule_name: str = "MindWire-Operator-Block-Magickit"
    magickit_addresses: tuple[str, ...] = ()
    #: D2'' (a): repositories whose ``main`` must require a pull request / status checks.
    github_repos: tuple[str, ...] = ()
    #: D2: the session's wall-clock limit (the runner kills the Job past it).
    session_timeout_sec: int = 3600
    #: D5: spawn failures (no session started) are retried up to this many attempts in total.
    max_spawn_attempts: int = 2
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


def _strings(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"expected a list of strings, got {value!r}")
    return tuple(value)


# On Windows a read that lands while another writer's ``os.replace`` swaps the file in is refused
# (PermissionError); it is transient, so the config readers retry briefly (PR #505 gate).
_READ_RETRIES = 50


def _read_text(path: Path) -> str:
    for attempt in range(_READ_RETRIES + 1):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt >= _READ_RETRIES:
                raise
            time.sleep(0.02)
    raise AssertionError("unreachable")  # pragma: no cover


def load_config(path: Path) -> LaneConfig:
    """Read the config. A missing file is a disabled lane; a malformed one raises ``ValueError``."""
    try:
        raw = json.loads(_read_text(path))
    except FileNotFoundError:
        return LaneConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object")
    base = LaneConfig()
    return LaneConfig(
        enabled=raw.get("enabled") is True,
        projects=_strings(raw.get("projects"), base.projects),
        operator_author=str(raw.get("operator_author") or base.operator_author),
        claude_path=str(raw.get("claude_path") or base.claude_path),
        run_as_user=str(raw.get("run_as_user") or base.run_as_user),
        credential_target=str(raw.get("credential_target") or base.credential_target),
        session_workdir=str(raw.get("session_workdir") or ""),
        firewall_rule_name=str(raw.get("firewall_rule_name") or base.firewall_rule_name),
        magickit_addresses=_strings(raw.get("magickit_addresses"), ()),
        github_repos=_strings(raw.get("github_repos"), ()),
        session_timeout_sec=int(raw.get("session_timeout_sec") or base.session_timeout_sec),
        max_spawn_attempts=int(raw.get("max_spawn_attempts") or base.max_spawn_attempts),
        raw=raw,
    )


def _read_bytes(path: Path) -> bytes:
    for attempt in range(_READ_RETRIES + 1):
        try:
            return path.read_bytes()
        except PermissionError:
            if attempt >= _READ_RETRIES:
                raise
            time.sleep(0.02)
    raise AssertionError("unreachable")  # pragma: no cover


def backup_name(path: Path, now: datetime) -> str:
    """``<config>.malformed-<YYYYMMDDTHHMMSSZ>-<pid>-<hex6>.bak`` (T-operator-task-lane msg-7660).

    Only ``[A-Za-z0-9._-]``: ``iso(now)`` carries colons, which Windows refuses in a file name, so
    the time is written compactly here and ``iso`` is kept for the ``disabled_at`` value only. The
    pid and the random suffix keep two calls in the same second from sharing (overwriting) a name.
    """
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{path.name}.malformed-{stamp}-{os.getpid()}-{secrets.token_hex(3)}.bak"


def disable(path: Path, *, reason: str, now: datetime) -> None:
    """Turn the lane off (guard_open). Everything else in the file is kept as it was.

    A config that cannot be read (bad JSON, bad UTF-8, not an object) is still overwritten with
    ``enabled: false``: leaving it alone would let a later hand fix of the syntax bring back
    ``"enabled": true`` and un-latch the guard (msg-7657). Its bytes are first copied to a
    :func:`backup_name` file so the operator's settings are not lost; restoring them is by hand
    (``docs/operator-lane.md``). The disabling write always wins over the copy: a copy that fails
    is recorded in ``disabled_reason`` and does not stop the write; only the write's own failure
    raises (msg-7660).
    """
    try:
        original: bytes | None = _read_bytes(path)
    except FileNotFoundError:
        original = None
    raw: dict[str, Any] = {}
    unreadable = False
    if original is not None:
        try:
            parsed = json.loads(original.decode("utf-8"))
        except ValueError:  # JSONDecodeError and UnicodeDecodeError are both ValueError
            parsed = None
        if isinstance(parsed, dict):
            raw = parsed
        else:
            unreadable = True
    if unreadable:
        assert original is not None
        bak = path.with_name(backup_name(path, now))
        try:
            atomic_write_bytes(bak, original, unique_tmp=True)
        except OSError as exc:
            reason = f"{reason}; original config unreadable, backup failed: {type(exc).__name__}"
        else:
            reason = f"{reason}; original config unreadable, preserved at {bak.name}"
    raw["enabled"] = False
    raw["disabled_reason"] = reason
    raw["disabled_at"] = iso(now)
    # No mutex owns the config file, so a concurrent writer must never share our temp file.
    atomic_write_text(path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n", unique_tmp=True)


__all__ = ["DEFAULT_PROJECTS", "LaneConfig", "backup_name", "disable", "load_config"]
