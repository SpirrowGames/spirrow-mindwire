"""Tick-wide stop for the turn-isolation layer (ADR-2026-10-09-23 unit 2a, spec S4 / S8).

Every failure in this package is a fact about the *host* (a missing credential, a logon the
OS refuses, a clone the turn user cannot use), not about any one thread. Spec S4 therefore
makes it a tick-wide stop with its own exit code: no thread runs, nobody is parked or
quarantined. The stop reason names only the step and the object that failed — never a
credential value.

Unit 2a defines the code and raises it from the launcher, from ``mindwire
turn-isolation-check``, from the per-launch gate (:mod:`.gate`) and from the implementer
adapter's launcher route. ``loop_runner.main`` turns it into exit 10 with one stop line, and the
sweep wrapper treats exit 10 as a tick-wide stop (no quarantine, launch reverted, the rest of
the tick skipped). Unit 2b reuses the same code for a failed Vaultwarden fetch.
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
# msg-9439 W4: a UPN (``user@domain``) is refused at parse time, before any credential read or
# logon, so the stop names the real cause instead of LogonUserW's "bad credential".
TURN_USER_UPN_NOT_SUPPORTED = "turn user must be DOMAIN\\user or user (UPN not supported)"
# msg-9439 W3 / ADR-2026-10-09-23 §7.1(c): a check that timed out or raised is "not passed".
TURN_ISOLATION_CHECK_TIMED_OUT = "turn-isolation-check timed out"
TURN_ISOLATION_CHECK_FAILED = "turn-isolation-check failed"
# msg-9439 W2 / msg-9441: the launcher route of the implementer adapter. The cli_path shim and
# the SDK environment it needs are decided by the operator spike (msg-8340 items 3-4); until
# then the route is wired but refuses, by name, to start a turn (fail closed).
TURN_LAUNCH_ROUTE_PENDING_SPIKE = (
    "turn launcher route not available: cli_path shim awaits the spike (msg-8340)"
)
TURN_LAUNCH_ROUTE_FAILED = "turn launcher route failed"


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
