"""Conductor: NEXT-driven single-thread design-loop driver (msg-520 / Tier-C decide msg-523).

PR-1 (this package) is the pure, daemon-independent logic: :mod:`handoff` (parse + resolve the
``NEXT:`` directive) and :mod:`core` (the serial drive loop with naysayer enforcement and the D-4
stop conditions). Daemon wiring (``mindwire-loop --mode conductor`` + config + ChatroomWatcher
auto-reply retirement) is PR-2.
"""

from __future__ import annotations

from .control import ControlState, LoopControl, LoopControlReader
from .core import Conductor, ConductorDispatcher, ConductorOutcome, StopReason
from .handoff import (
    HUMAN_TOKEN,
    NONE_TOKEN,
    Handoff,
    HandoffKind,
    build_handoff_protocol_block,
    parse_next_token,
    resolve_handoff,
)
from .head_skip import (
    BASE,
    CAP,
    HEAD_CACHE_TTL,
    REPORT_MODE_ENV,
    REPORT_MODE_VALUE,
    STOP_TOKENS,
    Decision,
    Record,
    Verdict,
    can_reuse_cached_parse,
    commit_launch,
    commit_observation,
    decide,
    needs_head_reparse,
    parse_head_token,
    record_from_json,
    record_to_json,
    verdict_to_json,
)

__all__ = [
    "BASE",
    "CAP",
    "HEAD_CACHE_TTL",
    "HUMAN_TOKEN",
    "NONE_TOKEN",
    "REPORT_MODE_ENV",
    "REPORT_MODE_VALUE",
    "STOP_TOKENS",
    "Conductor",
    "ConductorDispatcher",
    "ConductorOutcome",
    "ControlState",
    "Decision",
    "Handoff",
    "HandoffKind",
    "LoopControl",
    "LoopControlReader",
    "Record",
    "StopReason",
    "Verdict",
    "build_handoff_protocol_block",
    "can_reuse_cached_parse",
    "commit_launch",
    "commit_observation",
    "decide",
    "needs_head_reparse",
    "parse_head_token",
    "parse_next_token",
    "record_from_json",
    "record_to_json",
    "resolve_handoff",
    "verdict_to_json",
]
