"""``sweep.json`` key ``settled_close`` (all optional; absent means disabled).

::

    {"enabled": false, "author": "settled-closer", "role": "integrator",
     "grace_seconds": 86400, "max_attempts": 10, "breaker": 3}

``enabled`` is the one switch for the sweep tick (msg-8455 §2 6-3). It ships ``false`` and
turning it on is a Tier-C decision together with registering the closer in Prismind and adding
it to magickit's ``settled_close.closers``; this repository never turns it on. The other keys:

* ``author`` / ``role`` — who the close is sent as. The defaults are the dedicated closer
  (``settled-closer`` + ``integrator``; the name lives once, in
  ``identity/classification.py::SETTLED_CLOSER``, which is also the one machine
  ``MACHINE_ROLE_EXCEPTIONS`` lets hold ``integrator`` — T-done-disposition-auto-close
  msg-8589 / msg-8601 §1-2 S-5). The former default ``orchestrator`` has
  ``allowed_roles=[]`` and was therefore refused on every close (msg-8556 §1).
* ``grace_seconds`` — the client's copy of magickit's ``settled_close.grace_seconds`` (default
  24h). It only shapes the prediction: magickit judges the grace itself.
* ``max_attempts`` — at most this many close calls per pass. ``breaker`` — this many
  consecutive refusals the client cannot place end the pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..identity.classification import SETTLED_CLOSER

DEFAULT_AUTHOR = SETTLED_CLOSER
DEFAULT_ROLE = "integrator"
DEFAULT_GRACE_SECONDS = 24 * 60 * 60
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_BREAKER = 3


class SettledCloseConfigError(ValueError):
    """``sweep.json``'s ``settled_close`` cannot be used. Reported; nothing is read or closed."""


@dataclass(frozen=True)
class SettledCloseConfig:
    enabled: bool = False
    author: str = DEFAULT_AUTHOR
    role: str = DEFAULT_ROLE
    grace_seconds: int = DEFAULT_GRACE_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    breaker: int = DEFAULT_BREAKER


def _positive_int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SettledCloseConfigError(f"settled_close.{key} must be an integer >= 1, got {value!r}")
    return value


def _name(raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise SettledCloseConfigError(f"settled_close.{key} must be a non-empty string")
    return value


def parse_settled_close_config(sweep: dict[str, Any]) -> SettledCloseConfig:
    raw = sweep.get("settled_close")
    if raw is None:
        return SettledCloseConfig()
    if not isinstance(raw, dict):
        raise SettledCloseConfigError("settled_close must be an object")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise SettledCloseConfigError(f"settled_close.enabled must be a boolean, got {enabled!r}")
    return SettledCloseConfig(
        enabled=enabled,
        author=_name(raw, "author", DEFAULT_AUTHOR),
        role=_name(raw, "role", DEFAULT_ROLE),
        grace_seconds=_positive_int(raw, "grace_seconds", DEFAULT_GRACE_SECONDS),
        max_attempts=_positive_int(raw, "max_attempts", DEFAULT_MAX_ATTEMPTS),
        breaker=_positive_int(raw, "breaker", DEFAULT_BREAKER),
    )


def sweep_projects(sweep: dict[str, Any]) -> list[str]:
    """Every project ``sweep.json`` names: ``clones`` keys, ``projects`` and ``candidates``.

    This is *not* the whole chatroom: there is no MCP read of Conclair's project set
    (``/v1/projects`` is reachable only from magickit's dashboard, ``/dashboard/_ops``), and the
    registry's ``list_projects`` is a different set. A full inventory passes ``--projects``.
    """
    out: set[str] = set()
    clones = sweep.get("clones")
    if isinstance(clones, dict):
        out.update(k for k in clones if isinstance(k, str) and k)
    projects = sweep.get("projects")
    if isinstance(projects, list):
        out.update(p for p in projects if isinstance(p, str) and p.strip())
    candidates = sweep.get("candidates")
    if isinstance(candidates, list):
        for entry in candidates:
            project = entry.get("project") if isinstance(entry, dict) else None
            if isinstance(project, str) and project:
                out.add(project)
    return sorted(p.strip() for p in out)


__all__ = [
    "SettledCloseConfig",
    "SettledCloseConfigError",
    "parse_settled_close_config",
    "sweep_projects",
]
