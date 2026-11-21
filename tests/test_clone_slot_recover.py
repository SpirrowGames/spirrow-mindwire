"""``mindwire slot-recover`` (T-ephemeral-impl-clone-slot-per-thread A4, Bohr msg-9877 (a),
Einstein's go after it): the turn end of a lease left held, run by an operator.

Real git, the same fixtures as the A3 turn-end tests: a bare ``origin`` stands in for GitHub and
the slot is borrowed with ``SlotPrep``. The borrowed lease is left ``launching`` + spawning, as an
exit 7 or a throw after spawn leaves it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_clone_slot_finish import (
    REPO,
    _borrow,
    _break_origin,
    _finish,
    _git,
    _lease_state,
    _ledger,
    _retreats,
    _store,
)
from test_clone_slot_finish import origin as origin

from spirrow_mindwire.cli import main
from spirrow_mindwire.clone_slot_finish import FinishOutcome
from spirrow_mindwire.clone_slot_lease import LeaseState

OPERATOR = "operator"


def _left_held(tmp_path: Path, origin: Path, thread: str = "T-a", turn: str = "t1") -> Path:
    slot = _borrow(tmp_path, origin, thread=thread, turn=turn)
    _store(tmp_path).mark_spawning(REPO, "slot-01", turn)
    return slot


def test_unpushed_commit_is_rescued_then_released(tmp_path: Path, origin: Path) -> None:
    slot = _left_held(tmp_path, origin)
    (slot / "b.txt").write_text("b\n", encoding="utf-8")
    _git(slot, "add", ".")
    _git(slot, "commit", "-q", "-m", "unpushed")
    res = _finish(tmp_path, recovered_by=OPERATOR).recover(slot="slot-01", confirm_no_process=True)
    assert res.outcome is FinishOutcome.RELEASED, res.detail
    (row,) = _ledger(tmp_path)
    # Turn and thread come from the lease, not from the operator.
    assert row["outcome"] == "rescued" and row["turn_id"] == "t1" and row["thread"] == "T-a"
    assert row["recovered_by"] == OPERATOR
    refs = row["refs"]
    assert isinstance(refs, list) and refs
    assert _git(origin, "rev-parse", str(refs[0]["ref"])).strip() == refs[0]["sha"]
    assert _lease_state(tmp_path) is None
    assert slot.is_dir()  # a saved recovery does not rebuild the slot


def test_failed_rescue_is_quarantined_and_the_next_borrow_provisions_anew(
    tmp_path: Path, origin: Path
) -> None:
    slot = _left_held(tmp_path, origin)
    (slot / "wip.txt").write_text("wip\n", encoding="utf-8")
    _break_origin(slot, tmp_path)
    res = _finish(tmp_path, recovered_by=OPERATOR).recover(slot="slot-01", confirm_no_process=True)
    assert res.outcome is FinishOutcome.QUARANTINED, res.detail
    assert res.quarantine_path is not None and (res.quarantine_path / "wip.txt").is_file()
    assert [(r["outcome"], r["recovered_by"]) for r in _ledger(tmp_path)] == [
        ("rescue_failed", OPERATOR)
    ]
    (q,) = _retreats(tmp_path)
    assert q["quarantine_reason"] == "rescue_failed" and q["recoverable"] is False
    notice = res.notice
    assert notice is not None and notice["recovered_by"] == OPERATOR
    assert notice["path"] == str(res.quarantine_path)
    assert _lease_state(tmp_path) is None
    again = _borrow(tmp_path, origin, thread="T-b", turn="t2")
    assert again == slot and not (again / "wip.txt").exists()


@pytest.mark.parametrize("case", ["no_confirm", "returned", "quarantine_failed"])
def test_refusals_touch_nothing(tmp_path: Path, origin: Path, case: str) -> None:
    slot = _left_held(tmp_path, origin)
    (slot / "wip.txt").write_text("wip\n", encoding="utf-8")
    store = _store(tmp_path)
    if case == "returned":
        store.release(REPO, "slot-01", "t1")
    if case == "quarantine_failed":
        store.mark_quarantine_failed(REPO, "slot-01", "t1")
    before = _lease_state(tmp_path)
    res = _finish(tmp_path, recovered_by=OPERATOR).recover(
        slot="slot-01", confirm_no_process=case != "no_confirm"
    )
    assert res.outcome is FinishOutcome.REFUSED
    assert _lease_state(tmp_path) is before
    assert not (tmp_path / "state" / "continuation_ledger.jsonl").exists()
    assert _retreats(tmp_path) == []
    assert (slot / "wip.txt").is_file()
    if case == "quarantine_failed":
        assert before is LeaseState.QUARANTINE_FAILED and "does not clear" in res.detail


def _cli(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "slot-recover",
        "--slots-root",
        str(tmp_path / "slots"),
        "--repo",
        REPO,
        "--project",
        "proj",
        "--state-dir",
        str(tmp_path / "state"),
        "--slot",
        "slot-01",
        *extra,
    ]


def test_cli_recovers_with_confirm_and_refuses_without(
    tmp_path: Path, origin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    slot = _left_held(tmp_path, origin)
    (slot / "wip.txt").write_text("wip\n", encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        main(_cli(tmp_path))
    assert refused.value.code == 2
    assert _lease_state(tmp_path) is LeaseState.LAUNCHING
    capsys.readouterr()
    with pytest.raises(SystemExit) as done:
        main(_cli(tmp_path, "--confirm-no-process"))
    assert done.value.code == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line.startswith("MINDWIRE_SLOT_RECOVER ")
    payload = json.loads(line[len("MINDWIRE_SLOT_RECOVER ") :])
    assert payload["outcome"] == "released" and payload["recovered_by"] == OPERATOR
    assert _lease_state(tmp_path) is None
