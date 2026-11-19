"""check_pushed's patch-equivalence pass (T-dirty-clone-check-disagreement, stage 1).

A local branch whose every commit is a patch-equal copy of a remote-reachable commit holds nothing
that exists only in the shared clone. Anything less certain stays ``unpushed``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_conductor_continuation import build_pushed_clone

from spirrow_mindwire.clone_guard import (
    CloneGuard,
    DirtyCloneError,
    DirtyCloneReason,
    normalized_patch,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _commit(repo: Path, name: str, text: str, msg: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    r = build_pushed_clone(tmp_path)
    _git(r, "switch", "-q", "feature/x")
    _commit(r, "b.txt", "b\n", "add b")
    _commit(r, "c.txt", "c\n", "add c")
    _git(r, "push", "-q", "origin", "feature/x")
    _git(r, "switch", "-q", "main")
    return r


def _unpushed(repo: Path) -> DirtyCloneError:
    with pytest.raises(DirtyCloneError) as ei:
        CloneGuard(repo).check_pushed()
    assert ei.value.reason is DirtyCloneReason.UNPUSHED
    return ei.value


def test_cherry_picks_onto_another_base_are_pushed(clone: Path) -> None:
    _commit(clone, "z.txt", "z\n", "main moves on")  # a different base for the picks
    _git(clone, "push", "-q", "origin", "main")
    _git(clone, "switch", "-q", "-c", "rescue", "main")
    _git(clone, "cherry-pick", "feature/x~1", "feature/x")
    _git(clone, "switch", "-q", "main")
    CloneGuard(clone).check_pushed()


def test_same_subject_different_change_is_unpushed(clone: Path) -> None:
    _git(clone, "switch", "-q", "-c", "rescue", "main")
    _commit(clone, "b.txt", "b, but edited\n", "add b")
    _git(clone, "switch", "-q", "main")
    assert "rescue (no upstream)" in _unpushed(clone).detail


def test_one_extra_commit_keeps_the_branch_unpushed(clone: Path) -> None:
    _git(clone, "switch", "-q", "-c", "rescue", "main")
    _git(clone, "cherry-pick", "feature/x~1")
    _commit(clone, "d.txt", "d\n", "only here")
    _git(clone, "switch", "-q", "main")
    assert "rescue" in _unpushed(clone).detail


def test_merge_commit_outside_the_remotes_is_unpushed(clone: Path) -> None:
    _git(clone, "switch", "-q", "-c", "side", "main")
    _commit(clone, "s.txt", "s\n", "side")
    _git(clone, "switch", "-q", "-c", "rescue", "main")
    _git(clone, "cherry-pick", "feature/x~1")
    _git(clone, "merge", "-q", "--no-ff", "--no-edit", "side")
    _git(clone, "switch", "-q", "main")
    _git(clone, "branch", "-D", "side")
    assert "rescue" in _unpushed(clone).detail


def test_same_subject_whitespace_only_difference_is_unpushed(clone: Path) -> None:
    """PR #590 gate Obj-1: "a b" and "ab" are different changes."""
    _git(clone, "switch", "-q", "feature/x")
    _commit(clone, "w.txt", "a b\n", "add w")
    _git(clone, "push", "-q", "origin", "feature/x")
    _git(clone, "switch", "-q", "-c", "rescue", "main")
    _commit(clone, "w.txt", "ab\n", "add w")
    _git(clone, "switch", "-q", "main")
    assert "rescue" in _unpushed(clone).detail


def test_same_subject_different_binary_is_unpushed(clone: Path) -> None:
    """PR #590 gate Obj-2: two binary changes print the same summary once index is dropped."""
    _git(clone, "switch", "-q", "feature/x")
    (clone / "blob.bin").write_bytes(bytes([0, 1, 2, 3]))
    _git(clone, "add", "blob.bin")
    _git(clone, "commit", "-q", "-m", "add blob")
    _git(clone, "push", "-q", "origin", "feature/x")
    _git(clone, "switch", "-q", "-c", "rescue", "main")
    (clone / "blob.bin").write_bytes(bytes([0, 9, 9, 3]))
    _git(clone, "add", "blob.bin")
    _git(clone, "commit", "-q", "-m", "add blob")
    _git(clone, "switch", "-q", "main")
    assert "rescue" in _unpushed(clone).detail


def test_normalized_patch_ignores_index_and_hunk_positions() -> None:
    a = "diff --git a/f b/f\nindex 111..222 100644\n@@ -1,2 +1,3 @@ def x\n a\n+b\n"
    b = "diff --git a/f b/f\nindex 333..444 100644\n@@ -9,2 +9,3 @@\n a\n+b\n"
    c = "diff --git a/f b/f\nindex 333..444 100644\n@@ -9,2 +9,3 @@\n a\n+b \n"
    assert normalized_patch(a) == normalized_patch(b)
    assert normalized_patch(a) != normalized_patch(c)  # whitespace is kept
    assert normalized_patch("Binary files a/x and b/x differ\n") is None
