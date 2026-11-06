"""Launch-end WIP retreat / export-then-discard (T-dirty-clone-wip-retreat-and-silent-stall-alerts,
PR-1a, design v1.5: Bohr msg-7907 / msg-7909 / msg-7911 / msg-7913, go msg-7914, human Tier-C
decision 2026-11-06 on msg-7916).

Real git repositories (a bare ``origin`` and a clone of it) in ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spirrow_mindwire import cli
from spirrow_mindwire.clone_guard import CloneGuard
from spirrow_mindwire.clone_retreat import (
    ABORT_COMMANDS,
    INCOMPLETE_EXIT,
    OK_EXIT,
    RETREAT_PREFIX,
    CloneRetreater,
    RetreatOutcome,
    git_env,
)

THREAD = "T-sample"


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
def clone(tmp_path: Path, origin: Path) -> Path:
    c = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(c)], check=True, capture_output=True)
    _cfg(c)
    (c / ".mindwire").mkdir()
    (c / ".mindwire" / "pin").write_text("schema_version: 1\nmode: bootstrap\n", encoding="utf-8")
    return c


def _retreater(clone: Path, state: Path) -> CloneRetreater:
    return CloneRetreater(
        clone,
        thread_id=THREAD,
        head_msg="msg-1",
        stop_reason="adapter.turn_timeout",
        state_dir=state,
        now=lambda: datetime(2026, 11, 6, 1, 2, 3, tzinfo=UTC),
    )


def _assert_clean(clone: Path) -> None:
    CloneGuard(clone).check()  # raises if the guard still refuses
    assert (clone / ".mindwire" / "pin").exists(), "the pin must survive"


def _commit(repo: Path, name: str, text: str, msg: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


def test_clean_clone_is_left_alone(clone: Path, tmp_path: Path) -> None:
    res = _retreater(clone, tmp_path / "state").retreat()
    assert res.outcome is RetreatOutcome.CLEAN
    assert res.steps == []
    assert not (tmp_path / "state").exists()


def test_wrong_head_on_clean_tree_is_not_eligible(clone: Path, tmp_path: Path) -> None:
    _git(clone, "switch", "-q", "-c", "feature/x")
    res = _retreater(clone, tmp_path / "state").retreat()
    assert res.outcome is RetreatOutcome.NOT_ELIGIBLE
    assert _git(clone, "branch", "--show-current").strip() == "feature/x"


@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked"])
def test_dirty_tree_is_retreated_to_a_pushed_wip_branch(
    clone: Path, origin: Path, tmp_path: Path, kind: str
) -> None:
    _git(clone, "switch", "-q", "-c", "feature/work")
    if kind == "untracked":
        (clone / "new.txt").write_text("n1\nn2\n", encoding="utf-8")
    else:
        (clone / "a.txt").write_text("base\nmore\n", encoding="utf-8")
        if kind == "staged":
            _git(clone, "add", "a.txt")
    res = _retreater(clone, tmp_path / "state").retreat()
    assert res.outcome is RetreatOutcome.RETREATED, res.row()
    assert res.branch == f"wip/{THREAD}-20261106T010203Z"
    assert res.pushed is True
    assert res.files == 1
    assert res.insertions == (2 if kind == "untracked" else 1)
    _assert_clean(clone)
    assert _git(clone, "branch", "--show-current").strip() == "main"
    log = _git(origin, "log", "-1", "--format=%an%n%B", res.branch)
    assert "mindwire-wrapper" in log
    assert f"thread_id: {THREAD}" in log and "head_msg: msg-1" in log
    assert "stop_reason: adapter.turn_timeout" in log
    assert not (tmp_path / "state").exists(), "a retreat writes no discard patch"


def test_push_failure_keeps_the_branch_local_and_still_cleans(clone: Path, tmp_path: Path) -> None:
    _git(clone, "remote", "set-url", "--push", "origin", str(tmp_path / "no-such-remote.git"))
    (clone / "a.txt").write_text("base\nlocal\n", encoding="utf-8")
    res = _retreater(clone, tmp_path / "state").retreat()
    assert res.outcome is RetreatOutcome.RETREATED
    assert res.pushed is False
    _assert_clean(clone)
    assert _git(clone, "show", f"{res.branch}:a.txt") == "base\nlocal\n"


def _conflicted_merge(clone: Path, origin: Path, tmp_path: Path) -> str:
    """Leave ``clone`` mid-merge with a.txt conflicted; return the other side's sha."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, capture_output=True)
    _cfg(other)
    _commit(other, "a.txt", "theirs\n", "theirs")
    _git(other, "push", "-q", "origin", "main:side")
    _git(clone, "fetch", "-q", "origin")
    _git(clone, "switch", "-q", "-c", "feature/m")
    _commit(clone, "a.txt", "ours\n", "ours")
    _git(clone, "merge", "origin/side", check=False)
    return _git(clone, "rev-parse", "origin/side").strip()


def test_unmerged_paths_export_hand_resolution_before_any_abort(
    clone: Path, origin: Path, tmp_path: Path
) -> None:
    theirs = _conflicted_merge(clone, origin, tmp_path)
    # The implementer was resolving the conflict by hand when the turn was cut.
    (clone / "a.txt").write_text("HAND-RESOLVED\n", encoding="utf-8")
    (clone / "scratch.txt").write_text("x\n", encoding="utf-8")
    res = _retreater(clone, tmp_path / "state").retreat()
    assert res.outcome is RetreatOutcome.DISCARDED, res.row()
    assert res.patch is not None and res.untracked is not None
    patch = Path(res.patch).read_text(encoding="utf-8")
    assert "+HAND-RESOLVED" in patch, "the patch must hold the work the aborts destroy"
    assert Path(res.patch).name == f"clone-discarded-{THREAD}-20261106T010203Z.patch"
    listing = Path(res.untracked).read_text(encoding="utf-8")
    assert "# op_in_progress: merge" in listing
    assert f"# MERGE_HEAD: {theirs}" in listing
    assert "scratch.txt" in listing
    _assert_clean(clone)
    assert _git(clone, "branch", "--show-current").strip() == "main"
    assert not _git(clone, "branch", "--list", "wip/*").strip()


def test_dirty_without_op_runs_all_five_aborts_then_reset(clone: Path, tmp_path: Path) -> None:
    # Unmerged paths without any op marker: a conflicted `stash pop`.
    (clone / "a.txt").write_text("stashed\n", encoding="utf-8")
    _git(clone, "stash", "-q")
    _commit(clone, "a.txt", "moved\n", "moved")
    _git(clone, "stash", "pop", check=False)
    assert _git(clone, "ls-files", "-u").strip()
    res = _retreater(clone, tmp_path / "state").retreat()
    assert res.outcome is RetreatOutcome.DISCARDED, res.row()
    ran = [s.split(" -> ")[0] for s in res.steps]
    assert ran[:5] == [f"git {' '.join(a)}" for a in ABORT_COMMANDS]
    assert "git reset --hard -q" in ran and "git clean -fd -q" in ran
    _assert_clean(clone)


def test_cherry_pick_sequence_is_cleared(clone: Path, tmp_path: Path) -> None:
    _git(clone, "switch", "-q", "-c", "src")
    one = _commit(clone, "a.txt", "one\n", "one")
    two = _commit(clone, "b.txt", "two\n", "two")
    _git(clone, "switch", "-q", "main")
    _commit(clone, "a.txt", "conflict\n", "conflict")
    _git(clone, "cherry-pick", one, two, check=False)
    git_dir = Path(_git(clone, "rev-parse", "--absolute-git-dir").strip())
    assert (git_dir / "sequencer").exists()
    res = _retreater(clone, tmp_path / "state").retreat()
    assert res.outcome is RetreatOutcome.DISCARDED, res.row()
    assert not (git_dir / "sequencer").exists()
    _assert_clean(clone)


def test_revert_in_progress_is_cleared(clone: Path, tmp_path: Path) -> None:
    first = _commit(clone, "a.txt", "v1\n", "v1")
    _commit(clone, "a.txt", "v2\n", "v2")
    _git(clone, "revert", "--no-edit", first, check=False)
    git_dir = Path(_git(clone, "rev-parse", "--absolute-git-dir").strip())
    assert (git_dir / "REVERT_HEAD").exists()
    res = _retreater(clone, tmp_path / "state").retreat()
    assert res.outcome is RetreatOutcome.DISCARDED, res.row()
    _assert_clean(clone)


def test_am_in_progress_is_cleared(clone: Path, tmp_path: Path) -> None:
    _git(clone, "switch", "-q", "-c", "src")
    _commit(clone, "a.txt", "patched\n", "patched")
    mbox = tmp_path / "p.mbox"
    mbox.write_text(_git(clone, "format-patch", "-1", "--stdout"), encoding="utf-8")
    _git(clone, "switch", "-q", "main")
    _commit(clone, "a.txt", "diverged\n", "diverged")
    _git(clone, "am", str(mbox), check=False)
    git_dir = Path(_git(clone, "rev-parse", "--absolute-git-dir").strip())
    assert (git_dir / "rebase-apply").exists()
    res = _retreater(clone, tmp_path / "state").retreat()
    assert res.outcome is RetreatOutcome.DISCARDED, res.row()
    _assert_clean(clone)


def test_marker_left_after_discard_is_incomplete_and_not_removed(
    clone: Path, tmp_path: Path
) -> None:
    # A bisect is not one of the five aborts: its marker survives, and nothing under .git/ is
    # removed by hand.
    _commit(clone, "a.txt", "v1\n", "v1")
    _git(clone, "bisect", "start")
    (clone / "a.txt").write_text("dirty\n", encoding="utf-8")
    res = _retreater(clone, tmp_path / "state").retreat()
    assert res.outcome is RetreatOutcome.DISCARD_INCOMPLETE, res.row()
    assert res.exit_code == INCOMPLETE_EXIT
    assert res.marker == "BISECT_START"
    git_dir = Path(_git(clone, "rev-parse", "--absolute-git-dir").strip())
    assert (git_dir / "BISECT_START").exists()


def test_git_editor_true_does_not_wait_on_an_editor(clone: Path) -> None:
    # The human decision (2026-11-06) asks to confirm GIT_EDITOR=true works on the Windows host:
    # `commit -e` opens the editor, and it must return at once and keep the message.
    proc = subprocess.run(
        ["git", "-C", str(clone), "commit", "--allow-empty", "-e", "-q", "-m", "kept"],
        capture_output=True,
        text=True,
        env=git_env(),
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert _git(clone, "log", "-1", "--format=%s").strip() == "kept"


def test_cli_prints_one_row(
    clone: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (clone / "a.txt").write_text("base\ncli\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "clone-retreat",
                "--repo-dir",
                str(clone),
                "--thread-id",
                THREAD,
                "--head-msg",
                "msg-9",
                "--stop-reason",
                "no_handoff_to_human",
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )
    assert exc.value.code == OK_EXIT
    rows = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(RETREAT_PREFIX)]
    assert len(rows) == 1
    payload = json.loads(rows[0].removeprefix(RETREAT_PREFIX))
    assert payload["outcome"] == "retreated" and payload["pushed"] is True
