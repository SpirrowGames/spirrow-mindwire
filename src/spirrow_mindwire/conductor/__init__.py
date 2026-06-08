"""Conductor: NEXT-driven single-thread design-loop driver (msg-520 / Tier-C decide msg-523).

PR-1 (this package) is the pure, daemon-independent logic: :mod:`handoff` (parse + resolve the
``NEXT:`` directive) and :mod:`core` (the serial drive loop with naysayer enforcement and the D-4
stop conditions). Daemon wiring (``mindwire-loop --mode conductor`` + config + ChatroomWatcher
auto-reply retirement) is PR-2.
"""

from __future__ import annotations

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

__all__ = [
    "HUMAN_TOKEN",
    "NONE_TOKEN",
    "Conductor",
    "ConductorDispatcher",
    "ConductorOutcome",
    "Handoff",
    "HandoffKind",
    "StopReason",
    "build_handoff_protocol_block",
    "parse_next_token",
    "resolve_handoff",
]
