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
from spirrow_mindwire.magickit.client import MagickitMcpError, raise_if_envelope


class _FakeMcp:
    """Records calls; returns a scripted result or raises a scripted error per tool.

    Mimics the *client boundary*, not the raw transport. A scripted payload
    shaped like a conclair error envelope is elevated to
    :class:`MagickitMcpError` here via :func:`raise_if_envelope`, exactly the
    way :class:`~spirrow_mindwire.magickit.client.StreamableHttpChatroomMcp`
    does in production — so a fake that returns the measured envelope shape
    (T-error-envelope-read-as-data DoD #3) drives the same code path a real
    server does.
    """

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        result = self._results.get(name)
        if isinstance(result, Exception):
            raise result
        raise_if_envelope(result)
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


@pytest.mark.anyio
async def test_read_fails_safe_when_the_call_returns_an_error_envelope() -> None:
    """Row 2 of msg-1115 §1, re-checked at the elevation boundary.

    The client now elevates a conclair error envelope to
    :class:`MagickitMcpError` at the transport, so ``read``'s wide catch
    (``except Exception``) rescues it and produces the same ``hold`` as any
    other unreadable state — the pre-elevation behaviour (``parse_control_payload``
    failing safe on a payload with no ``desired_state``) lands at the same
    ``hold`` too, so this refactor **must not** change what row 2 does. Pinned
    against a verbatim envelope so a future narrowing of the ``read`` catch
    cannot silently regress the fail-safe direction.
    """
    envelope = {
        "error_type": "ChatroomUnavailableError",
        "error": "project 'p' has no loop_control row",
        "details": {"project": "p"},
    }
    mcp = _FakeMcp({"loop_control_get": envelope})
    assert await LoopControlReader(mcp, project="p").read() is FAILSAFE_CONTROL_STATE


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
    #
    # The exception type is ``MagickitMcpError`` deliberately: the production client wraps every
    # transport failure (``TimeoutError``, ``ConnectionError``, …) into that class before returning,
    # so a fake that raised the raw ``TimeoutError`` was testing a state the caller cannot see.
    # Under msg-1496 §4.1's narrow catch (``MagickitMcpError`` only, no bare ``except Exception``),
    # the older fake would leak the wrapped-away exception; using the class production actually
    # produces is the correct simulation.
    mcp = _FakeMcp({"loop_control_report_observed": MagickitMcpError("transport slow")})
    reader = LoopControlReader(mcp, project="p")
    await reader.report_observed(ControlState.HOLD)  # must not raise
    await reader.report_observed(ControlState.HOLD)
    assert len(mcp.calls) == 2


@pytest.mark.anyio
async def test_report_observed_swallows_an_error_envelope_and_does_not_advance() -> None:
    """DoD #4 (a): the envelope must not escape the call site, and the marker must not advance.

    ``report_observed`` used to run under ``except Exception``, so an envelope
    returned as a "success" payload flowed through silently and
    ``_last_reported`` advanced as if the write had landed — a docstring saying
    "advances only on success" with an implementation that could not tell the
    two apart. Under the elevation (T-error-envelope-read-as-data DoD #1) the
    envelope becomes :class:`MagickitMcpError`; the narrow catch keeps the call
    site quiet but only for real refusals, and *never* moves the marker.
    """
    envelope = {
        "error_type": "ChatroomNotFoundError",
        "error": "loop_control row for project 'p' not found",
        "details": {"project": "p"},
    }
    mcp = _FakeMcp({"loop_control_report_observed": envelope})
    reader = LoopControlReader(mcp, project="p")

    await reader.report_observed(ControlState.HOLD)  # must not raise

    assert reader._last_reported is None  # the marker did not advance
    assert [name for name, _ in mcp.calls] == ["loop_control_report_observed"]


@pytest.mark.anyio
async def test_report_observed_retries_the_same_value_next_round_after_failure() -> None:
    """DoD #4 (b): pin the docstring's "retried on the next round" literally.

    Round 1 fails with an envelope-elevated :class:`MagickitMcpError`; round 2
    is programmed to succeed (empty dict is not an envelope). The second
    ``report_observed`` for the *same* state must therefore go through — which
    is only true if ``_last_reported`` was NOT advanced by round 1. If a later
    edit "helpfully" moves ``_last_reported`` on the failure path (which is
    exactly the change msg-1496 §4.3 exists to forbid), round 2 will short-
    circuit on the ``if state is self._last_reported`` guard and this test will
    catch it.
    """

    class _EnvelopeThenOk:
        """First call raises via the envelope elevation, second call succeeds."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self._first = True

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((name, arguments))
            if self._first:
                self._first = False
                raise_if_envelope(
                    {
                        "error_type": "ChatroomUnavailableError",
                        "error": "chatroom down",
                        "details": {"project": arguments["project"]},
                    }
                )
            return {}

    mcp = _EnvelopeThenOk()
    reader = LoopControlReader(mcp, project="p")

    await reader.report_observed(ControlState.HOLD)  # round 1: silently dropped
    await reader.report_observed(ControlState.HOLD)  # round 2: goes through

    # Two attempts, both for the same state — the retry the docstring promises.
    assert len(mcp.calls) == 2
    assert [args["state"] for _, args in mcp.calls] == ["hold", "hold"]
    assert reader._last_reported is ControlState.HOLD  # advanced by round 2, not round 1


@pytest.mark.anyio
async def test_report_observed_lets_programming_errors_propagate() -> None:
    """msg-1496 §4.1: the narrow catch refuses to drink ``Exception``.

    A programming bug (``TypeError`` from a mis-shaped argument list, say) is
    exactly what an ``except Exception`` here would swallow silently — and the
    silent-success hole msg-1115 §3 objected to is what it takes to grow back.
    Pin that anything that is not a ``MagickitMcpError`` escapes.
    """

    class _RaisesTypeError:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            raise TypeError("argument bug")

    reader = LoopControlReader(_RaisesTypeError(), project="p")
    with pytest.raises(TypeError):
        await reader.report_observed(ControlState.HOLD)


@pytest.mark.anyio
async def test_reader_never_calls_loop_control_set() -> None:
    # INV-4 at the transport edge: the loop is given the half of the contract that reports what it
    # saw, never the half that decides what it should do. A regression here would let the loop
    # release its own hold.
    mcp = _FakeMcp({"loop_control_get": _payload(), "loop_control_report_observed": {}})
    reader = LoopControlReader(mcp, project="p")
    await reader.report_observed(await reader.read())
    assert "loop_control_set" not in {name for name, _ in mcp.calls}
