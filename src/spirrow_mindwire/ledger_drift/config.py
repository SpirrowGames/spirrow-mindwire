"""``sweep.json``'s ``ledger_drift`` section (design v0.3, msg-9524 Obj-1-2 / msg-9526 Obj-2-2).

All keys optional::

    "ledger_drift": {
      "enabled": false,           # the tick does nothing (and calls nothing) unless true
      "window_days": 30,          # (a) / (a') / unlinked read PRs merged in the last N days
      "stale_after_minutes": 60,  # the digest calls an older result "古い結果"
      "bases": ["develop"]        # the base branches whose merges count
    }

``stale_after_minutes`` is meant to be about three ticks (msg-9526). The tick period is not fixed
in this repository (the Task Scheduler registration sets it), so the default is a stated guess,
and an operator sets it from the real period.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_WINDOW_DAYS = 30
DEFAULT_STALE_AFTER_MINUTES = 60
DEFAULT_BASES = ("develop",)


@dataclass(frozen=True)
class LedgerDriftConfig:
    enabled: bool = False
    window_days: int = DEFAULT_WINDOW_DAYS
    stale_after_minutes: int = DEFAULT_STALE_AFTER_MINUTES
    bases: tuple[str, ...] = DEFAULT_BASES


def parse_ledger_drift_config(sweep: Mapping[str, Any]) -> LedgerDriftConfig:
    section = sweep.get("ledger_drift")
    if section is None:
        return LedgerDriftConfig()
    if not isinstance(section, dict):
        raise ValueError("ledger_drift must be an object")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("ledger_drift.enabled must be true or false")
    window = _positive_int(section, "window_days", DEFAULT_WINDOW_DAYS)
    stale = _positive_int(section, "stale_after_minutes", DEFAULT_STALE_AFTER_MINUTES)
    bases = section.get("bases", list(DEFAULT_BASES))
    if (
        not isinstance(bases, list)
        or not bases
        or not all(isinstance(b, str) and b.strip() for b in bases)
    ):
        raise ValueError("ledger_drift.bases must be a non-empty list of branch names")
    return LedgerDriftConfig(
        enabled=enabled,
        window_days=window,
        stale_after_minutes=stale,
        bases=tuple(b.strip() for b in bases),
    )


def _positive_int(section: Mapping[str, Any], key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"ledger_drift.{key} must be a positive integer")
    return value
