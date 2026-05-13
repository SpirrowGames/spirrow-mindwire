"""Lifecycle state machine for Spirrow MindWire ThreadMeta (Feature 2).

The single entry point for ThreadStatus transitions is
:func:`transition_state`, which loads the current meta.yaml,
validates the transition against :data:`_ALLOWED_TRANSITIONS`, and
writes the updated meta atomically. Callers must NOT mutate
``meta.yaml`` outside this module (operator manual edits via
``yq`` etc. are tolerated under the Phase 0 race-acceptance contract;
see ``docs/feature-2-design.md`` §3.6).

Forbidden transitions raise :class:`InvalidTransitionError`.
"""

from __future__ import annotations

from .transitions import (
    REQUEUE_STATES,
    TERMINAL_STATES,
    InvalidTransitionError,
    bump_retry_count,
    set_awaiting_from,
    transition_state,
)

__all__ = [
    "REQUEUE_STATES",
    "TERMINAL_STATES",
    "InvalidTransitionError",
    "bump_retry_count",
    "set_awaiting_from",
    "transition_state",
]
