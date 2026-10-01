"""D-16ab driver, codec and tick flow (T-stalled-pr-has-no-detector msg-4685 §4-§7 + amendments)."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.stall_ledger.adapters import (
    ADAPTER_FORMAT_VERSION,
    MARKER_CHECK_VARIANTS,
    UNVERIFIABLE_REASONS,
    Marked,
    MarkerCheck,
    MotionEvent,
    RemedyMarkerCodec,
    SourceResult,
    UnitObservation,
    Unmarked,
    Unverifiable,
)
from spirrow_mindwire.stall_ledger.classifier import ClassifierInput
from spirrow_mindwire.stall_ledger.driver import (
    TickOutcome,
    TickPaths,
    marker_flags,
    run_tick,
)
from spirrow_mindwire.stall_ledger.heartbeat import FetchOutcome, SourceReport
from spirrow_mindwire.stall_ledger.model import Unit, UnitKind
from spirrow_mindwire.stall_ledger.store import new_record_json, write_clear_request

DAY1 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
N = timedelta(hours=12)


# ── codec (msg-4685 §7-5) ─────────────────────────────────────────────────────────────


def test_codec_round_trip() -> None:
    for attempt_id in ("a1", "01J9Z-X.y:z_"):
        body = RemedyMarkerCodec.encode(attempt_id) + "\nrest of the body"
        assert RemedyMarkerCodec.decode(body) == Marked(attempt_id)


@pytest.mark.parametrize(
    "body",
    [
        "> " + RemedyMarkerCodec.encode("a1"),
        " " + RemedyMarkerCodec.encode("a1"),
        "```\n" + RemedyMarkerCodec.encode("a1") + "\n```",
        "first line\n" + RemedyMarkerCodec.encode("a1"),
        "",
        "plain body",
    ],
    ids=["quote", "leading-space", "code-fence", "line-2", "empty", "plain"],
)
def test_codec_does_not_match_outside_line_one_column_zero(body: str) -> None:
    assert RemedyMarkerCodec.decode(body) == Unmarked()


@pytest.mark.parametrize(
    "first_line",
    [
        "<!-- stall-ledger-remedy:v1 attempt= -->",
        "<!-- stall-ledger-remedy:v1 attempt=a b -->",
        "<!-- stall-ledger-remedy:v2 attempt=a1 -->",
        "<!-- stall-ledger-remedy:v1 attempt=a1 --> trailing",
        "<!-- stall-ledger-remedy",
    ],
)
def test_codec_malformed_marker_is_unverifiable(first_line: str) -> None:
    assert RemedyMarkerCodec.decode(first_line + "\nx") == Unverifiable("malformed_marker")


def test_codec_crlf_first_line() -> None:
    assert RemedyMarkerCodec.decode(RemedyMarkerCodec.encode("a1") + "\r\nx") == Marked("a1")


# ── E table exhaustiveness (msg-4685 §7-3, msg-4683 §2) ───────────────────────────────


def _instances() -> list[MarkerCheck]:
    return [Marked("a"), Unmarked(), *(Unverifiable(r) for r in UNVERIFIABLE_REASONS)]


def test_every_marker_check_variant_is_handled() -> None:
    seen = {type(x) for x in _instances()}
    assert seen == set(MARKER_CHECK_VARIANTS)
    for result in _instances():
        flags, reason = marker_flags(result)
        if isinstance(result, Unmarked):
            assert flags == [] and reason is None
        else:
            assert flags == ["origin_unknown"]


def test_an_unknown_variant_is_not_trusted() -> None:
    @dataclass(frozen=True)
    class Future:
        pass

    with pytest.raises(AssertionError):
        marker_flags(Future())  # type: ignore[arg-type]


# ── fakes ──────────────────────────────────────────────────────────────────────────────


@dataclass
class FakeAdapter:
    """One source. ``units`` maps identifier -> (needs_actor, motion, baseline)."""

    name: str = "fake"
    kind: UnitKind = UnitKind.PR
    units: dict[str, tuple[bool, tuple[MotionEvent, ...], datetime]] = field(default_factory=dict)
    marker: MarkerCheck = field(default_factory=Unmarked)
    outcome: FetchOutcome = FetchOutcome.OK
    unobservable: frozenset[str] = frozenset()
    checks: list[tuple[str, str]] = field(default_factory=list)
    on_fetch: Callable[[], object] | None = None
    klass_input: ClassifierInput = field(default_factory=ClassifierInput)

    def scope(self, unit: Unit) -> bool:
        return unit.kind == self.kind

    async def fetch(self) -> SourceResult:
        if self.on_fetch is not None:
            self.on_fetch()
        ok = self.outcome == FetchOutcome.OK
        obs = (
            tuple(
                UnitObservation(
                    unit=Unit(self.kind, ident),
                    needs_actor=needs,
                    n=N,
                    classifier_input=self.klass_input,
                    motion=motion,
                    baseline_at=baseline,
                )
                for ident, (needs, motion, baseline) in self.units.items()
            )
            if ok
            else ()
        )
        n_obs = len(obs)
        return SourceResult(
            report=SourceReport(
                name=self.name,
                fetch_outcome=self.outcome,
                examined=n_obs + len(self.unobservable),
                recognized=n_obs,
                unrecognized=len(self.unobservable),
                observed_format_version=ADAPTER_FORMAT_VERSION,
            ),
            observations=obs,
            unobservable=self.unobservable,
            complete=ok,
            scope=self.scope,
            checker=self,
        )

    async def check_marker(self, unit: Unit, event: MotionEvent) -> MarkerCheck:
        self.checks.append((unit.key, event.id))
        return self.marker


def _tick(
    tmp_path: Path,
    adapters: list[Any],
    now: datetime,
    **kw: Any,
) -> TickOutcome:
    paths = TickPaths(state_dir=tmp_path / "state")
    return asyncio.run(
        run_tick(paths=paths, adapters=adapters, now=lambda: now, out=io.StringIO(), **kw)
    )


def _lines(outcome: TickOutcome, kind: str) -> list[dict[str, Any]]:
    return [line for line in outcome.lines if line["kind"] == kind]


def _store(tmp_path: Path) -> dict[str, Any]:
    data = json.loads((tmp_path / "state" / "stall-ledger.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _review(rid: str, at: datetime) -> MotionEvent:
    return MotionEvent("review", rid, at)


# ── msg-4685 §7-2: motion_at_open / silence_hours (msg-4669 §1) ───────────────────────


def test_day1_day15_rule_b_silence_is_true_gap(tmp_path: Path) -> None:
    day15 = DAY1 + timedelta(days=14)
    a = FakeAdapter(units={"o/r#1": (True, (_review("r1", DAY1),), DAY1 - timedelta(days=1))})
    opened = _tick(tmp_path, [a], day15)
    [line] = _lines(opened, "open")
    assert line["motion_at_open"] == DAY1.isoformat()
    assert line["flags"] == ["epoch_unknown"]  # store was absent (first deploy)
    new_motion = day15 + timedelta(hours=2)
    a.units["o/r#1"] = (True, (_review("r1", DAY1), _review("r2", new_motion)), DAY1)
    closed = _tick(tmp_path, [a], day15 + timedelta(hours=3))
    [close] = _lines(closed, "close")
    assert close["rule"] == "b"
    assert close["silence_endpoint"] == "motion"
    assert close["silence_hours"] == pytest.approx(14 * 24 + 2)
    assert close["flags"] == ["epoch_unknown"]
    assert _store(tmp_path)["records"] == {}


def test_day1_day15_rule_a_silence_ends_now(tmp_path: Path) -> None:
    day15 = DAY1 + timedelta(days=14)
    a = FakeAdapter(units={"o/r#1": (True, (_review("r1", DAY1),), DAY1)})
    _tick(tmp_path, [a], day15)
    a.units["o/r#1"] = (False, (_review("r1", DAY1),), DAY1)
    closed = _tick(tmp_path, [a], day15 + timedelta(hours=2))
    [close] = _lines(closed, "close")
    assert close["rule"] == "a"
    assert close["silence_endpoint"] == "now"
    assert close["silence_hours"] == pytest.approx(14 * 24 + 2)
    assert close["age"] == pytest.approx(2.0)


def test_unit_missing_from_complete_listing_closes_rule_a(tmp_path: Path) -> None:
    a = FakeAdapter(units={"o/r#1": (True, (), DAY1)})
    _tick(tmp_path, [a], DAY1 + timedelta(days=2))
    a.units = {}
    closed = _tick(tmp_path, [a], DAY1 + timedelta(days=3))
    assert [c["rule"] for c in _lines(closed, "close")] == ["a"]


def test_failed_or_unobservable_source_closes_nothing(tmp_path: Path) -> None:
    a = FakeAdapter(units={"o/r#1": (True, (), DAY1), "o/r#2": (True, (), DAY1)})
    _tick(tmp_path, [a], DAY1 + timedelta(days=2))
    a.outcome = FetchOutcome.HTTP_ERROR
    out = _tick(tmp_path, [a], DAY1 + timedelta(days=3))
    assert _lines(out, "close") == []
    [hb] = _lines(out, "heartbeat")
    assert hb["state"] == "ingest_failure"
    a.outcome = FetchOutcome.OK
    a.units = {"o/r#1": (True, (), DAY1)}
    a.unobservable = frozenset({"pr:o/r#2"})
    out = _tick(tmp_path, [a], DAY1 + timedelta(days=4))
    assert _lines(out, "close") == []
    assert sorted(_store(tmp_path)["records"]) == ["pr:o/r#1", "pr:o/r#2"]


# ── msg-4685 §7-3: E table ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("marker", "flags", "reason"),
    [
        (Marked("x"), ["epoch_unknown", "origin_unknown"], None),
        (Unmarked(), ["epoch_unknown"], None),
        (Unverifiable("fetch_failed"), ["epoch_unknown", "origin_unknown"], "fetch_failed"),
        (Unverifiable("deleted"), ["epoch_unknown", "origin_unknown"], "deleted"),
        (Unverifiable("malformed_marker"), ["epoch_unknown", "origin_unknown"], "malformed_marker"),
    ],
)
def test_e_table_review(
    tmp_path: Path, marker: MarkerCheck, flags: list[str], reason: str | None
) -> None:
    a = FakeAdapter(units={"o/r#1": (True, (_review("r1", DAY1),), DAY1)}, marker=marker)
    out = _tick(tmp_path, [a], DAY1 + timedelta(days=1))
    [line] = _lines(out, "open")
    assert line["flags"] == flags
    assert line.get("marker_reason") == reason
    assert a.checks == [("pr:o/r#1", "r1")]


def test_e_table_chatroom_msg_is_checked(tmp_path: Path) -> None:
    a = FakeAdapter(
        kind=UnitKind.THREAD,
        units={"p/T-x": (True, (MotionEvent("chatroom_msg", "msg-9", DAY1),), DAY1)},
        marker=Marked("a"),
    )
    out = _tick(tmp_path, [a], DAY1 + timedelta(days=1))
    assert _lines(out, "open")[0]["flags"] == ["epoch_unknown", "origin_unknown"]


def test_e_table_head_push_is_not_checked(tmp_path: Path) -> None:
    motion = (_review("r1", DAY1), MotionEvent("head_push", "sha", DAY1 + timedelta(hours=1)))
    a = FakeAdapter(units={"o/r#1": (True, motion, DAY1)}, marker=Marked("x"))
    out = _tick(tmp_path, [a], DAY1 + timedelta(days=1))
    assert _lines(out, "open")[0]["flags"] == ["epoch_unknown"]
    assert a.checks == []


def test_e_table_unknown_type(tmp_path: Path) -> None:
    a = FakeAdapter(units={"o/r#1": (True, (MotionEvent("label", "l1", DAY1),), DAY1)})
    out = _tick(tmp_path, [a], DAY1 + timedelta(days=1))
    assert _lines(out, "open")[0]["flags"] == ["epoch_unknown", "origin_unknown"]
    assert _lines(out, "unknown_motion_type") == [
        {
            "at": _lines(out, "open")[0]["at"],
            "kind": "unknown_motion_type",
            "unit": "pr:o/r#1",
            "type": "label",
        }
    ]
    assert a.checks == []


# ── msg-4685 §7-4 / msg-4695 §4: fetch count ──────────────────────────────────────────


def test_fetch_count_absent_at_most_n_present_zero(tmp_path: Path) -> None:
    units: dict[str, tuple[bool, tuple[MotionEvent, ...], datetime]] = {
        f"o/r#{i}": (True, (_review(f"r{i}", DAY1),), DAY1) for i in range(5)
    }
    a = FakeAdapter(units=units)
    out = _tick(tmp_path, [a], DAY1 + timedelta(days=1))
    assert len(_lines(out, "open")) == 5
    assert out.counters.body_fetches == len(a.checks) <= 5
    a.checks.clear()
    units.update({f"o/r#{i}": (True, (_review(f"r{i}", DAY1),), DAY1) for i in range(5, 8)})
    out = _tick(tmp_path, [a], DAY1 + timedelta(days=2))
    assert len(_lines(out, "open")) == 3
    assert a.checks == []  # store present, no quarantine -> 0 fetches
    assert all(line["flags"] == [] for line in _lines(out, "open"))


def test_quarantined_unit_reopens_epoch_unknown_with_one_fetch(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    bad = new_record_json(
        unit=Unit(UnitKind.PR, "o/r#9"),
        now=DAY1,
        klass="unclassified",
        flags=[],
        motion_at_open=DAY1,
    )
    (state / "stall-ledger.json").write_text(
        json.dumps({"schema_version": "1.0", "records": {"pr:o/r#1": bad}}), encoding="utf-8"
    )
    a = FakeAdapter(units={"o/r#1": (True, (_review("r1", DAY1),), DAY1)})
    out = _tick(tmp_path, [a], DAY1 + timedelta(days=2))
    assert [q["reason"] for q in _lines(out, "record_quarantined")] == ["key_mismatch"]
    [line] = _lines(out, "open")
    assert "epoch_unknown" in line["flags"]
    assert a.checks == [("pr:o/r#1", "r1")]
    [hb] = _lines(out, "heartbeat")
    assert hb["quarantined_records"] == 1
    assert _store(tmp_path)["quarantined_records"]["pr:o/r#1"]["count"] == 1


# ── msg-4687 §5: rollback case ────────────────────────────────────────────────────────


def test_rollback_store_with_remedy_attempts_does_not_false_close(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    epoch = DAY1
    review_x = epoch + timedelta(days=14)
    rec = new_record_json(
        unit=Unit(UnitKind.PR, "o/r#1"),
        now=epoch,
        klass="unclassified",
        flags=[],
        motion_at_open=epoch - N * 2,
    )
    rec["remedy_attempts"] = [
        {
            "at": (review_x - timedelta(minutes=1)).isoformat(),
            "kind": "gate-refire",
            "head_sha": "abc",
            "state": "completed",
            "emitted_event_ids": {"github_review_ids": ["review-X"], "chatroom_msg_ids": []},
            "outcome": "no-state-change",
            "flags": [],
        }
    ]
    rec["ladder_stage"] = "second"
    (state / "stall-ledger.json").write_text(
        json.dumps({"schema_version": "1.0", "records": {"pr:o/r#1": rec}}), encoding="utf-8"
    )
    a = FakeAdapter(units={"o/r#1": (True, (_review("review-X", review_x),), epoch - N * 3)})
    out = _tick(tmp_path, [a], review_x + timedelta(hours=1))
    assert _lines(out, "close") == []
    written = _store(tmp_path)["records"]["pr:o/r#1"]
    assert written["remedy_attempts"] == rec["remedy_attempts"]
    assert written["ladder_stage"] == "second"


# ── msg-4689 §2-4 / msg-4691: halted evaluation ───────────────────────────────────────


@pytest.mark.parametrize(
    ("version", "store_value"),
    [("2.0", "version_ahead"), ("bogus", "version_unrecognized")],
)
def test_halted_heartbeat_says_so_and_writes_nothing(
    tmp_path: Path, version: str, store_value: str
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    path = state / "stall-ledger.json"
    blob = json.dumps({"schema_version": version, "records": {}}).encode()
    path.write_bytes(blob)
    write_clear_request(state / "stall-ledger.requests", "k", "op", DAY1)
    a = FakeAdapter(units={"o/r#1": (True, (), DAY1)})
    out = _tick(tmp_path, [a], DAY1 + timedelta(days=3))
    [hb] = _lines(out, "heartbeat")
    assert hb["store"] == store_value
    assert hb["evaluated"] is False
    assert _lines(out, "open") == [] and _lines(out, "close") == []
    assert path.read_bytes() == blob
    assert len(list((state / "stall-ledger.requests").glob("clear-*.json"))) == 1


# ── heartbeat line ────────────────────────────────────────────────────────────────────


def test_heartbeat_line_fields(tmp_path: Path) -> None:
    a = FakeAdapter(units={"o/r#1": (True, (), DAY1)})
    out = _tick(tmp_path, [a], DAY1 + timedelta(days=1))
    [hb] = _lines(out, "heartbeat")
    assert hb["store"] == "absent"
    assert hb["evaluated"] is True
    assert hb["state"] == "healthy"
    assert hb["stalls"] == ["pr:o/r#1"]
    assert hb["quarantined_records"] == 0
    assert hb["quarantine_repeats"] == 0
    assert hb["sources"][0]["name"] == "fake"
    assert _lines(out, "store_absent")
    # second tick: store valid, no store_absent line
    out = _tick(tmp_path, [a], DAY1 + timedelta(days=1, hours=1))
    assert _lines(out, "heartbeat")[0]["store"] == "valid"
    assert not _lines(out, "store_absent")


def test_reclassify_appends_class_history(tmp_path: Path) -> None:
    a = FakeAdapter(units={"o/r#1": (True, (), DAY1)})
    _tick(tmp_path, [a], DAY1 + timedelta(days=1))
    a.klass_input = ClassifierInput(is_externally_blocked=True)
    _tick(tmp_path, [a], DAY1 + timedelta(days=1, hours=1))
    rec = _store(tmp_path)["records"]["pr:o/r#1"]
    assert rec["klass"] == "externally-blocked"
    assert [h["klass"] for h in rec["class_history"]] == ["unclassified", "externally-blocked"]


# ── msg-4697 §3: record_quarantined only once, repeats on heartbeat ───────────────────


def test_record_quarantined_logged_once_repeats_counted(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    path = state / "stall-ledger.json"
    bad = {"unit": {"kind": "pr", "identifier": "o/r#1"}}
    a = FakeAdapter()
    path.write_text(json.dumps({"schema_version": "1.0", "records": {"pr:o/r#1": bad}}))
    first = _tick(tmp_path, [a], DAY1)
    assert len(_lines(first, "record_quarantined")) == 1
    assert _lines(first, "heartbeat")[0]["quarantine_repeats"] == 0
    data = _store(tmp_path)
    data["records"]["pr:o/r#1"] = bad  # another writer re-corrupts it
    path.write_text(json.dumps(data))
    second = _tick(tmp_path, [a], DAY1 + timedelta(hours=1))
    assert _lines(second, "record_quarantined") == []
    assert _lines(second, "heartbeat")[0]["quarantine_repeats"] == 1
    assert _store(tmp_path)["quarantined_records"]["pr:o/r#1"]["count"] == 2


# ── msg-4699 §5: clear requests through the tick ──────────────────────────────────────


def _seed_quarantine(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    q = {"pr:o/r#1": {"reason": "key_mismatch", "count": 1, "first_raw": {}, "last_raw": {}}}
    (state / "stall-ledger.json").write_text(
        json.dumps({"schema_version": "1.0", "records": {}, "quarantined_records": q})
    )
    return state


def test_clear_during_tick_fetch_is_applied_and_tick_updates_kept(tmp_path: Path) -> None:
    state = _seed_quarantine(tmp_path)
    a = FakeAdapter(
        units={"o/r#2": (True, (), DAY1)},
        on_fetch=lambda: write_clear_request(
            state / "stall-ledger.requests", "pr:o/r#1", "op", DAY1
        ),
    )
    out = _tick(tmp_path, [a], DAY1 + timedelta(days=1))
    data = _store(tmp_path)
    assert data["quarantined_records"] == {}
    assert list(data["records"]) == ["pr:o/r#2"]
    assert [c["kind"] for c in out.lines if "clear" in c["kind"]] == ["quarantine_cleared"]
    assert not list((state / "stall-ledger.requests").glob("clear-*.json"))


def test_crash_between_store_write_and_request_delete_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _seed_quarantine(tmp_path)
    write_clear_request(state / "stall-ledger.requests", "pr:o/r#1", "op", DAY1)
    a = FakeAdapter()

    def crash(self: Path, missing_ok: bool = False) -> None:
        raise KeyboardInterrupt("killed after the store write")

    monkeypatch.setattr(Path, "unlink", crash)
    with pytest.raises(KeyboardInterrupt):
        _tick(tmp_path, [a], DAY1)
    monkeypatch.undo()
    after_crash = _store(tmp_path)
    assert after_crash["quarantined_records"] == {}
    out = _tick(tmp_path, [a], DAY1 + timedelta(minutes=5))
    assert [c["kind"] for c in out.lines if "clear" in c["kind"]] == ["clear_request_noop"]
    assert _store(tmp_path) == after_crash


# ── msg-4701 §5 (kept by msg-4703): deadline ──────────────────────────────────────────


def test_deadline_abort_writes_nothing_and_keeps_requests(tmp_path: Path) -> None:
    state = _seed_quarantine(tmp_path)
    before = (state / "stall-ledger.json").read_bytes()
    write_clear_request(state / "stall-ledger.requests", "pr:o/r#1", "op", DAY1)
    ticks = iter([0.0, 10_000.0])
    a = FakeAdapter(units={"o/r#2": (True, (), DAY1)})
    paths = TickPaths(state_dir=state)
    out = asyncio.run(
        run_tick(
            paths=paths,
            adapters=[a],
            now=lambda: DAY1 + timedelta(days=1),
            monotonic=lambda: next(ticks),
            t_tick_max=timedelta(minutes=10),
            out=io.StringIO(),
        )
    )
    assert out.aborted == "deadline"
    [hb] = _lines(out, "heartbeat")
    assert hb["tick_aborted"] == "deadline" and hb["evaluated"] is False
    assert (state / "stall-ledger.json").read_bytes() == before
    assert len(list((state / "stall-ledger.requests").glob("clear-*.json"))) == 1
    assert not [c for c in out.lines if "clear" in c["kind"]]
    # PR-gate #361 @ f303068 objection 1: nothing that reports a store change may be
    # logged by a tick whose store write never happened.
    assert [line["kind"] for line in out.lines] == ["heartbeat"]
    assert hb["stalls"] == []


def test_deadline_abort_says_nothing_then_the_next_tick_says_it_once(tmp_path: Path) -> None:
    """Objection 1, positive side: the open the aborted tick computed is reported by
    the tick that actually persists it -- exactly once, not twice."""
    a = FakeAdapter(units={"o/r#1": (True, (), DAY1)})
    ticks = iter([0.0, 10_000.0])
    paths = TickPaths(state_dir=tmp_path / "state")
    aborted = asyncio.run(
        run_tick(
            paths=paths,
            adapters=[a],
            now=lambda: DAY1 + timedelta(days=2),
            monotonic=lambda: next(ticks),
            t_tick_max=timedelta(minutes=10),
            out=io.StringIO(),
        )
    )
    assert aborted.aborted == "deadline"
    assert _lines(aborted, "open") == []
    landed = _tick(tmp_path, [a], DAY1 + timedelta(days=2, minutes=5))
    assert [o["unit"] for o in _lines(landed, "open")] == ["pr:o/r#1"]


def test_deadline_abort_does_not_report_a_record_quarantine(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "stall-ledger.json").write_text(
        json.dumps({"schema_version": "1.0", "records": {"pr:o/r#1": {"unit": "bogus"}}}),
        encoding="utf-8",
    )
    ticks = iter([0.0, 10_000.0])
    out = asyncio.run(
        run_tick(
            paths=TickPaths(state_dir=state),
            adapters=[FakeAdapter()],
            now=lambda: DAY1,
            monotonic=lambda: next(ticks),
            t_tick_max=timedelta(minutes=10),
            out=io.StringIO(),
        )
    )
    assert out.aborted == "deadline"
    assert _lines(out, "record_quarantined") == []
    landed = _tick(tmp_path, [FakeAdapter()], DAY1)
    assert len(_lines(landed, "record_quarantined")) == 1


def test_invalid_clear_request_is_reported_once_and_moved_aside(tmp_path: Path) -> None:
    """PR-gate #361 @ f303068 objection 2: an unparseable request must not be re-read
    and re-logged on every tick forever. It is renamed (never deleted), once."""
    req_dir = tmp_path / "state" / "stall-ledger.requests"
    req_dir.mkdir(parents=True)
    bad = req_dir / "clear-20260901T000000Z-bad.json"
    bad.write_text("{not json", encoding="utf-8")
    first = _tick(tmp_path, [FakeAdapter()], DAY1)
    [line] = _lines(first, "clear_request_invalid")
    assert line["path"] == str(bad)
    assert line["moved_to"] == str(bad) + ".invalid"
    assert not bad.exists()
    assert (req_dir / (bad.name + ".invalid")).read_text(encoding="utf-8") == "{not json"
    second = _tick(tmp_path, [FakeAdapter()], DAY1 + timedelta(minutes=5))
    assert _lines(second, "clear_request_invalid") == []


def test_invalid_clear_request_is_untouched_by_an_aborted_tick(tmp_path: Path) -> None:
    req_dir = tmp_path / "state" / "stall-ledger.requests"
    req_dir.mkdir(parents=True)
    bad = req_dir / "clear-20260901T000000Z-bad.json"
    bad.write_text("[]", encoding="utf-8")
    ticks = iter([0.0, 10_000.0])
    out = asyncio.run(
        run_tick(
            paths=TickPaths(state_dir=tmp_path / "state"),
            adapters=[FakeAdapter()],
            now=lambda: DAY1,
            monotonic=lambda: next(ticks),
            t_tick_max=timedelta(minutes=10),
            out=io.StringIO(),
        )
    )
    assert out.aborted == "deadline"
    assert bad.exists()
    assert _lines(out, "clear_request_invalid") == []
