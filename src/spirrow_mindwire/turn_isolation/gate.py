"""The per-launch isolation gate and the implementer's launcher route (msg-9439 W1/W2, msg-9441).

:func:`resolve_turn_launch` is the one place the loop decides how the implementer's turn starts:

* ``[turn.isolation] user`` unset → returns ``None``. The check is not called, the launcher is
  not called, and the adapter keeps its direct SDK spawn: behaviour is exactly today's (INV-0).
* set → runs the S8 check **in-process, now**, and returns the launcher route only if it
  passed. The result never touches a file: it is the return value, handed straight to the
  adapter's constructor (ADR-2026-10-09-23 §7.1(c) — only a check from this same launch counts).
  Any failure of the check — a stop, a timeout, any other exception — becomes one
  :class:`~.errors.TurnIsolationStop` here, at the caller, so a check that raises cannot turn
  into a traceback (§7.1(c)). Nothing has been dispatched yet, so no turn starts down either path.

The route itself (:data:`pending_spike_launch_factory`) is the launcher seam of
:class:`~spirrow_mindwire.adapters.implementer.ImplementerSdkAdapter`. Whether the SDK reaches the
launcher through a ``cli_path`` shim, and which SDK environment the child needs, are what the
operator spike measures (msg-8340 items 3-4); they are not guessed here. Until then the route
refuses by name, so a host that sets ``[turn.isolation] user`` before the spike stops loudly
(exit 10) instead of running the turn as the owner.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .check import as_stop, check_turn_isolation
from .errors import TURN_LAUNCH_ROUTE_PENDING_SPIKE, TurnIsolationStop

# The launcher route: builds the SDK client the adapter would otherwise build directly, given
# the same ``ClaudeAgentOptions``. It raises :class:`TurnIsolationStop` to refuse.
TurnLaunchFactory = Callable[[Any], Any]

CheckFn = Callable[[str, Sequence[Path]], bool]


def pending_spike_launch_factory(options: Any) -> Any:
    """The production route until the spike: refuse by name, never fall back (fail closed)."""
    raise TurnIsolationStop(TURN_LAUNCH_ROUTE_PENDING_SPIKE)


def _default_check(user: str, clones: Sequence[Path]) -> bool:
    return check_turn_isolation(user, clones)


def resolve_turn_launch(
    user: str | None,
    clones: Sequence[Path],
    *,
    check: CheckFn = _default_check,
    route: TurnLaunchFactory | None = None,
) -> TurnLaunchFactory | None:
    """``None`` when isolation is off; the launcher route when this launch's check passed.

    Raises :class:`TurnIsolationStop` for every way the check can fail to pass.
    """
    if not user:
        return None
    try:
        passed = check(user, clones)
    except Exception as exc:
        raise as_stop(exc) from None
    if passed is not True:
        # ``check_turn_isolation`` returns False only for an unset user, which cannot happen
        # here; anything but an explicit pass is not a pass.
        raise TurnIsolationStop("turn-isolation-check did not pass")
    return route if route is not None else pending_spike_launch_factory
