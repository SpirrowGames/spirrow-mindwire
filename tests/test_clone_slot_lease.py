"""Lease records of the pooled clone slots (T-ephemeral-impl-clone-slot-per-thread, A2)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.clone_slot_lease import (
    WAIT_NOTICE_AFTER_S,
    AcquireResult,
    Lease,
    LeaseError,
    LeaseLockTimeoutError,
    LeaseState,
    LeaseStore,
    judge_unreturned,
    pid_alive,
)
from spirrow_mindwire.stall_ledger.lock import LedgerLock

T0 = datetime(2026, 11, 19, 1, 0, tzinfo=UTC)


def _store(
    tmp_path: Path, alive: Callable[[int], bool] = lambda pid: False, **kw: Any
) -> LeaseStore:
    return LeaseStore(tmp_path / "state", alive=alive, **kw)


def _acq(
    store: LeaseStore, thread: str, turn: str, n: int = 2, now: datetime = T0, **kw: Any
) -> AcquireResult:
    return store.acquire("mindwire", n, thread_id=thread, turn_id=turn, now=now, **kw)


def test_acquire_lends_lowest_free_slot_in_launching_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    r = _acq(store, "T-a", "t1", wrapper_pid=11, session_name="s-a", job_name="j-a")
    assert r.lease is not None and r.reason is None
    assert r.lease.slot == "slot-01"
    assert r.lease.state is LeaseState.LAUNCHING
    assert (r.lease.wrapper_pid, r.lease.session_name, r.lease.job_name) == (11, "s-a", "j-a")
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    rec = raw["repos"]["mindwire"]["slots"]["slot-01"]
    for key in ("slot", "thread_id", "turn_id", "acquired_at", "wrapper_pid", "state"):
        assert key in rec
    assert rec["acquired_at"] == "2026-11-19T01:00:00Z"


def test_one_slot_one_turn_and_one_turn_one_slot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = _acq(store, "T-a", "t1").lease
    b = _acq(store, "T-b", "t2").lease
    assert a is not None and b is not None and a.slot != b.slot
    again = _acq(store, "T-a", "t3")
    assert again.lease is None and again.reason == "thread_holds_lease"


def test_no_free_slot_waits_without_parking_and_notices_once_after_30_min(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _acq(store, "T-a", "t1", n=1)
    first = _acq(store, "T-b", "t2", n=1)
    assert first.lease is None and first.reason == "no_free_slot" and not first.notice
    at_limit = _acq(store, "T-b", "t3", n=1, now=T0 + timedelta(seconds=WAIT_NOTICE_AFTER_S))
    assert not at_limit.notice
    later = T0 + timedelta(seconds=WAIT_NOTICE_AFTER_S + 1)
    over = _acq(store, "T-b", "t4", n=1, now=later)
    assert over.notice and over.waiting_since == "2026-11-19T01:00:00Z"
    assert not _acq(store, "T-b", "t5", n=1, now=later + timedelta(hours=1)).notice


def test_waiting_entry_cleared_on_acquire(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _acq(store, "T-a", "t1", n=1)
    _acq(store, "T-b", "t2", n=1)
    store.release("mindwire", "slot-01", "t1")
    assert _acq(store, "T-b", "t3", n=1).lease is not None
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["repos"]["mindwire"]["waiting"] == {}


def test_release_prefers_slot_the_same_thread_used_last(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _acq(store, "T-a", "t1")
    _acq(store, "T-b", "t2")
    store.release("mindwire", "slot-01", "t1")
    store.release("mindwire", "slot-02", "t2")
    r = _acq(store, "T-b", "t3")
    assert r.lease is not None and r.lease.slot == "slot-02"
    r2 = _acq(store, "T-c", "t4")
    assert r2.lease is not None and r2.lease.slot == "slot-01"


def test_spawning_marker_then_running(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lease = _acq(store, "T-a", "t1").lease
    assert lease is not None
    s = store.mark_spawning("mindwire", lease.slot, "t1")
    assert s.spawning and s.state is LeaseState.LAUNCHING
    r = store.mark_running("mindwire", lease.slot, "t1", child_pid=99)
    assert r.state is LeaseState.RUNNING and not r.spawning and r.child_pid == 99


def test_transitions_refuse_another_turn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _acq(store, "T-a", "t1")
    with pytest.raises(LeaseError):
        store.mark_running("mindwire", "slot-01", "other", child_pid=1)
    with pytest.raises(LeaseError):
        store.release("mindwire", "slot-01", "other")
    with pytest.raises(LeaseError):
        store.release("mindwire", "slot-02", "t1")


def test_quarantine_failed_is_never_returned_nor_lent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _acq(store, "T-a", "t1", n=1)
    q = store.mark_quarantine_failed("mindwire", "slot-01", "t1")
    assert q.state is LeaseState.QUARANTINE_FAILED
    with pytest.raises(LeaseError):
        store.release("mindwire", "slot-01", "t1")
    with pytest.raises(LeaseError):
        store.mark_spawning("mindwire", "slot-01", "t1")
    r = _acq(store, "T-b", "t2", n=1)
    assert r.lease is None and r.reason == "no_free_slot"
    [(lease, verdict)] = store.unreturned("mindwire")
    assert lease.slot == "slot-01" and verdict.reason == "quarantine_failed"
    assert not verdict.reclaim


def _lease(**kw: Any) -> Lease:
    base: dict[str, object] = {
        "repo": "mindwire",
        "slot": "slot-01",
        "thread_id": "T-a",
        "turn_id": "t1",
        "acquired_at": "2026-11-19T01:00:00Z",
        "state": LeaseState.RUNNING,
        "wrapper_pid": 42,
        "child_pid": 43,
    }
    base.update(kw)
    return Lease(**base)  # type: ignore[arg-type]


def test_unreturned_lease_with_live_wrapper_is_in_use() -> None:
    v = judge_unreturned(_lease(), alive=lambda pid: pid == 42)
    assert v.in_use and not v.reclaim


@pytest.mark.parametrize(
    "lease",
    [
        _lease(),
        _lease(state=LeaseState.LAUNCHING),
        _lease(spawning=True),
        _lease(wrapper_pid=None),
    ],
)
def test_crashed_wrapper_lease_is_kept_job_containment_unverified(lease: Lease) -> None:
    """msg-9395 Obj-35-1 fix_cut: no automatic reclaim; keep the lease and notice."""
    v = judge_unreturned(lease, alive=lambda pid: False)
    assert not v.in_use and not v.reclaim
    assert v.reason == "job_containment_unverified"


def test_unreturned_lists_only_dead_wrapper_leases(tmp_path: Path) -> None:
    store = _store(tmp_path, alive=lambda pid: pid == 1)
    _acq(store, "T-a", "t1", wrapper_pid=1)
    _acq(store, "T-b", "t2", wrapper_pid=2)
    [(lease, v)] = store.unreturned("mindwire")
    assert lease.thread_id == "T-b" and v.reason == "job_containment_unverified"


def test_repos_are_independent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.acquire("magickit", 1, thread_id="T-a", turn_id="t1", now=T0).lease is not None
    assert store.acquire("mindwire", 1, thread_id="T-b", turn_id="t2", now=T0).lease is not None


_HOLDER = """
import sys, time
from pathlib import Path
from spirrow_mindwire.stall_ledger.lock import LedgerLock
lock = LedgerLock(Path(sys.argv[1]))
assert lock.acquire({"pid": 0})
print("held", flush=True)
time.sleep(60)
"""


def _hold_lock_in_child(lock_path: Path) -> subprocess.Popen[str]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock_path)], stdout=subprocess.PIPE, text=True
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "held"
    return child


def test_lock_held_by_another_process_times_out(tmp_path: Path) -> None:
    store = _store(tmp_path, lock_wait_s=0.2)
    child = _hold_lock_in_child(store.lock_path)
    try:
        with pytest.raises(LeaseLockTimeoutError):
            _acq(store, "T-a", "t1")
        assert not store.path.exists()
    finally:
        child.kill()
        child.wait()


def test_lock_of_killed_process_is_released_by_the_os(tmp_path: Path) -> None:
    """A holder killed mid-lock leaves the lock file behind; the OS has released its lock, so the
    next contender gets it with no pid check and no takeover."""
    store = _store(tmp_path, lock_wait_s=5.0)
    child = _hold_lock_in_child(store.lock_path)
    child.kill()
    child.wait()
    assert store.lock_path.exists()
    assert _acq(store, "T-a", "t1").lease is not None


def test_stale_lock_text_is_never_deleted_or_trusted(tmp_path: Path) -> None:
    """The old pid-in-file takeover is gone: a lock file holding a dead pid is just a file."""
    store = _store(tmp_path, alive=lambda pid: False, lock_wait_s=0.2)
    store.lock_path.parent.mkdir(parents=True)
    store.lock_path.write_text("4242", encoding="ascii")
    assert _acq(store, "T-a", "t1").lease is not None
    assert store.lock_path.exists()


_CONTENDER = """
import json, sys
from datetime import UTC, datetime
from pathlib import Path
from spirrow_mindwire.clone_slot_lease import LeaseStore
state, thread, go = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
while not go.exists():
    pass
r = LeaseStore(state, lock_wait_s=30.0).acquire(
    "mindwire", 8, thread_id=thread, turn_id="t-" + thread,
    now=datetime(2026, 11, 19, 1, 0, tzinfo=UTC),
)
print(json.dumps(r.lease.slot if r.lease else None))
"""


def test_concurrent_contenders_after_a_killed_holder_get_distinct_slots(tmp_path: Path) -> None:
    """Obj-1-1: contenders that all arrive while a dead holder's lock file is present must not
    share a slot. Every contender is a separate process; the holder is killed, then all go."""
    state = tmp_path / "state"
    store = LeaseStore(state)
    holder = _hold_lock_in_child(store.lock_path)
    go = tmp_path / "go"
    kids = [
        subprocess.Popen(
            [sys.executable, "-c", _CONTENDER, str(state), f"T-{i}", str(go)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for i in range(6)
    ]
    holder.kill()
    holder.wait()
    go.write_text("go", encoding="ascii")
    slots = [json.loads(k.communicate(timeout=60)[0]) for k in kids]
    assert all(k.returncode == 0 for k in kids)
    assert None not in slots and len(set(slots)) == 6
    held = {lease.slot: lease.thread_id for lease in store.leases("mindwire")}
    assert len(held) == 6 and set(held) == set(slots)


@pytest.mark.parametrize(
    "content",
    ["not json", json.dumps({"version": 2, "repos": {}}), json.dumps({"version": 1, "repos": []})],
)
def test_unreadable_file_fails_loud_and_is_not_overwritten(tmp_path: Path, content: str) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(content, encoding="utf-8")
    with pytest.raises(LeaseError):
        _acq(store, "T-a", "t1")
    assert store.path.read_text(encoding="utf-8") == content
    assert LedgerLock(store.lock_path).acquire({"pid": 0}), "the lock was released"


def test_malformed_lease_record_fails_loud(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True)
    bad = {"slot": "slot-01", "thread_id": "T-x", "turn_id": "t", "acquired_at": "x", "state": "zz"}
    store.path.write_text(
        json.dumps({"version": 1, "repos": {"mindwire": {"slots": {"slot-01": bad}}}}),
        encoding="utf-8",
    )
    with pytest.raises(LeaseError):
        _acq(store, "T-a", "t1")


def test_pid_alive_on_this_process_and_nonsense() -> None:
    assert pid_alive(os.getpid())
    assert not pid_alive(0)
    assert not pid_alive(-5)
