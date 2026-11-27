"""Borrow-time slot procedure (T-ephemeral-impl-clone-slot-per-thread A2: Bohr msg-9386 D-1 改訂
2-7, msg-9391 Obj-33-2, msg-9393 Obj-34-2, msg-9402 Obj-36-1 §3, msg-9404 Obj-37-1 §4).

Real git: a bare ``origin`` stands in for GitHub; the slot pool lives under ``tmp_path/slots``.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import make_guard_hooks

from spirrow_mindwire.clone_retreat import git_env
from spirrow_mindwire.clone_slot import (
    PARK_PROVISION_FAILED,
    PARK_UNRECOVERABLE,
    PARK_WORKSPACE_UNTRUSTED,
    mirror_path,
    quarantine_root,
)
from spirrow_mindwire.clone_slot_lease import LeaseState, LeaseStore
from spirrow_mindwire.clone_slot_prep import PrepOutcome, PrepResult, SlotPrep, judge_recoverable
from spirrow_mindwire.conductor.continuation_ledger import SlotTurnOutcome

REPO = "sample"
NOW = datetime(2026, 11, 19, 2, 3, 4, tzinfo=UTC)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True, env=git_env()
    ).stdout


def _cfg(repo: Path) -> None:
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _cfg(seed)
    (seed / "a.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "branch", "feature/other")
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True, capture_output=True
    )
    return bare


def _prep(tmp_path: Path, origin: Path, *, n: int = 1, **kw: object) -> SlotPrep:
    kw.setdefault("hooks_path", str(make_guard_hooks(tmp_path / "guard-hooks")))
    return SlotPrep(
        slots_root=tmp_path / "slots",
        repo=REPO,
        origin_url=str(origin),
        store=LeaseStore(tmp_path / "state", alive=lambda pid: False),
        slots_per_repo=n,
        clock=lambda: NOW,
        **kw,  # type: ignore[arg-type]
    )


def _borrow(prep: SlotPrep, thread: str, turn: str, branch: str = "feature/T-a") -> PrepResult:
    return prep.borrow(thread_id=thread, turn_id=turn, branch=branch, base="main", wrapper_pid=1)


def test_first_borrow_creates_mirror_and_slot_on_own_branch(tmp_path: Path, origin: Path) -> None:
    prep = _prep(tmp_path, origin)
    res = _borrow(prep, "T-a", "t1")
    assert res.outcome is PrepOutcome.READY, res.detail
    assert res.provisioned and res.clone_seconds is not None
    assert (mirror_path(tmp_path / "slots", REPO) / "HEAD").is_file()
    slot = res.slot.path  # type: ignore[union-attr]
    assert slot == tmp_path / "slots" / REPO / "slot-01"
    assert _git(slot, "branch", "--show-current").strip() == "feature/T-a"
    # A8: no mirror remote (P2 refuses its local URL); its refs stay under refs/remotes/mirror/.
    remotes = _git(slot, "remote").split()
    assert remotes == ["origin"]
    assert _git(slot, "rev-parse", "--verify", "refs/remotes/mirror/main").strip()
    assert res.lease is not None and res.lease.state is LeaseState.LAUNCHING
    assert res.to_json()["outcome"] == "ready"


def test_existing_branch_starts_from_mirror_tip(tmp_path: Path, origin: Path) -> None:
    res = _borrow(_prep(tmp_path, origin), "T-o", "t1", branch="feature/other")
    assert res.outcome is PrepOutcome.READY
    slot = res.slot.path  # type: ignore[union-attr]
    assert _git(slot, "rev-parse", "HEAD") == _git(origin, "rev-parse", "feature/other")


def test_clean_returned_slot_is_reused_and_other_branches_deleted(
    tmp_path: Path, origin: Path
) -> None:
    prep = _prep(tmp_path, origin)
    first = _borrow(prep, "T-a", "t1", branch="feature/other")
    prep.store.release(REPO, "slot-01", "t1")
    second = _borrow(prep, "T-b", "t2", branch="feature/T-b")
    assert second.outcome is PrepOutcome.READY, second.detail
    assert not second.provisioned and second.quarantine is None
    slot = second.slot.path  # type: ignore[union-attr]
    assert first.slot.path == slot  # type: ignore[union-attr]
    assert "feature/other" in second.deleted_branches
    assert _git(slot, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split() == [
        "feature/T-b"
    ]


def test_unsaved_branch_of_previous_borrower_is_kept_until_rescued(
    tmp_path: Path, origin: Path
) -> None:
    prep = _prep(tmp_path, origin)
    first = _borrow(prep, "T-a", "t1")
    slot = first.slot.path  # type: ignore[union-attr]
    _cfg(slot)
    (slot / "b.txt").write_text("b\n", encoding="utf-8")
    _git(slot, "add", "b.txt")
    _git(slot, "commit", "-q", "-m", "local only")
    sha = _git(slot, "rev-parse", "HEAD").strip()
    prep.store.release(REPO, "slot-01", "t1")

    res = _borrow(prep, "T-b", "t2", branch="feature/T-b")
    assert res.outcome is PrepOutcome.READY, res.detail
    q = res.quarantine
    assert q is not None and q.reason == "unpublished" and q.previous_thread == "T-a"
    assert q.rescue is not None and q.rescue.outcome is SlotTurnOutcome.RESCUED
    assert q.recoverable and q.recoverable_reason is None
    assert q.quarantine_path is not None and q.quarantine_path.parent == quarantine_root(
        tmp_path / "slots", REPO
    )
    assert q.quarantine_path.name == "slot-01-T-a-20261119-020304"
    assert (q.quarantine_path / "b.txt").is_file()
    assert _git(origin, "rev-parse", "refs/heads/rescue/T-a/t2/01").strip() == sha
    assert res.provisioned
    assert _git(res.slot.path, "branch", "--show-current").strip() == "feature/T-b"  # type: ignore[union-attr]


def test_dirty_slot_is_classified_and_rescued(tmp_path: Path, origin: Path) -> None:
    prep = _prep(tmp_path, origin)
    slot = _borrow(prep, "T-a", "t1").slot.path  # type: ignore[union-attr]
    (slot / "new.txt").write_text("wip\n", encoding="utf-8")
    prep.store.release(REPO, "slot-01", "t1")
    res = _borrow(prep, "T-b", "t2")
    assert res.quarantine is not None and res.quarantine.reason == "untracked_only"
    assert res.quarantine.rescue is not None
    assert res.quarantine.rescue.outcome is SlotTurnOutcome.RESCUED


def test_operation_in_progress_and_index_lock_are_quarantined(tmp_path: Path, origin: Path) -> None:
    prep = _prep(tmp_path, origin)
    slot = _borrow(prep, "T-a", "t1").slot.path  # type: ignore[union-attr]
    (slot / ".git" / "index.lock").write_text("", encoding="utf-8")
    prep.store.release(REPO, "slot-01", "t1")
    res = _borrow(prep, "T-b", "t2")
    assert res.quarantine is not None and res.quarantine.reason == "index_lock"
    assert not res.quarantine.recoverable
    assert res.outcome is PrepOutcome.READY

    slot = res.slot.path  # type: ignore[union-attr]
    head = _git(slot, "rev-parse", "HEAD").strip()
    (slot / ".git" / "MERGE_HEAD").write_text(head + "\n", encoding="utf-8")
    prep.store.release(REPO, "slot-01", "t2")
    res2 = _borrow(prep, "T-c", "t3")
    assert res2.quarantine is not None and res2.quarantine.reason == "merge_in_progress"


def test_not_a_repository_is_quarantined_unrecoverable(tmp_path: Path, origin: Path) -> None:
    prep = _prep(tmp_path, origin)
    slot = tmp_path / "slots" / REPO / "slot-01"
    slot.mkdir(parents=True)
    (slot / "junk").write_text("x", encoding="utf-8")
    res = _borrow(prep, "T-a", "t1")
    assert res.outcome is PrepOutcome.READY, res.detail
    q = res.quarantine
    assert q is not None and q.reason == "provision_failed" and q.rescue is None
    assert not q.recoverable and q.previous_thread == "unknown-borrower"


def test_failed_rescue_still_quarantines_and_rebuilds(tmp_path: Path, origin: Path) -> None:
    prep = _prep(tmp_path, origin)
    slot = _borrow(prep, "T-a", "t1").slot.path  # type: ignore[union-attr]
    (slot / "new.txt").write_text("wip\n", encoding="utf-8")
    _git(slot, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    prep.store.release(REPO, "slot-01", "t1")
    res = _borrow(prep, "T-b", "t2")
    assert res.outcome is PrepOutcome.READY
    q = res.quarantine
    assert q is not None and q.rescue is not None
    assert q.rescue.outcome is SlotTurnOutcome.RESCUE_FAILED
    assert not q.recoverable and q.recoverable_reason == "rescue_failed"
    assert q.quarantine_path is not None and (q.quarantine_path / "new.txt").is_file()


def test_rename_failure_retries_then_marks_quarantine_failed(tmp_path: Path, origin: Path) -> None:
    calls: list[Path] = []

    def refuse(src: Path, dst: Path) -> None:
        calls.append(dst)
        raise PermissionError("handle open")

    prep = _prep(tmp_path, origin, rename=refuse)
    slot = _borrow(prep, "T-a", "t1").slot.path  # type: ignore[union-attr]
    (slot / "new.txt").write_text("wip\n", encoding="utf-8")
    prep.store.release(REPO, "slot-01", "t1")
    res = _borrow(prep, "T-b", "t2")
    assert res.outcome is PrepOutcome.PARKED and res.park_code == PARK_UNRECOVERABLE
    assert [p.name.rsplit("-", 1)[-1] for p in calls] == ["020304", "retry1", "retry2"]
    [lease] = prep.store.leases(REPO)
    assert lease.state is LeaseState.QUARANTINE_FAILED
    again = _borrow(prep, "T-c", "t3")
    assert again.outcome is PrepOutcome.WAITING
    assert [k[1].reason for k in again.kept] == ["quarantine_failed"]


def test_no_free_slot_waits_and_reports_kept_leases(tmp_path: Path, origin: Path) -> None:
    prep = _prep(tmp_path, origin)
    assert _borrow(prep, "T-a", "t1").outcome is PrepOutcome.READY
    res = _borrow(prep, "T-b", "t2")
    assert res.outcome is PrepOutcome.WAITING and res.park_code is None
    assert res.to_json()["waiting_reason"] == "no_free_slot"
    assert [k[1].reason for k in res.kept] == ["job_containment_unverified"]


def test_untrusted_slot_parks_this_turn_and_returns_the_lease(tmp_path: Path, origin: Path) -> None:
    prep = _prep(tmp_path, origin, trusted=lambda p: False)
    res = _borrow(prep, "T-a", "t1")
    assert res.outcome is PrepOutcome.PARKED and res.park_code == PARK_WORKSPACE_UNTRUSTED
    assert str(res.slot.path) in res.detail  # type: ignore[union-attr]
    assert prep.store.leases(REPO) == []


def test_unreachable_origin_without_mirror_parks_provision_failed(tmp_path: Path) -> None:
    prep = _prep(tmp_path, tmp_path / "missing.git")
    res = _borrow(prep, "T-a", "t1")
    assert res.outcome is PrepOutcome.PARKED and res.park_code == PARK_PROVISION_FAILED
    assert prep.store.leases(REPO) == []


def _with_gitlink(slot: Path, path: str, declared: bool) -> None:
    sha = _git(slot, "rev-parse", "HEAD").strip()
    _git(slot, "update-index", "--add", "--cacheinfo", f"160000,{sha},{path}")
    if declared:
        (slot / ".gitmodules").write_text(
            f'[submodule "{path}"]\n\tpath = {path}\n\turl = ./x\n', encoding="utf-8"
        )
        _git(slot, "add", ".gitmodules")
    _git(slot, "commit", "-q", "-m", "gitlink")


def test_recoverable_true_without_gitlinks(tmp_path: Path, origin: Path) -> None:
    slot = _borrow(_prep(tmp_path, origin), "T-a", "t1").slot.path  # type: ignore[union-attr]
    assert judge_recoverable(slot) == (True, None)


def test_undeclared_gitlink_is_embedded_repo(tmp_path: Path, origin: Path) -> None:
    slot = _borrow(_prep(tmp_path, origin), "T-a", "t1").slot.path  # type: ignore[union-attr]
    _cfg(slot)
    _with_gitlink(slot, "vendor/x", declared=False)
    assert judge_recoverable(slot) == (False, "embedded_repo")


def test_declared_submodule_that_cannot_be_fetched_is_unverified(
    tmp_path: Path, origin: Path
) -> None:
    slot = _borrow(_prep(tmp_path, origin), "T-a", "t1").slot.path  # type: ignore[union-attr]
    _cfg(slot)
    _with_gitlink(slot, "vendor/x", declared=True)
    assert judge_recoverable(slot) == (False, "submodule_unverified")


def test_dirty_status_is_not_recoverable(tmp_path: Path, origin: Path) -> None:
    slot = _borrow(_prep(tmp_path, origin), "T-a", "t1").slot.path  # type: ignore[union-attr]
    (slot / "x").write_text("x", encoding="utf-8")
    assert judge_recoverable(slot) == (False, "status_unverified")


_SUB = ("-c", "protocol.file.allow=always")


def _bare_with_file(tmp_path: Path, name: str) -> Path:
    work = tmp_path / f"{name}-seed"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _cfg(work)
    (work / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", name)
    bare = tmp_path / f"{name}.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True
    )
    return bare


def _bare_with_submodule(tmp_path: Path, name: str, child: Path, path: str) -> Path:
    work = tmp_path / f"{name}-work"
    subprocess.run(["git", "clone", "-q", str(_bare_with_file(tmp_path, name)), str(work)],
                   check=True, capture_output=True)  # fmt: skip
    _cfg(work)
    _git(work, *_SUB, "submodule", "add", "-q", str(child), path)
    _git(work, "commit", "-q", "-m", f"add {path}")
    _git(work, "push", "-q", "origin", "main")
    return tmp_path / f"{name}.git"


def _nested_slot(tmp_path: Path) -> Path:
    """parent -> sub -> nested, each with its own bare origin, cloned recursively."""
    nested = _bare_with_file(tmp_path, "nested")
    sub = _bare_with_submodule(tmp_path, "sub", nested, "nested")
    parent = _bare_with_submodule(tmp_path, "parent", sub, "sub")
    slot = tmp_path / "slot"
    subprocess.run(
        ["git", *_SUB, "clone", "-q", "--recurse-submodules", str(parent), str(slot)],
        check=True,
        capture_output=True,
        env=git_env(),
    )
    for repo in (slot, slot / "sub", slot / "sub" / "nested"):
        _cfg(repo)
    return slot


def test_published_nested_submodules_are_recoverable(tmp_path: Path) -> None:
    slot = _nested_slot(tmp_path)
    assert judge_recoverable(slot) == (True, None)


def test_unpublished_nested_submodule_commit_is_not_recoverable(tmp_path: Path) -> None:
    """PR #589 Obj-1-3: the parent is clean and the direct submodule's commit is on its origin,
    but that commit records a nested commit that exists only in this slot."""
    slot = _nested_slot(tmp_path)
    inner = slot / "sub" / "nested"
    _git(inner, "checkout", "-q", "-b", "local-only")
    (inner / "only-here.txt").write_text("x\n", encoding="utf-8")
    _git(inner, "add", ".")
    _git(inner, "commit", "-q", "-m", "local only")
    sub = slot / "sub"
    _git(sub, "add", "nested")
    _git(sub, "commit", "-q", "-m", "bump nested")
    _git(sub, "push", "-q", "origin", "HEAD:main")
    _git(slot, "add", "sub")
    _git(slot, "commit", "-q", "-m", "bump sub")
    _git(slot, "push", "-q", "origin", "HEAD:main")
    assert _git(slot, "status", "--porcelain", "--ignore-submodules=none") == ""
    assert judge_recoverable(slot) == (False, "submodule_unverified")


# --- A5 D-7 (Bohr msg-9899 §2): the provisioned slot's size ---


def test_provisioned_slot_records_its_size(tmp_path: Path, origin: Path) -> None:
    res = _borrow(_prep(tmp_path, origin), "T-a", "t1")
    assert res.provisioned and isinstance(res.slot_bytes, int) and res.slot_bytes > 0
    assert res.to_json()["slot_bytes"] == res.slot_bytes


def test_reused_slot_has_no_size(tmp_path: Path, origin: Path) -> None:
    prep = _prep(tmp_path, origin)
    first = _borrow(prep, "T-a", "t1")
    prep.store.release(REPO, first.lease.slot, "t1")  # type: ignore[union-attr]
    again = _borrow(prep, "T-b", "t2", branch="feature/T-b")
    assert not again.provisioned and again.slot_bytes is None


def test_measure_failure_is_none_and_never_raises(tmp_path: Path) -> None:
    from spirrow_mindwire.clone_slot_prep import measure_bytes

    assert measure_bytes(tmp_path / "missing") in (0, None)
    (tmp_path / "f").write_bytes(b"abc")
    assert measure_bytes(tmp_path) == 3
