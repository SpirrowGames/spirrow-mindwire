"""I8 SessionState FSM — ADR-2026-05-21-06 §4 (T13 dispatcher skeleton).

Table-driven enforcement of the §4 I8 SessionState transition rules. The
*pattern* is borrowed from
:mod:`spirrow_mindwire.lifecycle.transitions` (the Phase 0 ThreadMeta
FSM), but the states / semantics are entirely different (session
lifecycle, not thread lifecycle) and **no Phase 0 code is reused**
(ADR-06 §6 — pattern-level reuse only).

This is the Step 1' skeleton: the validated state machine the dispatcher
core (Step 2) drives while managing adapter sessions. It owns no I/O and
holds no adapter references.
"""

from __future__ import annotations

from spirrow_mindwire.value_objects import SessionState

# I8 transition table (ADR-06 §4):
# - idle -> processing        (deliver_event received)
# - processing -> idle        (after reply emit)
# - idle | processing -> halting   (halt() called)
# - halting -> halted         (graceful stop complete)
# - any non-terminal -> failed     (fatal exception, terminal)
# - halted | failed: terminal, no outgoing transitions
_ALLOWED_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.IDLE: frozenset(
        {SessionState.PROCESSING, SessionState.HALTING, SessionState.FAILED}
    ),
    SessionState.PROCESSING: frozenset(
        {SessionState.IDLE, SessionState.HALTING, SessionState.FAILED}
    ),
    SessionState.HALTING: frozenset({SessionState.HALTED, SessionState.FAILED}),
    SessionState.HALTED: frozenset(),
    SessionState.FAILED: frozenset(),
}

TERMINAL_STATES: frozenset[SessionState] = frozenset({SessionState.HALTED, SessionState.FAILED})
"""States with no outgoing transitions (ADR-06 §4 I8; recovery = fresh spawn)."""

HALT_NOOP_STATES: frozenset[SessionState] = frozenset(
    {SessionState.HALTED, SessionState.FAILED, SessionState.HALTING}
)
"""States where ``halt()`` is an idempotent no-op (ADR-06 §4 I8)."""


class InvalidSessionTransitionError(ValueError):
    """Raised when a SessionState transition violates I8 (ADR-06 §4)."""

    def __init__(self, old: SessionState, new: SessionState) -> None:
        allowed = sorted(s.value for s in _ALLOWED_TRANSITIONS[old])
        super().__init__(
            f"session transition {old.value!r} -> {new.value!r} is not allowed; "
            f"allowed from {old.value!r}: {allowed}"
        )
        self.old = old
        self.new = new


def validate_session_transition(old: SessionState, new: SessionState) -> None:
    """Raise :class:`InvalidSessionTransitionError` if ``(old, new)`` violates I8."""
    if new not in _ALLOWED_TRANSITIONS[old]:
        raise InvalidSessionTransitionError(old, new)


def is_halt_noop(state: SessionState) -> bool:
    """Return True if ``halt()`` on a session in ``state`` is an idempotent no-op (I8)."""
    return state in HALT_NOOP_STATES


class SessionStateMachine:
    """Minimal validated SessionState holder (T13 Step 1' skeleton).

    The dispatcher core (Step 2) owns one per live session and calls
    :meth:`transition` as lifecycle events occur; illegal moves raise
    :class:`InvalidSessionTransitionError`. Starts in
    :attr:`SessionState.IDLE` (post-spawn).
    """

    def __init__(self, state: SessionState = SessionState.IDLE) -> None:
        self._state = state

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in TERMINAL_STATES

    def transition(self, new: SessionState) -> SessionState:
        """Validate + apply a transition; return the new state.

        Leaves the current state unchanged if the move is rejected.
        """
        validate_session_transition(self._state, new)
        self._state = new
        return self._state


__all__ = [
    "HALT_NOOP_STATES",
    "TERMINAL_STATES",
    "InvalidSessionTransitionError",
    "SessionStateMachine",
    "is_halt_noop",
    "validate_session_transition",
]
