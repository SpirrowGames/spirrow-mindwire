"""Wall-clock budget for one conductor run (W-1, the soft limit).

Thread: T-agmsg-transport-lessons-readiness-session-claim-board (Bohr msg-5496 W-1..W-5, revised
in msg-5498 after Einstein msg-5497; endorsed by Einstein msg-5499 / msg-5501; authorised by the
human). Before this, ``mindwire-loop --mode conductor`` had no upper bound on its run time. A run
that hung (an adapter ``query`` or drain that never returned) held the sweep until Task Scheduler's
``ExecutionTimeLimit`` (PT4H) and said nothing anywhere.

Two limits, one inside the process and one outside it:

- **Soft (this module).** :func:`run_with_budget` bounds the whole of ``run_conductor`` by
  ``[conductor].run_budget_s``. When it expires:

  1. The ``conductor.run_timeout`` event is written to the log as one warning line
     (:func:`emit_run_timeout`). This happens **first**, from a timer callback at the deadline,
     before the run is cancelled and before its teardown runs, so neither a stuck teardown nor a
     failed post can keep the line out of the quarantine record's log tail (Einstein msg-5497
     objection 1).
  2. The run is cancelled, and a stop notice ending ``NEXT: human`` is posted under
     ``conductor-relay`` with a ``mindwire:stop v1`` marker. The post itself is bounded by
     :data:`RELAY_POST_BUDGET_S` (objection 2: a run that hung because the chatroom stopped
     answering would otherwise hang again here).
  3. Posted: exit 0, ``conductor stopped: reason=human``, the same shape the spawn-timeout give-up
     uses. Not posted (refused, no ``msg_id``, raised, or timed out): exit
     :data:`RUN_TIMEOUT_EXIT_CODE`.

- **Hard (``deploy/lib/ConductorBudget.ps1``).** ``run-conductor.ps1`` waits for the process for
  at most ``[conductor].run_hard_budget_s``, then kills the whole tree and exits
  :data:`RUN_KILLED_EXIT_CODE`, or :data:`RUN_KILL_UNCONFIRMED_EXIT_CODE` if it cannot confirm
  the tree is gone. That limit covers what this one cannot: an event loop blocked by a synchronous
  call never runs the timer above.

The ordering soft + :data:`RELAY_POST_BUDGET_S` < hard is checked at daemon startup
(:func:`validate_budgets`), so a normal timeout is always reported by the soft path first.

``phase`` is where the run was when the deadline hit: ``<role>.spawn``, ``<role>.dispatch``,
``pr_gate.review``, ``thread.read``, ``launch.resolve``, ``teardown``, or ``conductor`` (between
calls). The Conductor sets it through :class:`RunPhase`. Per-call budgets are deliberately not
added (msg-5496 W-1 DECIDED): the phase records which call actually stalls, and a per-call budget
is added for that call once it is known.

Event vocabulary: kind ``conductor.run_timeout``. Fields ``budget_s``, ``elapsed_s``, ``phase``,
``project``, ``thread``. The hard limit's line is ``conductor.run_killed budget_s=… pid=…``,
written by the PowerShell side; :data:`EVENT_KIND_RUN_KILLED` is its name here so the vocabulary
has one Python source (a test pins the PowerShell literal to it).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from ..magickit.client import McpToolCaller, ThreadResolvedError
from ..ulid_util import new_ulid
from ..value_objects import Event
from .handoff import HUMAN_TOKEN
from .stop_marker import render_stop_marker

logger = logging.getLogger(__name__)

EVENT_KIND_RUN_TIMEOUT = "conductor.run_timeout"
EVENT_KIND_RUN_KILLED = "conductor.run_killed"

# Exit codes. 1 = crash / adapter error, 2 = environment terminal (not quarantined), 3 = T44
# stand-down with no chatroom, 4 = T42 stall notice not posted. 5 and 6 get no special case in the
# sweep wrapper ("unknown non-zero goes to quarantine", the same as 3 and 4); the log tail says
# which one it was (design §18.5). 7 is the one the sweep branches on: an orphan tree may still be
# running, so the rest of that tick's sweep is skipped (msg-5498 W-3).
# 8 is taken too: clone_guard.DIRTY_CLONE_EXIT_CODE (dirty shared clone, not quarantined).
RUN_TIMEOUT_EXIT_CODE = 5
RUN_KILLED_EXIT_CODE = 6
RUN_KILL_UNCONFIRMED_EXIT_CODE = 7

# A policy constant, not a knob (msg-5498, the same treatment as ``STALL_THRESHOLD``): how long the
# stop notice may take to post after the soft budget expired.
RELAY_POST_BUDGET_S = 30.0

PHASE_IDLE = "conductor"

_T = TypeVar("_T")


class RunPhase:
    """Where the run currently is, for the ``phase`` field of ``conductor.run_timeout``.

    A mutable object the composition root owns and hands to the Conductor, like
    :class:`~spirrow_mindwire.conductor.core.ConductorStopSlot`. :meth:`enter` restores the
    previous value on exit, so between two calls the phase reads :data:`PHASE_IDLE` again rather
    than naming a call that already returned.
    """

    def __init__(self) -> None:
        self.current = PHASE_IDLE
        # Rounds the Conductor has STARTED, including one still running. Reported as ``rounds``
        # on the timeout's ``conductor stopped:`` line: a round cut off after an hour may already
        # have posted, so the sweep must treat the run as one that did work (it then ends the
        # sweep for the tick and keeps the raw output in the log) rather than as an idle no-op.
        self.rounds_started = 0

    def enter(self, phase: str) -> _PhaseScope:
        return _PhaseScope(self, phase)


class _PhaseScope:
    """Sets a phase on enter and restores the previous one on exit.

    A plain class rather than ``contextlib.contextmanager``: the generator form writes
    ``__traceback__`` on any exception passing through it, and the conductor's dispatch path must
    re-raise an adapter's exception untouched, including one that refuses attribute writes
    (``tests/test_adapter_error_stop_line.py``). ``__exit__`` never looks at the exception.
    """

    __slots__ = ("_phase", "_previous", "_run_phase")

    def __init__(self, run_phase: RunPhase | None, phase: str) -> None:
        self._run_phase = run_phase
        self._phase = phase
        self._previous = PHASE_IDLE

    def __enter__(self) -> None:
        if self._run_phase is not None:
            self._previous = self._run_phase.current
            self._run_phase.current = self._phase

    def __exit__(self, *_exc: object) -> None:
        if self._run_phase is not None:
            self._run_phase.current = self._previous


def enter_phase(run_phase: RunPhase | None, phase: str) -> _PhaseScope:
    """:meth:`RunPhase.enter`, or nothing when no phase object was wired (tests, library use)."""
    return _PhaseScope(run_phase, phase)


class RunTimeoutError(SystemExit):
    """Exit :data:`RUN_TIMEOUT_EXIT_CODE`: the soft budget expired and the notice did not land.

    A ``SystemExit`` subclass for the same reason as ``StalledError``: ``loop_runner.main``
    re-raises it without a traceback.
    """

    def __init__(self, event: Event) -> None:
        super().__init__(RUN_TIMEOUT_EXIT_CODE)
        self.event = event


def validate_budgets(*, run_budget_s: float, run_hard_budget_s: float) -> None:
    """Refuse to start when the soft path could not finish before the hard kill (msg-5498).

    Raises ``SystemExit`` with the reason, like the other ``[conductor]`` startup checks.
    """
    if run_budget_s <= 0:
        raise SystemExit(f"[conductor].run_budget_s must be > 0 (got {run_budget_s})")
    if run_hard_budget_s <= run_budget_s + RELAY_POST_BUDGET_S:
        raise SystemExit(
            f"[conductor].run_hard_budget_s ({run_hard_budget_s}) must be greater than "
            f"run_budget_s ({run_budget_s}) + {RELAY_POST_BUDGET_S:g} s (the stop-notice post "
            f"budget), so a soft timeout is reported before the hard kill"
        )


def run_timeout_event(
    *, budget_s: float, elapsed_s: float, phase: str, project: str, thread: str
) -> Event:
    """Build the ``conductor.run_timeout`` event (no message body, per the I6 Event convention)."""
    return Event(
        event_id=new_ulid(),
        occurred_at=datetime.now(UTC),
        kind=EVENT_KIND_RUN_TIMEOUT,
        fields={
            "budget_s": budget_s,
            "elapsed_s": elapsed_s,
            "phase": phase,
            "project": project,
            "thread": thread,
        },
    )


def emit_run_timeout(event: Event) -> None:
    """Write the event as one warning line (the line a quarantine record's log tail carries)."""
    f = event.fields
    logger.warning(
        "%s budget_s=%s elapsed_s=%s phase=%s project=%s thread=%s",
        event.kind,
        f.get("budget_s"),
        f.get("elapsed_s"),
        f.get("phase"),
        f.get("project"),
        f.get("thread"),
    )


def render_run_timeout_notice(event: Event) -> str:
    """The notice posted into the thread after the soft budget expired.

    Ends on ``NEXT: human`` like the conductor's other stop notices, with the stop marker above
    it (design §18.4), so the next sweep tick SKIPs instead of launching into the same hang.
    """
    f = event.fields
    return (
        f"Conductor run timeout — 1 回の起動の時間上限を超えたので止めました\n\n"
        f"- 上限: {f.get('budget_s')} 秒 (`[conductor].run_budget_s`)\n"
        f"- 経過: {f.get('elapsed_s')} 秒\n"
        f"- 止まっていた箇所: `{f.get('phase')}`\n\n"
        f"この起動は上限までに終わらなかったので、実行中の呼び出しを取り消しました "
        f"(`{EVENT_KIND_RUN_TIMEOUT}`)。途中のターンの返信は投稿されていない可能性があります。"
        f"原因は特定していません。上の箇所が、上限の時点で待っていた呼び出しです。\n\n"
        f"次にやること: ループ host のログで、この箇所の呼び出しが返らなかった理由を"
        f"確認してください。直したら、進め先の `NEXT:` を書いてください。"
        f"head が動くまでループは再開しません。\n\n"
        f"{render_stop_marker(event)}\n\n"
        f"NEXT: {HUMAN_TOKEN}"
    )


async def post_run_timeout_notice(
    mcp: McpToolCaller, *, project: str, thread_id: str, event: Event, author: str
) -> str:
    """Post the run-timeout notice, bounded by :data:`RELAY_POST_BUDGET_S`. Returns the ``msg_id``.

    Producer declaration (OBL-CHATROOM-PRODUCER-READER-SURFACE):

    - Intended reader: the human who owns the target thread, who opens it because its head asked
      for a turn that did not finish.
    - Fallback surface when the thread is gone: disposition **(3) fail loudly**. A
      :class:`ThreadResolvedError`, a post with no ``msg_id``, any other exception, or a post that
      does not finish within the budget raises :class:`RunTimeoutError` → exit 5 → the wrapper's
      quarantine and Discord alert, with the ``conductor.run_timeout`` line already in the log
      tail. The refusal is never posted into another chatroom thread.
    """
    try:
        async with asyncio.timeout(RELAY_POST_BUDGET_S):
            result: Any = await mcp.call_tool(
                "chatroom_post_message",
                {
                    "project": project,
                    "thread_id": thread_id,
                    "msg_type": "report",
                    "author": author,
                    "content": render_run_timeout_notice(event),
                },
            )
    except ThreadResolvedError as exc:
        logger.warning(
            "%s notice refused (thread %r resolved): %s — exiting %d",
            EVENT_KIND_RUN_TIMEOUT,
            thread_id,
            exc,
            RUN_TIMEOUT_EXIT_CODE,
        )
        raise RunTimeoutError(event) from exc
    except TimeoutError as exc:
        logger.warning(
            "%s notice not posted within %g s in thread %r — exiting %d",
            EVENT_KIND_RUN_TIMEOUT,
            RELAY_POST_BUDGET_S,
            thread_id,
            RUN_TIMEOUT_EXIT_CODE,
        )
        raise RunTimeoutError(event) from exc
    except Exception as exc:
        logger.warning(
            "%s notice could not be posted in thread %r (%s: %s) — exiting %d",
            EVENT_KIND_RUN_TIMEOUT,
            thread_id,
            type(exc).__name__,
            exc,
            RUN_TIMEOUT_EXIT_CODE,
        )
        raise RunTimeoutError(event) from exc
    msg = result.get("msg") if isinstance(result, dict) else None
    msg_id = str(msg.get("msg_id") or "") if isinstance(msg, dict) else ""
    if not msg_id:
        logger.warning(
            "%s notice did not land in thread %r (no msg_id) — exiting %d",
            EVENT_KIND_RUN_TIMEOUT,
            thread_id,
            RUN_TIMEOUT_EXIT_CODE,
        )
        raise RunTimeoutError(event)
    return msg_id


async def run_with_budget(
    body: Callable[[], Awaitable[_T]],
    *,
    budget_s: float,
    run_phase: RunPhase,
    project: str,
    thread: str,
) -> tuple[_T | None, Event | None]:
    """Run ``body()`` for at most ``budget_s`` seconds.

    Returns ``(result, None)`` when the body finished, or ``(None, event)`` when the budget
    expired; the event's line has already been written. Posting the notice is the caller's job,
    so the post can use the caller's own transport.

    The line is written by a timer callback at the deadline, which then moves the
    ``asyncio.timeout`` deadline to "now". The cancellation therefore arrives after the line, and
    the body's ``finally`` blocks (adapter teardown) run after it too.

    Teardown runs to completion, awaits included. asyncio's cancellation is edge-triggered: the
    expired scope calls ``task.cancel()`` once, the single ``CancelledError`` lands on the await
    that was pending at the deadline, and later awaits in ``finally`` (``await cond.aclose()``)
    are not cancelled again. The scope being in its expired state does not re-cancel them
    (``test_async_teardown_runs_to_completion_after_the_deadline``). The soft budget therefore
    does not bound teardown: a teardown that never returns is left to the hard budget
    (``test_teardown_that_hangs_cannot_keep_the_line_out``), by which time the line is already
    in the log. If the body happens to
    return in the same loop iteration the timer fired, the line has been written but the result
    stands: the run finished, exits 0, and the log tail of an exit-0 run is not classified.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    fired: list[Event] = []
    scope = asyncio.timeout(None)

    def _on_deadline() -> None:
        event = run_timeout_event(
            budget_s=budget_s,
            elapsed_s=round(loop.time() - started, 1),
            phase=run_phase.current,
            project=project,
            thread=thread,
        )
        emit_run_timeout(event)
        fired.append(event)
        scope.reschedule(loop.time())

    try:
        async with scope:
            handle = loop.call_at(started + budget_s, _on_deadline)
            try:
                return await body(), None
            finally:
                handle.cancel()
    except TimeoutError:
        # Only our own deadline is converted. A TimeoutError the body raised on its own, before
        # the deadline, is not ours and propagates unchanged.
        if fired and scope.expired():
            return None, fired[0]
        raise


__all__ = [
    "EVENT_KIND_RUN_KILLED",
    "EVENT_KIND_RUN_TIMEOUT",
    "PHASE_IDLE",
    "RELAY_POST_BUDGET_S",
    "RUN_KILLED_EXIT_CODE",
    "RUN_KILL_UNCONFIRMED_EXIT_CODE",
    "RUN_TIMEOUT_EXIT_CODE",
    "RunPhase",
    "RunTimeoutError",
    "emit_run_timeout",
    "enter_phase",
    "post_run_timeout_notice",
    "render_run_timeout_notice",
    "run_timeout_event",
    "run_with_budget",
    "validate_budgets",
]
