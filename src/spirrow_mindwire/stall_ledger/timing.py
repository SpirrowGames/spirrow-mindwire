"""D-16c timing values for the scheduled stall-ledger tick.

Spec: T-stalled-pr-has-no-detector msg-5744 §3 (C1, C2, C4) and msg-5746 §2 (A1), with
the advisory in msg-5747 (the fetch window must fit at least one fetch).

These replace the provisional D-16ab values. The inequalities below are pinned by
``tests/test_stall_ledger_d16c_timing.py``, so a later change to one value that breaks
another fails the build instead of the schedule.

* ``HEARTBEAT_INTERVAL`` (H) -- how often Task Scheduler fires the tick
  (``deploy/Register-StallLedgerTask.ps1``, ``RepetitionInterval``).
* ``T_TICK_MAX`` -- the whole-tick deadline. A tick that runs past it saves nothing.
* ``T_LOCK_STALE`` -- the longest a tick process can hold the ledger lock. The lock is an
  OS lock that is released when its process ends (``lock.py``) and has no takeover
  timer, so the only thing that ends a hung holder is the scheduled task's
  ``ExecutionTimeLimit``, which kills the process. That limit IS ``T_LOCK_STALE``.
* ``FETCH_TIMEOUT`` -- the per-request timeout of every adapter fetch and body fetch.
* ``FETCH_MARGIN`` -- the end of the tick reserved for the store write and moving
  request files aside. Body fetches may start only while one more ``FETCH_TIMEOUT``
  still ends before ``T_TICK_MAX - FETCH_MARGIN`` (A1).

Required order: ``FETCH_TIMEOUT <= T_TICK_MAX - FETCH_MARGIN`` and
``T_TICK_MAX < T_LOCK_STALE < HEARTBEAT_INTERVAL``. The first keeps the fetch window
able to hold one fetch, so owed fetches always drain (msg-5747). The second means an
on-time tick is never killed, a hung tick is killed before the next one fires, and
ticks never queue behind the lock.
"""

from __future__ import annotations

from datetime import timedelta

HEARTBEAT_INTERVAL = timedelta(minutes=15)
T_TICK_MAX = timedelta(minutes=10)
T_LOCK_STALE = timedelta(minutes=14)
FETCH_TIMEOUT = timedelta(seconds=30)
FETCH_MARGIN = timedelta(minutes=2)


def fetch_window(t_tick_max: timedelta, margin: timedelta = FETCH_MARGIN) -> timedelta:
    """How long after the tick starts body fetches may still be running (A1)."""
    return t_tick_max - margin


def check_timing(
    *,
    t_tick_max: timedelta,
    fetch_timeout: timedelta,
    margin: timedelta = FETCH_MARGIN,
) -> str | None:
    """``None`` if the values can make progress; otherwise why not.

    The CLI runs this on its overrides, so a value passed by hand can't bypass the test.
    """
    if fetch_timeout <= timedelta(0):
        return "fetch timeout must be positive"
    if fetch_window(t_tick_max, margin) < fetch_timeout:
        return (
            f"t_tick_max - margin ({fetch_window(t_tick_max, margin).total_seconds()}s) "
            f"is shorter than one fetch timeout ({fetch_timeout.total_seconds()}s): "
            "no body fetch could ever start, and owed fetches would never drain"
        )
    return None
