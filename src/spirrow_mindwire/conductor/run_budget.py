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
from dataclasses import dataclass
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

# T-run-budget-exhausted-by-chain-dispatches-new-turn (Bohr msg-9833 D1 / msg-9835 D1' / msg-9837
# D3'', endorsed by Einstein msg-9838). A turn is dispatched only while the run has at least its
# reserve left; otherwise the run stops on ``budget_yield`` and the next tick picks the same head
# up with a full budget.
#: Margin on top of the implementer's own turn timeout: the stop-notice post
#: (:data:`RELAY_POST_BUDGET_S`) plus teardown. A margin, not a guarantee (Einstein msg-9834 #2):
#: what holds is that the check runs immediately before the implementer's ``query``.
YIELD_MARGIN_S = 120.0
#: Reserve for every other turn (proposer / naysayer / the PR gate), which has no turn timeout of
#: its own. A policy constant: re-read it against the per-run turn table this change records.
NON_IMPLEMENTER_RESERVE_S = 600.0

#: Phase of a chatroom post made outside a role's dispatch (the conductor's or the gate's own
#: relay). A role's reply is ``<role>.post``. Both are set by :class:`PostPhaseMcp`, the one place
#: every chatroom write of a conductor run passes through (msg-9837 D3''-2).
PHASE_RELAY_POST = "relay.post"
PHASE_PR_GATE = "pr_gate.review"

#: The phases in which a run cut off by the budget is re-run on the next tick without a notice
#: (msg-9837 D3''-4). None of them writes outside before its reply is posted, and a post in
#: progress is ``<role>.post`` / ``relay.post``, which is not here. ``conductor`` (between two
#: calls) is deliberately absent: the gap after the PR gate returned and before its relay was
#: posted is ``conductor``, and re-running it would fire the gate again. ``implementer.dispatch``,
#: ``pr_gate.review`` and ``teardown`` are absent because they may have written outside.
AUTO_RESUME_PHASES: frozenset[str] = frozenset(
    {
        "proposer.spawn",
        "proposer.dispatch",
        "naysayer.spawn",
        "naysayer.dispatch",
        "implementer.spawn",
        "thread.read",
        "launch.resolve",
    }
)

#: Log line of a dispatch the budget refused (D1').
EVENT_KIND_BUDGET_YIELD = "conductor.budget_yield"
#: Log line of a run cut off in an :data:`AUTO_RESUME_PHASES` phase (D3'').
EVENT_KIND_BUDGET_CUT = "conductor.budget_cut"

_T = TypeVar("_T")


@dataclass
class TurnRecord:
    """One turn (or PR-gate review) this run dispatched (msg-9835 D2'): the timeout report's rows.

    ``started_s`` / ``duration_s`` are seconds from the start of the run; ``duration_s`` is
    ``None`` for the turn still running at the deadline. ``msg_id`` is the last post that landed
    during the turn, ``-`` when none did.
    """

    round: int
    role: str
    identity: str
    phase: str
    started_s: float
    duration_s: float | None = None
    msg_id: str = "-"


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
        # Set by :func:`run_with_budget` (loop clock). ``None`` = no budget around this run
        # (tests, library use): every budget check passes.
        self.started_at: float | None = None
        self.deadline: float | None = None
        # When :attr:`current` was entered (loop clock); restored together with it.
        self.phase_started_at: float | None = None
        # The implementer's reserve (D1'): its configured turn timeout + YIELD_MARGIN_S. The
        # composition root sets it; the default only covers the margin.
        self.implementer_reserve_s = YIELD_MARGIN_S
        self.other_reserve_s = NON_IMPLEMENTER_RESERVE_S
        self.turns: list[TurnRecord] = []

    def enter(self, phase: str) -> _PhaseScope:
        return _PhaseScope(self, phase)

    @staticmethod
    def now() -> float | None:
        """The running loop's clock, or ``None`` outside a loop."""
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:
            return None

    def remaining_s(self) -> float | None:
        """Seconds left before the run's deadline, or ``None`` when no budget is running."""
        now = self.now()
        if self.deadline is None or now is None:
            return None
        return self.deadline - now

    def reserve_s(self, role: str) -> float:
        """What a ``role`` turn needs left to start (D1')."""
        if role == "implementer":
            return self.implementer_reserve_s
        return self.other_reserve_s

    def short_for(self, role: str) -> tuple[float, float] | None:
        """``(remaining, reserve)`` when a ``role`` turn must not start now, else ``None``.

        Boundary (msg-9835 D1'): ``remaining < reserve`` stops; equal goes on.
        """
        remaining = self.remaining_s()
        if remaining is None:
            return None
        reserve = self.reserve_s(role)
        if remaining < reserve:
            return remaining, reserve
        return None

    def elapsed_s(self) -> float:
        now = self.now()
        if self.started_at is None or now is None:
            return 0.0
        return now - self.started_at

    def begin_turn(self, *, round_index: int, role: str, identity: str, phase: str) -> TurnRecord:
        record = TurnRecord(
            round=round_index,
            role=role,
            identity=identity,
            phase=phase,
            started_s=round(self.elapsed_s(), 1),
        )
        self.turns.append(record)
        return record

    def end_turn(self, record: TurnRecord) -> None:
        record.duration_s = round(self.elapsed_s() - record.started_s, 1)

    def note_post(self, msg_id: str) -> None:
        """A chatroom post landed: credit it to the turn still running, if any."""
        if msg_id and self.turns and self.turns[-1].duration_s is None:
            self.turns[-1].msg_id = msg_id

    def in_flight_s(self) -> float:
        """How long the current phase has been running."""
        now = self.now()
        if self.phase_started_at is None or now is None:
            return 0.0
        return now - self.phase_started_at


class _PhaseScope:
    """Sets a phase on enter and restores the previous one on exit.

    A plain class rather than ``contextlib.contextmanager``: the generator form writes
    ``__traceback__`` on any exception passing through it, and the conductor's dispatch path must
    re-raise an adapter's exception untouched, including one that refuses attribute writes
    (``tests/test_adapter_error_stop_line.py``). ``__exit__`` never looks at the exception.
    """

    __slots__ = ("_phase", "_previous", "_previous_at", "_run_phase")

    def __init__(self, run_phase: RunPhase | None, phase: str) -> None:
        self._run_phase = run_phase
        self._phase = phase
        self._previous = PHASE_IDLE
        self._previous_at: float | None = None

    def __enter__(self) -> None:
        if self._run_phase is not None:
            self._previous = self._run_phase.current
            self._previous_at = self._run_phase.phase_started_at
            self._run_phase.current = self._phase
            self._run_phase.phase_started_at = RunPhase.now()

    def __exit__(self, *_exc: object) -> None:
        if self._run_phase is not None:
            self._run_phase.current = self._previous
            self._run_phase.phase_started_at = self._previous_at


def enter_phase(run_phase: RunPhase | None, phase: str) -> _PhaseScope:
    """:meth:`RunPhase.enter`, or nothing when no phase object was wired (tests, library use)."""
    return _PhaseScope(run_phase, phase)


def post_phase_for(current: str) -> str:
    """The phase of a chatroom post made while the run is in ``current`` (msg-9837 D3''-2).

    Inside a role's dispatch the post is that role's reply (``<role>.post``); anywhere else it is
    the conductor's or the gate's own relay (:data:`PHASE_RELAY_POST`).
    """
    role, _, step = current.partition(".")
    if step == "dispatch" and role in ("proposer", "naysayer", "implementer"):
        return f"{role}.post"
    return PHASE_RELAY_POST


class PostPhaseMcp:
    """Wraps the run's :class:`McpToolCaller` so every chatroom post runs in a ``*.post`` phase.

    The one place a conductor run's chatroom writes pass through (msg-9837 D3''-2): the
    composition root hands this object to the gateway (role replies), the PR-gate orchestrator
    (verdict relay, ledger) and the Conductor (its own relay posts). A post cut off by the budget
    is therefore never in an :data:`AUTO_RESUME_PHASES` phase, and its unknown outcome goes to a
    person. A post that landed is credited to the running turn (:meth:`RunPhase.note_post`).
    """

    def __init__(self, inner: McpToolCaller, run_phase: RunPhase) -> None:
        self._inner = inner
        self._run_phase = run_phase

    @property
    def inner(self) -> McpToolCaller:
        """The transport this wraps: resolution and the run still share one client."""
        return self._inner

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name != "chatroom_post_message":
            return await self._inner.call_tool(name, arguments)
        with self._run_phase.enter(post_phase_for(self._run_phase.current)):
            result = await self._inner.call_tool(name, arguments)
        msg = result.get("msg") if isinstance(result, dict) else None
        if isinstance(msg, dict) and msg.get("msg_id"):
            self._run_phase.note_post(str(msg["msg_id"]))
        return result


def emit_budget_yield(*, remaining_s: float, reserve_s: float, role: str, rounds: int) -> None:
    """The one log line of a dispatch the budget refused (D1)."""
    logger.info(
        "%s remaining_s=%.1f reserve_s=%.1f role=%s rounds=%d",
        EVENT_KIND_BUDGET_YIELD,
        remaining_s,
        reserve_s,
        role,
        rounds,
    )


class RunTimeoutError(SystemExit):
    """Exit :data:`RUN_TIMEOUT_EXIT_CODE`: the soft budget expired and the notice did not land.

    A ``SystemExit`` subclass for the same reason as ``StalledError``: ``loop_runner.main``
    re-raises it without a traceback.
    """

    def __init__(self, event: Event) -> None:
        super().__init__(RUN_TIMEOUT_EXIT_CODE)
        self.event = event


def validate_budgets(
    *,
    run_budget_s: float,
    run_hard_budget_s: float,
    implementer_reserve_s: float | None = None,
    other_reserve_s: float | None = None,
) -> None:
    """Refuse to start when the soft path could not finish before the hard kill (msg-5498).

    With ``implementer_reserve_s`` (msg-9833 D1), also refuse a budget no implementer turn could
    ever start under: the reserve must be below ``run_budget_s``. With ``other_reserve_s``, the
    same for proposer / naysayer / PR-gate turns (PR #597 gate, REQUEST_CHANGES @ 9bf1a1a): a
    budget at or below their reserve would make every such turn yield on a fresh run, so the
    thread could never advance and would stand down instead.

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
    if implementer_reserve_s is not None and implementer_reserve_s >= run_budget_s:
        raise SystemExit(
            f"[conductor].run_budget_s ({run_budget_s}) must be greater than the implementer's "
            f"reserve ({implementer_reserve_s:g} s = its turn timeout + {YIELD_MARGIN_S:g} s), "
            f"or no implementer turn could ever be dispatched"
        )
    if other_reserve_s is not None and other_reserve_s >= run_budget_s:
        raise SystemExit(
            f"[conductor].run_budget_s ({run_budget_s}) must be greater than the "
            f"proposer / naysayer / PR-gate reserve ({other_reserve_s:g} s), "
            f"or none of those turns could ever be dispatched"
        )


def run_timeout_event(
    *,
    budget_s: float,
    elapsed_s: float,
    phase: str,
    project: str,
    thread: str,
    in_flight_s: float = 0.0,
    turns: int = 0,
) -> Event:
    """Build the ``conductor.run_timeout`` event (no message body, per the I6 Event convention).

    ``in_flight_phase`` / ``in_flight_s`` / ``in_flight_share`` describe the call running at the
    deadline (msg-9835 D2'). The share is an observation only and decides nothing (Einstein
    msg-9834 #1). ``auto_resume`` is decided by the phase alone (:data:`AUTO_RESUME_PHASES`).
    """
    share = round(in_flight_s / elapsed_s, 2) if elapsed_s > 0 else 0.0
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
            "in_flight_phase": phase,
            "in_flight_s": round(in_flight_s, 1),
            "in_flight_share": share,
            "turns": turns,
            "auto_resume": phase in AUTO_RESUME_PHASES,
        },
    )


def emit_run_timeout(event: Event) -> None:
    """Write the event as one warning line (the line a quarantine record's log tail carries)."""
    f = event.fields
    logger.warning(
        "%s budget_s=%s elapsed_s=%s phase=%s project=%s thread=%s "
        "in_flight_phase=%s in_flight_s=%s turns=%s",
        event.kind,
        f.get("budget_s"),
        f.get("elapsed_s"),
        f.get("phase"),
        f.get("project"),
        f.get("thread"),
        f.get("in_flight_phase"),
        f.get("in_flight_s"),
        f.get("turns"),
    )


def emit_budget_cut(event: Event) -> None:
    """The run was cut off in an :data:`AUTO_RESUME_PHASES` phase: no notice, rerun next tick."""
    f = event.fields
    logger.info(
        "%s phase=%s in_flight_s=%s turns=%s project=%s thread=%s",
        EVENT_KIND_BUDGET_CUT,
        f.get("in_flight_phase"),
        f.get("in_flight_s"),
        f.get("turns"),
        f.get("project"),
        f.get("thread"),
    )


def render_turn_table(turns: tuple[TurnRecord, ...]) -> str:
    """The turns of this run as a Markdown table (D2')."""
    if not turns:
        return "(この起動で dispatch したターンはありません)"
    lines = [
        "| round | role | identity | phase | 開始 (秒) | 所要 (秒) | msg |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in turns:
        duration = "実行中" if t.duration_s is None else f"{t.duration_s}"
        lines.append(
            f"| {t.round} | {t.role} | {t.identity} | `{t.phase}` | {t.started_s} | "
            f"{duration} | {t.msg_id} |"
        )
    return "\n".join(lines)


def render_run_timeout_notice(event: Event, turns: tuple[TurnRecord, ...] = ()) -> str:
    """The notice posted into the thread after the soft budget expired.

    Ends on ``NEXT: human`` like the conductor's other stop notices, with the stop marker above
    it (design §18.4), so the next sweep tick SKIPs instead of launching into the same hang.

    Facts only (msg-9835 D2'): the phase running at the deadline, how long it had run and its
    share of the run (an observation, not a diagnosis), and the turns of this run. A cut-off
    implementer turn adds where the wrapper may have put its unpushed commits.
    """
    f = event.fields
    phase = str(f.get("phase"))
    wip = (
        "\n\n実装ターンの途中で取り消しました。ラッパーが未 push のコミットをローカル ref "
        "`wip/*` に退避していることがあります。"
        "`git for-each-ref refs/heads/wip --sort=-creatordate` で確かめてください。"
        if phase.startswith("implementer.")
        else ""
    )
    return (
        f"Conductor run timeout — 1 回の起動の時間上限を超えたので止めました\n\n"
        f"- 上限: {f.get('budget_s')} 秒 (`[conductor].run_budget_s`)\n"
        f"- 経過: {f.get('elapsed_s')} 秒\n"
        f"- 上限の時点で動いていたのは `{phase}` で、経過は {f.get('in_flight_s')} 秒"
        f" (起動全体の {f.get('in_flight_share')})\n\n"
        f"この起動は上限までに終わらなかったので、実行中の呼び出しを取り消しました "
        f"(`{EVENT_KIND_RUN_TIMEOUT}`)。上の処理は外部に書いた可能性があるため、"
        f"自動では再実行しません。\n\n"
        f"この起動で進めたターン:\n\n{render_turn_table(turns)}"
        f"{wip}\n\n"
        f"次にやること: 上の処理が外部に何を残したかを確かめ、進め先の `NEXT:` を書いてください。"
        f"head が動くまでループは再開しません。\n\n"
        f"{render_stop_marker(event)}\n\n"
        f"NEXT: {HUMAN_TOKEN}"
    )


async def post_run_timeout_notice(
    mcp: McpToolCaller,
    *,
    project: str,
    thread_id: str,
    event: Event,
    author: str,
    turns: tuple[TurnRecord, ...] = (),
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
                    "content": render_run_timeout_notice(event, turns),
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
    run_phase.started_at = started
    run_phase.deadline = started + budget_s
    fired: list[Event] = []
    scope = asyncio.timeout(None)

    def _on_deadline() -> None:
        event = run_timeout_event(
            budget_s=budget_s,
            elapsed_s=round(loop.time() - started, 1),
            phase=run_phase.current,
            project=project,
            thread=thread,
            in_flight_s=run_phase.in_flight_s(),
            turns=len(run_phase.turns),
        )
        # The ``conductor.run_timeout`` line is written for every phase (it is what a quarantine
        # record's log tail looks for). D3'': a cut-off in an auto-resume phase adds the
        # ``conductor.budget_cut`` line; nobody is told about it and the next tick re-runs it.
        emit_run_timeout(event)
        if event.fields["auto_resume"]:
            emit_budget_cut(event)
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
    "AUTO_RESUME_PHASES",
    "EVENT_KIND_BUDGET_CUT",
    "EVENT_KIND_BUDGET_YIELD",
    "EVENT_KIND_RUN_KILLED",
    "EVENT_KIND_RUN_TIMEOUT",
    "NON_IMPLEMENTER_RESERVE_S",
    "PHASE_IDLE",
    "PHASE_PR_GATE",
    "PHASE_RELAY_POST",
    "RELAY_POST_BUDGET_S",
    "RUN_KILLED_EXIT_CODE",
    "RUN_KILL_UNCONFIRMED_EXIT_CODE",
    "RUN_TIMEOUT_EXIT_CODE",
    "YIELD_MARGIN_S",
    "PostPhaseMcp",
    "RunPhase",
    "RunTimeoutError",
    "TurnRecord",
    "emit_budget_cut",
    "emit_budget_yield",
    "emit_run_timeout",
    "enter_phase",
    "post_phase_for",
    "post_run_timeout_notice",
    "render_run_timeout_notice",
    "render_turn_table",
    "run_timeout_event",
    "run_with_budget",
    "validate_budgets",
]
