"""Turn-end slot procedure (T-ephemeral-impl-clone-slot-per-thread A3: msg-8047 D-4, Bohr msg-9400
§1-§2, msg-9402 Obj-36-4, msg-9754 §1-§2).

Real git: a bare ``origin`` stands in for GitHub; the slot is borrowed with the A2 ``SlotPrep``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spirrow_mindwire.cli import main
from spirrow_mindwire.clone_retreat import git_env
from spirrow_mindwire.clone_slot import PARK_UNRECOVERABLE, quarantine_root
from spirrow_mindwire.clone_slot_finish import FinishOutcome, SlotFinish, refusal_reason
from spirrow_mindwire.clone_slot_lease import LeaseState, LeaseStore
from spirrow_mindwire.clone_slot_prep import PrepOutcome, SlotPrep
from spirrow_mindwire.config import CONTINUATION_LEDGER_FILENAME

REPO = "sample"
NOW = datetime(2026, 11, 19, 2, 3, 4, tzinfo=UTC)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True, env=git_env()
    ).stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.invalid")
    _git(seed, "config", "user.name", "t")
    (seed / "a.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True, capture_output=True
    )
    return bare


def _store(tmp_path: Path) -> LeaseStore:
    return LeaseStore(tmp_path / "state", alive=lambda pid: False)


def _borrow(tmp_path: Path, origin: Path, thread: str = "T-a", turn: str = "t1") -> Path:
    prep = SlotPrep(
        slots_root=tmp_path / "slots",
        repo=REPO,
        origin_url=str(origin),
        store=_store(tmp_path),
        slots_per_repo=1,
        clock=lambda: NOW,
    )
    res = prep.borrow(
        thread_id=thread, turn_id=turn, branch=f"feature/{thread}", base="main", wrapper_pid=1
    )
    assert res.outcome is PrepOutcome.READY, res.detail
    slot = res.slot.path  # type: ignore[union-attr]
    _git(slot, "config", "user.email", "t@example.invalid")
    _git(slot, "config", "user.name", "t")
    return slot


def _finish(tmp_path: Path, **kw: object) -> SlotFinish:
    return SlotFinish(
        slots_root=tmp_path / "slots",
        repo=REPO,
        project="proj",
        store=_store(tmp_path),
        ledger=tmp_path / "state" / CONTINUATION_LEDGER_FILENAME,
        retreats=tmp_path / "state" / "clone-retreats.jsonl",
        clock=lambda: NOW,
        **kw,  # type: ignore[arg-type]
    )


def _ledger(tmp_path: Path) -> list[dict[str, object]]:
    p = tmp_path / "state" / CONTINUATION_LEDGER_FILENAME
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _retreats(tmp_path: Path) -> list[dict[str, object]]:
    p = tmp_path / "state" / "clone-retreats.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _lease_state(tmp_path: Path) -> LeaseState | None:
    leases = _store(tmp_path).leases(REPO)
    return leases[0].state if leases else None


def _break_origin(slot: Path, tmp_path: Path) -> None:
    _git(slot, "remote", "set-url", "origin", str(tmp_path / "no-such-origin.git"))


def test_pushed_turn_is_recorded_then_released(tmp_path: Path, origin: Path) -> None:
    slot = _borrow(tmp_path, origin)
    (slot / "b.txt").write_text("b\n", encoding="utf-8")
    _git(slot, "add", ".")
    _git(slot, "commit", "-q", "-m", "work")
    _git(slot, "push", "-q", "origin", "HEAD:refs/heads/feature/T-a")
    res = _finish(tmp_path).finish(slot="slot-01", thread_id="T-a", turn_id="t1")
    assert res.outcome is FinishOutcome.RELEASED, res.detail
    assert res.notice is None
    assert [r["outcome"] for r in _ledger(tmp_path)] == ["pushed"]
    assert _lease_state(tmp_path) is None  # handed back
    assert slot.is_dir()


def test_unsaved_work_is_rescued_then_released(tmp_path: Path, origin: Path) -> None:
    slot = _borrow(tmp_path, origin)
    (slot / "wip.txt").write_text("wip\n", encoding="utf-8")
    res = _finish(tmp_path).finish(slot="slot-01", thread_id="T-a", turn_id="t1")
    assert res.outcome is FinishOutcome.RELEASED, res.detail
    rows = _ledger(tmp_path)
    assert [r["outcome"] for r in rows] == ["rescued"]
    refs = rows[0]["refs"]
    assert isinstance(refs, list) and refs[0]["ref"] == "refs/heads/rescue/T-a/t1/01"
    assert _git(origin, "rev-parse", "rescue/T-a/t1/01").strip() == refs[0]["sha"]
    assert _lease_state(tmp_path) is None


def test_rescue_failed_quarantines_with_recoverable_false_and_releases(
    tmp_path: Path, origin: Path
) -> None:
    slot = _borrow(tmp_path, origin)
    (slot / "wip.txt").write_text("wip\n", encoding="utf-8")
    _break_origin(slot, tmp_path)
    res = _finish(tmp_path).finish(slot="slot-01", thread_id="T-a", turn_id="t1")
    assert res.outcome is FinishOutcome.QUARANTINED, res.detail
    assert not slot.exists()
    assert res.quarantine_path is not None and res.quarantine_path.is_dir()
    assert res.quarantine_path.parent == quarantine_root(tmp_path / "slots", REPO)
    assert (res.quarantine_path / "wip.txt").is_file()  # renamed, never deleted
    assert [r["outcome"] for r in _ledger(tmp_path)] == ["rescue_failed"]
    (row,) = _retreats(tmp_path)
    assert row["kind"] == "slot_quarantine" and row["action"] == "rebuilt"
    assert row["quarantine_reason"] == "rescue_failed"
    assert row["recoverable"] is False
    assert row["quarantine_path"] == str(res.quarantine_path)
    assert _lease_state(tmp_path) is None  # returned: the next borrow provisions anew
    notice = res.notice
    assert notice == {
        "reason": "rescue_failed",
        "path": str(res.quarantine_path),
        "detail": res.detail,
    }


def test_next_borrow_after_turn_end_quarantine_provisions_anew(
    tmp_path: Path, origin: Path
) -> None:
    slot = _borrow(tmp_path, origin)
    (slot / "wip.txt").write_text("wip\n", encoding="utf-8")
    _break_origin(slot, tmp_path)
    _finish(tmp_path).finish(slot="slot-01", thread_id="T-a", turn_id="t1")
    again = _borrow(tmp_path, origin, thread="T-b", turn="t2")
    assert again == slot and not (again / "wip.txt").exists()


def test_rename_failure_keeps_lease_quarantine_failed_and_parks(
    tmp_path: Path, origin: Path
) -> None:
    slot = _borrow(tmp_path, origin)
    (slot / "wip.txt").write_text("wip\n", encoding="utf-8")
    _break_origin(slot, tmp_path)

    def refuse(src: Path, dst: Path) -> None:
        raise PermissionError("in use")

    res = _finish(tmp_path, rename=refuse).finish(slot="slot-01", thread_id="T-a", turn_id="t1")
    assert res.outcome is FinishOutcome.PARKED, res.detail
    assert res.park_code == PARK_UNRECOVERABLE
    assert slot.is_dir()
    assert _lease_state(tmp_path) is LeaseState.QUARANTINE_FAILED  # never lent again
    assert [r["outcome"] for r in _ledger(tmp_path)] == ["rescue_failed", "quarantine_failed"]
    (row,) = _retreats(tmp_path)
    assert row["action"] == "parked" and row["park_code"] == PARK_UNRECOVERABLE
    assert row["quarantine_path"] is None
    notice = res.notice
    assert notice is not None
    assert notice["reason"] == PARK_UNRECOVERABLE and notice["path"] == str(slot)
    assert "rename failed" in str(notice["detail"])


def test_obstructed_quarantine_root_keeps_lease_quarantine_failed_and_parks(
    tmp_path: Path, origin: Path
) -> None:
    """A file where ``_quarantine/<repo>`` should be makes mkdir fail; that is a failed
    quarantine, not a crash (PR #594 gate Obj-1-2)."""
    slot = _borrow(tmp_path, origin)
    (slot / "wip.txt").write_text("wip\n", encoding="utf-8")
    _break_origin(slot, tmp_path)
    qroot = quarantine_root(tmp_path / "slots", REPO)
    qroot.parent.mkdir(parents=True, exist_ok=True)
    qroot.write_text("obstruction\n", encoding="utf-8")

    res = _finish(tmp_path).finish(slot="slot-01", thread_id="T-a", turn_id="t1")
    assert res.outcome is FinishOutcome.PARKED, res.detail
    assert res.park_code == PARK_UNRECOVERABLE
    assert slot.is_dir() and (slot / "wip.txt").is_file()
    assert _lease_state(tmp_path) is LeaseState.QUARANTINE_FAILED
    assert [r["outcome"] for r in _ledger(tmp_path)] == ["rescue_failed", "quarantine_failed"]
    (row,) = _retreats(tmp_path)
    assert row["action"] == "parked" and row["park_code"] == PARK_UNRECOVERABLE
    notice = res.notice
    assert notice is not None
    assert notice["reason"] == PARK_UNRECOVERABLE and notice["path"] == str(slot)
    assert "quarantine root" in str(notice["detail"])


def test_ledger_write_failure_is_never_released_as_saved(tmp_path: Path, origin: Path) -> None:
    slot = _borrow(tmp_path, origin)
    ledger_dir = tmp_path / "state" / CONTINUATION_LEDGER_FILENAME
    ledger_dir.mkdir(parents=True)  # a directory: every append fails
    res = _finish(tmp_path).finish(slot="slot-01", thread_id="T-a", turn_id="t1")
    assert res.outcome is FinishOutcome.QUARANTINED
    assert res.save is not None and "ledger write failed" in (res.save.detail or "")
    assert not slot.exists()


@pytest.mark.parametrize(
    ("thread", "turn"), [("T-a", "other-turn"), ("T-other", "t1")], ids=["turn", "thread"]
)
def test_lease_not_this_turns_is_refused_untouched(
    tmp_path: Path, origin: Path, thread: str, turn: str
) -> None:
    slot = _borrow(tmp_path, origin)
    (slot / "wip.txt").write_text("wip\n", encoding="utf-8")
    res = _finish(tmp_path).finish(slot="slot-01", thread_id=thread, turn_id=turn)
    assert res.outcome is FinishOutcome.REFUSED
    assert (slot / "wip.txt").is_file()
    assert _lease_state(tmp_path) is LeaseState.LAUNCHING
    assert not (tmp_path / "state" / CONTINUATION_LEDGER_FILENAME).exists()


def test_quarantine_failed_lease_is_refused(tmp_path: Path, origin: Path) -> None:
    _borrow(tmp_path, origin)
    _store(tmp_path).mark_quarantine_failed(REPO, "slot-01", "t1")
    res = _finish(tmp_path).finish(slot="slot-01", thread_id="T-a", turn_id="t1")
    assert res.outcome is FinishOutcome.REFUSED


def _finish_argv(tmp_path: Path, turn: str = "t1") -> list[str]:
    return [
        "slot-finish",
        "--slots-root", str(tmp_path / "slots"),
        "--repo", REPO,
        "--project", "proj",
        "--state-dir", str(tmp_path / "state"),
        "--slot", "slot-01",
        "--thread-id", "T-a",
        "--turn-id", turn,
    ]  # fmt: skip


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    out = capsys.readouterr().out
    (line,) = [ln for ln in out.splitlines() if ln.startswith("MINDWIRE_SLOT_FINISH ")]
    return int(exc.value.code or 0), json.loads(line.removeprefix("MINDWIRE_SLOT_FINISH "))


def test_cli_released_exit_0(
    tmp_path: Path, origin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _borrow(tmp_path, origin)
    capsys.readouterr()
    code, payload = _run(_finish_argv(tmp_path), capsys)
    assert code == 0
    assert payload["outcome"] == "released" and payload["turn_outcome"] == "pushed"
    assert payload["notice"] is None


def test_cli_quarantined_exit_5_carries_notice(
    tmp_path: Path, origin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    slot = _borrow(tmp_path, origin)
    (slot / "wip.txt").write_text("wip\n", encoding="utf-8")
    _break_origin(slot, tmp_path)
    capsys.readouterr()
    code, payload = _run(_finish_argv(tmp_path), capsys)
    assert code == 5
    notice = payload["notice"]
    assert isinstance(notice, dict) and notice["reason"] == "rescue_failed"


def test_cli_refused_exit_2(
    tmp_path: Path, origin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _borrow(tmp_path, origin)
    capsys.readouterr()
    code, payload = _run(_finish_argv(tmp_path, turn="nope"), capsys)
    assert code == 2 and payload["outcome"] == "refused"


# --- A5 D-5 (Bohr msg-9899 §1): the clone guard refused the borrowed slot (conductor exit 8) ---


def _ledger_or_empty(tmp_path: Path) -> list[dict[str, object]]:
    p = tmp_path / "state" / CONTINUATION_LEDGER_FILENAME
    return _ledger(tmp_path) if p.exists() else []


def test_guard_refused_saved_slot_is_quarantined_without_notice(
    tmp_path: Path, origin: Path
) -> None:
    slot = _borrow(tmp_path, origin)
    (slot / "new.txt").write_text("new\n", encoding="utf-8")
    res = _finish(tmp_path).finish(
        slot="slot-01", thread_id="T-a", turn_id="t1", guard_reason="dirty_tree"
    )
    assert res.outcome is FinishOutcome.QUARANTINED, res.detail
    assert res.save is not None and res.save.saved
    assert res.notice is None  # saved: nobody needs to act
    assert not slot.exists() and res.quarantine_path is not None
    assert (res.quarantine_path / "new.txt").is_file()  # renamed, never deleted
    assert [r["outcome"] for r in _ledger(tmp_path)] == ["rescued"]
    (row,) = _retreats(tmp_path)
    assert row["action"] == "rebuilt" and row["quarantine_reason"] == "untracked_only"
    assert "clone guard refused (dirty_tree)" in str(row["detail"])
    assert _lease_state(tmp_path) is None  # returned: the next borrow provisions anew


def test_guard_refused_pushed_slot_writes_no_turn_row(tmp_path: Path, origin: Path) -> None:
    # The guard may have refused before any role ran: a `pushed` row for that unused borrow would
    # let a refused CONTINUE through (PR #598 gate Obj-1-1), so none is written (fail closed).
    slot = _borrow(tmp_path, origin)
    res = _finish(tmp_path).finish(
        slot="slot-01", thread_id="T-a", turn_id="t1", guard_reason="clone_busy"
    )
    assert res.outcome is FinishOutcome.QUARANTINED, res.detail
    assert res.notice is None
    assert _ledger_or_empty(tmp_path) == []
    (row,) = _retreats(tmp_path)
    assert row["quarantine_reason"] == "index_lock"
    assert not slot.exists()


def test_guard_refused_unsaved_slot_is_quarantined_with_notice(
    tmp_path: Path, origin: Path
) -> None:
    slot = _borrow(tmp_path, origin)
    (slot / "a.txt").write_text("changed\n", encoding="utf-8")
    _break_origin(slot, tmp_path)
    res = _finish(tmp_path).finish(
        slot="slot-01", thread_id="T-a", turn_id="t1", guard_reason="dirty_tree"
    )
    assert res.outcome is FinishOutcome.QUARANTINED, res.detail
    assert [r["outcome"] for r in _ledger(tmp_path)] == ["rescue_failed"]
    (row,) = _retreats(tmp_path)
    assert row["quarantine_reason"] == "modified" and row["recoverable"] is False
    notice = res.notice
    assert notice is not None and notice["path"] == str(res.quarantine_path)


def test_guard_refused_rename_failure_parks_this_thread_only(tmp_path: Path, origin: Path) -> None:
    slot = _borrow(tmp_path, origin)
    (slot / "new.txt").write_text("new\n", encoding="utf-8")

    def refuse(src: Path, dst: Path) -> None:
        raise PermissionError("in use")

    res = _finish(tmp_path, rename=refuse).finish(
        slot="slot-01", thread_id="T-a", turn_id="t1", guard_reason="dirty_tree"
    )
    assert res.outcome is FinishOutcome.PARKED and res.park_code == PARK_UNRECOVERABLE
    assert _lease_state(tmp_path) is LeaseState.QUARANTINE_FAILED
    assert slot.is_dir()
    notice = res.notice
    assert notice is not None and notice["path"] == str(slot)


def test_after_guard_refusal_another_thread_borrows_a_fresh_slot(
    tmp_path: Path, origin: Path
) -> None:
    slot = _borrow(tmp_path, origin)
    (slot / "new.txt").write_text("new\n", encoding="utf-8")
    _finish(tmp_path).finish(
        slot="slot-01", thread_id="T-a", turn_id="t1", guard_reason="dirty_tree"
    )
    again = _borrow(tmp_path, origin, thread="T-b", turn="t2")  # asserts READY
    assert again == slot and not (again / "new.txt").exists()


def test_cli_guard_reason_quarantines_with_null_notice(
    tmp_path: Path, origin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    slot = _borrow(tmp_path, origin)
    (slot / "new.txt").write_text("new\n", encoding="utf-8")
    capsys.readouterr()
    code, payload = _run([*_finish_argv(tmp_path), "--guard-reason", "dirty_tree"], capsys)
    assert code == 5
    assert payload["outcome"] == "quarantined" and payload["turn_outcome"] == "rescued"
    assert payload["notice"] is None and payload["quarantine_reason"] == "untracked_only"


@pytest.mark.parametrize(
    ("guard", "expected"),
    [
        ("op_in_progress", "merge_in_progress"),
        ("clone_busy", "index_lock"),
        ("wrong_head", "wrong_head"),
        ("pin_not_ignored", "modified"),
        ("git_unavailable", "provision_failed"),
        ("unknown", "modified"),
    ],
)
def test_refusal_reason_maps_guard_reasons(tmp_path: Path, guard: str, expected: str) -> None:
    assert refusal_reason(tmp_path, guard) == expected


# --- A5 D-6 (Bohr msg-9901 §1): the quarantine is exported before the rename ---


def test_turn_end_quarantine_exports_and_writes_a_manifest(tmp_path: Path, origin: Path) -> None:
    slot = _borrow(tmp_path, origin)
    (slot / "new.bin").write_bytes(b"\x00\x01only here")
    _break_origin(slot, tmp_path)
    exports = tmp_path / "state" / "slot-exports"
    res = _finish(tmp_path, exports_root=exports).finish(
        slot="slot-01", thread_id="T-a", turn_id="t1"
    )
    assert res.outcome is FinishOutcome.QUARANTINED, res.detail
    (mp,) = list((exports / REPO).glob("*.manifest.json"))
    m = json.loads(mp.read_text(encoding="utf-8"))
    assert m["quarantine_path"] == str(res.quarantine_path) and m["complete"] is True
    (row,) = _retreats(tmp_path)
    assert row["patch"] and row["untracked"] and str(row["untracked"]).endswith(".untracked.tar")


def test_turn_end_export_past_its_deadline_still_quarantines(tmp_path: Path, origin: Path) -> None:
    # #600 gate Obj-4: the export has one deadline inside the wrapper's slot-finish bound; past
    # it the export stops (complete false, kept by the GC) and the rename and lease release run.
    slot = _borrow(tmp_path, origin)
    (slot / "new.bin").write_bytes(b"\x00\x01only here")
    _break_origin(slot, tmp_path)
    exports = tmp_path / "state" / "slot-exports"
    t = [0.0]

    def mono() -> float:
        return t[0]

    fin = _finish(tmp_path, exports_root=exports, monotonic=mono)
    t[0] = 1000.0  # the rescue used up the whole allowance before the export starts
    res = fin.finish(slot="slot-01", thread_id="T-a", turn_id="t1")
    assert res.outcome is FinishOutcome.QUARANTINED, res.detail
    assert res.quarantine_path is not None and res.quarantine_path.is_dir()
    (mp,) = list((exports / REPO).glob("*.manifest.json"))
    m = json.loads(mp.read_text(encoding="utf-8"))
    assert m["complete"] is False
    assert any(r.startswith("export_timeout") for r in m["incomplete_reasons"])
    assert _lease_state(tmp_path) is None  # released: the next borrow provisions anew
