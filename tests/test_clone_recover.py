"""wrong_head auto-recovery (T-dirty-clone-wip-retreat-and-silent-stall-alerts, PR-0).

Operator msg-7774, approved by Takahito on 2026-11-04 (§2). Real git repositories (a bare
``origin`` and a clone of it) in ``tmp_path``. Every case where the clone must not be touched
also checks that HEAD and the branch list are unchanged.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from spirrow_mindwire import cli
from spirrow_mindwire.clone_guard import CloneGuard, DirtyCloneError
from spirrow_mindwire.clone_recover import (
    NOT_ELIGIBLE_EXIT,
    RECOVER_PREFIX,
    RECOVERED_EXIT,
    CloneRecoverer,
    RecoverOutcome,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
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
    (seed / "a.txt").write_text("a\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True, capture_output=True
    )
    return bare


@pytest.fixture
def clone(tmp_path: Path, origin: Path) -> Path:
    c = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(c)], check=True, capture_output=True)
    _cfg(c)
    return c


def _feature_commit(clone: Path, branch: str = "feature/x", *, push: bool = True) -> str:
    _git(clone, "switch", "-q", "-c", branch)
    (clone / "f.txt").write_text("f\n", encoding="utf-8")
    _git(clone, "add", "f.txt")
    _git(clone, "commit", "-q", "-m", "feat")
    if push:
        _git(clone, "push", "-q", "origin", branch)
    return _git(clone, "rev-parse", "HEAD").strip()


def _merge_on_remote(tmp_path: Path, origin: Path, branch: str, into: str = "main") -> None:
    """Merge ``branch`` into ``into`` on the remote, as a PR merge would, then delete it there."""
    w = tmp_path / f"merger-{into}"
    subprocess.run(["git", "clone", "-q", str(origin), str(w)], check=True, capture_output=True)
    _cfg(w)
    _git(w, "switch", "-q", into)
    _git(w, "merge", "-q", "--no-ff", "-m", "merge", f"origin/{branch}")
    _git(w, "push", "-q", "origin", into)
    _git(w, "push", "-q", "origin", "--delete", branch)


def _head(clone: Path) -> str:
    return _git(clone, "symbolic-ref", "-q", "--short", "HEAD").strip()


def _branches(clone: Path) -> list[str]:
    return sorted(_git(clone, "branch", "--format=%(refname:short)").split())


def test_merged_branch_is_recovered_and_the_guard_then_passes(
    tmp_path: Path, origin: Path, clone: Path
) -> None:
    _feature_commit(clone)
    _merge_on_remote(tmp_path, origin, "feature/x")  # incident 2: remote branch already deleted
    res = CloneRecoverer(clone).recover()
    assert res.outcome is RecoverOutcome.RECOVERED, res
    assert (res.branch, res.default, res.detail) == ("feature/x", "main", "")
    assert _head(clone) == "main"
    assert _branches(clone) == ["main"]
    assert _git(clone, "rev-parse", "HEAD") == _git(clone, "rev-parse", "origin/main")
    CloneGuard(clone).check()  # no exception: the parking is released by the next clone-check


def test_branch_whose_commits_are_only_on_develop_is_recovered(
    tmp_path: Path, origin: Path, clone: Path
) -> None:
    _git(clone, "push", "-q", "origin", "main:develop")
    _feature_commit(clone)
    _merge_on_remote(tmp_path, origin, "feature/x", into="develop")
    res = CloneRecoverer(clone).recover()
    assert res.outcome is RecoverOutcome.RECOVERED, res
    assert _head(clone) == "main"
    assert "feature/x" not in _branches(clone)


def test_pushed_but_unmerged_branch_is_not_eligible(clone: Path) -> None:
    """Pushed, not merged: the commit is not on origin/main, so the branch is NOT eligible."""
    _feature_commit(clone)
    res = CloneRecoverer(clone).recover()
    assert res.outcome is RecoverOutcome.NOT_ELIGIBLE
    assert "1 commit(s) not on origin/main" in res.detail
    assert _head(clone) == "feature/x"


def test_unpushed_commit_is_not_eligible_and_nothing_changes(
    tmp_path: Path, origin: Path, clone: Path
) -> None:
    _feature_commit(clone)
    _merge_on_remote(tmp_path, origin, "feature/x")
    (clone / "g.txt").write_text("g\n", encoding="utf-8")
    _git(clone, "add", "g.txt")
    _git(clone, "commit", "-q", "-m", "local only")
    before = (_head(clone), _branches(clone), _git(clone, "rev-parse", "HEAD"))
    res = CloneRecoverer(clone).recover()
    assert res.outcome is RecoverOutcome.NOT_ELIGIBLE
    assert "1 commit(s) not on" in res.detail
    assert (_head(clone), _branches(clone), _git(clone, "rev-parse", "HEAD")) == before


def test_dirty_tree_is_not_eligible(tmp_path: Path, origin: Path, clone: Path) -> None:
    _feature_commit(clone)
    _merge_on_remote(tmp_path, origin, "feature/x")
    (clone / "untracked.txt").write_text("u\n", encoding="utf-8")
    res = CloneRecoverer(clone).recover()
    assert res.outcome is RecoverOutcome.NOT_ELIGIBLE
    assert "dirty_tree" in res.detail
    assert _head(clone) == "feature/x"
    assert (clone / "untracked.txt").exists()


def test_op_in_progress_marker_is_not_eligible(tmp_path: Path, origin: Path, clone: Path) -> None:
    _feature_commit(clone)
    _merge_on_remote(tmp_path, origin, "feature/x")
    git_dir = Path(_git(clone, "rev-parse", "--absolute-git-dir").strip())
    (git_dir / "BISECT_START").write_text("main\n", encoding="utf-8")
    res = CloneRecoverer(clone).recover()
    assert res.outcome is RecoverOutcome.NOT_ELIGIBLE
    assert "op_in_progress" in res.detail
    assert _head(clone) == "feature/x"


def test_detached_head_is_not_eligible(clone: Path) -> None:
    _git(clone, "switch", "-q", "--detach", "HEAD")
    res = CloneRecoverer(clone).recover()
    assert res.outcome is RecoverOutcome.NOT_ELIGIBLE
    assert res.detail == "HEAD is detached"


def test_clean_clone_on_default_is_not_eligible(clone: Path) -> None:
    res = CloneRecoverer(clone).recover()
    assert res.outcome is RecoverOutcome.NOT_ELIGIBLE
    assert res.detail == "clone is already clean"


def test_failed_fetch_is_not_eligible(tmp_path: Path, origin: Path, clone: Path) -> None:
    _feature_commit(clone)
    _merge_on_remote(tmp_path, origin, "feature/x")
    _git(clone, "fetch", "-q", "origin")  # the local refs already say "merged"...
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    res = CloneRecoverer(clone).recover()  # ...but without a fresh fetch it does not act
    assert res.outcome is RecoverOutcome.NOT_ELIGIBLE
    assert res.detail.startswith("fetch failed:")
    assert _head(clone) == "feature/x"


def test_cli_prints_one_row_and_maps_exit_codes(
    tmp_path: Path, origin: Path, clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as nope:
        cli.main(["clone-recover", "--repo-dir", str(clone)])
    assert nope.value.code == NOT_ELIGIBLE_EXIT
    _feature_commit(clone)
    _merge_on_remote(tmp_path, origin, "feature/x")
    with pytest.raises(SystemExit) as ok:
        cli.main(["clone-recover", "--repo-dir", str(clone)])
    assert ok.value.code == RECOVERED_EXIT
    rows = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(RECOVER_PREFIX)]
    assert len(rows) == 2
    assert json.loads(rows[0][len(RECOVER_PREFIX) :])["outcome"] == "not_eligible"
    last = json.loads(rows[1][len(RECOVER_PREFIX) :])
    assert last == {
        "outcome": "recovered",
        "branch": "feature/x",
        "default": "main",
        "detail": "",
        "preserved_ref": None,
        "preserved_sha": None,
    }


# --- T-role-exit-leaves-clone-off-default-branch PR-A: --preserve (Bohr msg-8698) ---

STAMP = "20261110T000000Z"


def _stamp() -> str:
    return STAMP


def _remote_refs(origin: Path) -> list[str]:
    return sorted(_git(origin, "for-each-ref", "--format=%(refname)").split())


def _local_commit(clone: Path, name: str = "g.txt") -> str:
    (clone / name).write_text("g\n", encoding="utf-8")
    _git(clone, "add", name)
    _git(clone, "commit", "-q", "-m", "local only")
    return _git(clone, "rev-parse", "HEAD").strip()


def test_preserve_keeps_unpushed_commits_on_a_local_wip_ref(origin: Path, clone: Path) -> None:
    sha = _feature_commit(clone, push=False)
    remote_before = _remote_refs(origin)
    res = CloneRecoverer(clone, preserve=True, clock=_stamp).recover()
    assert res.outcome is RecoverOutcome.RECOVERED, res
    ref = f"wip/feature/x-{STAMP}"
    assert (res.branch, res.preserved_ref, res.preserved_sha) == ("feature/x", ref, sha)
    assert _head(clone) == "main"
    assert _branches(clone) == ["main", ref]  # the original branch is gone, the commits are not
    assert _git(clone, "rev-parse", ref).strip() == sha
    CloneGuard(clone).check()
    assert _remote_refs(origin) == remote_before  # nothing pushed
    _git(clone, "checkout", "-q", "-b", "feature/x")  # the name is free for the next session


def test_preserve_pushed_but_unmerged_branch_is_kept_too(origin: Path, clone: Path) -> None:
    sha = _feature_commit(clone)  # on origin/feature/x but not on origin/main
    res = CloneRecoverer(clone, preserve=True, clock=_stamp).recover()
    assert res.outcome is RecoverOutcome.RECOVERED, res
    assert res.preserved_sha == sha
    assert _head(clone) == "main"


def test_preserve_with_nothing_to_keep_is_a_plain_recover(
    tmp_path: Path, origin: Path, clone: Path
) -> None:
    _feature_commit(clone)
    _merge_on_remote(tmp_path, origin, "feature/x")
    res = CloneRecoverer(clone, preserve=True, clock=_stamp).recover()
    assert res.outcome is RecoverOutcome.RECOVERED, res
    assert (res.preserved_ref, res.preserved_sha, res.detail) == (None, None, "")
    assert _branches(clone) == ["main"]


def test_preserve_detached_head_reachable_from_remote_switches_without_a_ref(
    clone: Path,
) -> None:
    _git(clone, "switch", "-q", "--detach", "HEAD")
    res = CloneRecoverer(clone, preserve=True, clock=_stamp).recover()
    assert res.outcome is RecoverOutcome.RECOVERED, res
    assert (res.branch, res.preserved_ref) == (None, None)
    assert _head(clone) == "main"
    assert _branches(clone) == ["main"]


def test_preserve_detached_head_with_local_commits_gets_a_detached_ref(
    origin: Path, clone: Path
) -> None:
    _git(clone, "switch", "-q", "--detach", "HEAD")
    sha = _local_commit(clone)
    remote_before = _remote_refs(origin)
    res = CloneRecoverer(clone, preserve=True, clock=_stamp).recover()
    assert res.outcome is RecoverOutcome.RECOVERED, res
    ref = f"wip/detached-{sha[:8]}-{STAMP}"
    assert (res.preserved_ref, res.preserved_sha) == (ref, sha)
    assert _head(clone) == "main"
    assert _git(clone, "rev-parse", ref).strip() == sha
    assert _remote_refs(origin) == remote_before


def test_preserve_ref_that_cannot_be_made_fails_and_changes_nothing(clone: Path) -> None:
    _feature_commit(clone, push=False)
    before = (_head(clone), _branches(clone), _git(clone, "rev-parse", "HEAD"))
    res = CloneRecoverer(clone, preserve=True, clock=lambda: "bad..name").recover()
    assert res.outcome is RecoverOutcome.FAILED, res
    assert res.detail.startswith("preserve: creating wip/feature/x-bad..name failed")
    assert res.preserved_ref is None
    assert (_head(clone), _branches(clone), _git(clone, "rev-parse", "HEAD")) == before
    with pytest.raises(DirtyCloneError):  # still parked
        CloneGuard(clone).check()


def test_preserve_dirty_tree_is_not_eligible(clone: Path) -> None:
    _feature_commit(clone, push=False)
    (clone / "untracked.txt").write_text("u\n", encoding="utf-8")
    res = CloneRecoverer(clone, preserve=True, clock=_stamp).recover()
    assert res.outcome is RecoverOutcome.NOT_ELIGIBLE
    assert "dirty_tree" in res.detail
    assert _head(clone) == "feature/x"
    assert not any(b.startswith("wip/") for b in _branches(clone))


def test_plain_mode_still_refuses_unpushed_commits(clone: Path) -> None:
    _feature_commit(clone, push=False)
    res = CloneRecoverer(clone, clock=_stamp).recover()
    assert res.outcome is RecoverOutcome.NOT_ELIGIBLE
    assert _branches(clone) == ["feature/x", "main"]


def test_cli_preserve_writes_one_record_for_the_thread(
    tmp_path: Path, clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = _feature_commit(clone, push=False)
    state = tmp_path / "state"
    with pytest.raises(SystemExit) as ok:
        cli.main(
            [
                "clone-recover",
                "--repo-dir",
                str(clone),
                "--preserve",
                "--thread-id",
                "T-a",
                "--state-dir",
                str(state),
            ]
        )
    assert ok.value.code == RECOVERED_EXIT
    [line] = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(RECOVER_PREFIX)]
    row = json.loads(line[len(RECOVER_PREFIX) :])
    assert row["preserved_sha"] == sha
    assert row["preserved_ref"].startswith("wip/feature/x-")
    [rec] = [
        json.loads(x) for x in (state / "clone-retreats.jsonl").read_text("utf-8").splitlines()
    ]
    assert (rec["kind"], rec["thread_id"], rec["outcome"]) == ("retreat", "T-a", "preserved")
    assert (rec["preserved_ref"], rec["preserved_sha"]) == (row["preserved_ref"], sha)
    assert rec["pushed"] is False


def test_cli_preserve_needs_thread_and_state_dir(clone: Path) -> None:
    with pytest.raises(SystemExit) as bad:
        cli.main(["clone-recover", "--repo-dir", str(clone), "--preserve"])
    assert bad.value.code == 2


def test_cli_plain_mode_writes_no_record(tmp_path: Path, clone: Path) -> None:
    _feature_commit(clone, push=False)
    with pytest.raises(SystemExit) as nope:
        cli.main(["clone-recover", "--repo-dir", str(clone)])
    assert nope.value.code == NOT_ELIGIBLE_EXIT
    assert not (tmp_path / "state").exists()
