"""T13 Dispatcher core — ADR-2026-05-21-06 §3.2 / §4 (Phase 1).

- Step 1' (skeleton): I8 SessionState FSM + dict-backed AdapterRegistry.
- Step 2 (live dispatch loop): :class:`Dispatcher` — spawn_role / dispatch
  (I4 dedup, I9 per-session FIFO) / reply completion (I5 idempotency_key
  via :class:`ChatroomGateway`) / on_event_log (I7 isolation).

Out of scope (Step 3): §8 smoke test, ChatroomWatcher (T14) intake, the
concrete magickit-MCP ChatroomGateway (runtime wiring).
"""

from __future__ import annotations

from .core import (
    Dispatcher,
    EventSink,
    NoQualifiedAdapterError,
    UnknownSessionError,
)
from .dedup import DEFAULT_DEDUP_SET_SIZE, EventDedup
from .gateway import ChatroomGateway
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
    "DEFAULT_DEDUP_SET_SIZE",
    "HALT_NOOP_STATES",
    "TERMINAL_STATES",
    "AdapterAlreadyRegisteredError",
    "AdapterNotFoundError",
    "ChatroomGateway",
    "Dispatcher",
    "EventDedup",
    "EventSink",
    "InMemoryAdapterRegistry",
    "InvalidSessionTransitionError",
    "NoQualifiedAdapterError",
    "SessionStateMachine",
    "UnknownSessionError",
    "is_halt_noop",
    "validate_session_transition",
]
