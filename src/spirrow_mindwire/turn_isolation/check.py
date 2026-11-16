"""``mindwire turn-isolation-check``: the owner-side startup checks of S8 (unit 2a).

With ``[turn.isolation] user`` unset this does nothing and exits 0: behaviour stays exactly as
it is today (D1). With it set, in order:

1. both credentials the launcher needs exist in the owner's Credential Manager
   (``turn user credential missing`` / ``implementer PAT credential missing``);
2. the S8 probe passes for every clone given, run as the turn user through the launcher.

A failure — a stop, a probe timeout (``subprocess.TimeoutExpired``, msg-7363 A) or any other
exception — prints one ``turn-isolation: STOP <reason>`` line on stderr and exits
:data:`~.errors.SECRETS_STOP_EXIT_CODE`. A UPN-form user stops before any credential read (W4).
The loop runs the same check in-process at each conductor launch through :mod:`.gate`.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .errors import (
    IMPLEMENTER_PAT_CREDENTIAL_MISSING,
    TURN_ISOLATION_CHECK_FAILED,
    TURN_ISOLATION_CHECK_TIMED_OUT,
    TURN_USER_CREDENTIAL_MISSING,
    TurnIsolationStop,
)
from .launcher import IMPLEMENTER_PAT_CREDENTIAL, TURN_USER_CREDENTIAL, TurnUser, read_secret
from .probe import LauncherOps, TurnUserOps, probe_clones
from .win32 import CredentialReader, default_api


def launcher_ops(user: str) -> TurnUserOps:
    """Production ops: the real launcher, told which user the caller resolved."""
    return LauncherOps(user=user)


def check_turn_isolation(
    user: str | None,
    clones: Sequence[Path],
    *,
    api_factory: Callable[[], CredentialReader] = default_api,
    ops_factory: Callable[[str], TurnUserOps] = launcher_ops,
) -> bool:
    """Return ``False`` when isolation is off, ``True`` when every check passed; else raise."""
    if not user:
        return False
    # Parse first: a UPN stops here, before any credential is read or the launcher runs (W4).
    TurnUser.parse(user)
    api = api_factory()
    for target, reason in (
        (TURN_USER_CREDENTIAL, TURN_USER_CREDENTIAL_MISSING),
        (IMPLEMENTER_PAT_CREDENTIAL, IMPLEMENTER_PAT_CREDENTIAL_MISSING),
    ):
        read_secret(api, target, reason).wipe()
    probe_clones(clones, ops_factory(user))
    return True


def as_stop(exc: BaseException) -> TurnIsolationStop:
    """Map any failure of the check to the tick-wide stop (msg-7363 A, ADR-23 §7.1(c)).

    A :class:`TurnIsolationStop` keeps its reason. A probe that ran past its timeout is
    :data:`TURN_ISOLATION_CHECK_TIMED_OUT`; anything else names only its exception class, never
    its message, so no value can reach the stop line.
    """
    if isinstance(exc, TurnIsolationStop):
        return exc
    if isinstance(exc, subprocess.TimeoutExpired):
        return TurnIsolationStop(TURN_ISOLATION_CHECK_TIMED_OUT)
    return TurnIsolationStop(f"{TURN_ISOLATION_CHECK_FAILED} ({type(exc).__name__})")


def run(
    user: str | None,
    clones: Sequence[Path],
    *,
    api_factory: Callable[[], CredentialReader] = default_api,
    ops_factory: Callable[[str], TurnUserOps] = launcher_ops,
) -> int:
    try:
        isolated = check_turn_isolation(
            user, clones, api_factory=api_factory, ops_factory=ops_factory
        )
    except Exception as exc:
        stop = as_stop(exc)
        print(stop.stop_line(), file=sys.stderr, flush=True)
        return stop.exit_code
    print("turn-isolation: ok" if isolated else "turn-isolation: off ([turn.isolation] user unset)")
    return 0
