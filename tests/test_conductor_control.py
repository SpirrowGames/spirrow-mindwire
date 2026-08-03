"""Tests for per-project loop control (``run`` / ``supervised`` / ``hold``).

The load-bearing behaviour here is the *direction* each failure falls in, so most of these tests are
about what happens when something goes wrong rather than when it goes right:

- a project nobody has configured runs (magickit answers ``run`` / ``configured: false``);
- a state that cannot be read holds;

and those two must not collapse into one code path, because the whole inversion depends on
distinguishing "no one has stopped this" from "I do not know whether someone stopped this".
"""

from __future__ import annotations

from typing import Any

import pytest

from spirrow_mindwire.conductor.control import (
    BASELINE_CONTROL_STATE,
    FAILSAFE_CONTROL_STATE,
    ControlState,
    LoopControlReader,
    parse_control_payload,
)


class _FakeMcp:
    """Records calls; returns a scripted result or raises a scripted error per tool."""

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        result = self._results.get(name)
        if isinstance(result, Exception):
            raise result
        return result


def _payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "project": "spirrow-voxelworld",
        "desired_state": "run",
        "desired_actor": "human",
        "desired_at": "2026-08-04T00:00:00Z",
        "observed_state": None,
        "observed_actor": None,
        "observed_at": None,
        "configured": True,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# parse_control_payload — pure, so the fail-safe direction is testable alone
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("run", ControlState.RUN),
        ("supervised", ControlState.SUPERVISED),
        ("hold", ControlState.HOLD),
    ],
)
def test_parse_accepts_every_state(raw: str, expected: ControlState) -> None:
    assert parse_control_payload(_payload(desired_state=raw)) is expected


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "run",  # a bare string, e.g. an unparsed text content block
        [],
        {},  # no desired_state at all
        {"desired_state": None},
        {"desired_state": 3},
        {"desired_state": "paused"},  # a state this build does not know about
        {"desired_state": "RUN"},  # the enum is exact; near-misses are not guessed at
    ],
)
def test_parse_fails_safe_on_anything_unusable(payload: Any) -> None:
    # Every one of these means "I could not learn the state". There is no safe guess, and guessing
    # `run` would hand the loop autonomy nobody granted.
    assert parse_control_payload(payload) is FAILSAFE_CONTROL_STATE
    assert FAILSAFE_CONTROL_STATE is ControlState.HOLD


def test_unconfigured_project_is_not_a_failure() -> None:
    # THE distinction. magickit answers an absent row with a normal result carrying `run`, so it
    # flows through the same path as any other value — it must NOT be mistaken for a read failure.
    unconfigured = _payload(desired_state="run", configured=False)
    assert parse_control_payload(unconfigured) is ControlState.RUN


def test_baseline_and_failsafe_are_different_states() -> None:
    # "No control source was wired" (baseline) and "the control source is unreachable" (failsafe)
    # are different facts and must stay different values; collapsing them would make a bare
    # Conductor unrunnable or an unreachable control plane autonomous.
    assert BASELINE_CONTROL_STATE is ControlState.SUPERVISED
    assert FAILSAFE_CONTROL_STATE is ControlState.HOLD


# --------------------------------------------------------------------------- #
# LoopControlReader.read
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_read_calls_loop_control_get_for_its_project() -> None:
    mcp = _FakeMcp({"loop_control_get": _payload(desired_state="hold")})
    state = await LoopControlReader(mcp, project="spirrow-voxelworld").read()
    assert state is ControlState.HOLD
    assert mcp.calls == [("loop_control_get", {"project": "spirrow-voxelworld"})]


@pytest.mark.anyio
async def test_read_fails_safe_on_a_transport_error() -> None:
    # Not re-raised: a daemon that dies with a traceback is both harder to reason about and
    # invisible to the dashboard, where a recorded hold is not.
    mcp = _FakeMcp({"loop_control_get": ConnectionError("magickit unreachable")})
    assert await LoopControlReader(mcp, project="p").read() is ControlState.HOLD


# --------------------------------------------------------------------------- #
# LoopControlReader.report_observed
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_report_observed_writes_once_per_change() -> None:
    # Called every round but only writes on a change: the conductor re-reads per round, and a write
    # per round would be pure noise on a loop that mostly sits in one state.
    mcp = _FakeMcp({"loop_control_report_observed": {}})
    reader = LoopControlReader(mcp, project="p")
    for state in (ControlState.RUN, ControlState.RUN, ControlState.HOLD, ControlState.HOLD):
        await reader.report_observed(state)
    assert [args["state"] for _, args in mcp.calls] == ["run", "hold"]
    assert mcp.calls[0][0] == "loop_control_report_observed"
    assert mcp.calls[0][1]["actor"] == "mindwire-conductor"


@pytest.mark.anyio
async def test_report_observed_swallows_failures_and_retries_next_time() -> None:
    # Observability must never stop the loop — the run's decision is already made. And because the
    # de-dup marker only advances on success, a dropped report is retried rather than lost, so the
    # dashboard converges instead of showing a stale value forever.
    mcp = _FakeMcp({"loop_control_report_observed": TimeoutError("slow")})
    reader = LoopControlReader(mcp, project="p")
    await reader.report_observed(ControlState.HOLD)  # must not raise
    await reader.report_observed(ControlState.HOLD)
    assert len(mcp.calls) == 2


@pytest.mark.anyio
async def test_reader_never_calls_loop_control_set() -> None:
    # INV-4 at the transport edge: the loop is given the half of the contract that reports what it
    # saw, never the half that decides what it should do. A regression here would let the loop
    # release its own hold.
    mcp = _FakeMcp({"loop_control_get": _payload(), "loop_control_report_observed": {}})
    reader = LoopControlReader(mcp, project="p")
    await reader.report_observed(await reader.read())
    assert "loop_control_set" not in {name for name, _ in mcp.calls}
