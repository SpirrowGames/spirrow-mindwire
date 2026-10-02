"""D-16c: the fetch budget, owed marker fetches, and the timing values.

Spec: T-stalled-pr-has-no-detector msg-5744 §1/§3 (A1, A3, C1, C2, C4), msg-5746 §2 (A2',
A3 extended, A4', A5) and the msg-5747 advisory (the fetch window must fit one fetch).
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.stall_ledger import timing
from spirrow_mindwire.stall_ledger.adapters import (
    ADAPTER_FORMAT_VERSION,
    Marked,
    MarkerCheck,
    MotionEvent,
    QuarantineFileAdapter,
    SourceResult,
    UnitObservation,
    Unmarked,
    Unverifiable,
)
from spirrow_mindwire.stall_ledger.classifier import ClassifierInput
from spirrow_mindwire.stall_ledger.driver import TickOutcome, TickPaths, run_tick
from spirrow_mindwire.stall_ledger.heartbeat import FetchOutcome, SourceReport
from spirrow_mindwire.stall_ledger.model import Unit, UnitKind
from spirrow_mindwire.stall_ledger.store import new_record_json

REPO = Path(__file__).resolve().parents[1]
DAY1 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
N = timedelta(hours=12)
REVIEW_AT = DAY1 + timedelta(hours=1)
NOW = DAY1 + timedelta(days=2)
FETCH_SECONDS = 30.0  # every fake body fetch takes the whole default FETCH_TIMEOUT


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Source:
    """One PR source. ``units``: identifier -> (motion, needs_actor)."""

    clock: FakeClock
    name: str = "fake"
    units: dict[str, tuple[tuple[MotionEvent, ...], bool]] = field(default_factory=dict)
    markers: dict[str, MarkerCheck] = field(default_factory=dict)
    complete: bool = True
    listing_seconds: float = 0.0
    fetched: list[str] = field(default_factory=list)

    def scope(self, unit: Unit) -> bool:
        return unit.kind == UnitKind.PR

    async def fetch(self) -> SourceResult:
        self.clock.t += self.listing_seconds
        obs = tuple(
            UnitObservation(
                unit=Unit(UnitKind.PR, ident),
                needs_actor=needs,
                n=N,
                classifier_input=ClassifierInput(),
                motion=motion,
                baseline_at=DAY1,
            )
            for ident, (motion, needs) in self.units.items()
        )
        return SourceResult(
            report=SourceReport(
                name="fake",
                fetch_outcome=FetchOutcome.OK,
                examined=len(obs),
                recognized=len(obs),
                unrecognized=0,
                observed_format_version=ADAPTER_FORMAT_VERSION,
            ),
            observations=obs,
            unobservable=frozenset(),
            complete=self.complete,
            scope=self.scope,
            checker=self,
        )

    async def check_marker(self, unit: Unit, event: MotionEvent) -> MarkerCheck:
        self.clock.t += FETCH_SECONDS
        self.fetched.append(unit.identifier)
        return self.markers.get(unit.identifier, Unmarked())


def _review(ident: str, at: datetime = REVIEW_AT) -> tuple[MotionEvent, ...]:
    return (MotionEvent("review", f"r-{ident}", at),)


def _source(clock: FakeClock, n: int) -> Source:
    variants: list[MarkerCheck] = [Unmarked(), Marked("a1"), Unverifiable("deleted")]
    src = Source(clock=clock)
    for i in range(n):
        ident = f"o/r#{i:03d}"
        src.units[ident] = (_review(ident), True)
        src.markers[ident] = variants[i % len(variants)]
    return src


def _tick(state: Path, src: Source, clock: FakeClock, now: datetime, **kw: Any) -> TickOutcome:
    return asyncio.run(
        run_tick(
            paths=TickPaths(state_dir=state),
            adapters=[src],
            now=lambda: now,
            monotonic=clock,
            out=io.StringIO(),
            **kw,
        )
    )


def _lines(outcome: TickOutcome, kind: str) -> list[dict[str, Any]]:
    return [line for line in outcome.lines if line["kind"] == kind]


def _records(state: Path) -> dict[str, Any]:
    data = json.loads((state / "stall-ledger.json").read_text(encoding="utf-8"))
    records = data["records"]
    assert isinstance(records, dict)
    return records


def _final(records: dict[str, Any]) -> dict[str, tuple[list[str], str | None]]:
    return {k: (r["flags"], r["evidence"].get("marker_reason")) for k, r in records.items()}


# Default budget: window = 10 min - 2 min = 480 s; a 30 s fetch may start while
# t + 30 <= 480, i.e. at t = 0, 30, ..., 450 -> 16 fetches per tick.
PER_TICK = 16


# ── A4': the first tick after a cold start saves, and the backlog drains ─────────────


def test_cold_start_tick_saves_and_opens_everything(tmp_path: Path) -> None:
    """Fails on D-16ab ``main``: there the 40 fetches overrun T_TICK_MAX and the tick
    aborts without saving -- and the next tick, still ABSENT, does the same."""
    clock = FakeClock()
    src = _source(clock, 40)
    out = _tick(tmp_path / "s", src, clock, NOW, t_tick_max=timedelta(minutes=10))
    assert out.aborted is None
    assert out.wrote_store
    records = _records(tmp_path / "s")
    assert len(records) == 40
    assert len(src.fetched) == PER_TICK
    owed = [r for r in records.values() if r["evidence"].get("marker_pending") is True]
    assert len(owed) == 40 - PER_TICK
    for r in owed:
        assert "origin_unknown" in r["flags"]
        assert r["evidence"]["marker_reason"] == "budget"
    beat = _lines(out, "heartbeat")[-1]
    assert beat["budget_exhausted"] is True
    assert beat["deferred_fetches"] == 40 - PER_TICK
    assert beat["pending_markers"] == 40 - PER_TICK
    budget_opens = [x for x in _lines(out, "open") if x.get("marker_pending")]
    assert len(budget_opens) == 40 - PER_TICK
    assert all(x["marker_reason"] == "budget" for x in budget_opens)


def test_owed_fetches_drain_to_the_unbudgeted_answer(tmp_path: Path) -> None:
    clock = FakeClock()
    src = _source(clock, 40)
    pending: list[int] = []
    now = NOW
    for _ in range(6):
        out = _tick(tmp_path / "s", src, clock, now)
        assert out.aborted is None and out.wrote_store
        pending.append(_lines(out, "heartbeat")[-1]["pending_markers"])
        now += timing.HEARTBEAT_INTERVAL
        if pending[-1] == 0:
            break
    assert pending == [24, 8, 0]
    assert len(src.fetched) == 40
    assert len(set(src.fetched)) == 40  # every unit fetched exactly once

    # The same fixtures through one tick with no budget limit.
    ref_clock = FakeClock()
    ref = _source(ref_clock, 40)
    _tick(tmp_path / "ref", ref, ref_clock, NOW, t_tick_max=timedelta(days=1))
    budgeted = _records(tmp_path / "s")
    assert _final(budgeted) == _final(_records(tmp_path / "ref"))
    assert not any("marker_pending" in r["evidence"] for r in budgeted.values())


def test_retry_tick_logs_marker_resolved_and_is_held_on_abort(tmp_path: Path) -> None:
    clock = FakeClock()
    src = _source(clock, PER_TICK + 1)
    _tick(tmp_path / "s", src, clock, NOW)
    # Next tick aborts on its deadline: the settlement must not be reported or saved.
    src.listing_seconds = 0.0
    before = _records(tmp_path / "s")

    def overrun() -> None:
        clock.t += 3600.0

    out = _tick(tmp_path / "s", src, clock, NOW + timedelta(minutes=15), before_store_write=overrun)
    assert out.aborted == "deadline"
    assert _lines(out, "marker_resolved") == []
    assert _records(tmp_path / "s") == before
    out = _tick(tmp_path / "s", src, clock, NOW + timedelta(minutes=30))
    resolved = _lines(out, "marker_resolved")
    assert len(resolved) == 1
    assert _lines(out, "heartbeat")[-1]["pending_markers"] == 0


# ── A5 edge cases ──────────────────────────────────────────────────────────────────────


def _one_owed(tmp_path: Path) -> tuple[Path, Source, FakeClock]:
    """One record opened with its fetch owed: the listing eats the whole window."""
    clock = FakeClock()
    src = Source(clock=clock, units={"o/r#1": (_review("o/r#1"), True)})
    src.listing_seconds = 470.0
    state = tmp_path / "s"
    out = _tick(state, src, clock, NOW)
    assert out.wrote_store and src.fetched == []
    assert _records(state)["pr:o/r#1"]["evidence"]["marker_pending"] is True
    src.listing_seconds = 0.0
    return state, src, clock


def test_owed_fetch_whose_event_is_gone_settles_e_not_found(tmp_path: Path) -> None:
    state, src, clock = _one_owed(tmp_path)
    src.units["o/r#1"] = ((), True)  # the review that opened the stall is gone
    out = _tick(state, src, clock, NOW + timedelta(minutes=15))
    rec = _records(state)["pr:o/r#1"]
    assert rec["flags"] == ["epoch_unknown", "origin_unknown"]
    assert rec["evidence"]["marker_reason"] == "e_not_found"
    assert "marker_pending" not in rec["evidence"]
    assert src.fetched == []  # no fetch spent on it
    assert _lines(out, "marker_resolved")[0]["marker_reason"] == "e_not_found"
    assert _lines(out, "heartbeat")[-1]["pending_markers"] == 0


def test_owed_record_closed_by_rule_b_says_so(tmp_path: Path) -> None:
    state, src, clock = _one_owed(tmp_path)
    later = NOW + timedelta(minutes=10)
    src.units["o/r#1"] = ((*_review("o/r#1"), MotionEvent("review", "r-new", later)), True)
    out = _tick(state, src, clock, NOW + timedelta(minutes=15))
    (close,) = _lines(out, "close")
    assert close["rule"] == "b"
    assert close["marker_pending"] is True
    assert src.fetched == []
    assert _records(state) == {}


def test_owed_unit_not_observed_waits(tmp_path: Path) -> None:
    state, src, clock = _one_owed(tmp_path)
    src.units = {}
    src.complete = False  # not listed, and the listing can't close it
    out = _tick(state, src, clock, NOW + timedelta(minutes=15))
    assert _records(state)["pr:o/r#1"]["evidence"]["marker_pending"] is True
    assert _lines(out, "marker_resolved") == []
    assert _lines(out, "heartbeat")[-1]["pending_markers"] == 1


def test_owed_fetches_go_first_oldest_first(tmp_path: Path) -> None:
    """Budget for one fetch: it goes to the oldest owed record, not a newer owed one
    and not a new untrusted open (A5 order)."""
    state = tmp_path / "s"
    state.mkdir()
    records: dict[str, Any] = {}
    for ident, opened in (("o/r#a", NOW - timedelta(hours=1)), ("o/r#z", NOW - timedelta(hours=2))):
        unit = Unit(UnitKind.PR, ident)
        records[unit.key] = new_record_json(
            unit=unit,
            now=opened,
            klass="unknown",
            flags=["epoch_unknown", "origin_unknown"],
            motion_at_open=REVIEW_AT,
            evidence={"marker_reason": "budget", "marker_pending": True},
        )
    quarantined = {"pr:o/r#new": {"reason": "key_mismatch", "count": 1}}
    (state / "stall-ledger.json").write_text(
        json.dumps(
            {"schema_version": "1.0", "records": records, "quarantined_records": quarantined}
        ),
        encoding="utf-8",
    )
    clock = FakeClock()
    src = Source(
        clock=clock,
        units={i: (_review(i), True) for i in ("o/r#a", "o/r#z", "o/r#new")},
    )
    src.listing_seconds = 430.0  # one 30 s fetch fits before 480 s, a second does not
    out = _tick(state, src, clock, NOW)
    assert src.fetched == ["o/r#z"]
    saved = _records(state)
    assert "marker_pending" not in saved["pr:o/r#z"]["evidence"]
    assert saved["pr:o/r#a"]["evidence"]["marker_pending"] is True
    assert saved["pr:o/r#new"]["evidence"]["marker_pending"] is True
    beat = _lines(out, "heartbeat")[-1]
    assert beat["deferred_fetches"] == 2
    assert beat["pending_markers"] == 2


# ── C2 / C4: the timing values ─────────────────────────────────────────────────────────


def _ps1_duration(name: str) -> timedelta:
    text = (REPO / "deploy" / "Register-StallLedgerTask.ps1").read_text(encoding="utf-8")
    m = re.search(rf"^\${name} = 'PT(\d+)M'$", text, re.MULTILINE)
    assert m, f"${name} not found as a PT<n>M literal"
    return timedelta(minutes=int(m.group(1)))


def test_timing_inequalities() -> None:
    assert timing.T_TICK_MAX < timing.T_LOCK_STALE
    assert timing.T_TICK_MAX < timing.HEARTBEAT_INTERVAL
    assert timing.T_LOCK_STALE < timing.HEARTBEAT_INTERVAL
    # msg-5747 advisory: the fetch window must hold at least one fetch.
    assert timing.T_TICK_MAX - timing.FETCH_MARGIN >= timing.FETCH_TIMEOUT
    assert (
        timing.check_timing(t_tick_max=timing.T_TICK_MAX, fetch_timeout=timing.FETCH_TIMEOUT)
        is None
    )


def test_scheduled_task_uses_the_timing_values() -> None:
    assert _ps1_duration("HeartbeatInterval") == timing.HEARTBEAT_INTERVAL
    assert _ps1_duration("ExecutionTimeLimit") == timing.T_LOCK_STALE


def test_driver_and_adapter_defaults_are_the_timing_values() -> None:
    from spirrow_mindwire.stall_ledger.adapters import DEFAULT_FETCH_TIMEOUT
    from spirrow_mindwire.stall_ledger.driver import DEFAULT_T_TICK_MAX

    assert DEFAULT_T_TICK_MAX == timing.T_TICK_MAX
    assert DEFAULT_FETCH_TIMEOUT == timing.FETCH_TIMEOUT


@pytest.mark.parametrize(
    ("tick", "fetch"),
    [(timedelta(minutes=2), timedelta(seconds=30)), (timedelta(minutes=3), timedelta(minutes=2))],
)
def test_check_timing_rejects_a_window_without_room_for_one_fetch(
    tick: timedelta, fetch: timedelta
) -> None:
    assert timing.check_timing(t_tick_max=tick, fetch_timeout=fetch) is not None


def test_cli_refuses_an_override_with_no_fetch_room(tmp_path: Path) -> None:
    path = REPO / "scripts" / "stall_ledger_tick.py"
    spec = importlib.util.spec_from_file_location("_d16c_stall_ledger_tick", path)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cli
    spec.loader.exec_module(cli)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--data-dir", str(tmp_path), "--t-tick-max-seconds", "130"])
    assert exc.value.code == 2
    assert not (tmp_path / "state").exists()


def _load_cli() -> Any:
    path = REPO / "scripts" / "stall_ledger_tick.py"
    spec = importlib.util.spec_from_file_location("_d16c_stall_ledger_tick_run", path)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cli
    spec.loader.exec_module(cli)
    return cli


def test_cli_runs_a_full_tick_and_passes_the_fetch_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The PR-gate on 2cf12bc asked whether `_tick` binds `fetch_timeout`; only the refusal
    # branch of `main()` was exercised. Drive the success path end to end (quarantine-only
    # source, no network) and check the value the CLI hands to `run_tick`.
    cli = _load_cli()
    seen: dict[str, Any] = {}
    real_run_tick = cli.run_tick

    async def spy(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return await real_run_tick(**kwargs)

    monkeypatch.setattr(cli, "run_tick", spy)
    code = cli.main(["--data-dir", str(tmp_path), "--fetch-timeout-seconds", "20"])
    assert code == 0
    assert seen["fetch_timeout"] == timedelta(seconds=20)
    assert seen["t_tick_max"] == timing.T_TICK_MAX
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    heartbeat = [line for line in lines if line.get("kind") == "heartbeat"]
    assert len(heartbeat) == 1
    assert heartbeat[0]["budget_exhausted"] is False
    assert heartbeat[0]["deferred_fetches"] == 0
    assert heartbeat[0]["pending_markers"] == 0


def test_cli_fetch_timeout_override_reaches_every_network_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR-gate on 96c41b4: the budget must not believe a fetch takes <= N seconds while
    # the adapter can block longer. Pin that one `--fetch-timeout-seconds` value sizes
    # the budget AND bounds every network call: each adapter's `_bounded` timeout and
    # the GitHub HTTP client's own timeout.
    from spirrow_mindwire.github import client as gh_client
    from spirrow_mindwire.magickit import client as mk_client

    cli = _load_cli()
    seen: dict[str, Any] = {}

    class FakeGitHub:
        def __init__(self, *, timeout_seconds: float) -> None:
            seen["github_timeout_seconds"] = timeout_seconds

        async def aclose(self) -> None:
            pass

    class FakeMcp:
        def __init__(self, url: Any) -> None:
            pass

    async def spy(**kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(gh_client, "GitHubClient", FakeGitHub)
    monkeypatch.setattr(mk_client, "StreamableHttpChatroomMcp", FakeMcp)
    monkeypatch.setattr(cli, "run_tick", spy)
    argv = ["--data-dir", str(tmp_path), "--fetch-timeout-seconds", "5"]
    argv += ["--repo", "o/r", "--project", "p"]
    assert cli.main(argv) == 0
    want = timedelta(seconds=5)
    assert seen["fetch_timeout"] == want
    assert seen["github_timeout_seconds"] == 5.0
    network = [a for a in seen["adapters"] if not isinstance(a, QuarantineFileAdapter)]
    assert {type(a).__name__ for a in network} == {"GitHubOpenPrAdapter", "ChatroomThreadAdapter"}
    assert all(a._timeout == want for a in network)


# ── marker_flags' whole list reaches the record (PR-gate finding on 66fb508) ─────────


def _flags_with_extra(result: MarkerCheck) -> tuple[list[str], str | None]:
    return ["origin_unknown", "extra_flag"], "test"


def test_every_marker_flag_reaches_the_record_at_open_and_at_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_judge_e`` must carry everything ``marker_flags`` returns, not only
    ``origin_unknown`` -- on the open path and on the A5 settle path alike."""
    from spirrow_mindwire.stall_ledger import driver

    monkeypatch.setattr(driver, "marker_flags", _flags_with_extra)
    # Open path: an untrusted open whose fetch fits the budget.
    clock = FakeClock()
    src = Source(clock=clock, units={"o/r#1": (_review("o/r#1"), True)})
    out = _tick(tmp_path / "open", src, clock, NOW)
    rec = _records(tmp_path / "open")["pr:o/r#1"]
    assert rec["flags"] == ["epoch_unknown", "origin_unknown", "extra_flag"]
    assert _lines(out, "open")[0]["flags"] == rec["flags"]
    # Settle path: a record opened with its fetch owed, settled on the next tick.
    state, src, clock = _one_owed(tmp_path)
    out = _tick(state, src, clock, NOW + timedelta(minutes=15))
    rec = _records(state)["pr:o/r#1"]
    assert rec["flags"] == ["epoch_unknown", "origin_unknown", "extra_flag"]
    assert rec["evidence"]["marker_reason"] == "test"
    assert _lines(out, "marker_resolved")[0]["flags"] == rec["flags"]
