"""Tick-wide stop for the turn-isolation layer (ADR-2026-10-09-23 unit 2a, spec S4 / S8).

Every failure in this package is a fact about the *host* (a missing credential, a logon the
OS refuses, a clone the turn user cannot use), not about any one thread. Spec S4 therefore
makes it a tick-wide stop with its own exit code: no thread runs, nobody is parked or
quarantined. The stop reason names only the step and the object that failed — never a
credential value.

Unit 2a defines the code and raises it from the launcher and from ``mindwire
turn-isolation-check``. Teaching the sweep wrapper to treat it as a tick-wide stop is part of
the wiring that waits for the spike (msg-7100 item 5); unit 2b reuses the same code for a failed
Vaultwarden fetch.
"""

from __future__ import annotations

# The next free daemon exit code: 1 crash, 2 environment terminal, 3 stand-down, 4 stall,
# 5/6/7 wall-clock budget, 8 dirty clone, 9 git-sync deferred (git_sync/errors.py).
SECRETS_STOP_EXIT_CODE = 10

# Prefix of the single stderr line a stop prints, so a caller that only has the child's
# stderr (the S8 probe reading the launcher) can recover the reason verbatim.
STOP_LINE_PREFIX = "turn-isolation: STOP "

TURN_USER_CREDENTIAL_MISSING = "turn user credential missing"
IMPLEMENTER_PAT_CREDENTIAL_MISSING = "implementer PAT credential missing"
TURN_JOB_ASSIGNMENT_FAILED = "turn job assignment failed"


class TurnIsolationStop(Exception):  # noqa: N818 - a stop, not an error (S4)
    """The tick must stop with :data:`SECRETS_STOP_EXIT_CODE`; ``reason`` is the stop line."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @property
    def exit_code(self) -> int:
        return SECRETS_STOP_EXIT_CODE

    def stop_line(self) -> str:
        return STOP_LINE_PREFIX + self.reason


def parse_stop_line(stderr: str) -> str | None:
    """Return the reason from the first stop line in ``stderr``, or ``None`` when there is none."""
    for line in stderr.splitlines():
        if line.startswith(STOP_LINE_PREFIX):
            return line[len(STOP_LINE_PREFIX) :].strip()
    return None
