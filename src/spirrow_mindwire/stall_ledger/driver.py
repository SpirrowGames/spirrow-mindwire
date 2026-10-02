"""D-16ab log-only evaluation driver.

Spec: T-stalled-pr-has-no-detector msg-4685 (§0 scope, §4 driver, §6 log lines) with the
amendments in msg-4687 §3, msg-4689 §2, msg-4691 §2, msg-4693 §2-§3, msg-4695 §3-§4,
msg-4697 §1-§2, msg-4699 §2 (clear requests), msg-4701 §3 (deadline and timeouts only)
and msg-4703 §3 / msg-4705 §3 (the lock).

On each heartbeat: take the lock -> load the store -> apply clear requests -> fetch the
sources -> predicates + classifier -> update the open-record store -> write JSON log lines
to stdout. **No remedies, no digest output, no alerts** (§0). Nothing here writes to the
digest, Discord, the chatroom or GitHub; ``remedy_attempts`` and ``ladder_stage`` are
never added to or changed (msg-4687 §3-3).

Every log line is one JSON object with a ``kind``. The heartbeat level line is written
on every heartbeat, including one that did not evaluate (``evaluated=false``) -- a stop
is never silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO, assert_never

from spirrow_mindwire.stall_ledger.adapters import (
    ADAPTER_FORMAT_VERSION,
    Adapter,
    Marked,
    MarkerCheck,
    MotionEvent,
    MotionType,
    SourceResult,
    UnitObservation,
    Unmarked,
    Unverifiable,
)
from spirrow_mindwire.stall_ledger.classifier import classify
from spirrow_mindwire.stall_ledger.heartbeat import HeartbeatRecord, derive_state
from spirrow_mindwire.stall_ledger.lock import LOCK_FILENAME, LedgerLock
from spirrow_mindwire.stall_ledger.model import (
    Event,
    OriginKind,
    RemedyAttempt,
    StallRecord,
)
from spirrow_mindwire.stall_ledger.origin import origin
from spirrow_mindwire.stall_ledger.predicates import stalled
from spirrow_mindwire.stall_ledger.store import (
    REQUESTS_DIRNAME,
    STORE_FILENAME,
    InvalidRequest,
    LoadedStore,
    StoreStatus,
    load_store,
    new_record_json,
    read_clear_requests,
    save_store,
    utc_iso,
)
from spirrow_mindwire.stall_ledger.timing import (
    FETCH_MARGIN,
    FETCH_TIMEOUT,
    T_TICK_MAX,
    fetch_window,
)

#: Whole-tick deadline (msg-4701 §3). Set by D-16c in :mod:`.timing`.
DEFAULT_T_TICK_MAX = T_TICK_MAX

#: Suffix an unusable clear request is renamed to, taking it out of ``clear-*.json``.
INVALID_REQUEST_SUFFIX = ".invalid"

FLAG_EPOCH_UNKNOWN = "epoch_unknown"
FLAG_ORIGIN_UNKNOWN = "origin_unknown"

#: Driver-side ``marker_reason`` values (D-16c, msg-5746 §2). No adapter returns them.
#: ``budget``: the fetch is owed (``marker_pending``). ``e_not_found``: an owed fetch
#: whose event is no longer in the listing.
MARKER_REASON_BUDGET = "budget"
MARKER_REASON_E_NOT_FOUND = "e_not_found"

LogFn = Callable[[dict[str, Any]], None]


def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600.0, 4)


# ─── Pure evaluation ───────────────────────────────────────────────────────────────────


def last_participant_motion_at(obs: UnitObservation, attempts: Sequence[RemedyAttempt]) -> datetime:
    """Newest motion ``origin()`` calls participant, floored at the unit's baseline.

    ``attempts`` are the STORED ``remedy_attempts`` of the unit's record (msg-4687 §3-2);
    empty for a unit with no record. The existing ``origin()`` does the filtering -- this
    function adds no rule of its own.
    """
    probe = StallRecord(
        unit=obs.unit,
        stall_epoch_start=obs.baseline_at,
        condition_first_seen_at=obs.baseline_at,
        klass="",
        remedy_attempts=list(attempts),
    )
    newest = obs.baseline_at
    for event in obs.motion:
        if origin(Event(id=event.id, at=event.at), probe) == OriginKind.PARTICIPANT:
            newest = max(newest, event.at)
    return newest


def select_e(obs: UnitObservation, attempts: Sequence[RemedyAttempt]) -> MotionEvent | None:
    """E = the newest participant motion event, i.e. the event that set
    ``motion_at_open`` (msg-4675 §1). ``None`` when the baseline set it."""
    probe = StallRecord(
        unit=obs.unit,
        stall_epoch_start=obs.baseline_at,
        condition_first_seen_at=obs.baseline_at,
        klass="",
        remedy_attempts=list(attempts),
    )
    best: MotionEvent | None = None
    for event in obs.motion:
        if origin(Event(id=event.id, at=event.at), probe) != OriginKind.PARTICIPANT:
            continue
        if event.at < obs.baseline_at:
            continue
        if best is None or event.at > best.at:
            best = event
    return best


def marker_flags(result: MarkerCheck) -> tuple[list[str], str | None]:
    """The E table's rows for ``review`` / ``chatroom_msg`` (msg-4685 §4-2, msg-4683 §2).

    Exhaustive over :data:`MarkerCheck`: a new variant fails type checking here and the
    exhaustiveness test, instead of falling through to "trusted".
    """
    match result:
        case Marked():
            return [FLAG_ORIGIN_UNKNOWN], None
        case Unmarked():
            return [], None
        case Unverifiable(reason=reason):
            return [FLAG_ORIGIN_UNKNOWN], reason
        case _:
            assert_never(result)


@dataclass
class TickCounters:
    body_fetches: int = 0
    opened: int = 0
    closed: int = 0
    markers_resolved: int = 0


class FetchBudget:
    """The shared time budget for body fetches in one tick (D-16c A1, msg-5744 §1).

    A fetch may start only if a full ``fetch_timeout`` from now still ends before
    ``deadline`` (a ``monotonic`` reading). So a fetch never pushes the tick past its
    window, and running out of budget never aborts a tick. Once one fetch is refused,
    every later one in the same tick is refused as well (A2': "once the budget is used
    up"). ``deadline=None`` means no limit.
    """

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        deadline: float | None = None,
        fetch_timeout: timedelta = FETCH_TIMEOUT,
    ) -> None:
        self._monotonic = monotonic
        self._deadline = deadline
        self._fetch_timeout = fetch_timeout.total_seconds()
        self.exhausted = False
        self.deferred = 0

    def try_start(self) -> bool:
        if not self.exhausted and (
            self._deadline is None or self._monotonic() + self._fetch_timeout <= self._deadline
        ):
            return True
        self.exhausted = True
        self.deferred += 1
        return False


@dataclass(frozen=True)
class _EVerdict:
    """What E says about a unit's origin. ``pending``: the fetch is owed (A2').

    ``flags`` is every flag the E table assigns, carried whole -- for a body fetch that
    is exactly the list :func:`marker_flags` returned, so a flag it adds later reaches
    the record instead of being dropped here.
    """

    flags: tuple[str, ...]
    reason: str | None
    pending: bool = False


async def _judge_e(
    result: SourceResult,
    obs: UnitObservation,
    e: MotionEvent | None,
    budget: FetchBudget,
    log: LogFn,
    counters: TickCounters,
) -> _EVerdict:
    """The E table (msg-4685 §4-2), with the D-16c budget in front of the body fetch."""
    if e is None or e.type == MotionType.HEAD_PUSH:
        return _EVerdict((), None)
    if e.type in (MotionType.REVIEW, MotionType.CHATROOM_MSG):
        if not budget.try_start():
            return _EVerdict((FLAG_ORIGIN_UNKNOWN,), MARKER_REASON_BUDGET, pending=True)
        counters.body_fetches += 1
        extra, reason = marker_flags(await result.checker.check_marker(obs.unit, e))
        return _EVerdict(tuple(extra), reason)
    log({"kind": "unknown_motion_type", "unit": obs.unit.key, "type": e.type})
    return _EVerdict((FLAG_ORIGIN_UNKNOWN,), None)


async def evaluate(
    *,
    store: LoadedStore,
    results: Sequence[SourceResult],
    now: datetime,
    log: LogFn,
    counters: TickCounters,
    budget: FetchBudget | None = None,
) -> None:
    """Open / reclassify / close records for every observed unit (msg-4685 §4).

    ``store`` must be writable. Mutates it in place. Three passes (D-16c, msg-5746 §2):

    1. close (rules a / b) or reclassify every existing record, and collect the units
       to open -- no body fetch happens here;
    2. retry owed marker fetches (A5) on records still open and observed this tick,
       oldest ``stall_epoch_start`` first -- they get the budget before new opens;
    3. open the collected units, spending what is left of the budget.

    The open / close rules themselves are unchanged from D-16ab.
    """
    budget = budget if budget is not None else FetchBudget()
    untrusted_all = store.status in (StoreStatus.ABSENT, StoreStatus.CORRUPT)
    to_open: list[tuple[SourceResult, UnitObservation, datetime]] = []
    still_open: dict[str, tuple[SourceResult, UnitObservation]] = {}
    for result in results:
        observed: set[str] = set()
        for obs in result.observations:
            key = obs.unit.key
            observed.add(key)
            record = store.records.get(key)
            attempts = record.remedy_attempts if record is not None else []
            lpm = last_participant_motion_at(obs, attempts)
            if record is None:
                if stalled(obs.unit.kind, lpm, now, obs.needs_actor, obs.n):
                    to_open.append((result, obs, lpm))
                continue
            # Step 3: open record -- close on INV-2, else reclassify.
            if not obs.needs_actor:
                _close(store, record, "a", now, now, "now", log, counters)
            elif lpm > record.stall_epoch_start:
                _close(store, record, "b", now, lpm, "motion", log, counters)
            else:
                store.reclassify(key, classify(obs.classifier_input).value, now)
                still_open[key] = (result, obs)
        if not result.complete:
            continue
        # A complete listing that no longer shows a unit in its scope: needs_actor is
        # false (e.g. the PR closed, the quarantine entry was cleared) -> rule (a).
        for key, record in list(store.records.items()):
            if key in observed or key in result.unobservable:
                continue
            if result.scope(record.unit):
                _close(store, record, "a", now, now, "now", log, counters)

    owed = sorted(
        (
            (store.records[key], result, obs)
            for key, (result, obs) in still_open.items()
            if key in store.records and store.records[key].evidence.get("marker_pending") is True
        ),
        key=lambda item: (item[0].stall_epoch_start, item[0].unit.key),
    )
    for record, result, obs in owed:
        await _settle_owed(store, record, result, obs, budget, log, counters)

    for result, obs, lpm in to_open:
        untrusted = untrusted_all or obs.unit.key in store.quarantined
        await _open(store, result, obs, lpm, now, untrusted, budget, log, counters)


async def _settle_owed(
    store: LoadedStore,
    record: StallRecord,
    result: SourceResult,
    obs: UnitObservation,
    budget: FetchBudget,
    log: LogFn,
    counters: TickCounters,
) -> None:
    """Retry one owed marker fetch (A5, msg-5746 §2).

    E is recomputed from this tick's observation exactly as ``_open`` computed it, and
    used only if ``E.at == motion_at_open``. Any other E (or none) settles the record
    as ``e_not_found``: the event the stall was opened on can no longer be found, and
    that is the loud side. A fetch the budget defers again leaves the record unchanged.
    """
    motion_at_open_raw = record.evidence["motion_at_open"]
    assert isinstance(motion_at_open_raw, str)
    motion_at_open = datetime.fromisoformat(motion_at_open_raw)
    e = select_e(obs, [])
    if e is None or e.at != motion_at_open:
        verdict = _EVerdict((FLAG_ORIGIN_UNKNOWN,), MARKER_REASON_E_NOT_FOUND)
    else:
        verdict = await _judge_e(result, obs, e, budget, log, counters)
        if verdict.pending:
            return
    # The owed record carries the budget verdict's provisional flags; the settled verdict
    # replaces them. Every other flag (epoch_unknown) is kept.
    flags = [f for f in record.flags if f != FLAG_ORIGIN_UNKNOWN]
    flags.extend(f for f in verdict.flags if f not in flags)
    store.settle_marker(record.unit.key, flags, verdict.reason)
    counters.markers_resolved += 1
    line: dict[str, Any] = {
        "kind": "marker_resolved",
        "record_id": record.record_id,
        "unit": record.unit.key,
        "flags": flags,
    }
    if verdict.reason is not None:
        line["marker_reason"] = verdict.reason
    log(line)


async def _open(
    store: LoadedStore,
    result: SourceResult,
    obs: UnitObservation,
    lpm: datetime,
    now: datetime,
    untrusted: bool,
    budget: FetchBudget,
    log: LogFn,
    counters: TickCounters,
) -> None:
    flags: list[str] = []
    evidence: dict[str, Any] = {}
    verdict = _EVerdict((), None)
    if untrusted:
        flags.append(FLAG_EPOCH_UNKNOWN)
        verdict = await _judge_e(result, obs, select_e(obs, []), budget, log, counters)
        flags.extend(f for f in verdict.flags if f not in flags)
        if verdict.reason is not None:
            evidence["marker_reason"] = verdict.reason
        if verdict.pending:
            evidence["marker_pending"] = True
    klass = classify(obs.classifier_input).value
    raw = new_record_json(
        unit=obs.unit, now=now, klass=klass, flags=flags, motion_at_open=lpm, evidence=evidence
    )
    store.open_record(obs.unit.key, raw)
    record = store.records[obs.unit.key]
    counters.opened += 1
    line: dict[str, Any] = {
        "kind": "open",
        "record_id": record.record_id,
        "unit": obs.unit.key,
        "class": klass,
        "flags": list(flags),
        "motion_at_open": utc_iso(lpm),
        "hours_since_last_participant_motion": _hours(now - lpm),
    }
    if verdict.reason is not None:
        line["marker_reason"] = verdict.reason
    if verdict.pending:
        line["marker_pending"] = True
    log(line)


def _close(
    store: LoadedStore,
    record: StallRecord,
    rule: str,
    now: datetime,
    endpoint: datetime,
    endpoint_kind: str,
    log: LogFn,
    counters: TickCounters,
) -> None:
    motion_at_open_raw = record.evidence["motion_at_open"]
    assert isinstance(motion_at_open_raw, str)
    motion_at_open = datetime.fromisoformat(motion_at_open_raw)
    store.close_record(record.unit.key)
    counters.closed += 1
    line: dict[str, Any] = {
        "kind": "close",
        "record_id": record.record_id,
        "rule": rule,
        "age": _hours(record.age(now)),
        "flags": list(record.flags),
        "silence_hours": _hours(endpoint - motion_at_open),
        "silence_endpoint": endpoint_kind,
    }
    # Closed while its marker fetch was still owed: the flags are not final (msg-5746 §2).
    if record.evidence.get("marker_pending") is True:
        line["marker_pending"] = True
    log(line)


# ─── Tick orchestration ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TickPaths:
    state_dir: Path

    @property
    def store(self) -> Path:
        return self.state_dir / STORE_FILENAME

    @property
    def lock(self) -> Path:
        return self.state_dir / LOCK_FILENAME

    @property
    def requests(self) -> Path:
        return self.state_dir / REQUESTS_DIRNAME

    @property
    def quarantine_json(self) -> Path:
        return self.state_dir / "quarantine.json"


@dataclass
class TickOutcome:
    """What a tick did -- for tests and for the CLI's exit code."""

    evaluated: bool
    wrote_store: bool
    skipped: str | None = None
    aborted: str | None = None
    lines: list[dict[str, Any]] = field(default_factory=list)
    counters: TickCounters = field(default_factory=TickCounters)


class _Clock:
    def __init__(self, now: Callable[[], datetime], monotonic: Callable[[], float]) -> None:
        self.now = now
        self.monotonic = monotonic


def _emitter(out: TextIO, sink: list[dict[str, Any]], now: datetime) -> LogFn:
    def log(line: dict[str, Any]) -> None:
        line = {"at": utc_iso(now), **line}
        sink.append(line)
        out.write(json.dumps(line, ensure_ascii=True, sort_keys=False) + "\n")
        out.flush()

    return log


async def run_tick(
    *,
    paths: TickPaths,
    adapters: Sequence[Adapter],
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    t_tick_max: timedelta = DEFAULT_T_TICK_MAX,
    fetch_timeout: timedelta = FETCH_TIMEOUT,
    fetch_margin: timedelta = FETCH_MARGIN,
    out: TextIO | None = None,
    before_store_write: Callable[[], None] | None = None,
) -> TickOutcome:
    """One heartbeat. See the module docstring for the order.

    Write order (msg-4699 §2-3): the store is written FIRST, request files are deleted
    AFTER. A crash between the two leaves requests that the next tick reapplies as
    ``clear_request_noop`` -- clearing is idempotent.

    ``before_store_write`` is a test seam: called right before the deadline check that
    guards the store write.

    Body fetches share one :class:`FetchBudget` that ends ``t_tick_max - fetch_margin``
    after the tick started (D-16c A1). Listing time is spent from the same clock, so the
    margin is left for the store write and moving request files aside.
    """
    clock = _Clock(now or (lambda: datetime.now(UTC)), monotonic or time.monotonic)
    stream = out if out is not None else sys.stdout
    tick_now = clock.now()
    started = clock.monotonic()
    outcome = TickOutcome(evaluated=False, wrote_store=False)
    log = _emitter(stream, outcome.lines, tick_now)
    budget = FetchBudget(
        monotonic=clock.monotonic,
        deadline=started + fetch_window(t_tick_max, fetch_margin).total_seconds(),
        fetch_timeout=fetch_timeout,
    )

    lock = LedgerLock(paths.lock)
    if not lock.acquire({"pid": os.getpid(), "started_at": utc_iso(tick_now)}):
        outcome.skipped = "locked"
        log({"kind": "heartbeat", "evaluated": False, "tick_skipped": "locked"})
        return outcome
    try:
        await _locked_tick(
            paths=paths,
            adapters=adapters,
            tick_now=tick_now,
            clock=clock,
            started=started,
            t_tick_max=t_tick_max,
            budget=budget,
            log=log,
            outcome=outcome,
            before_store_write=before_store_write,
        )
    finally:
        lock.release({"released_at": utc_iso(clock.now())})
    return outcome


async def _locked_tick(
    *,
    paths: TickPaths,
    adapters: Sequence[Adapter],
    tick_now: datetime,
    clock: _Clock,
    started: float,
    t_tick_max: timedelta,
    budget: FetchBudget,
    log: LogFn,
    outcome: TickOutcome,
    before_store_write: Callable[[], None] | None,
) -> None:
    store = load_store(paths.store, tick_now)
    if store.status == StoreStatus.ABSENT:
        log({"kind": "store_absent"})
    elif store.status == StoreStatus.CORRUPT:
        log(
            {
                "kind": "store_corrupt",
                "detail": store.detail,
                "moved_to": str(store.corrupt_moved_to) if store.corrupt_moved_to else None,
            }
        )
    elif store.status == StoreStatus.VERSION_AHEAD:
        log({"kind": "store_version_ahead", "detail": store.detail})
    elif store.status == StoreStatus.VERSION_UNRECOGNIZED:
        log({"kind": "store_version_unrecognized", "detail": store.detail})
    # Every line that reports a change to the STORE is held in ``held`` and emitted only
    # after ``save_store`` lands: a tick that aborts on its deadline applied nothing, and
    # must not say it did (PR-gate #361 @ f303068, objection 1). That covers record
    # quarantines, the clear-request outcomes, and everything ``evaluate`` reports
    # (open / close / reclassify / unknown_motion_type). Lines about things that already
    # happened on disk regardless of the write -- ``store_corrupt``'s move-aside, the
    # store status -- go out immediately.
    held: list[dict[str, Any]] = []
    for event in store.quarantine_events:
        if event.first:
            held.append({"kind": "record_quarantined", "key": event.key, "reason": event.reason})

    results = [await adapter.fetch() for adapter in adapters]

    # Clear requests are read after the fetch, immediately before evaluation
    # (msg-4699 §2-2), so a request dropped while this tick was waiting on the network
    # is applied by this tick rather than the next one.
    # Their log lines are held with the rest (see ``held`` above). An INVALID request is
    # moved aside to ``<name>.invalid`` after the store write, so it is reported once and
    # then leaves the ``clear-*.json`` glob instead of being re-read and re-logged on
    # every tick forever (PR-gate #361 @ f303068, objection 2). It is renamed, not
    # deleted: the operator's file stays on disk as evidence of what was wrong with it.
    applied_requests: list[Path] = []
    invalid_requests: list[InvalidRequest] = []
    if store.writable:
        requests, invalid_requests = read_clear_requests(paths.requests)
        for req in requests:
            cleared = store.clear_quarantined(req.unit_key)
            held.append(
                {
                    "kind": "quarantine_cleared" if cleared else "clear_request_noop",
                    "key": req.unit_key,
                    "requested_by": req.requested_by,
                    "requested_at": req.requested_at,
                }
            )
            applied_requests.append(req.path)

    if store.writable:
        await evaluate(
            store=store,
            results=results,
            now=tick_now,
            log=held.append,
            counters=outcome.counters,
            budget=budget,
        )
        if before_store_write is not None:
            before_store_write()
        elapsed = timedelta(seconds=clock.monotonic() - started)
        if elapsed > t_tick_max:
            outcome.aborted = "deadline"
            _heartbeat(log, results, tick_now, store, budget, evaluated=False, aborted="deadline")
            return
        assert store.envelope is not None
        save_store(paths.store, store.envelope)
        outcome.wrote_store = True
        for line in held:
            log(line)
        for path in applied_requests:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        for bad in invalid_requests:
            log(
                {
                    "kind": "clear_request_invalid",
                    "path": str(bad.path),
                    "detail": bad.detail,
                    "moved_to": _move_invalid_request_aside(bad.path),
                }
            )
        outcome.evaluated = True
        _heartbeat(log, results, tick_now, store, budget, evaluated=True, aborted=None)
    else:
        _heartbeat(log, results, tick_now, store, budget, evaluated=False, aborted=None)


def _move_invalid_request_aside(path: Path) -> str | None:
    """Rename an unusable request to ``<name>.invalid``; ``None`` if that failed.

    A failed rename is reported (``moved_to: null``) and the file is retried next tick
    -- loud, never silent, and never a delete.
    """
    target = path.with_name(path.name + INVALID_REQUEST_SUFFIX)
    try:
        os.replace(path, target)
    except OSError:
        return None
    return str(target)


def _heartbeat(
    log: LogFn,
    results: Sequence[SourceResult],
    now: datetime,
    store: LoadedStore,
    budget: FetchBudget,
    *,
    evaluated: bool,
    aborted: str | None,
) -> None:
    """The level line (msg-4685 §6, msg-4689 §2, msg-4695 §3-4, msg-4697 §1).

    ``previous_last_valid_ingest_at`` is not persisted by D-16ab: the heartbeat's
    cross-tick freshness (``is_stale``'s process boundary) is carried to D-16d
    (msg-4685 §9). Each line therefore states this tick's own ingest verdict.
    """
    record = HeartbeatRecord.build(
        evaluated_at=now,
        input_format_version=ADAPTER_FORMAT_VERSION,
        sources=tuple(r.report for r in results),
        previous_last_valid_ingest_at=None,
        # Only a tick whose store write landed reports its stalls: an aborted tick's
        # in-memory records were never persisted (same invariant as ``held``).
        stalls=tuple(sorted(store.records)) if store.writable and evaluated else (),
    )
    line: dict[str, Any] = {
        "kind": "heartbeat",
        "state": derive_state(record).value,
        "evaluated_at": utc_iso(record.evaluated_at),
        "input_format_version": record.input_format_version,
        "last_valid_ingest_at": (
            utc_iso(record.last_valid_ingest_at) if record.last_valid_ingest_at else None
        ),
        "expires_at": utc_iso(record.expires_at) if record.expires_at else None,
        "sources": [
            {
                "name": s.name,
                "fetch_outcome": s.fetch_outcome.value,
                "examined": s.examined,
                "recognized": s.recognized,
                "unrecognized": s.unrecognized,
                "observed_format_version": s.observed_format_version,
            }
            for s in record.sources
        ],
        "stalls": list(record.stalls),
        "store": store.status.value,
        "evaluated": evaluated,
        "quarantined_records": len(store.quarantined) if store.writable else None,
        "quarantine_repeats": store.quarantine_repeats,
        # D-16c A3 (msg-5744 §1, msg-5746 §2): a fetch backlog that is not shrinking
        # shows up here. ``pending_markers`` counts saved records only, like ``stalls``.
        "budget_exhausted": budget.exhausted,
        "deferred_fetches": budget.deferred,
        "pending_markers": (
            sum(1 for r in store.records.values() if r.evidence.get("marker_pending") is True)
            if store.writable and evaluated
            else None
        ),
    }
    if aborted is not None:
        line["tick_aborted"] = aborted
    log(line)


def run_tick_sync(**kwargs: Any) -> TickOutcome:
    return asyncio.run(run_tick(**kwargs))
