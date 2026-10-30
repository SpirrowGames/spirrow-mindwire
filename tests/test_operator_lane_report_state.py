# ruff: noqa: RUF001 - Japanese report text uses fullwidth punctuation on purpose
"""T-operator-task-lane PR2: the report format (D3, D2⁵ - D2⁷) and the state files (D3'' - D3⁸).

The mutex / lock tests run on the real filesystem (D3⁸: "モックにはしません"):
``msvcrt.locking`` on Windows, ``flock`` elsewhere, and real processes where one has to die.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spirrow_mindwire.operator_lane import report as rpt
from spirrow_mindwire.operator_lane import state as st

RUN = "run0001"
NOW = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)


def good_report(run_id: str = RUN, next_line: str = "NEXT: Bohr") -> str:
    return (
        f"{rpt.header(run_id, 'msg-1', 'do the thing')}\n"
        f"{rpt.marker(run_id, rpt.KIND_REPORT)}\n"
        "### 測った事実\n- x\n### 実行したこと\n- y\n### 残っていること\n- なし\n"
        f"{next_line}\n"
    )


class Probe:
    def __init__(self, procs: dict[int, datetime] | None = None) -> None:
        self.procs = dict(procs or {})

    def started_at(self, pid: int) -> datetime | None:
        return self.procs.get(pid)


# --- report -----------------------------------------------------------------------------------


def test_valid_report_passes() -> None:
    assert rpt.validate_report(good_report(), run_id=RUN, requester="Bohr") == []


def test_report_requires_every_section() -> None:
    text = good_report().replace("### 実行したこと\n", "")
    assert any("実行したこと" in p for p in rpt.validate_report(text, run_id=RUN, requester="Bohr"))


def test_report_requires_its_own_marker() -> None:
    text = good_report().replace(rpt.marker(RUN, "report"), rpt.marker("other", "report"))
    problems = rpt.validate_report(text, run_id=RUN, requester="Bohr")
    assert any("missing marker" in p for p in problems)
    assert any("another run" in p for p in problems)


def test_report_with_truncated_next_line_fails() -> None:
    """D2⁷: a report cut before its final ``NEXT:`` must never validate (and so never be posted)."""
    text = good_report().rsplit("NEXT:", 1)[0]
    assert "last line is not a NEXT: line" in rpt.validate_report(
        text, run_id=RUN, requester="Bohr"
    )


def test_report_next_must_be_requester_human_or_operator() -> None:
    text = good_report(next_line="NEXT: Einstein")
    assert rpt.validate_report(text, run_id=RUN, requester="Bohr")
    assert (
        rpt.validate_report(good_report(next_line="NEXT: human"), run_id=RUN, requester="Bohr")
        == []
    )


def test_operator_report_needs_three_line_form_and_lane_line() -> None:
    bare = good_report(next_line="NEXT: operator")
    problems = rpt.validate_report(bare, run_id=RUN, requester="Bohr")
    assert any("3-line form" in p for p in problems)
    assert any("OPERATOR-LANE" in p for p in problems)
    ok = good_report(
        next_line="OPERATOR-TASK: 再ログオンする\nTIER-C-CHECK: none\nNEXT: operator"
    ).replace("### 測った事実", "OPERATOR-LANE: needs-hands 再ログオン\n### 測った事実")
    assert rpt.validate_report(ok, run_id=RUN, requester="Bohr") == []


def test_quarantine_report_validates() -> None:
    text = rpt.render_quarantine(run_id=RUN, head_msg_id="msg-1", operator_task="t", facts=["a"])
    assert rpt.validate_report(text, run_id=RUN, requester="Bohr") == []
    assert rpt.has_lane_line(text)


def test_audit_hit_replaces_the_final_handoff_with_the_operator_form() -> None:
    session = "### 測った事実\n- x\n### 実行したこと\n- y\n### 残っていること\n- なし\nNEXT: Bohr"
    text = rpt.compose_report(
        session, run_id=RUN, head_msg_id="msg-1", operator_task="t", audit_hits=["gh pr merge"]
    )
    assert "OPERATOR-LANE: audit-hit gh pr merge" in text
    assert text.rstrip().endswith("NEXT: operator")
    assert rpt.validate_report(text, run_id=RUN, requester="Bohr") == []


def test_late_report_is_prefixed_once() -> None:
    once = rpt.with_late_report(good_report(), "msg-1", 2)
    assert once.startswith("OPERATOR-LANE: late-report（msg-1 の後に 2 件の投稿あり）")
    assert rpt.with_late_report(once, "msg-1", 3) == once
    assert rpt.with_late_report(good_report(), "msg-1", 0) == good_report()
    assert rpt.validate_report(once, run_id=RUN, requester="Bohr") == []


def test_posted_matches_operator_author_and_run_id_after_the_head() -> None:
    msgs = [
        {"msg_id": "msg-0", "author": "operator", "content": rpt.marker(RUN, "report")},
        {"msg_id": "msg-1", "author": "Bohr", "content": "task"},
        {"msg_id": "msg-2", "author": "Bohr", "content": rpt.marker(RUN, "report")},
    ]
    assert not rpt.posted(msgs, head_msg_id="msg-1", run_id=RUN, operator_author="operator")
    msgs.append({"msg_id": "msg-3", "author": "operator", "content": rpt.marker("x", "report")})
    assert not rpt.posted(msgs, head_msg_id="msg-1", run_id=RUN, operator_author="operator")
    msgs.append({"msg_id": "msg-4", "author": "Operator", "content": rpt.marker(RUN, "quarantine")})
    assert rpt.posted(msgs, head_msg_id="msg-1", run_id=RUN, operator_author="operator")


# --- atomic write -----------------------------------------------------------------------------


def test_atomic_write_leaves_no_partial_target(tmp_path: Path) -> None:
    target = tmp_path / "r.md"
    st.atomic_write_text(target, "whole")
    assert target.read_text(encoding="utf-8") == "whole"
    assert not (tmp_path / "r.md.tmp").exists()


def test_killed_mid_write_leaves_only_tmp(tmp_path: Path) -> None:
    """D2⁷: a runner killed while writing leaves ``.md.tmp`` and no ``.md``."""
    script = textwrap.dedent(
        f"""
        import os, sys, time
        p = {str(tmp_path / "run.md.tmp")!r}
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("## partial")
            fh.flush()
            os.fsync(fh.fileno())
            print("ready", flush=True)
            time.sleep(60)
        os.replace(p, p[:-4])
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    proc.kill()
    proc.wait(10)
    assert (tmp_path / "run.md.tmp").exists()
    assert not (tmp_path / "run.md").exists()


# --- mutex ------------------------------------------------------------------------------------


def _hold_mutex_in_child(path: Path) -> subprocess.Popen[str]:
    script = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        from spirrow_mindwire.operator_lane.state import hold_mutex
        with hold_mutex(Path({str(path)!r})):
            print("held", flush=True)
            time.sleep(120)
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_mutex_is_released_when_its_holder_is_killed(tmp_path: Path) -> None:
    paths = st.LanePaths(tmp_path)
    proc = _hold_mutex_in_child(paths.mutex)
    try:
        with pytest.raises(st.MutexTimeoutError), st.hold_mutex(paths.mutex, timeout=0.3):
            pass
    finally:
        proc.kill()
        proc.wait(10)
    started = time.monotonic()
    with st.hold_mutex(paths.mutex, timeout=5):
        pass
    assert time.monotonic() - started < 2.0
    assert paths.mutex.exists()


def test_mutex_excludes_a_second_handle_in_the_same_process(tmp_path: Path) -> None:
    paths = st.LanePaths(tmp_path)
    with (
        st.hold_mutex(paths.mutex),
        pytest.raises(st.MutexTimeoutError),
        st.hold_mutex(paths.mutex, timeout=0.2),
    ):
        pass


# --- lock -------------------------------------------------------------------------------------


def _target() -> dict[str, str]:
    return {
        "project": "spirrow-mindwire",
        "thread_id": "T-x",
        "head_msg_id": "msg-1",
        "requester": "Bohr",
        "operator_task": "t",
    }


def _spawning(paths: st.LanePaths, now: datetime, wrapper_pid: int = 100) -> dict[str, object]:
    lock = st.conductor_lock(
        run_id=RUN, wrapper_pid=wrapper_pid, wrapper_started_at=NOW, now=now, target=_target()
    )
    with st.hold_mutex(paths.mutex):
        st.create_lock(paths, lock)
    return lock


def test_reclaim_copies_then_deletes_without_sharing_violation(tmp_path: Path) -> None:
    """D3⁸ / msg-7423: reclaim inside the mutex; no WinError 32, a stale copy remains."""
    paths = st.LanePaths(tmp_path)
    _spawning(paths, NOW)
    with st.hold_mutex(paths.mutex):
        copy = st.reclaim_lock(paths, NOW)
    assert not paths.lock.exists()
    assert json.loads(copy.read_text(encoding="utf-8"))["run_id"] == RUN


def test_spawning_lock_past_deadline_is_stale_even_with_a_live_pid() -> None:
    lock = st.conductor_lock(
        run_id=RUN, wrapper_pid=100, wrapper_started_at=NOW, now=NOW, target=_target()
    )
    probe = Probe({100: NOW})
    assert st.stale_reason(lock, NOW + timedelta(seconds=60), probe) is None
    assert st.stale_reason(lock, NOW + timedelta(seconds=121), probe) == "spawn_deadline_passed"


def test_running_lock_with_reused_pid_is_stale() -> None:
    lock = {"holder": "conductor", "state": "running", "pid": 7, "pid_started_at": st.iso(NOW)}
    assert st.stale_reason(lock, NOW, Probe({7: NOW})) is None
    assert st.stale_reason(lock, NOW, Probe({7: NOW + timedelta(minutes=5)})) == "runner_dead"
    assert st.stale_reason(lock, NOW, Probe({})) == "runner_dead"


def test_manual_lease_never_looks_at_a_pid() -> None:
    lock = {"holder": "manual", "lease_id": "L", "expires_at": st.iso(NOW + timedelta(minutes=1))}
    assert st.stale_reason(lock, NOW, Probe({})) is None
    assert st.stale_reason(lock, NOW + timedelta(minutes=2), Probe({})) == "lease_expired"


def test_runner_cas_refused_past_deadline_without_any_reclaim(tmp_path: Path) -> None:
    """D3⁷: the runner judges the deadline itself — no reclaim needs to have happened."""
    paths = st.LanePaths(tmp_path)
    _spawning(paths, NOW)
    with st.hold_mutex(paths.mutex):
        refused = st.runner_cas_to_running(
            paths, run_id=RUN, pid=5, pid_started_at=NOW, now=NOW + timedelta(seconds=121)
        )
        lock = st.read_lock(paths)
    assert refused == "spawn_deadline_passed"
    assert lock is not None and lock["state"] == "spawning"


def _race_once(paths: st.LanePaths, i: int) -> dict[str, object]:
    st.delete_lock(paths)
    _spawning(paths, NOW)
    deadline = NOW + st.SPAWN_DEADLINE
    outcome: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def runner() -> None:
        barrier.wait()
        with st.hold_mutex(paths.mutex):
            outcome["cas"] = st.runner_cas_to_running(
                paths, run_id=RUN, pid=9, pid_started_at=NOW, now=deadline
            )

    def conductor() -> None:
        barrier.wait()
        late = deadline + timedelta(microseconds=1)
        with st.hold_mutex(paths.mutex):
            lock = st.read_lock(paths)
            if lock is not None and st.stale_reason(lock, late, Probe({9: NOW, 100: NOW})):
                st.reclaim_lock(paths, late + timedelta(microseconds=i))
                outcome["reclaimed"] = True

    threads = [threading.Thread(target=runner), threading.Thread(target=conductor)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    return outcome


def test_runner_cas_and_conductor_reclaim_never_both_happen(tmp_path: Path) -> None:
    """D3⁷ / D3⁸: at the deadline boundary exactly one of CAS or reclaim wins, every time."""
    paths = st.LanePaths(tmp_path)
    for i in range(30):
        outcome = _race_once(paths, i)
        cas_won = outcome.get("cas") is None
        reclaimed = bool(outcome.get("reclaimed"))
        assert cas_won != reclaimed, outcome
        if cas_won:
            with st.hold_mutex(paths.mutex):
                lock = st.read_lock(paths)
            assert lock is not None and lock["state"] == "running"
        else:
            assert outcome["cas"] == "lock_missing"


def test_manual_lease_fencing(tmp_path: Path) -> None:
    """D3⁵: after a reclaim, renew and release by the old lease_id fail."""
    paths = st.LanePaths(tmp_path)
    with st.hold_mutex(paths.mutex):
        lock = st.manual_acquire(paths, lease_id="L1", ttl=timedelta(minutes=1), now=NOW, target={})
        assert lock["expires_at"] == st.iso(NOW + timedelta(minutes=1))
        st.manual_renew(paths, lease_id="L1", ttl=timedelta(minutes=5), now=NOW)
        st.reclaim_lock(paths, NOW)
        with pytest.raises(st.LockHeldError):
            st.manual_renew(paths, lease_id="L1", ttl=timedelta(minutes=5), now=NOW)
        with pytest.raises(st.LockHeldError):
            st.manual_release(paths, lease_id="L1")


def test_manual_acquire_refuses_a_conductor_lock(tmp_path: Path) -> None:
    paths = st.LanePaths(tmp_path)
    _spawning(paths, NOW)
    with st.hold_mutex(paths.mutex), pytest.raises(st.LockHeldError, match="lane run"):
        st.manual_acquire(paths, lease_id="L", ttl=st.DEFAULT_LEASE, now=NOW, target={})


def test_corrupt_lock_raises(tmp_path: Path) -> None:
    paths = st.LanePaths(tmp_path)
    paths.state_dir.mkdir(parents=True)
    paths.lock.write_text("{not json", encoding="utf-8")
    with pytest.raises(st.LockCorruptError):
        st.read_lock(paths)


# --- heads ------------------------------------------------------------------------------------


def test_heads_missing_or_corrupt_is_unavailable(tmp_path: Path) -> None:
    paths = st.LanePaths(tmp_path)
    paths.reports.mkdir(parents=True)  # the lane has run here before
    with pytest.raises(st.HeadsUnavailableError):
        st.load_heads(paths)
    assert not st.first_enable(paths)
    paths.heads.write_text("[]", encoding="utf-8")
    with pytest.raises(st.HeadsUnavailableError):
        st.load_heads(paths)


def test_first_enable_only_when_the_lane_never_ran(tmp_path: Path) -> None:
    paths = st.LanePaths(tmp_path)
    assert st.first_enable(paths)
    st.initialise(paths)
    assert st.load_heads(paths) == {}
    assert not st.first_enable(paths)


def test_concurrent_config_disable_never_corrupts_the_file(tmp_path: Path) -> None:
    """PR #505 gate: ``disable()`` has no mutex owner, so two writers racing on it must neither
    share a temp file nor leave a torn config: the file is always whole JSON, lane off."""
    from spirrow_mindwire.operator_lane.config import disable

    cfg = tmp_path / "operator_lane.json"
    cfg.write_text(json.dumps({"enabled": True, "projects": ["p"]}), encoding="utf-8")
    errors: list[BaseException] = []
    now = datetime(2026, 10, 30, tzinfo=UTC)

    def worker(n: int) -> None:
        try:
            for i in range(40):
                disable(cfg, reason=f"guard_open: writer {n} round {i}", now=now)
        except BaseException as exc:  # pragma: no cover - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["enabled"] is False and data["projects"] == ["p"]
    assert data["disabled_reason"].startswith("guard_open: writer ")
    assert list(tmp_path.glob("*.tmp")) == []


def test_unique_tmp_write_does_not_use_the_shared_tmp_name(tmp_path: Path) -> None:
    target = tmp_path / "f.json"
    shared = tmp_path / "f.json.tmp"
    shared.write_text("someone else's partial write", encoding="utf-8")
    st.atomic_write_text(target, "{}\n", unique_tmp=True)
    assert target.read_text(encoding="utf-8") == "{}\n"
    assert shared.read_text(encoding="utf-8") == "someone else's partial write"
