"""Phase 2 rules (msg-6014 §2 rules 1/5, msg-6016 §2 rules 2'-6', minimum tests of §4)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from spirrow_mindwire.magickit.client import MagickitMcpError, ThreadResolvedError
from spirrow_mindwire.pr_review_sweep.phase1 import LedgerRow, Phase1Class
from spirrow_mindwire.pr_review_sweep.phase2 import (
    AttemptRecord,
    AttemptResult,
    NotReached,
    Phase2Settings,
    State,
    backoff_delay,
    dump_state,
    load_state,
    order_candidates,
    report_to_json,
    run_tick,
)

_P = "spirrow-mindwire"
_NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
_CLOSED_AT = datetime(2026, 9, 1, tzinfo=UTC)
_ON = Phase2Settings(enabled=True)


def _row(
    n: int, cls: Phase1Class = Phase1Class.TERMINAL_MERGED, reason: str = "pr-merged"
) -> LedgerRow:
    return LedgerRow(
        thread_id=f"T-pr-review-{_P}-{n}",
        project=_P,
        cls=cls,
        reason=reason,
        thread_status="active",
        pr=f"SpirrowGames/{_P}#{n}",
        closed_at=_CLOSED_AT
        if cls in (Phase1Class.TERMINAL_MERGED, Phase1Class.TERMINAL_CLOSED)
        else None,
    )


def _tid(n: int) -> str:
    return f"T-pr-review-{_P}-{n}"


class _Mcp:
    """A chatroom that closes ledgers in ``closable`` and refuses everything else.

    ``mode`` per thread tweaks the close call independently of the resulting state, so
    the tests can show the read-back alone decides the outcome.
    """

    def __init__(
        self, closable: set[str] | None = None, mode: dict[str, str] | None = None
    ) -> None:
        self.closable = closable or set()
        self.mode = mode or {}
        self.status: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def closes(self) -> list[str]:
        return [a["thread_id"] for n, a in self.calls if n == "chatroom_close_thread"]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        tid = arguments["thread_id"]
        mode = self.mode.get(tid, "")
        if name == "chatroom_close_thread":
            if mode == "raise-but-resolved":
                self.status[tid] = "resolved"
                raise MagickitMcpError("transport hiccup after commit")
            if mode == "already-resolved":
                self.status[tid] = "resolved"
                raise ThreadResolvedError("already resolved", error_type="ChatroomStateError")
            if mode == "ok-but-still-active":
                return {"ok": True}
            if tid in self.closable:
                self.status[tid] = "resolved"
                return {"ok": True}
            raise MagickitMcpError(
                "PrGateLedgerNotClosableError: nope", error_type="PrGateLedgerNotClosableError"
            )
        if name == "chatroom_get_thread":
            if mode == "readback-fails":
                raise MagickitMcpError("read failed")
            return {"thread": {"status": self.status.get(tid, "active")}}
        raise AssertionError(name)


def _tick(
    mcp: _Mcp | None,
    rows: list[LedgerRow],
    state: State,
    *,
    now: datetime = _NOW,
    settings: Phase2Settings = _ON,
) -> Any:
    return asyncio.run(run_tick(mcp, rows, state, now, settings))


# --- rule 1: the read-back decides ------------------------------------------------------


def test_success_closes_and_removes_state() -> None:
    mcp = _Mcp(closable={_tid(1)})
    state: State = {
        (_P, _tid(1)): AttemptRecord(_NOW - timedelta(days=30), 1, _NOW - timedelta(days=30))
    }
    report = _tick(mcp, [_row(1)], state)
    assert [a.result for a in report.attempts] == [AttemptResult.CLOSED]
    assert report.state == {}
    payload = next(a for n, a in mcp.calls if n == "chatroom_close_thread")
    assert payload["author"] == "orchestrator"
    assert payload["embodiment"] == "unknown"
    assert "merged" in payload["summary_content"]


@pytest.mark.parametrize("mode", ["raise-but-resolved", "already-resolved"])
def test_refusal_then_resolved_on_readback_counts_as_closed(mode: str) -> None:
    mcp = _Mcp(mode={_tid(1): mode})
    report = _tick(mcp, [_row(1)], {})
    assert [a.result for a in report.attempts] == [AttemptResult.CLOSED]
    assert report.state == {}


def test_normal_return_but_not_resolved_counts_as_refused() -> None:
    mcp = _Mcp(mode={_tid(1): "ok-but-still-active"})
    report = _tick(mcp, [_row(1)], {})
    assert [a.result for a in report.attempts] == [AttemptResult.REFUSED]
    assert report.attempts[0].observed_status == "active"


def test_failed_readback_counts_as_refused() -> None:
    mcp = _Mcp(closable={_tid(1)}, mode={_tid(1): "readback-fails"})
    report = _tick(mcp, [_row(1)], {})
    assert report.attempts[0].result is AttemptResult.REFUSED
    assert report.attempts[0].observed_status is None


def test_refusal_envelope_recorded_verbatim() -> None:
    report = _tick(_Mcp(), [_row(1)], {})
    out = report_to_json(report)["refused"][0]
    assert out["error_type"] == "PrGateLedgerNotClosableError"
    assert "PrGateLedgerNotClosableError: nope" in out["error"]
    assert out["consecutive_refusals"] == 1
    assert out["first_refused_at"] == _NOW.isoformat()


# --- rule 5: off by default -------------------------------------------------------------


def test_default_settings_are_off() -> None:
    assert Phase2Settings().enabled is False


def test_disabled_makes_no_call_and_does_not_advance_state() -> None:
    mcp = _Mcp(closable={_tid(1), _tid(2)})
    state: State = {(_P, _tid(2)): AttemptRecord(_NOW - timedelta(days=1), 0)}
    report = _tick(mcp, [_row(1), _row(2)], dict(state), settings=Phase2Settings())
    assert mcp.calls == []
    assert report.attempts == []
    assert [r.thread_id for r in report.would_attempt] == [_tid(1), _tid(2)]
    assert report.state == state
    assert report_to_json(report)["wrote_anything"] is False


def test_disabled_accepts_no_client() -> None:
    report = _tick(None, [_row(1)], {}, settings=Phase2Settings())
    assert [r.thread_id for r in report.would_attempt] == [_tid(1)]


def test_disabled_listing_respects_cap() -> None:
    rows = [_row(n) for n in range(1, 6)]
    report = _tick(None, rows, {}, settings=Phase2Settings(max_attempts=2))
    assert len(report.would_attempt) == 2
    assert [why for _, why in report.not_reached] == [NotReached.CAP] * 3


# --- only TERMINAL_* is touched ---------------------------------------------------------


def test_non_candidates_are_never_tried() -> None:
    rows = [
        _row(1, Phase1Class.OPEN, "pr-open"),
        _row(2, Phase1Class.SKIP, "pr-indeterminate"),
        _row(3, Phase1Class.UNPARSEABLE, "pr-not-found"),
        _row(4, Phase1Class.TERMINAL_CLOSED, "pr-closed-unmerged"),
    ]
    mcp = _Mcp(closable={_tid(n) for n in range(1, 5)})
    report = _tick(mcp, rows, {})
    assert mcp.closes == [_tid(4)]
    out = report_to_json(report)
    assert out["unparseable"] == {"pr-not-found": [f"{_P}/{_tid(3)}"]}
    assert [s["thread_id"] for s in out["skip"]] == [_tid(2)]


# --- rules 3'/4'/5': ordering, backoff, breaker -----------------------------------------


def test_breaker_trips_after_k_and_next_tick_reaches_other_ledgers() -> None:
    rows = [_row(n) for n in range(1, 7)]  # 1..3 always refused, 4..6 closable
    mcp = _Mcp(closable={_tid(4), _tid(5), _tid(6)})
    first = _tick(mcp, rows, {})
    assert mcp.closes == [_tid(1), _tid(2), _tid(3)]
    assert first.breaker_tripped
    assert {r.thread_id for r, why in first.not_reached if why is NotReached.BREAKER} == {
        _tid(4),
        _tid(5),
        _tid(6),
    }

    mcp.calls.clear()
    second = _tick(mcp, rows, first.state, now=_NOW + timedelta(minutes=5))
    # 4..6 were never tried, so they lead; 1..3 are also still in backoff.
    assert mcp.closes == [_tid(4), _tid(5), _tid(6)]
    assert [a.result for a in second.attempts] == [AttemptResult.CLOSED] * 3
    assert {r.thread_id for r, _ in second.deferred} == {_tid(1), _tid(2), _tid(3)}


def test_fresh_merged_ledger_reached_within_one_tick_despite_many_poison_pills() -> None:
    poison = [_row(n) for n in range(100, 125)]  # 25 always refused
    state: State = {
        (_P, r.thread_id): AttemptRecord(_NOW - timedelta(days=10), 1, _NOW - timedelta(days=10))
        for r in poison
    }
    fresh = _row(1)
    mcp = _Mcp(closable={_tid(1)})
    report = _tick(mcp, [*poison, fresh], state)
    assert mcp.closes[0] == _tid(1)
    assert report.attempts[0].result is AttemptResult.CLOSED


def test_attempted_ledgers_move_to_the_back() -> None:
    rows = [_row(n) for n in range(1, 5)]
    state: State = {
        (_P, _tid(1)): AttemptRecord(_NOW - timedelta(hours=1), 0),
        (_P, _tid(2)): AttemptRecord(_NOW - timedelta(hours=3), 0),
        (_P, _tid(3)): AttemptRecord(_NOW - timedelta(hours=2), 0),
    }
    order = [r.thread_id for r in order_candidates(rows, state)]
    assert order == [_tid(4), _tid(2), _tid(3), _tid(1)]


def test_backoff_grows_and_saturates() -> None:
    s = Phase2Settings(backoff_unit_seconds=60)
    delays = [backoff_delay(r, s) for r in range(0, 10)]
    assert delays[0] == timedelta(0)
    assert [d.total_seconds() / 60 for d in delays[1:]] == [2, 4, 8, 16, 32, 64, 64, 64, 64]
    assert backoff_delay(10_000, s) == timedelta(minutes=64)


def test_default_backoff_unit_is_one_hour() -> None:
    assert backoff_delay(6, Phase2Settings()) == timedelta(hours=64)


def test_refused_ledger_waits_out_its_backoff() -> None:
    mcp = _Mcp()
    first = _tick(mcp, [_row(1)], {})
    rec = first.state[(_P, _tid(1))]
    assert rec.consecutive_refusals == 1
    # r=1 -> 2 units (2 h). One hour later it is still deferred, no call made.
    mcp.calls.clear()
    second = _tick(mcp, [_row(1)], first.state, now=_NOW + timedelta(hours=1))
    assert mcp.calls == []
    assert second.deferred[0][1] == _NOW + timedelta(hours=2)
    # Once due, it is tried again and the streak and first_refused_at carry over.
    third = _tick(mcp, [_row(1)], second.state, now=_NOW + timedelta(hours=2))
    rec = third.state[(_P, _tid(1))]
    assert rec.consecutive_refusals == 2
    assert rec.first_refused_at == _NOW
    assert rec.last_attempt_at == _NOW + timedelta(hours=2)


def test_attempt_cap() -> None:
    rows = [_row(n) for n in range(1, 31)]
    mcp = _Mcp(closable={r.thread_id for r in rows})
    report = _tick(mcp, rows, {}, settings=Phase2Settings(enabled=True, max_attempts=20))
    assert len(mcp.closes) == 20
    assert [why for _, why in report.not_reached] == [NotReached.CAP] * 10


def test_scattered_refusals_do_not_trip_breaker() -> None:
    rows = [_row(n) for n in range(1, 9)]
    closable = {_tid(n) for n in (2, 4, 6, 8)}  # refused, closed, refused, closed, ...
    mcp = _Mcp(closable=closable)
    report = _tick(mcp, rows, {})
    assert len(report.attempts) == 8
    assert not report.breaker_tripped


def test_needs_human_after_r_refusals() -> None:
    state: State = {
        (_P, _tid(1)): AttemptRecord(_NOW - timedelta(days=30), 4, _NOW - timedelta(days=60))
    }
    report = _tick(_Mcp(), [_row(1)], state)
    out = report_to_json(report)
    assert [h["thread_id"] for h in out["needs_human"]] == [_tid(1)]
    assert out["needs_human"][0]["consecutive_refusals"] == 5
    assert out["needs_human"][0]["first_refused_at"] == (_NOW - timedelta(days=60)).isoformat()


def test_ledger_no_longer_terminal_is_pruned() -> None:
    state: State = {(_P, _tid(1)): AttemptRecord(_NOW - timedelta(days=1), 3)}
    report = _tick(_Mcp(), [_row(1, Phase1Class.SKIP, "pr-indeterminate")], state)
    assert report.state == {}


# --- rule 2': the state file ------------------------------------------------------------


def test_state_round_trip() -> None:
    state: State = {
        (_P, _tid(1)): AttemptRecord(_NOW, 2, _NOW - timedelta(hours=2)),
        (_P, _tid(2)): AttemptRecord(_NOW, 0, None),
        ("other", _tid(3)): AttemptRecord(_NOW, 1, _NOW),
    }
    text = dump_state(_P, state)
    loaded, warning = load_state(_P, text)
    assert warning is None
    assert loaded == {k: v for k, v in state.items() if k[0] == _P}


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        json.dumps([1, 2]),
        json.dumps({"schema_version": 99, "ledgers": {}}),
        json.dumps({"schema_version": 1}),
    ],
)
def test_corrupt_state_file_falls_back_to_never_tried(text: str) -> None:
    state, warning = load_state(_P, text)
    assert state == {}
    assert warning
    mcp = _Mcp(closable={_tid(1)})
    report = _tick(mcp, [_row(1)], state)
    assert mcp.closes == [_tid(1)]
    assert report.attempts[0].result is AttemptResult.CLOSED


def test_bad_state_lines_are_dropped_individually() -> None:
    text = json.dumps(
        {
            "schema_version": 1,
            "ledgers": {
                _tid(1): {"last_attempt_at": _NOW.isoformat(), "consecutive_refusals": 1},
                _tid(2): {"last_attempt_at": "2026-10-03T12:00:00"},  # naive
                _tid(3): {"last_attempt_at": _NOW.isoformat(), "consecutive_refusals": -1},
                _tid(4): "garbage",
            },
        }
    )
    state, warning = load_state(_P, text)
    assert set(state) == {(_P, _tid(1))}
    assert warning and "3 unusable" in warning


def test_missing_state_file_is_silent() -> None:
    assert load_state(_P, None) == ({}, None)


def test_settings_reject_nonpositive() -> None:
    with pytest.raises(ValueError):
        Phase2Settings(breaker=0)
    with pytest.raises(ValueError):
        Phase2Settings(author=" ")
