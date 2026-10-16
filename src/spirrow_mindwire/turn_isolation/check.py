"""``mindwire turn-isolation-check``: the owner-side startup checks of S8 (unit 2a).

With ``[turn.isolation] user`` unset this does nothing and exits 0: behaviour stays exactly as
it is today (D1). With it set, in order:

1. both credentials the launcher needs exist in the owner's Credential Manager
   (``turn user credential missing`` / ``implementer PAT credential missing``);
2. the S8 probe passes for every clone given, run as the turn user through the launcher.

A failure prints one ``turn-isolation: STOP <reason>`` line on stderr and exits
:data:`~.errors.SECRETS_STOP_EXIT_CODE`. Calling this from the sweep wrapper once per tick, and
teaching the wrapper that the code is a tick-wide stop, is wiring that waits for the spike
(msg-7100 item 5).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .errors import (
    IMPLEMENTER_PAT_CREDENTIAL_MISSING,
    TURN_USER_CREDENTIAL_MISSING,
    TurnIsolationStop,
)
from .launcher import IMPLEMENTER_PAT_CREDENTIAL, TURN_USER_CREDENTIAL, read_secret
from .probe import LauncherOps, TurnUserOps, probe_clones
from .win32 import CredentialReader, default_api


def check_turn_isolation(
    user: str | None,
    clones: Sequence[Path],
    *,
    api_factory: Callable[[], CredentialReader] = default_api,
    ops_factory: Callable[[], TurnUserOps] = LauncherOps,
) -> bool:
    """Return ``False`` when isolation is off, ``True`` when every check passed; else raise."""
    if not user:
        return False
    api = api_factory()
    for target, reason in (
        (TURN_USER_CREDENTIAL, TURN_USER_CREDENTIAL_MISSING),
        (IMPLEMENTER_PAT_CREDENTIAL, IMPLEMENTER_PAT_CREDENTIAL_MISSING),
    ):
        read_secret(api, target, reason).wipe()
    probe_clones(clones, ops_factory())
    return True


def run(
    user: str | None,
    clones: Sequence[Path],
    *,
    api_factory: Callable[[], CredentialReader] = default_api,
    ops_factory: Callable[[], TurnUserOps] = LauncherOps,
) -> int:
    try:
        isolated = check_turn_isolation(
            user, clones, api_factory=api_factory, ops_factory=ops_factory
        )
    except TurnIsolationStop as stop:
        print(stop.stop_line(), file=sys.stderr, flush=True)
        return stop.exit_code
    print("turn-isolation: ok" if isolated else "turn-isolation: off ([turn.isolation] user unset)")
    return 0
