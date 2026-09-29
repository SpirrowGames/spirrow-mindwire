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
    LoadedStore,
    StoreStatus,
    load_store,
    new_record_json,
    read_clear_requests,
    save_store,
    utc_iso,
)

#: Whole-tick deadline (msg-4701 §3). Provisional; D-16c fixes it with the heartbeat
#: interval under the constraint ``T_TICK_MAX < T_LOCK_STALE``.
DEFAULT_T_TICK_MAX = timedelta(minutes=10)

FLAG_EPOCH_UNKNOWN = "epoch_unknown"
FLAG_ORIGIN_UNKNOWN = "origin_unknown"

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


async def evaluate(
    *,
    store: LoadedStore,
    results: Sequence[SourceResult],
    now: datetime,
    log: LogFn,
    counters: TickCounters,
) -> None:
    """Open / reclassify / close records for every observed unit (msg-4685 §4).

    ``store`` must be writable. Mutates it in place.
    """
    untrusted_all = store.status in (StoreStatus.ABSENT, StoreStatus.CORRUPT)
    for result in results:
        observed: set[str] = set()
        for obs in result.observations:
            key = obs.unit.key
            observed.add(key)
            record = store.records.get(key)
            attempts = record.remedy_attempts if record is not None else []
            lpm = last_participant_motion_at(obs, attempts)
            if record is None:
                if not stalled(obs.unit.kind, lpm, now, obs.needs_actor, obs.n):
                    continue
                untrusted = untrusted_all or key in store.quarantined
                await _open(store, result, obs, lpm, now, untrusted, log, counters)
                continue
            # Step 3: open record -- close on INV-2, else reclassify.
            if not obs.needs_actor:
                _close(store, record, "a", now, now, "now", log, counters)
            elif lpm > record.stall_epoch_start:
                _close(store, record, "b", now, lpm, "motion", log, counters)
            else:
                store.reclassify(key, classify(obs.classifier_input).value, now)
        if not result.complete:
            continue
        # A complete listing that no longer shows a unit in its scope: needs_actor is
        # false (e.g. the PR closed, the quarantine entry was cleared) -> rule (a).
        for key, record in list(store.records.items()):
            if key in observed or key in result.unobservable:
                continue
            if result.scope(record.unit):
                _close(store, record, "a", now, now, "now", log, counters)


async def _open(
    store: LoadedStore,
    result: SourceResult,
    obs: UnitObservation,
    lpm: datetime,
    now: datetime,
    untrusted: bool,
    log: LogFn,
    counters: TickCounters,
) -> None:
    flags: list[str] = []
    marker_reason: str | None = None
    if untrusted:
        flags.append(FLAG_EPOCH_UNKNOWN)
        e = select_e(obs, [])
        if e is None or e.type == MotionType.HEAD_PUSH:
            pass
        elif e.type in (MotionType.REVIEW, MotionType.CHATROOM_MSG):
            counters.body_fetches += 1
            extra, marker_reason = marker_flags(await result.checker.check_marker(obs.unit, e))
            flags.extend(extra)
        else:
            flags.append(FLAG_ORIGIN_UNKNOWN)
            log({"kind": "unknown_motion_type", "unit": obs.unit.key, "type": e.type})
    klass = classify(obs.classifier_input).value
    raw = new_record_json(unit=obs.unit, now=now, klass=klass, flags=flags, motion_at_open=lpm)
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
    if marker_reason is not None:
        line["marker_reason"] = marker_reason
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
    log(
        {
            "kind": "close",
            "record_id": record.record_id,
            "rule": rule,
            "age": _hours(record.age(now)),
            "flags": list(record.flags),
            "silence_hours": _hours(endpoint - motion_at_open),
            "silence_endpoint": endpoint_kind,
        }
    )


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
    out: TextIO | None = None,
    before_store_write: Callable[[], None] | None = None,
) -> TickOutcome:
    """One heartbeat. See the module docstring for the order.

    Write order (msg-4699 §2-3): the store is written FIRST, request files are deleted
    AFTER. A crash between the two leaves requests that the next tick reapplies as
    ``clear_request_noop`` -- clearing is idempotent.

    ``before_store_write`` is a test seam: called right before the deadline check that
    guards the store write.
    """
    clock = _Clock(now or (lambda: datetime.now(UTC)), monotonic or time.monotonic)
    stream = out if out is not None else sys.stdout
    tick_now = clock.now()
    started = clock.monotonic()
    outcome = TickOutcome(evaluated=False, wrote_store=False)
    log = _emitter(stream, outcome.lines, tick_now)

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
    for event in store.quarantine_events:
        if event.first:
            log({"kind": "record_quarantined", "key": event.key, "reason": event.reason})

    results = [await adapter.fetch() for adapter in adapters]

    # Clear requests are read after the fetch, immediately before evaluation
    # (msg-4699 §2-2), so a request dropped while this tick was waiting on the network
    # is applied by this tick rather than the next one.
    # Their log lines are held until the store write lands: a tick that aborts on its
    # deadline applied nothing, and must not say it did.
    applied_requests: list[Path] = []
    clear_lines: list[dict[str, Any]] = []
    if store.writable:
        requests, invalid = read_clear_requests(paths.requests)
        for bad in invalid:
            log({"kind": "clear_request_invalid", "path": str(bad.path), "detail": bad.detail})
        for req in requests:
            cleared = store.clear_quarantined(req.unit_key)
            clear_lines.append(
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
            store=store, results=results, now=tick_now, log=log, counters=outcome.counters
        )
        if before_store_write is not None:
            before_store_write()
        elapsed = timedelta(seconds=clock.monotonic() - started)
        if elapsed > t_tick_max:
            outcome.aborted = "deadline"
            _heartbeat(log, results, tick_now, store, evaluated=False, aborted="deadline")
            return
        assert store.envelope is not None
        save_store(paths.store, store.envelope)
        outcome.wrote_store = True
        for line in clear_lines:
            log(line)
        for path in applied_requests:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        outcome.evaluated = True
        _heartbeat(log, results, tick_now, store, evaluated=True, aborted=None)
    else:
        _heartbeat(log, results, tick_now, store, evaluated=False, aborted=None)


def _heartbeat(
    log: LogFn,
    results: Sequence[SourceResult],
    now: datetime,
    store: LoadedStore,
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
        stalls=tuple(sorted(store.records)) if store.writable else (),
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
    }
    if aborted is not None:
        line["tick_aborted"] = aborted
    log(line)


def run_tick_sync(**kwargs: Any) -> TickOutcome:
    return asyncio.run(run_tick(**kwargs))
