"""Slot save check and rescue (T-ephemeral-impl-clone-slot-per-thread A2: Bohr msg-9400 §1/§3,
msg-9402 Obj-36-1/36-2, msg-9404 Obj-37-1/37-2; Einstein msg-9405 check point).

Real git repositories (a bare ``origin`` and a clone of it) in ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spirrow_mindwire.clone_retreat import git_env
from spirrow_mindwire.clone_slot_save import (
    RescueKind,
    SaveResult,
    SlotSaver,
    record_turn_result,
    rescue_ref,
)
from spirrow_mindwire.conductor.continuation_ledger import (
    SlotTurnOutcome,
    read_ledger,
    summarize,
)

THREAD = "T-sample"
TURN = "turn-0001"


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env=git_env(),
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
    (seed / ".gitignore").write_text(".mindwire/\n", encoding="utf-8")
    (seed / "a.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True, capture_output=True
    )
    return bare


@pytest.fixture
def slot(tmp_path: Path, origin: Path) -> Path:
    c = tmp_path / "slot-01"
    subprocess.run(["git", "clone", "-q", str(origin), str(c)], check=True, capture_output=True)
    _cfg(c)
    (c / ".mindwire").mkdir()
    (c / ".mindwire" / "pin").write_text("schema_version: 1\nmode: bootstrap\n", encoding="utf-8")
    return c


def _commit(repo: Path, name: str, text: str, msg: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


def _remote_refs(origin: Path) -> dict[str, str]:
    out = _git(origin, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/rescue/")
    return dict(line.split(" ", 1) for line in out.splitlines())


def test_rescue_ref_layout_and_validation() -> None:
    assert rescue_ref(THREAD, TURN, 1) == "refs/heads/rescue/T-sample/turn-0001/01"
    assert rescue_ref(THREAD, TURN, 12).endswith("/12")
    for bad in ("a/b", "", ".x", "x.lock", "a..b", "a b", "a:b"):
        with pytest.raises(ValueError):
            rescue_ref(THREAD, bad, 1)
        with pytest.raises(ValueError):
            rescue_ref(bad, TURN, 1)


def test_clean_pushed_slot_is_pushed(slot: Path, origin: Path) -> None:
    _git(slot, "switch", "-q", "-c", "feature/x")
    _commit(slot, "b.txt", "b\n", "b")
    _git(slot, "push", "-q", "origin", "feature/x")
    res = SlotSaver(slot).check_and_rescue(THREAD, TURN)
    assert res.outcome is SlotTurnOutcome.PUSHED and res.saved
    assert _remote_refs(origin) == {}


def test_untracked_work_is_committed_and_rescued(slot: Path, origin: Path) -> None:
    (slot / "new.txt").write_text("wip\n", encoding="utf-8")
    res = SlotSaver(slot).check_and_rescue(THREAD, TURN)
    assert res.outcome is SlotTurnOutcome.RESCUED, res.detail
    assert "dirty" in res.reasons
    [t] = res.targets
    assert (t.kind, t.source, t.nn) == (RescueKind.BRANCH, "main", 1)
    assert _remote_refs(origin) == {t.ref: t.sha}
    assert "new.txt" in _git(origin, "ls-tree", "--name-only", t.sha)
    assert not _git(slot, "status", "--porcelain").strip()


def test_every_unsaved_branch_is_rescued_in_name_order(slot: Path, origin: Path) -> None:
    # A branch named ``stash`` and a nested ``foo/bar`` must not collide with anything (Obj-37-2).
    _git(slot, "switch", "-q", "-c", "stash")
    s1 = _commit(slot, "s.txt", "s\n", "s")
    _git(slot, "switch", "-q", "-c", "foo/bar", "main")
    s2 = _commit(slot, "f.txt", "f\n", "f")
    _git(slot, "switch", "-q", "-c", "pushed", "main")
    _commit(slot, "p.txt", "p\n", "p")
    _git(slot, "push", "-q", "origin", "pushed")
    res = SlotSaver(slot).check_and_rescue(THREAD, TURN)
    assert res.outcome is SlotTurnOutcome.RESCUED, res.detail
    assert [(t.nn, t.source, t.sha) for t in res.targets] == [(1, "foo/bar", s2), (2, "stash", s1)]
    assert _remote_refs(origin) == {t.ref: t.sha for t in res.targets}


def test_detached_head_commit_is_rescued(slot: Path, origin: Path) -> None:
    _git(slot, "switch", "-q", "--detach")
    sha = _commit(slot, "d.txt", "d\n", "d")
    res = SlotSaver(slot).check_and_rescue(THREAD, TURN)
    assert res.outcome is SlotTurnOutcome.RESCUED, res.detail
    [t] = res.targets
    assert (t.kind, t.source, t.sha) == (RescueKind.DETACHED, "HEAD", sha)


def test_every_stash_entry_is_rescued(slot: Path, origin: Path) -> None:
    # Two stashes that do not descend from each other: pushing refs/stash alone saves only the
    # newest (Obj-37-1).
    (slot / "a.txt").write_text("one\n", encoding="utf-8")
    _git(slot, "stash", "push", "-q")
    (slot / "u.txt").write_text("two\n", encoding="utf-8")
    _git(slot, "stash", "push", "-q", "-u")
    shas = _git(slot, "stash", "list", "--format=%H").split()
    res = SlotSaver(slot).check_and_rescue(THREAD, TURN)
    assert res.outcome is SlotTurnOutcome.RESCUED, res.detail
    assert "stash" in res.reasons
    stashes = [t for t in res.targets if t.kind is RescueKind.STASH]
    assert [(t.source, t.sha) for t in stashes] == [("stash@{0}", shas[0]), ("stash@{1}", shas[1])]
    remote = _remote_refs(origin)
    for t in stashes:
        assert remote[t.ref] == t.sha
    # The -u stash's untracked parent travelled with it.
    assert _git(origin, "cat-file", "-t", f"{shas[0]}^3").strip() == "commit"


def test_bad_turn_id_fails_before_anything_is_pushed(slot: Path, origin: Path) -> None:
    (slot / "new.txt").write_text("wip\n", encoding="utf-8")
    res = SlotSaver(slot).check_and_rescue(THREAD, "a/b")
    assert res.outcome is SlotTurnOutcome.RESCUE_FAILED and not res.saved
    assert _remote_refs(origin) == {}
    assert _git(slot, "status", "--porcelain").strip(), "nothing was committed either"


def test_unreachable_remote_is_rescue_failed(slot: Path, tmp_path: Path) -> None:
    _commit(slot, "b.txt", "b\n", "b")
    _git(slot, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    res = SlotSaver(slot).check_and_rescue(THREAD, TURN)
    assert res.outcome is SlotTurnOutcome.RESCUE_FAILED
    assert "fetch_failed" in res.reasons


def test_confirmation_mismatch_is_rescue_failed(slot: Path, origin: Path) -> None:
    from spirrow_mindwire.clone_guard import GitResult

    _commit(slot, "b.txt", "b\n", "b")

    def lying_git(cwd: Path, args: object) -> GitResult:
        argv = list(args)  # type: ignore[call-overload]
        proc = subprocess.run(
            ["git", "-C", str(cwd), *argv], capture_output=True, text=True, env=git_env()
        )
        if argv and argv[0] == "ls-remote":
            return GitResult(0, "0" * 40 + "\t" + argv[-1] + "\n", "")
        return GitResult(proc.returncode, proc.stdout, proc.stderr)

    res = SlotSaver(slot, git=lying_git).check_and_rescue(THREAD, TURN)
    assert res.outcome is SlotTurnOutcome.RESCUE_FAILED
    assert res.detail and "remote has" in res.detail


def test_mirror_remote_judgement(slot: Path, origin: Path, tmp_path: Path) -> None:
    # Borrow time judges against ``mirror`` (Obj-32-1); stale origin/* refs do not count.
    mirror = tmp_path / "mirror.git"
    subprocess.run(
        ["git", "clone", "-q", "--mirror", str(origin), str(mirror)],
        check=True,
        capture_output=True,
    )
    _git(slot, "remote", "add", "mirror", str(mirror))
    assert SlotSaver(slot).judge("mirror").saved
    _git(slot, "switch", "-q", "-c", "feature/y")
    _commit(slot, "y.txt", "y\n", "y")
    _git(slot, "push", "-q", "origin", "feature/y")  # on GitHub, but not yet in the mirror
    j = SlotSaver(slot).judge("mirror")
    assert j.reasons == ("unpushed",)


def test_mirror_judgement_from_the_path_without_a_mirror_remote(
    slot: Path, origin: Path, tmp_path: Path
) -> None:
    # A8 (msg-10088 §3, Einstein's go): a slot has no ``mirror`` remote. The fetch names the
    # mirror's path, the explicit refspec still writes refs/remotes/mirror/*, and the judgement and
    # the rescue read --remotes=mirror and mirror/<branch> as before.
    mirror = tmp_path / "mirror.git"
    subprocess.run(
        ["git", "clone", "-q", "--mirror", str(origin), str(mirror)],
        check=True,
        capture_output=True,
    )
    assert "mirror" not in _git(slot, "remote").split()
    saver = SlotSaver(slot)
    assert saver.judge("mirror", str(mirror)).saved
    main_sha = _git(origin, "rev-parse", "main").strip()
    assert _git(slot, "rev-parse", "mirror/main").strip() == main_sha

    _git(slot, "switch", "-q", "-c", "feature/y")
    sha = _commit(slot, "y.txt", "y\n", "y")
    _git(slot, "push", "-q", "origin", "feature/y")  # on GitHub, but not yet in the mirror
    j = saver.judge("mirror", str(mirror))
    assert j.reasons == ("unpushed",) and j.fetched
    res = saver.rescue(THREAD, TURN, j, "mirror")
    assert res.outcome is SlotTurnOutcome.RESCUED
    assert [t.sha for t in res.targets] == [sha]

    # Once the mirror has the branch, the next judgement sees it under mirror/feature/y; a branch
    # the mirror drops is pruned from refs/remotes/mirror/ too.
    subprocess.run(["git", "-C", str(mirror), "fetch", "-q", "--prune", "origin"], check=True)
    assert saver.judge("mirror", str(mirror)).saved
    assert _git(slot, "rev-parse", "mirror/feature/y").strip() == sha
    _git(origin, "branch", "-D", "feature/y")
    subprocess.run(["git", "-C", str(mirror), "fetch", "-q", "--prune", "origin"], check=True)
    saver.fetch("mirror", str(mirror))
    gone = _git(slot, "rev-parse", "-q", "--verify", "refs/remotes/mirror/feature/y", check=False)
    assert not gone.strip()


def test_ledger_row_carries_the_mapping_and_is_not_counted(slot: Path, tmp_path: Path) -> None:
    (slot / "new.txt").write_text("wip\n", encoding="utf-8")
    res = SlotSaver(slot).check_and_rescue(THREAD, TURN)
    ledger = tmp_path / "state" / "continuation_ledger.jsonl"
    out = record_turn_result(ledger, res, project="p", thread=THREAD, turn_id=TURN, slot=str(slot))
    assert out.outcome is SlotTurnOutcome.RESCUED
    [row] = read_ledger(ledger).rows
    assert row["outcome"] == "rescued" and row["turn_id"] == TURN
    assert row["refs"] == [
        {
            "nn": "01",
            "kind": "branch",
            "source": "main",
            "sha": res.targets[0].sha,
            "ref": res.targets[0].ref,
        }
    ]
    summaries, undated = summarize(read_ledger(ledger).rows, since=datetime.now(UTC) - timedelta(1))
    assert summaries == [] and undated == 0


def test_rescue_failed_and_quarantine_failed_rows_for_one_turn(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    for outcome in (SlotTurnOutcome.RESCUE_FAILED, SlotTurnOutcome.QUARANTINE_FAILED):
        record_turn_result(
            ledger,
            SaveResult(outcome, detail="x"),
            project="p",
            thread=THREAD,
            turn_id=TURN,
            slot="s",
        )
    assert [r["outcome"] for r in read_ledger(ledger).rows] == [
        "rescue_failed",
        "quarantine_failed",
    ]


def test_ledger_write_failure_is_never_saved(tmp_path: Path) -> None:
    ledger = tmp_path / "is-a-dir"
    ledger.mkdir()
    out = record_turn_result(
        ledger,
        SaveResult(SlotTurnOutcome.PUSHED),
        project="p",
        thread=THREAD,
        turn_id=TURN,
        slot="s",
    )
    assert out.outcome is SlotTurnOutcome.RESCUE_FAILED and not out.saved
    assert out.detail and out.detail.startswith("ledger write failed")


def test_malformed_slot_rows_are_counted(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "kind": "slot_turn_result",
                "project": "p",
                "thread": "t",
                "turn_id": "x",
                "outcome": "saved",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert read_ledger(ledger).malformed == 1
