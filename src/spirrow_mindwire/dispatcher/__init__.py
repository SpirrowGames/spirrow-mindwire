"""T13 Dispatcher core — ADR-2026-05-21-06 §3.2 / §4 (Phase 1 skeleton).

Step 1' scope: the I8 SessionState FSM and the dict-backed
AdapterRegistry. The live dispatch loop (I9 FIFO / I7 callback isolation /
I4 dedup / I5 idempotency_key) + ChatroomGateway (T12) integration is
Step 2.
"""

from __future__ import annotations

from .registry import (
    AdapterAlreadyRegisteredError,
    AdapterNotFoundError,
    InMemoryAdapterRegistry,
)
from .session_fsm import (
    HALT_NOOP_STATES,
    TERMINAL_STATES,
    InvalidSessionTransitionError,
    SessionStateMachine,
    is_halt_noop,
    validate_session_transition,
)

__all__ = [
    "HALT_NOOP_STATES",
    "TERMINAL_STATES",
    "AdapterAlreadyRegisteredError",
    "AdapterNotFoundError",
    "InMemoryAdapterRegistry",
    "InvalidSessionTransitionError",
    "SessionStateMachine",
    "is_halt_noop",
    "validate_session_transition",
]
