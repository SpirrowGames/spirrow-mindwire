"""Per-project loop control (``run`` / ``supervised`` / ``hold``) — the autonomy inversion, Part C.

This replaces the per-thread ``DELEGATE`` marker that used to authorise the design→implement edge.
That marker was **opt-in and non-sticky**: the human had to re-write it on every turn, and the
consequence of forgetting was that the loop stopped. Inverting the polarity (autonomy by default,
stop on demand) makes forgetting dangerous instead of safe, so the state is no longer derived from
the thread at all — it is stored per **project** in conclair, set from the dashboard or from
``loop_control_set``, and it **latches**: nothing but an explicit set changes it.

Three states (the vocabulary is conclair's; this module only consumes it):

- ``run`` — fully autonomous. The independent naysayer's proceed-handoff carries a design through
  to the implementer (the old carve-out ③, now project-wide rather than per-thread).
- ``supervised`` — the design loop turns, but only a human Tier-C decide or a PR-gate
  REQUEST_CHANGES reaches code. **This is the pre-inversion behaviour** and the baseline this
  module falls back to when no control source is wired at all.
- ``hold`` — the loop does not run. The sweep does not start it and a running conductor stops at
  the next round boundary.

**The two failure directions are deliberately different code paths**, and conflating them is the
one mistake that matters here:

- *The project has no row* — not a failure. magickit answers ``desired_state: "run"`` with
  ``configured: false`` and a normal result, so it flows through :func:`parse_control_payload`
  like any other value. This is the whole point of the inversion (a project nobody has touched
  runs).
- *We could not learn the state* — transport error, malformed payload, unknown value. There is no
  safe guess, so :data:`FAILSAFE_CONTROL_STATE` (``hold``) is returned. A control plane that is
  down must not silently hand the loop full autonomy.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any, Protocol

from ..magickit.client import McpToolCaller

logger = logging.getLogger(__name__)

# magickit tool names (spirrow-magickit, Part B). ``loop_control_set`` is deliberately NOT used
# here: the loop is given the half of the contract that reports what it saw, never the half that
# decides what it should do.
#
# How far that actually goes, stated exactly so nobody over-trusts it: the daemon never calls
# ``loop_control_set``, and the three role sessions cannot either — ``loop_runner`` wires no
# ``mcp_servers`` into any adapter, so they have no MCP tools at all (verified 2026-08-04). It is
# NOT a sandbox: the implementer session has shell tools and the magickit endpoint is unauthed on
# the tailnet, so an implementer that decided to could POST to it directly. That residual is the
# same environment trust model the rest of the loop rests on (see ``adapters/implementer.py`` on the
# blast-radius backstops); closing it needs auth on magickit, not a change here.
_TOOL_GET = "loop_control_get"
_TOOL_REPORT_OBSERVED = "loop_control_report_observed"

DEFAULT_CONTROL_ACTOR = "mindwire-conductor"
"""Reported as ``actor`` on the observed write-back, so the operator sees which component read."""


class ControlState(StrEnum):
    """A project's loop control state. Values match conclair's enum exactly."""

    RUN = "run"
    SUPERVISED = "supervised"
    HOLD = "hold"


FAILSAFE_CONTROL_STATE = ControlState.HOLD
"""Used when the state cannot be determined. Never fail open into autonomy."""

BASELINE_CONTROL_STATE = ControlState.SUPERVISED
"""Used when no control source is wired at all (e.g. a unit test constructing a bare Conductor).

Distinct from :data:`FAILSAFE_CONTROL_STATE`: "nobody asked me to consult a control plane" is not
the same as "the control plane is unreachable". The former keeps the pre-inversion behaviour (the
loop turns but never self-authorises code); the latter stops the loop outright.
"""


class LoopControl(Protocol):
    """The control-plane slice the conductor drives. :class:`LoopControlReader` satisfies it."""

    async def read(self) -> ControlState: ...

    async def report_observed(self, state: ControlState) -> None: ...


def parse_control_payload(payload: Any) -> ControlState:
    """Extract ``desired_state`` from a ``loop_control_get`` result.

    Returns :data:`FAILSAFE_CONTROL_STATE` for anything unusable — a non-mapping payload, a missing
    or non-string ``desired_state``, or a value outside the enum (which is what a magickit that has
    grown a fourth state this build does not understand would look like). Pure, so the fail-safe
    direction is unit-testable without a transport.
    """
    if not isinstance(payload, dict):
        logger.warning(
            "loop control: %s returned a non-mapping payload (%r) — failing safe to %s",
            _TOOL_GET,
            type(payload).__name__,
            FAILSAFE_CONTROL_STATE.value,
        )
        return FAILSAFE_CONTROL_STATE
    raw = payload.get("desired_state")
    if not isinstance(raw, str):
        logger.warning(
            "loop control: %s payload has no string desired_state (%r) — failing safe to %s",
            _TOOL_GET,
            raw,
            FAILSAFE_CONTROL_STATE.value,
        )
        return FAILSAFE_CONTROL_STATE
    try:
        return ControlState(raw)
    except ValueError:
        logger.warning(
            "loop control: unknown desired_state %r — failing safe to %s",
            raw,
            FAILSAFE_CONTROL_STATE.value,
        )
        return FAILSAFE_CONTROL_STATE


class LoopControlReader:
    """Reads one project's control state over magickit MCP and reports back what it acted on."""

    def __init__(
        self,
        mcp: McpToolCaller,
        *,
        project: str,
        actor: str = DEFAULT_CONTROL_ACTOR,
    ) -> None:
        self._mcp = mcp
        self._project = project
        self._actor = actor
        self._last_reported: ControlState | None = None

    async def read(self) -> ControlState:
        """The project's desired state, or :data:`FAILSAFE_CONTROL_STATE` if it cannot be read.

        Every exception is caught on purpose. The alternative — letting a transport error propagate
        — would stop the daemon with a traceback rather than with a recorded ``hold``, which is both
        less safe to reason about and invisible to the operator's dashboard.
        """
        try:
            payload = await self._mcp.call_tool(_TOOL_GET, {"project": self._project})
        except Exception as exc:
            logger.warning(
                "loop control: %s failed for project %r (%s) — failing safe to %s",
                _TOOL_GET,
                self._project,
                exc,
                FAILSAFE_CONTROL_STATE.value,
            )
            return FAILSAFE_CONTROL_STATE
        return parse_control_payload(payload)

    async def report_observed(self, state: ControlState) -> None:
        """Record that the loop read ``state`` and is acting on it — **only when it changes**.

        This is what closes the operator's feedback loop: a ``hold`` set from the dashboard shows
        as pending until this lands, so the UI never has to claim an immediate stop it cannot
        deliver. It is observability, not control: a failure here is logged and swallowed, because
        the run's own decision has already been made and killing it would be strictly worse than a
        stale dashboard. ``_last_reported`` advances only on success, so a failed report is retried
        on the next round rather than lost.
        """
        if state is self._last_reported:
            return
        try:
            await self._mcp.call_tool(
                _TOOL_REPORT_OBSERVED,
                {"project": self._project, "state": state.value, "actor": self._actor},
            )
        except Exception as exc:
            logger.warning(
                "loop control: %s failed for project %r (%s) — the dashboard will show a stale "
                "observed value; continuing on %s",
                _TOOL_REPORT_OBSERVED,
                self._project,
                exc,
                state.value,
            )
            return
        self._last_reported = state
        logger.info("loop control: observed %s for project %r", state.value, self._project)


__all__ = [
    "BASELINE_CONTROL_STATE",
    "DEFAULT_CONTROL_ACTOR",
    "FAILSAFE_CONTROL_STATE",
    "ControlState",
    "LoopControl",
    "LoopControlReader",
    "parse_control_payload",
]
