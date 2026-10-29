"""``<data_dir>\\config\\operator_lane.json`` — the lane's switch and its settings.

The lane is OFF unless this file says ``"enabled": true`` (D1': ``operator_lane.enabled``). A guard
that finds the isolation open turns it off again by rewriting ``enabled`` to ``false`` with the
reason (D2'' (b-3) / D2⁗); only the operator turns it back on, by hand, after fixing the cause.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .state import atomic_write_text, iso

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


def load_config(path: Path) -> LaneConfig:
    """Read the config. A missing file is a disabled lane; a malformed one raises ``ValueError``."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
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


def disable(path: Path, *, reason: str, now: datetime) -> None:
    """Turn the lane off (guard_open). Everything else in the file is kept as it was."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw["enabled"] = False
    raw["disabled_reason"] = reason
    raw["disabled_at"] = iso(now)
    atomic_write_text(path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")


__all__ = ["DEFAULT_PROJECTS", "LaneConfig", "disable", "load_config"]
