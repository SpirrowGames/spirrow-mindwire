"""Phase 2 of the PR-review sweep: close the ledgers whose PR has ended.

This is PR-B of ``T-pr-review-threads-outlive-their-prs``. The rules come from msg-6014 §2
(rules 1 and 5) and msg-6016 §2 (rules 2'-6', which replace msg-6014's 2-4 after the
naysayer's BLOCKING finding in msg-6015). The naysayer answered msg-6016 with PROCEED in
msg-6017.

Phase 2 only ever acts on Phase 1's two ``TERMINAL_*`` classes
(:data:`~spirrow_mindwire.pr_review_sweep.phase1.CLOSE_CANDIDATE_CLASSES`). It never
touches ``OPEN``, ``SKIP`` or ``UNPARSEABLE``.

The rules
=========

1. **The outcome is decided by reading the thread again, never by reading the error.**
   After each ``chatroom_close_thread`` call, whether it returned or raised, Phase 2 reads
   the thread with ``chatroom_get_thread``. ``status == "resolved"`` means ``CLOSED``.
   Anything else, including a read that fails, means ``REFUSED``. For a refusal the
   envelope's ``error_type`` and message are recorded **verbatim and not interpreted**.
   This is the same predicate ``gate_bootstrap.close_alert`` uses.

   Phase 2 does not try to tell a magickit policy refusal from an infrastructure failure
   (``_UpstreamError``). Both arrive inside ``PrGateLedgerNotClosableError`` (msg-4753),
   and matching on the text would fail silently whenever magickit rewords a message: an
   outage would be read as a policy refusal and dropped (msg-6016 §1). The rules below
   keep the sweep moving without ever classifying a refusal.

2'. **Per-ledger attempt state**, one small JSON file per project
   (:func:`load_state` / :func:`dump_state`). Keyed by ``thread_id``, with
   ``last_attempt_at``, ``consecutive_refusals`` and ``first_refused_at``. A ``CLOSED``
   ledger is removed, and so is a ledger that is no longer a Phase 1 close candidate
   (:func:`prune_state`). The file is local and can be deleted with no harm: a lost or
   corrupt file reads as "never tried" (fail-open), which only produces extra attempts.

3'. **Least recently tried first** (:func:`order_candidates`). Never-tried ledgers come
   first, then the oldest ``last_attempt_at``. Every attempt, whatever its result,
   stamps ``last_attempt_at = now`` and so sends that ledger to the back. A ledger that
   is always refused can therefore never keep the front of the queue, and a newly
   merged one, with no record, goes straight to the front.

4'. **Per-ledger backoff** (:func:`backoff_delay`). After ``r`` consecutive refusals a
   ledger waits ``min(2**r, cap)`` backoff units before it is tried again.

5'. **Circuit breaker for rate only.** ``K`` consecutive refusals inside one tick end
   that tick's attempts. Because of 3' the same ledgers do not head the next tick, so
   the breaker limits the rate during an outage (3 attempts per tick instead of 143 in a
   row) without causing starvation. Separately, a tick makes at most ``N`` attempts.

6'. **Report.** ``CLOSED``, ``REFUSED`` with the raw envelope, and a "needs a human"
   list of ledgers refused at least ``R`` times in a row. Deciding whether that is
   policy or infrastructure is the human's job; the machine makes no such judgement.

5. **Off by default.** With ``enabled=False`` Phase 2 makes **no MCP call of any kind**
   and writes no state. It only lists the ledgers it would try this tick, in order.

Ticks versus wall-clock time (msg-6017's advisory)
==================================================

msg-6016 states the backoff in ticks, but the sweep is a script run on a schedule with no
tick counter. Counting invocations would make the backoff depend on how often the
scheduler happens to fire. So the backoff is measured in **wall-clock time**, in a fixed
unit, :data:`DEFAULT_BACKOFF_UNIT_SECONDS` = **one hour**. A ledger refused ``r`` times in
a row is due again ``min(2**r, 64)`` hours after its last attempt: 2 h, 4 h, 8 h, 16 h,
32 h, then 64 h (about 2.7 days) from the sixth refusal on. One hour was chosen because
it is no shorter than any interval the sweep is expected to run at; if the sweep runs
less often than once an hour, the early steps of the backoff simply expire between runs
and the per-tick cap ``N`` and the breaker ``K`` are what bound the rate. The unit and
the cap are settings (:class:`Phase2Settings`), not constants buried in the logic.

Who reads what this module writes, and where it goes when the thread is gone
==========================================================================

(OBL-CHATROOM-PRODUCER-READER-SURFACE.) ``chatroom_close_thread`` writes one ``decide``
message into the ledger thread. Its intended reader is whoever later audits that PR's
review ledger: it says the PR ended, how, and that the sweep closed the ledger. If the
target thread is already resolved, magickit refuses the write (``ThreadResolvedError``,
a :class:`~spirrow_mindwire.magickit.client.MagickitMcpError` subclass). That refusal is
caught like every other one and decided by the read-back: a resolved thread is exactly
the goal state, so it is counted ``CLOSED`` and nothing is re-posted anywhere. Every
outcome, including every refusal with its raw envelope, is reported on the tick's JSON
stdout, which is the durable operator log (fallback surface (1)). No successor thread
is ever opened.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from spirrow_mindwire.magickit.client import MagickitMcpError

from .phase1 import CLOSE_CANDIDATE_CLASSES, LedgerRow, Phase1Class

#: The ledger's owner (msg-2016 §2). Whether magickit lets this role close the ledger is
#: not decided here: msg-6014 §3 leaves it to the first live run to observe.
DEFAULT_AUTHOR = "orchestrator"

#: Same value the gate-bootstrap sweeper declares on its own closes. ``chatroom_close_thread``
#: emits a ``decide`` message, so an embodiment is required on the call.
CLOSE_EMBODIMENT = "unknown"

#: msg-6016 §2 rule 4': the backoff stops growing at ``2**6`` units.
DEFAULT_BACKOFF_CAP_UNITS = 64
#: msg-6017 advisory: the wall-clock length of one backoff unit. See the module docstring.
DEFAULT_BACKOFF_UNIT_SECONDS = 3600
#: msg-6014 §2 rule 3: at most N close attempts per tick.
DEFAULT_MAX_ATTEMPTS = 20
#: msg-6016 §2 rule 5': K consecutive refusals end the tick.
DEFAULT_BREAKER = 3
#: msg-6016 §2 rule 6': R consecutive refusals put a ledger on the "needs a human" list.
DEFAULT_NEEDS_HUMAN_AFTER = 5

_RESOLVED = "resolved"

#: Every chatroom tool Phase 2 itself calls, and only when enabled. One write, one read.
CLOSE_TOOLS = frozenset({"chatroom_close_thread", "chatroom_get_thread"})


class ToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class AttemptResult(StrEnum):
    CLOSED = "closed"
    REFUSED = "refused"


class NotReached(StrEnum):
    """Why a due ledger was not tried this tick. It keeps its place for the next tick."""

    CAP = "attempt-cap"
    BREAKER = "breaker"


@dataclass(frozen=True)
class Phase2Settings:
    enabled: bool = False
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    breaker: int = DEFAULT_BREAKER
    needs_human_after: int = DEFAULT_NEEDS_HUMAN_AFTER
    backoff_cap_units: int = DEFAULT_BACKOFF_CAP_UNITS
    backoff_unit_seconds: int = DEFAULT_BACKOFF_UNIT_SECONDS
    author: str = DEFAULT_AUTHOR

    def __post_init__(self) -> None:
        for name in (
            "max_attempts",
            "breaker",
            "needs_human_after",
            "backoff_cap_units",
            "backoff_unit_seconds",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"Phase2Settings.{name} must be >= 1")
        if not self.author.strip():
            raise ValueError("Phase2Settings.author must be non-empty")


@dataclass(frozen=True)
class AttemptRecord:
    """One ledger's line in the state file (msg-6016 §2 rule 2')."""

    last_attempt_at: datetime
    consecutive_refusals: int = 0
    first_refused_at: datetime | None = None


#: In memory the key is ``(project, thread_id)``: two projects may hold the same id.
StateKey = tuple[str, str]
State = dict[StateKey, AttemptRecord]


def _key(row: LedgerRow) -> StateKey:
    return (row.project, row.thread_id)


# --- state file -------------------------------------------------------------------------

STATE_SCHEMA_VERSION = 1


def load_state(project: str, text: str | None) -> tuple[State, str | None]:
    """Parse one project's state file. Fail-open: never raises.

    ``text is None`` means the file does not exist. Returns the state and, when the file
    was present but unusable, a one-line warning for the report. An unusable file or line
    reads as "never tried", which only costs extra attempts (msg-6016 §4).
    """
    if text is None:
        return {}, None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"{project}: state file is not valid JSON, treated as empty: {exc}"
    if not isinstance(payload, dict) or payload.get("schema_version") != STATE_SCHEMA_VERSION:
        return {}, f"{project}: state file has an unknown shape, treated as empty"
    ledgers = payload.get("ledgers")
    if not isinstance(ledgers, dict):
        return {}, f"{project}: state file has no 'ledgers' object, treated as empty"

    state: State = {}
    dropped = 0
    for thread_id, raw in ledgers.items():
        record = _parse_record(raw)
        if record is None:
            dropped += 1
            continue
        state[(project, str(thread_id))] = record
    warning = (
        f"{project}: {dropped} unusable state line(s) treated as never tried" if dropped else None
    )
    return state, warning


def _parse_record(raw: Any) -> AttemptRecord | None:
    if not isinstance(raw, dict):
        return None
    try:
        last = _parse_ts(raw["last_attempt_at"])
        refusals = raw.get("consecutive_refusals", 0)
        if last is None or not isinstance(refusals, int) or isinstance(refusals, bool):
            return None
        if refusals < 0:
            return None
        first_raw = raw.get("first_refused_at")
        first = None if first_raw is None else _parse_ts(first_raw)
        if first_raw is not None and first is None:
            return None
    except KeyError:
        return None
    return AttemptRecord(last, refusals, first)


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # A naive timestamp cannot be compared with the tick's aware ``now``.
    return value if value.tzinfo is not None else None


def dump_state(project: str, state: State) -> str:
    """Serialise one project's slice of ``state``. Stable order, for readable diffs."""
    ledgers = {
        thread_id: {
            "last_attempt_at": rec.last_attempt_at.isoformat(),
            "consecutive_refusals": rec.consecutive_refusals,
            "first_refused_at": rec.first_refused_at.isoformat() if rec.first_refused_at else None,
        }
        for (proj, thread_id), rec in sorted(state.items())
        if proj == project
    }
    return json.dumps(
        {"schema_version": STATE_SCHEMA_VERSION, "ledgers": ledgers},
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    )


def prune_state(state: State, candidates: Iterable[LedgerRow]) -> State:
    """Drop every record whose ledger is no longer a close candidate (rule 2')."""
    keep = {_key(r) for r in candidates if r.cls in CLOSE_CANDIDATE_CLASSES}
    return {k: v for k, v in state.items() if k in keep}


# --- scheduling -------------------------------------------------------------------------


def backoff_delay(consecutive_refusals: int, settings: Phase2Settings) -> timedelta:
    """``min(2**r, cap)`` units; no wait at all for a ledger that was never refused."""
    if consecutive_refusals <= 0:
        return timedelta(0)
    units = min(2 ** min(consecutive_refusals, 62), settings.backoff_cap_units)
    return timedelta(seconds=units * settings.backoff_unit_seconds)


def due_at(record: AttemptRecord | None, settings: Phase2Settings) -> datetime | None:
    """When a ledger may next be tried. ``None`` means now: it was never tried."""
    if record is None:
        return None
    return record.last_attempt_at + backoff_delay(record.consecutive_refusals, settings)


def order_candidates(rows: Iterable[LedgerRow], state: State) -> list[LedgerRow]:
    """Least recently tried first (rule 3'). Never-tried ledgers lead.

    Ties break on ``(project, thread_id)`` so the order is deterministic.
    """

    def sort_key(row: LedgerRow) -> tuple[int, str, str, str]:
        record = state.get(_key(row))
        if record is None:
            return (0, "", row.project, row.thread_id)
        return (1, record.last_attempt_at.isoformat(), row.project, row.thread_id)

    return sorted(rows, key=sort_key)


# --- one close --------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptOutcome:
    row: LedgerRow
    result: AttemptResult
    #: The read-back's ``thread.status``; ``None`` when the read-back itself failed.
    observed_status: str | None
    #: Verbatim from the close call's exception, when it raised. Never branched on.
    error_type: str | None = None
    error: str | None = None


def close_summary(row: LedgerRow) -> str:
    """The ``decide`` body the close writes into the ledger. Fixed text, no LLM."""
    how = "merged" if row.cls is Phase1Class.TERMINAL_MERGED else "closed without merging"
    when = row.closed_at.isoformat() if row.closed_at else "unknown time"
    return (
        f"PR-review sweep (Phase 2): {row.pr or 'the PR'} was {how} at {when}. "
        "This 1:1 PR-review ledger is closed because its PR has ended "
        "(T-pr-review-threads-outlive-their-prs, rule 1a)."
    )


async def read_status(mcp: ToolCaller, project: str, thread_id: str) -> str | None:
    """``thread.status`` from ``chatroom_get_thread``; ``None`` on any read failure."""
    try:
        payload = await mcp.call_tool(
            "chatroom_get_thread", {"project": project, "thread_id": thread_id, "mode": "summary"}
        )
    except MagickitMcpError:
        return None
    thread = payload.get("thread") if isinstance(payload, dict) else None
    status = thread.get("status") if isinstance(thread, dict) else None
    return status if isinstance(status, str) else None


async def attempt_close(
    mcp: ToolCaller, row: LedgerRow, settings: Phase2Settings
) -> AttemptOutcome:
    """Send one close, then decide the outcome from the thread alone (rule 1)."""
    payload: dict[str, Any] = {
        "project": row.project,
        "thread_id": row.thread_id,
        "author": settings.author,
        "summary_content": close_summary(row),
        "embodiment": CLOSE_EMBODIMENT,
    }
    error_type: str | None = None
    error: str | None = None
    try:
        await mcp.call_tool("chatroom_close_thread", payload)
    except MagickitMcpError as exc:
        # Recorded, not interpreted. The read-back below is the only thing that decides.
        error_type = exc.error_type
        error = str(exc)
    status = await read_status(mcp, row.project, row.thread_id)
    result = AttemptResult.CLOSED if status == _RESOLVED else AttemptResult.REFUSED
    return AttemptOutcome(row, result, status, error_type, error)


# --- one tick ---------------------------------------------------------------------------


@dataclass
class Phase2Report:
    settings: Phase2Settings
    now: datetime
    #: Phase 1's rows, as given. The non-candidate ones are reported, never acted on.
    rows: list[LedgerRow]
    #: Enabled: what was tried, in order. Disabled: empty.
    attempts: list[AttemptOutcome] = field(default_factory=list)
    #: Disabled only: what would be tried this tick, in order.
    would_attempt: list[LedgerRow] = field(default_factory=list)
    #: Due, but not tried this tick (cap or breaker). They keep their place.
    not_reached: list[tuple[LedgerRow, NotReached]] = field(default_factory=list)
    #: Still waiting out their backoff: ``(row, due_at)``.
    deferred: list[tuple[LedgerRow, datetime]] = field(default_factory=list)
    breaker_tripped: bool = False
    #: The state after the tick. Written back only when enabled.
    state: State = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def closed(self) -> list[AttemptOutcome]:
        return [a for a in self.attempts if a.result is AttemptResult.CLOSED]

    @property
    def refused(self) -> list[AttemptOutcome]:
        return [a for a in self.attempts if a.result is AttemptResult.REFUSED]

    @property
    def needs_human(self) -> list[tuple[StateKey, AttemptRecord]]:
        return sorted(
            (k, v)
            for k, v in self.state.items()
            if v.consecutive_refusals >= self.settings.needs_human_after
        )


async def run_tick(
    mcp: ToolCaller | None,
    rows: list[LedgerRow],
    state: State,
    now: datetime,
    settings: Phase2Settings,
) -> Phase2Report:
    """One Phase 2 tick over Phase 1's rows.

    With ``settings.enabled`` false, ``mcp`` is never touched (it may be ``None``) and the
    returned state is only pruned, not advanced. The caller writes the state back only
    when enabled.
    """
    if now.tzinfo is None:
        raise ValueError("run_tick needs an aware 'now'")
    candidates = [r for r in rows if r.cls in CLOSE_CANDIDATE_CLASSES]
    state = prune_state(state, candidates)
    report = Phase2Report(settings=settings, now=now, rows=list(rows))

    due: list[LedgerRow] = []
    for row in order_candidates(candidates, state):
        when = due_at(state.get(_key(row)), settings)
        if when is not None and when > now:
            report.deferred.append((row, when))
        else:
            due.append(row)

    if not settings.enabled:
        report.would_attempt = due[: settings.max_attempts]
        report.not_reached = [(r, NotReached.CAP) for r in due[settings.max_attempts :]]
        report.state = state
        return report

    if mcp is None:
        raise ValueError("run_tick: enabled but no MCP client was given")

    streak = 0
    for index, row in enumerate(due):
        if index >= settings.max_attempts:
            report.not_reached.extend((r, NotReached.CAP) for r in due[index:])
            break
        if streak >= settings.breaker:
            report.not_reached.extend((r, NotReached.BREAKER) for r in due[index:])
            break
        outcome = await attempt_close(mcp, row, settings)
        report.attempts.append(outcome)
        key = _key(row)
        if outcome.result is AttemptResult.CLOSED:
            state.pop(key, None)
            streak = 0
            continue
        streak += 1
        previous = state.get(key)
        refusals = (previous.consecutive_refusals if previous else 0) + 1
        first = previous.first_refused_at if previous and previous.first_refused_at else now
        state[key] = AttemptRecord(now, refusals, first)

    # Also true when the K-th refusal was the tick's last due ledger: the tick still
    # ended in a run of K refusals, which is what the operator needs to see.
    report.breaker_tripped = streak >= settings.breaker
    report.state = state
    return report


def report_to_json(report: Phase2Report) -> dict[str, Any]:
    """Plain-data view of the tick, for ``json.dumps``. The four lists of rule 6'."""
    s = report.settings

    def ref(row: LedgerRow) -> dict[str, Any]:
        return {"project": row.project, "thread_id": row.thread_id, "pr": row.pr}

    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    refused = []
    for a in report.refused:
        rec = report.state.get(_key(a.row))
        refused.append(
            {
                **ref(a.row),
                "observed_status": a.observed_status,
                "error_type": a.error_type,
                "error": a.error,
                "consecutive_refusals": rec.consecutive_refusals if rec else None,
                "first_refused_at": iso(rec.first_refused_at) if rec else None,
            }
        )

    unparseable: dict[str, list[str]] = {}
    for row in report.rows:
        if row.cls is Phase1Class.UNPARSEABLE:
            unparseable.setdefault(row.reason, []).append(f"{row.project}/{row.thread_id}")

    return {
        "phase": 2,
        "enabled": s.enabled,
        "now": report.now.isoformat(),
        "wrote_anything": bool(report.attempts),
        "settings": {
            "max_attempts": s.max_attempts,
            "breaker": s.breaker,
            "needs_human_after": s.needs_human_after,
            "backoff_cap_units": s.backoff_cap_units,
            "backoff_unit_seconds": s.backoff_unit_seconds,
            "author": s.author,
        },
        "closed": [ref(a.row) for a in report.closed],
        "refused": refused,
        "needs_human": [
            {
                "project": proj,
                "thread_id": tid,
                "consecutive_refusals": rec.consecutive_refusals,
                "first_refused_at": iso(rec.first_refused_at),
                "last_attempt_at": iso(rec.last_attempt_at),
            }
            for (proj, tid), rec in report.needs_human
        ],
        # Keyed by Phase 1's reason, so ``pr-not-found`` stays apart (msg-6014 §1 #1).
        "unparseable": unparseable,
        "skip": [ref(r) | {"reason": r.reason} for r in report.rows if r.cls is Phase1Class.SKIP],
        "would_attempt": [ref(r) for r in report.would_attempt],
        "not_reached": [ref(r) | {"why": why.value} for r, why in report.not_reached],
        "deferred": [ref(r) | {"due_at": iso(when)} for r, when in report.deferred],
        "breaker_tripped": report.breaker_tripped,
        "warnings": list(report.warnings),
    }


__all__ = [
    "CLOSE_EMBODIMENT",
    "CLOSE_TOOLS",
    "DEFAULT_AUTHOR",
    "DEFAULT_BACKOFF_CAP_UNITS",
    "DEFAULT_BACKOFF_UNIT_SECONDS",
    "DEFAULT_BREAKER",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_NEEDS_HUMAN_AFTER",
    "STATE_SCHEMA_VERSION",
    "AttemptOutcome",
    "AttemptRecord",
    "AttemptResult",
    "NotReached",
    "Phase2Report",
    "Phase2Settings",
    "State",
    "attempt_close",
    "backoff_delay",
    "close_summary",
    "due_at",
    "dump_state",
    "load_state",
    "order_candidates",
    "prune_state",
    "read_status",
    "report_to_json",
    "run_tick",
]
