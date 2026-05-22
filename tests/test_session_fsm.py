"""Tests for the I8 SessionState FSM (ADR-06 §4, T13 PR-C skeleton)."""

from __future__ import annotations

import pytest

from spirrow_mindwire.dispatcher.session_fsm import (
    TERMINAL_STATES,
    InvalidSessionTransitionError,
    SessionStateMachine,
    is_halt_noop,
    validate_session_transition,
)
from spirrow_mindwire.value_objects import SessionState

_VALID = [
    (SessionState.IDLE, SessionState.PROCESSING),
    (SessionState.IDLE, SessionState.HALTING),
    (SessionState.IDLE, SessionState.FAILED),
    (SessionState.PROCESSING, SessionState.IDLE),
    (SessionState.PROCESSING, SessionState.HALTING),
    (SessionState.PROCESSING, SessionState.FAILED),
    (SessionState.HALTING, SessionState.HALTED),
    (SessionState.HALTING, SessionState.FAILED),
]

_INVALID = [
    (SessionState.IDLE, SessionState.HALTED),
    (SessionState.PROCESSING, SessionState.HALTED),
    (SessionState.HALTING, SessionState.IDLE),
    (SessionState.HALTED, SessionState.IDLE),
    (SessionState.HALTED, SessionState.PROCESSING),
    (SessionState.FAILED, SessionState.IDLE),
    (SessionState.PROCESSING, SessionState.PROCESSING),  # no self-loop
]


@pytest.mark.parametrize(("old", "new"), _VALID)
def test_valid_transitions(old: SessionState, new: SessionState) -> None:
    validate_session_transition(old, new)  # no raise


@pytest.mark.parametrize(("old", "new"), _INVALID)
def test_invalid_transitions(old: SessionState, new: SessionState) -> None:
    with pytest.raises(InvalidSessionTransitionError):
        validate_session_transition(old, new)


def test_terminal_states_have_no_outgoing() -> None:
    for terminal in TERMINAL_STATES:
        for target in SessionState:
            with pytest.raises(InvalidSessionTransitionError):
                validate_session_transition(terminal, target)


def test_halt_noop_states() -> None:
    assert is_halt_noop(SessionState.HALTED)
    assert is_halt_noop(SessionState.FAILED)
    assert is_halt_noop(SessionState.HALTING)
    assert not is_halt_noop(SessionState.IDLE)
    assert not is_halt_noop(SessionState.PROCESSING)


def test_state_machine_happy_path() -> None:
    sm = SessionStateMachine()
    assert sm.state is SessionState.IDLE
    assert not sm.is_terminal
    sm.transition(SessionState.PROCESSING)
    sm.transition(SessionState.IDLE)
    sm.transition(SessionState.HALTING)
    assert sm.transition(SessionState.HALTED) is SessionState.HALTED
    assert sm.is_terminal


def test_state_machine_rejects_illegal_and_keeps_state() -> None:
    sm = SessionStateMachine()
    with pytest.raises(InvalidSessionTransitionError):
        sm.transition(SessionState.HALTED)  # idle -> halted not allowed
    assert sm.state is SessionState.IDLE  # unchanged after a rejected transition
