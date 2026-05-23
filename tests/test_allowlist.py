"""Tests for the Stage 3 implementer allow-list (WIRING_ALLOWLIST_SPEC §B).

These exercise the SDK-agnostic decision logic directly via
:class:`ClassifiedAction` — the safety core. The SDK-tool classifier is tested
in ``test_implementer_adapter.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spirrow_mindwire.allowlist import (
    Allowlist,
    AllowlistConfigError,
    ClassifiedAction,
    Operation,
    default_allowlist,
)


def _al(repo_root: Path) -> Allowlist:
    return default_allowlist(repo_root=repo_root)


# --- Tier A: unconstrained allows ------------------------------------------ #


@pytest.mark.parametrize(
    "op", [Operation.EXEC_CODE, Operation.FS_READ, Operation.SEARCH, Operation.GITHUB_READ]
)
def test_unconstrained_tier_a_allowed(tmp_path: Path, op: Operation) -> None:
    assert _al(tmp_path).check(ClassifiedAction(op)).allowed is True


# --- fs.write path constraint (<repo>/** only) ----------------------------- #


def test_fs_write_absolute_inside_repo_allowed(tmp_path: Path) -> None:
    p = str(tmp_path / "src" / "x.py")
    assert _al(tmp_path).check(ClassifiedAction(Operation.FS_WRITE, path=p)).allowed is True


def test_fs_write_relative_inside_repo_allowed(tmp_path: Path) -> None:
    d = _al(tmp_path).check(ClassifiedAction(Operation.FS_WRITE, path="src/x.py"))
    assert d.allowed is True


def test_fs_write_outside_repo_denied(tmp_path: Path) -> None:
    outside = str(tmp_path.parent / "elsewhere" / "y.py")
    d = _al(tmp_path).check(ClassifiedAction(Operation.FS_WRITE, path=outside))
    assert d.allowed is False
    assert "outside" in d.reason


def test_fs_write_parent_escape_denied(tmp_path: Path) -> None:
    # `..` resolves outside repo_root → fail closed.
    d = _al(tmp_path).check(ClassifiedAction(Operation.FS_WRITE, path="../escape.py"))
    assert d.allowed is False


def test_fs_write_no_path_denied(tmp_path: Path) -> None:
    assert _al(tmp_path).check(ClassifiedAction(Operation.FS_WRITE, path=None)).allowed is False


# --- git.commit / git.push branch constraints ------------------------------ #


@pytest.mark.parametrize("branch", ["feature/x", "feature/deep/name", "develop", None])
def test_git_commit_allowed_branches(tmp_path: Path, branch: str | None) -> None:
    d = _al(tmp_path).check(ClassifiedAction(Operation.GIT_COMMIT, branch=branch))
    assert d.allowed is True


def test_git_commit_named_main_denied(tmp_path: Path) -> None:
    d = _al(tmp_path).check(ClassifiedAction(Operation.GIT_COMMIT, branch="main"))
    assert d.allowed is False


def test_git_push_feature_allowed(tmp_path: Path) -> None:
    d = _al(tmp_path).check(ClassifiedAction(Operation.GIT_PUSH, branch="feature/x", force=False))
    assert d.allowed is True


def test_git_push_to_main_denied(tmp_path: Path) -> None:
    d = _al(tmp_path).check(ClassifiedAction(Operation.GIT_PUSH, branch="main"))
    assert d.allowed is False


def test_git_push_force_flag_denied_even_on_feature(tmp_path: Path) -> None:
    # Defence in depth: the classifier maps a force push to FORCE_PUSH, but if a
    # force flag ever reaches GIT_PUSH the force:false constraint still denies it.
    d = _al(tmp_path).check(ClassifiedAction(Operation.GIT_PUSH, branch="feature/x", force=True))
    assert d.allowed is False
    assert "force" in d.reason


# --- git.merge source/target ----------------------------------------------- #


def test_git_merge_feature_to_develop_allowed(tmp_path: Path) -> None:
    d = _al(tmp_path).check(
        ClassifiedAction(Operation.GIT_MERGE, source="feature/x", target="develop")
    )
    assert d.allowed is True


def test_git_merge_source_not_feature_denied(tmp_path: Path) -> None:
    d = _al(tmp_path).check(ClassifiedAction(Operation.GIT_MERGE, source="develop"))
    assert d.allowed is False


# --- github.pr.open -------------------------------------------------------- #


@pytest.mark.parametrize("target", ["develop", "main", None])
def test_github_pr_open_allowed(tmp_path: Path, target: str | None) -> None:
    # Opening a PR is reversible (Tier A), to develop or main.
    assert _al(tmp_path).check(ClassifiedAction(Operation.GITHUB_PR_OPEN, target=target)).allowed


# --- Tier C: explicit forbidden -------------------------------------------- #


@pytest.mark.parametrize(
    "op",
    [
        Operation.GIT_MERGE_TO_MAIN,
        Operation.FORCE_PUSH,
        Operation.HISTORY_REWRITE,
        Operation.FS_DELETE,
        Operation.DRIVE_WRITE,
        Operation.EXTERNAL_PUBLISH,
    ],
)
def test_tier_c_forbidden_denied_with_reason(tmp_path: Path, op: Operation) -> None:
    d = _al(tmp_path).check(ClassifiedAction(op))
    assert d.allowed is False
    assert d.operation is op
    assert d.reason  # a concrete fail-loud reason


# --- default-deny ---------------------------------------------------------- #


def test_unknown_operation_default_denied(tmp_path: Path) -> None:
    d = _al(tmp_path).check(ClassifiedAction(Operation.UNKNOWN))
    assert d.allowed is False
    assert "default: deny" in d.reason


# --- config loading -------------------------------------------------------- #


def test_default_allowlist_has_rules(tmp_path: Path) -> None:
    al = _al(tmp_path)
    # smoke: the packaged §B.3 config loaded with both allow and forbidden rules.
    assert al.check(ClassifiedAction(Operation.EXEC_CODE)).allowed
    assert al.check(ClassifiedAction(Operation.FORCE_PUSH)).allowed is False


def test_from_mapping_rejects_non_dict(tmp_path: Path) -> None:
    with pytest.raises(AllowlistConfigError):
        Allowlist.from_mapping(["not", "a", "dict"], repo_root=tmp_path)


def test_from_mapping_rejects_unknown_default(tmp_path: Path) -> None:
    with pytest.raises(AllowlistConfigError):
        Allowlist.from_mapping({"default": "maybe"}, repo_root=tmp_path)


def test_from_mapping_rejects_unknown_operation(tmp_path: Path) -> None:
    with pytest.raises(AllowlistConfigError):
        Allowlist.from_mapping(
            {"default": "deny", "allow": [{"operation": "not.an.op"}]}, repo_root=tmp_path
        )


def test_default_allow_policy_permits_unlisted(tmp_path: Path) -> None:
    al = Allowlist.from_mapping({"default": "allow"}, repo_root=tmp_path)
    assert al.check(ClassifiedAction(Operation.UNKNOWN)).allowed is True


def test_default_allow_still_honours_forbidden(tmp_path: Path) -> None:
    al = Allowlist.from_mapping(
        {"default": "allow", "forbidden": [{"operation": "fs.delete", "reason": "no"}]},
        repo_root=tmp_path,
    )
    assert al.check(ClassifiedAction(Operation.FS_DELETE)).allowed is False
