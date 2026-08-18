"""Where a bare ``git push`` actually goes, measured against real repositories.

The guard used to answer this with the local branch name. That is only right for
``push.default`` values that push to a same-named branch; with ``upstream`` and
a retargeted tracking branch it is wrong, and wrong in the direction that costs
``main``. Measured end to end before the fix: from ``feature/x`` with
``push.default=upstream`` and ``git branch -u origin/main``, a bare
``git push --force`` reported ``feature/x -> main`` and moved the remote
``main`` — while the guard, reading the local name, allowed it.

The two commands can be issued as separate tool calls, so no single command ever
looks wrong and the chain check cannot see it. (Tier B, PR #158 round 16.)

These tests build a real bare remote and a real clone rather than faking git:
the thing under test is what git does with a configuration, and a fake would
only restate the assumption being checked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from spirrow_mindwire.adapters.implementer import _AllowlistGuard, _push_destination
from spirrow_mindwire.allowlist import default_allowlist


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _clone_with(tmp_path: Path, *, push_default: str | None, upstream: str | None) -> Path:
    """A clone whose ``origin`` already has ``main``, checked out on ``feature/x``."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    _run(work, "config", "user.email", "x@y")
    _run(work, "config", "user.name", "x")
    (work / "f").write_text("a", encoding="utf-8")
    _run(work, "add", ".")
    _run(work, "commit", "-qm", "c1")
    _run(work, "push", "-q", "origin", "HEAD:main")
    _run(work, "fetch", "-q", "origin")
    _run(work, "switch", "-q", "-c", "feature/x")
    if push_default is not None:
        _run(work, "config", "push.default", push_default)
    if upstream is not None:
        _run(work, "branch", "-u", upstream)
    return work


@pytest.mark.anyio
@pytest.mark.parametrize("command", ["git push --force", "git push"])
@pytest.mark.parametrize("mode", ["upstream", "tracking"])
async def test_a_retargeted_upstream_is_where_the_push_lands(
    tmp_path: Path, mode: str, command: str
) -> None:
    """The bypass: the local name says `feature/x`, the push writes `main`."""
    repo = _clone_with(tmp_path, push_default=mode, upstream="origin/main")
    assert _push_destination(repo) == "main"
    guard = _AllowlistGuard(default_allowlist(repo_root=repo))
    res = await guard("Bash", {"command": command}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)


@pytest.mark.anyio
@pytest.mark.parametrize(("mode", "expected"), [(None, "feature/x"), ("current", "feature/x")])
async def test_same_name_modes_keep_using_the_local_name(
    tmp_path: Path, mode: str | None, expected: str
) -> None:
    """`simple` (git's default) and `current` push to the same-named branch, so
    the local name IS the destination and ordinary work must keep passing."""
    repo = _clone_with(tmp_path, push_default=mode, upstream=None)
    assert _push_destination(repo) == expected
    guard = _AllowlistGuard(default_allowlist(repo_root=repo))
    res = await guard("Bash", {"command": "git push --force"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow)


@pytest.mark.anyio
async def test_an_upstream_inside_the_glob_still_passes(tmp_path: Path) -> None:
    """Reading the upstream must not turn into refusing every configured branch."""
    repo = _clone_with(tmp_path, push_default="upstream", upstream=None)
    _run(repo, "push", "-q", "origin", "HEAD:refs/heads/feature/x")
    _run(repo, "branch", "-u", "origin/feature/x")
    assert _push_destination(repo) == "feature/x"
    guard = _AllowlistGuard(default_allowlist(repo_root=repo))
    res = await guard("Bash", {"command": "git push --force"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["matching", "nothing"])
async def test_modes_that_name_no_single_branch_fail_closed(tmp_path: Path, mode: str) -> None:
    """`matching` writes every same-named branch and `nothing` refuses; neither
    is one branch, so the predicate declines and the guard denies."""
    repo = _clone_with(tmp_path, push_default=mode, upstream=None)
    assert _push_destination(repo) is None
    guard = _AllowlistGuard(default_allowlist(repo_root=repo))
    res = await guard("Bash", {"command": "git push --force"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)


@pytest.mark.anyio
async def test_an_explicit_refspec_is_not_second_guessed(tmp_path: Path) -> None:
    """A push that names its destination never consults the configuration."""
    repo = _clone_with(tmp_path, push_default="upstream", upstream="origin/main")
    guard = _AllowlistGuard(default_allowlist(repo_root=repo))
    allowed = await guard(
        "Bash", {"command": "git push --force origin feature/x"}, ToolPermissionContext()
    )
    assert isinstance(allowed, PermissionResultAllow)
    denied = await guard(
        "Bash", {"command": "git push --force origin main"}, ToolPermissionContext()
    )
    assert isinstance(denied, PermissionResultDeny)
