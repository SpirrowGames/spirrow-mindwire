"""A4 (msg-9400 §3, msg-9875 item 1): slot mode's ledger-based continuation verdict."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.clone_slot_lease import Lease, LeaseState, LeaseStore
from spirrow_mindwire.slot_continuation import (
    PARK_CODE,
    REASON_LEASE_HELD,
    REASON_NO_RECORD,
    REASON_NOT_SAVED,
    SLOT_CONTINUE_REFUSED_PREFIX,
    SlotContinuationRefused,
    SlotContinuationVerdict,
    emit_slot_continue_refused_payload,
    judge_slot_continuation,
)

ROOT = Path("/slots")
REPO = "mindwire"
P = "proj"
T = "T-a"


def _row(
    turn: str, outcome: str, *, thread: str = T, slot: str = "slot-01", **extra: object
) -> dict[str, Any]:
    return {
        "kind": "slot_turn_result",
        "project": P,
        "thread": thread,
        "turn_id": turn,
        "outcome": outcome,
        "slot": slot,
        "refs": [],
        **extra,
    }


def _lease(
    slot: str, turn: str, *, thread: str = T, state: LeaseState = LeaseState.RUNNING
) -> Lease:
    return Lease(
        repo=REPO,
        slot=slot,
        thread_id=thread,
        turn_id=turn,
        acquired_at="2026-11-22T00:00:00Z",
        state=state,
    )


def _judge(
    rows: Iterable[dict[str, Any]] = (),
    leases: Iterable[Lease] = (),
    current: Path | None = ROOT / REPO / "slot-02",
    retreats: Iterable[dict[str, Any]] = (),
) -> SlotContinuationVerdict:
    return judge_slot_continuation(
        project=P,
        thread=T,
        ledger_rows=list(rows),
        leases=list(leases),
        current_slot=current,
        slots_root=ROOT,
        repo=REPO,
        retreat_rows=list(retreats),
    )


@pytest.mark.parametrize("outcome", ["pushed", "rescued"])
def test_saved_last_turn_admits(outcome: str) -> None:
    v = _judge([_row("t1", "rescue_failed"), _row("t2", outcome)])
    assert v.admit and v.turn_id == "t2" and v.outcomes == (outcome,)


def test_no_record_is_never_pushed() -> None:
    v = _judge([_row("t1", "pushed", thread="T-other")])
    assert not v.admit and v.reason == REASON_NO_RECORD


def test_rescue_failed_names_the_quarantine_path() -> None:
    retreat = {
        "thread_id": T,
        "quarantine_path": "/slots/_quarantine/mindwire/slot-01-x",
        "detail": "turn end t2: push refused",
    }
    v = _judge(
        [_row("t1", "pushed"), _row("t2", "rescue_failed", detail="push refused")],
        retreats=[retreat],
    )
    assert not v.admit and v.reason == REASON_NOT_SAVED
    assert v.path == "/slots/_quarantine/mindwire/slot-01-x"
    assert "rescue_failed" in v.detail and "push refused" in v.detail


def test_rescue_failed_without_quarantine_row_points_at_the_quarantine_root() -> None:
    v = _judge([_row("t2", "rescue_failed")])
    assert not v.admit and v.path == str(ROOT / "_quarantine" / REPO)


def test_quarantine_failed_names_the_slot_itself() -> None:
    v = _judge([_row("t2", "rescue_failed"), _row("t2", "quarantine_failed")])
    assert not v.admit and v.outcomes == ("rescue_failed", "quarantine_failed")
    assert v.path == str(ROOT / REPO / "slot-01")


def test_a_lease_still_held_elsewhere_refuses_even_over_a_saved_row() -> None:
    v = _judge([_row("t1", "pushed")], leases=[_lease("slot-01", "t2")])
    assert not v.admit and v.reason == REASON_LEASE_HELD
    assert v.path == str(ROOT / REPO / "slot-01") and v.turn_id == "t2"


def test_this_runs_own_lease_and_other_threads_leases_are_ignored() -> None:
    v = _judge(
        [_row("t1", "pushed")],
        leases=[_lease("slot-02", "t3"), _lease("slot-01", "x", thread="T-other")],
    )
    assert v.admit


def test_payload_line(capsys: pytest.CaptureFixture[str]) -> None:
    v = _judge([_row("t2", "rescue_failed")])
    emit_slot_continue_refused_payload(SlotContinuationRefused(v, project=P, thread=T))
    out = capsys.readouterr().out.strip()
    assert out.startswith(SLOT_CONTINUE_REFUSED_PREFIX)
    payload = json.loads(out[len(SLOT_CONTINUE_REFUSED_PREFIX) :])
    assert payload["park_code"] == PARK_CODE and payload["thread"] == T
    assert payload["reason"] == REASON_NOT_SAVED and payload["path"]


def test_built_check_reads_the_ledger_and_lease_files(tmp_path: Path) -> None:
    from spirrow_mindwire.conductor.continuation_ledger import SlotTurnOutcome, record_slot_turn
    from spirrow_mindwire.slot_continuation import build_slot_continuation_check

    state = tmp_path / "state"
    ledger = state / "continuation_ledger.jsonl"
    check = build_slot_continuation_check(
        slots_root=tmp_path / "slots",
        current_slot=tmp_path / "slots" / REPO / "slot-02",
        state_dir=state,
        ledger_path=ledger,
        retreats_path=state / "clone-retreats.jsonl",
        project=P,
        thread=T,
    )
    with pytest.raises(SlotContinuationRefused) as no_row:
        check()
    assert no_row.value.verdict.reason == REASON_NO_RECORD
    record_slot_turn(
        ledger, project=P, thread=T, turn_id="t1", outcome=SlotTurnOutcome.PUSHED, slot="slot-01"
    )
    check()  # admitted
    record_slot_turn(
        ledger,
        project=P,
        thread=T,
        turn_id="t2",
        outcome=SlotTurnOutcome.RESCUE_FAILED,
        slot="slot-01",
    )
    with pytest.raises(SlotContinuationRefused) as failed:
        check()
    assert failed.value.verdict.reason == REASON_NOT_SAVED


@pytest.mark.parametrize("first", ["rescue_failed", None])
def test_a_refused_runs_unused_borrow_leaves_the_refusal_standing(
    tmp_path: Path, first: str | None
) -> None:
    """PR #598 gate Obj-1-1: the sequence, on the real ledger and lease files.

    t1 ends rescue_failed (or records nothing); the next run borrows a clean slot for t2, its
    CONTINUE is refused (exit 14), and the wrapper hands that unused borrow back by a plain
    release with no slot_turn_result row. A retry must refuse again — it would admit only had the
    unused borrow been recorded as ``pushed`` (the defect the gate found).
    """
    from datetime import UTC, datetime

    from spirrow_mindwire.conductor.continuation_ledger import SlotTurnOutcome, record_slot_turn
    from spirrow_mindwire.slot_continuation import build_slot_continuation_check

    state = tmp_path / "state"
    ledger = state / "continuation_ledger.jsonl"
    slots = tmp_path / "slots"
    if first is not None:
        record_slot_turn(
            ledger,
            project=P,
            thread=T,
            turn_id="t1",
            outcome=SlotTurnOutcome(first),
            slot="slot-01",
        )
    store = LeaseStore(state)

    def run_refused(turn: str) -> None:
        got = store.acquire(REPO, 2, thread_id=T, turn_id=turn, now=datetime.now(UTC))
        assert got.lease is not None
        check = build_slot_continuation_check(
            slots_root=slots,
            current_slot=slots / REPO / got.lease.slot,
            state_dir=state,
            ledger_path=ledger,
            retreats_path=state / "clone-retreats.jsonl",
            project=P,
            thread=T,
        )
        with pytest.raises(SlotContinuationRefused) as refused:
            check()
        assert refused.value.verdict.reason in (REASON_NOT_SAVED, REASON_NO_RECORD)
        store.release(REPO, got.lease.slot, turn)  # exit 14: returned unused, no row

    run_refused("t2")
    run_refused("t3")  # the retry / unpark: still refused
    assert not [r for r in _ledger_rows(ledger) if r.get("turn_id") in ("t2", "t3")]


def _ledger_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_loop_runner_builds_no_slot_check_without_slots_root() -> None:
    from spirrow_mindwire.config import MindwireSettings
    from spirrow_mindwire.loop_runner import _slot_continuation_check, _thread_ref

    assert _slot_continuation_check(MindwireSettings(), _thread_ref("p", "t")) is None
