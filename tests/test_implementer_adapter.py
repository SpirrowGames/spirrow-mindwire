"""Tests for T19 ``ImplementerSdkAdapter`` + the SDK-tool classifier.

The classifier (SDK tool call → allow-list :class:`Operation`) is the
safety-critical mapping and is tested exhaustively. The adapter lifecycle is
exercised with a fake SDK client that drives the ``can_use_tool`` guard, so the
fail-loud allow-list-violation path is covered without the real CLI.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)

from spirrow_mindwire.adapters.implementer import (
    _BENIGN_BUILTIN_TOOLS,
    _DEFAULT_IMPLEMENTER_SYSTEM_PROMPT,
    _IMPLEMENTER_BUILTIN_TOOLS,
    ImplementerAllowlistError,
    ImplementerSdkAdapter,
    ImplementerSdkDeliveryError,
    ImplementerSdkSpawnError,
    _AllowlistGuard,
    classify_tool_call,
)
from spirrow_mindwire.allowlist import Operation, default_allowlist
from spirrow_mindwire.obligations import load_manifest
from spirrow_mindwire.ports import RoleAdapter, SpawnContext
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    Event,
    EventType,
    NewMessagePayload,
    ReplyDraft,
    Role,
    SessionState,
    ThreadRef,
)

#: Quote characters, built rather than escaped, so the strings below stay legible.
_DQ = chr(34)

_TS = datetime(2026, 5, 23, tzinfo=UTC)

# Loop-readable obligations manifest — required by the implementer adapter now
# that the DECLARE-UNREADABLE clause has been MOVED to it (spec/process/README.md).
# Loaded once at import time; the manifest is immutable.
_OBLIGATIONS = load_manifest()


# --------------------------------------------------------------------------- #
# classifier
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,inp,expected",
    [
        ("Write", {"file_path": "a.py"}, Operation.FS_WRITE),
        ("Edit", {"file_path": "a.py"}, Operation.FS_WRITE),
        ("MultiEdit", {"file_path": "a.py"}, Operation.FS_WRITE),
        ("NotebookEdit", {"notebook_path": "a.ipynb"}, Operation.FS_WRITE),
        ("Read", {"file_path": "a.py"}, Operation.FS_READ),
        ("Glob", {"pattern": "**"}, Operation.SEARCH),
        ("Grep", {"pattern": "x"}, Operation.SEARCH),
        # T37 #3: benign built-ins (planning + background-shell mgmt) classify to
        # EXEC_CODE (Tier A allow), not UNKNOWN — else they halt the agent's first
        # planning step. Anything with real fs/git/external effect stays explicit.
        ("TodoWrite", {"todos": []}, Operation.EXEC_CODE),
        ("BashOutput", {"bash_id": "1"}, Operation.EXEC_CODE),
        ("KillShell", {"shell_id": "1"}, Operation.EXEC_CODE),
        ("Frobnicate", {}, Operation.UNKNOWN),
    ],
)
def test_classify_simple_tools(name: str, inp: dict[str, Any], expected: Operation) -> None:
    assert classify_tool_call(name, inp).operation is expected


def test_classify_fs_write_carries_path() -> None:
    assert classify_tool_call("Write", {"file_path": "src/x.py"}).path == "src/x.py"


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("pytest -q", Operation.EXEC_CODE),
        ("uv run pytest", Operation.EXEC_CODE),
        ("git status", Operation.EXEC_CODE),
        ("git add -A", Operation.EXEC_CODE),
        ("git checkout -b feature/x", Operation.EXEC_CODE),
        ("git pull", Operation.EXEC_CODE),
        ("git commit -m msg", Operation.GIT_COMMIT),
        ("git push origin feature/x", Operation.GIT_PUSH),
        ("git push --force origin feature/x", Operation.FORCE_PUSH),
        ("git push -f origin feature/x", Operation.FORCE_PUSH),
        ("git push --force-with-lease origin feature/x", Operation.FORCE_PUSH),
        ("git merge feature/x", Operation.GIT_MERGE),
        ("git rebase -i HEAD~2", Operation.HISTORY_REWRITE),
        ("git reset --hard HEAD", Operation.HISTORY_REWRITE),
        # `filter-branch` / `filter-repo` take rev-list arguments, so any of them
        # can name a branch and none of them can be told apart from a flag value.
        # Since PR #158 widened `history_rewrite` to a branch glob, "history
        # rewrite, target unspecified" would be filled with HEAD and pass on a
        # feature branch — so the classifier says UNKNOWN and default-deny fires,
        # the same idiom `_enrich` already uses when HEAD is undecidable.
        #
        # The cost is label fidelity: the denial record reads `unknown` for what
        # is plainly a history rewrite, which is the same category error Bohr
        # objected to for `_classify_mcp` (T-fs-delete-path-scope msg-1197 §7).
        # `detail` still carries the command. Taken deliberately: a correct denial
        # beats an accurate label, and inventing a branch name to keep the label
        # would put a fiction in the record instead.
        ("git filter-branch --tree-filter true HEAD", Operation.UNKNOWN),
        ("rm -rf build", Operation.FS_DELETE),
        ("rmdir foo", Operation.FS_DELETE),
        ("npm publish", Operation.EXTERNAL_PUBLISH),
        ("twine upload dist/*", Operation.EXTERNAL_PUBLISH),
        ("docker push myimg", Operation.EXTERNAL_PUBLISH),
        ("gh pr create --base develop", Operation.GITHUB_PR_OPEN),
        ("gh pr merge 5", Operation.GIT_MERGE_TO_MAIN),
        ("gh pr view 5", Operation.GITHUB_READ),
        # indirection backstop: wrappers hide the inner command from tokenization.
        ('bash -c "rm -rf x"', Operation.FS_DELETE),
        ('eval "git push --force origin feature/x"', Operation.FORCE_PUSH),
        ("echo hi && $(rm -rf y)", Operation.FS_DELETE),
        ("sh -c 'git reset --hard HEAD~3'", Operation.HISTORY_REWRITE),
        # T23: mutating `gh api` → deny (UNKNOWN); a read (GET / no fields) stays read.
        ("gh api repos/o/r/merges -X PUT", Operation.UNKNOWN),
        ("gh api --method POST repos/o/r/pulls -f title=x", Operation.UNKNOWN),
        ("gh api -f base=main repos/o/r/merges", Operation.UNKNOWN),
        ("gh api repos/o/r/contents/x", Operation.GITHUB_READ),
        ("gh api repos/o/r -X GET", Operation.GITHUB_READ),
        # T23: external-publish + mutating gh api wrapped in indirection.
        ('bash -c "npm publish"', Operation.EXTERNAL_PUBLISH),
        ("eval 'twine upload dist/*'", Operation.EXTERNAL_PUBLISH),
        ('bash -c "gh api repos/o/r/merges -X PUT"', Operation.UNKNOWN),
        # T23 review (naysayer MUST-1): field-flag gh api via indirection (gh
        # defaults to POST) + lowercase verb must also deny, not fall to READ.
        ('bash -c "gh api repos/o/r/pulls -f title=x"', Operation.UNKNOWN),
        ('bash -c "gh api repos/o/r/merges -f base=main"', Operation.UNKNOWN),
        ('bash -c "gh api repos/o/r/contents/x -X post"', Operation.UNKNOWN),
        # T23 review (main SHOULD): direct `-X` with value concatenated (no space).
        ("gh api repos/o/r/merges -XPUT", Operation.UNKNOWN),
        ("gh api repos/o/r/contents/x -XDELETE", Operation.UNKNOWN),
        ("gh api repos/o/r -Xget", Operation.GITHUB_READ),
    ],
)
def test_classify_bash(cmd: str, expected: Operation) -> None:
    assert classify_tool_call("Bash", {"command": cmd}).operation is expected


# --------------------------------------------------------------------------- #
# T27: indirection is unified via recursion (direct == wrapped), not a regex
# mirror. These cover the shell-extraction edge cases (nesting, multiple -c,
# $() nesting, tokenizer-defeating quoting, depth bound).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # nested wrappers: the extracted inner is recursed through the SAME
        # classifier, so danger surfaces however deep the wrapper nests.
        ("bash -c \"bash -c 'rm -rf x'\"", Operation.FS_DELETE),
        ("eval \"bash -c 'git push --force origin feature/x'\"", Operation.FORCE_PUSH),
        # nested command substitution.
        ("echo $(echo $(rm -rf z))", Operation.FS_DELETE),
        ("echo $(git push --force origin feature/x)", Operation.FORCE_PUSH),
        # a wrapped main-merge one-liner is still surfaced as merge-to-main.
        ('bash -c "git checkout main && git merge develop"', Operation.GIT_MERGE_TO_MAIN),
        # gh api precision now comes from recursion (single source), not a regex
        # mirror: a wrapped field-flag gh api (gh defaults to POST) still denies.
        ('bash -c "gh api repos/o/r/pulls --field title=x"', Operation.UNKNOWN),
        # legit wrapped commands stay allowed (EXEC_CODE) — not over-denied.
        ('bash -c "pytest -q && echo done"', Operation.EXEC_CODE),
        ("sh -c 'uv run mypy src'", Operation.EXEC_CODE),
        # `bash script.sh` runs a file (not -c) → not inline indirection.
        ("bash deploy.sh", Operation.EXEC_CODE),
        # backticks: a closed pair, and (main #2) an unclosed trailing backtick
        # whose remainder is taken as the body — deny-safe, symmetric with $(.
        ("echo `rm -rf x` done", Operation.FS_DELETE),
        ("echo `gh pr merge 5", Operation.GIT_MERGE_TO_MAIN),
    ],
)
def test_classify_bash_indirection_recursed(cmd: str, expected: Operation) -> None:
    assert classify_tool_call("Bash", {"command": cmd}).operation is expected


@pytest.mark.parametrize(
    "inner",
    [
        "rm -rf x",
        "git push --force origin feature/x",
        "git reset --hard HEAD~3",
        "npm publish",
        "gh api repos/o/r/merges -X PUT",
        "gh api repos/o/r/pulls -f title=x",
        "gh pr merge 5",
        "pytest -q",
        "git status",
    ],
)
def test_classify_direct_equals_wrapped(inner: str) -> None:
    # The T27 invariant: wrapping a command in `bash -c "..."` must not change its
    # classification — direct and indirection share one classifier (no drift).
    direct = classify_tool_call("Bash", {"command": inner}).operation
    wrapped = classify_tool_call("Bash", {"command": f'bash -c "{inner}"'}).operation
    assert direct == wrapped


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # ANSI-C $'...' quoting hides the verb from shlex → recursion mis-tokenizes,
        # but the coarse defence-in-depth floor still catches the raw verb.
        ("eval $'rm -rf x'", Operation.FS_DELETE),
        # a Tier C verb smuggled into a non-executed `bash -c X Y` slot (only X
        # runs) is still denied by the coarse floor.
        ('bash -c "echo hi" -c "rm -rf x"', Operation.FS_DELETE),
    ],
)
def test_classify_bash_coarse_floor_backstops_untokenizable(cmd: str, expected: Operation) -> None:
    assert classify_tool_call("Bash", {"command": cmd}).operation is expected


def test_classify_bash_nesting_depth_fails_closed() -> None:
    # Pathologically nested indirection exceeds the recursion bound → fail closed
    # (UNKNOWN → deny) rather than spin or silently pass. Neutral inner verb so the
    # coarse floor doesn't classify it first.
    cmd = "$(" * 10 + "git status" + ")" * 10
    assert classify_tool_call("Bash", {"command": cmd}).operation is Operation.UNKNOWN


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # #74 naysayer MUST-1 / Copilot: a launcher before `bash -c` must not hide
        # the inner from the structural classifier (#71 MUST-1 must not regress).
        ('exec bash -c "gh api repos/o/r/merges -f base=main"', Operation.UNKNOWN),
        ('env bash -c "gh api repos/o/r/merges -f base=main"', Operation.UNKNOWN),
        ('command bash -c "rm -rf x"', Operation.FS_DELETE),
        # value-taking launcher (timeout DURATION / nice -n N) before the shell.
        ('timeout 5 bash -c "gh api repos/o/r/merges -f base=main"', Operation.UNKNOWN),
        ('nice -n 10 bash -c "git push --force origin feature/x"', Operation.FORCE_PUSH),
        # value-taking bash OPTIONS hide `-c` (--rcfile F / -O shopt): skip the arg.
        ('bash --rcfile myrc -c "rm -rf x"', Operation.FS_DELETE),
        ('bash --rcfile myrc -c "gh api repos/o/r/merges -f base=main"', Operation.UNKNOWN),
        ('bash -O extglob -c "rm -rf x"', Operation.FS_DELETE),
        # #74 main Round-2 MINOR: lookahead for value-taking options used WITHOUT
        # their argument (`bash -O -c "<x>"` / `bash +O -c …` / `bash --rcfile -c
        # …`). Real bash 5.x errors out on these so the runtime would not execute
        # the inner, but classify on intent so the gate denies the form anyway
        # (defensive against a future bash / non-bash shell with looser semantics).
        ('bash -O -c "rm -rf x"', Operation.FS_DELETE),
        ('bash +O -c "gh api repos/o/r/merges -f base=main"', Operation.UNKNOWN),
        ('bash --rcfile -c "rm -rf x"', Operation.FS_DELETE),
        # control: option used WITH its argument still parses as before (no false deny).
        ('bash -O extglob -c "echo hi"', Operation.EXEC_CODE),
        # leading shell flags / option-arg + ANSI-C inner: structural mis-tokenizes,
        # but the (broadened) indirection gate lets the coarse floor catch the verb.
        ("bash -l -c $'rm -rf x'", Operation.FS_DELETE),
        ("bash --rcfile x -c $'rm -rf x'", Operation.FS_DELETE),
        # must NOT over-deny: a launcher/shell name as a mere argument isn't a wrapper.
        ('echo bash -c "hello world"', Operation.EXEC_CODE),
    ],
)
def test_classify_bash_launcher_and_option_wrappers(cmd: str, expected: Operation) -> None:
    # #74 naysayer MUST-1: `_indirection_inner` must reach the inner across leading
    # launchers (env/exec/timeout) and value-taking bash options (--rcfile/-O), so
    # the #71 field-flag-gh-api indirection bypass cannot reappear behind a wrapper.
    assert classify_tool_call("Bash", {"command": cmd}).operation is expected


def test_classify_push_feature_branch_params() -> None:
    a = classify_tool_call("Bash", {"command": "git push origin feature/x"})
    assert a.branch == "feature/x"
    assert a.force is False


@pytest.mark.parametrize(
    "cmd", ["git push origin main", "git push origin HEAD:main", "git push origin develop:main"]
)
def test_classify_push_to_main_detected(cmd: str) -> None:
    a = classify_tool_call("Bash", {"command": cmd})
    assert a.operation is Operation.GIT_PUSH
    assert a.branch == "main"


def test_classify_merge_source_extracted() -> None:
    a = classify_tool_call("Bash", {"command": "git merge feature/x"})
    assert a.operation is Operation.GIT_MERGE
    assert a.source == "feature/x"


def test_classify_compound_picks_most_dangerous() -> None:
    rm = classify_tool_call("Bash", {"command": "cd src && rm -rf x"})
    assert rm.operation is Operation.FS_DELETE
    push = classify_tool_call("Bash", {"command": "pytest && git push origin feature/x"})
    assert push.operation is Operation.GIT_PUSH


def test_classify_checkout_main_then_merge() -> None:
    a = classify_tool_call("Bash", {"command": "git checkout main && git merge develop"})
    assert a.operation is Operation.GIT_MERGE_TO_MAIN


def test_classify_env_prefix_force_push() -> None:
    a = classify_tool_call("Bash", {"command": "FOO=bar git push --force origin feature/x"})
    assert a.operation is Operation.FORCE_PUSH


def test_classify_sudo_rm() -> None:
    a = classify_tool_call("Bash", {"command": "sudo rm -rf /tmp/x"})
    assert a.operation is Operation.FS_DELETE


@pytest.mark.parametrize(
    "name,inp,expected",
    [
        ("mcp__github__merge_pull_request", {}, Operation.GIT_MERGE_TO_MAIN),
        ("mcp__github__create_pull_request", {"base": "develop"}, Operation.GITHUB_PR_OPEN),
        ("mcp__github__delete_file", {}, Operation.FS_DELETE),
        ("mcp__github__create_or_update_file", {"branch": "feature/x"}, Operation.GIT_PUSH),
        ("mcp__github__push_files", {"branch": "feature/x"}, Operation.GIT_PUSH),
        ("mcp__drive__smart_update_document", {}, Operation.DRIVE_WRITE),
        ("mcp__github__get_file_contents", {}, Operation.GITHUB_READ),
        ("mcp__weird__frobnicate", {}, Operation.UNKNOWN),
        # default-deny: a "delete" variant must not pass as read; unknown read-ish → deny.
        ("mcp__github__list_and_delete", {}, Operation.FS_DELETE),
        ("mcp__github__list_widgets", {}, Operation.UNKNOWN),
    ],
)
def test_classify_mcp(name: str, inp: dict[str, Any], expected: Operation) -> None:
    assert classify_tool_call(name, inp).operation is expected


def test_force_push_to_main_denied_end_to_end(tmp_path: Path) -> None:
    """`git push --force origin main` still denied — the invariant that survives the widening.

    ``force_push`` was moved from unconditional Tier C to branch-scoped Tier A on 2026-08-15
    (T-branch-scoped-implementer-permissions). What must not budge is that ``main`` stays
    off-limits: `_push_target_and_force` surfaces the destination as ``"main"`` so the
    ``branch_glob`` outside ``feature/*`` / ``develop`` fires and denies.
    """
    al = default_allowlist(repo_root=tmp_path)
    action = classify_tool_call("Bash", {"command": "git push --force origin main"})
    assert action.operation is Operation.FORCE_PUSH
    assert action.branch == "main"
    assert al.check(action).allowed is False


def test_force_push_to_feature_allowed_end_to_end(tmp_path: Path) -> None:
    """`git push --force origin feature/x` now allowed (branch-scoped Tier A, 2026-08-15).

    ``main`` is the line, not the verb — see the docstring of ``allowlist`` and the yaml
    comment above the ``force_push`` allow rule.
    """
    al = default_allowlist(repo_root=tmp_path)
    action = classify_tool_call("Bash", {"command": "git push --force origin feature/x"})
    assert action.operation is Operation.FORCE_PUSH
    assert action.branch == "feature/x"
    assert al.check(action).allowed is True


def test_repo_internal_write_allowed_end_to_end(tmp_path: Path) -> None:
    al = default_allowlist(repo_root=tmp_path)
    action = classify_tool_call("Write", {"file_path": str(tmp_path / "src" / "x.py")})
    assert al.check(action).allowed is True


# --------------------------------------------------------------------------- #
# guard
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_guard_allows_exec(tmp_path: Path) -> None:
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "pytest"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow)
    assert guard.violations == []


@pytest.mark.anyio
async def test_guard_denies_force_push_to_main_with_interrupt(tmp_path: Path) -> None:
    """Denial still fail-loud (``interrupt=True``) when a force push targets ``main``.

    The refspec surfaces the target as ``"main"`` in the classifier, so this path does
    not need HEAD enrichment — it is the direct-argument deny. See
    ``test_guard_force_push_bare_on_main_denied`` below for the enrichment counterpart.
    """
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git push --force origin main"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    assert res.interrupt is True
    assert res.message
    assert len(guard.violations) == 1
    assert guard.violations[0].operation is Operation.FORCE_PUSH


# --------------------------------------------------------------------------- #
# guard branch enrichment (fail-closed on missing branch/target)
# --------------------------------------------------------------------------- #


def _init_head(repo_root: Path, branch: str | None) -> None:
    """Make ``repo_root`` a real git repo checked out on ``branch``.

    ``_current_branch`` now shells out to ``git rev-parse --abbrev-ref HEAD``
    (worktree / packed-ref safe, T23), so the test needs a real repo rather than a
    hand-written ``.git/HEAD``. ``branch=None`` leaves a non-repo → ``rev-parse``
    fails → None (fail-closed).
    """
    if branch is None:
        return
    subprocess.run(
        ["git", "init", "-q", "-b", branch], cwd=repo_root, check=True, capture_output=True
    )
    # An empty commit makes the branch born, so `git rev-parse --abbrev-ref HEAD`
    # returns it on every git version. Identity via -c (don't depend on global cfg).
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@e.test",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )


def _init_detached_head(repo_root: Path) -> None:
    """Make ``repo_root`` a real git repo checked out at a detached HEAD.

    Initialises on ``main``, records the commit sha, then checks that sha out
    directly so ``git rev-parse --abbrev-ref HEAD`` returns the literal
    ``HEAD`` (which ``_current_branch`` treats as None → fail-closed).
    """
    _init_head(repo_root, "main")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "checkout", "-q", "--detach", sha],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )


@pytest.mark.anyio
async def test_guard_bare_push_on_feature_allowed(tmp_path: Path) -> None:
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git push"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow)


@pytest.mark.anyio
async def test_guard_bare_push_on_main_denied(tmp_path: Path) -> None:
    # branch enriched from HEAD (main) → outside feature/*+develop → deny.
    _init_head(tmp_path, "main")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git push"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    assert guard.violations[-1].operation is Operation.GIT_PUSH


@pytest.mark.anyio
async def test_guard_commit_on_main_denied(tmp_path: Path) -> None:
    _init_head(tmp_path, "main")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git commit -m wip"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)


@pytest.mark.anyio
async def test_guard_merge_while_on_main_denied(tmp_path: Path) -> None:
    # `git merge feature/x` while on main → target enriched to main → deny.
    _init_head(tmp_path, "main")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git merge feature/x"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)


@pytest.mark.anyio
async def test_guard_sync_merge_on_feature_allowed(tmp_path: Path) -> None:
    """`git merge origin/main` on a feature branch — the exact call that halted the loop.

    Target is enriched to `feature/x` from HEAD, which is what contains it: the merge cannot
    touch a protected branch. See the SYNC rule in implementer_allowlist.yaml.
    """
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git merge origin/main"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow)
    assert guard.violations == []


@pytest.mark.anyio
async def test_guard_merge_undeterminable_branch_fails_closed(tmp_path: Path) -> None:
    """No repo → merge target cannot be resolved → UNKNOWN → deny.

    This carries more weight since the SYNC rule landed: `_constraints_pass` skips a target
    constraint when the target is None, so the merge path's containment now rests on this
    enrichment. The push variant below was already covered; merge was not.
    """
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git merge origin/main"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    assert guard.violations[-1].operation is Operation.UNKNOWN


@pytest.mark.anyio
async def test_guard_undeterminable_branch_fails_closed(tmp_path: Path) -> None:
    # no .git/HEAD → branch can't be resolved → downgrade to UNKNOWN → deny.
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git push"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    assert guard.violations[-1].operation is Operation.UNKNOWN


# --------------------------------------------------------------------------- #
# guard: force_push / history_rewrite branch enrichment
#
# `force_push` and `history_rewrite` moved from unconditional Tier C to
# branch-scoped Tier A on 2026-08-15 (T-branch-scoped-implementer-permissions).
# The move rests on TWO things happening together — the allow rule with
# `branch_glob`, AND the enrichment of the current branch from `git rev-parse`.
# Miss the enrichment and `branch_glob` never fires, because the classifier
# emits `branch=None` for both operations (rebase mutates HEAD, a bare
# `git push --force` inherits the checkout).
#
# These tests are the ONLY guard against silently missing a new op from the
# `_enrich` enumeration (a static enum omission produces zero warnings and is
# invisible to type checks). If a future widening reuses `branch_glob` without
# also touching `_enrich`, these tests must break — that is what they are for.
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_guard_force_push_bare_on_feature_allowed(tmp_path: Path) -> None:
    """Bare ``git push --force`` on a feature branch — HEAD → ``feature/x`` → allow."""
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git push --force"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow)
    assert guard.violations == []


@pytest.mark.anyio
async def test_guard_force_push_bare_on_main_denied(tmp_path: Path) -> None:
    """Bare ``git push --force`` on ``main`` — HEAD → ``main`` → outside glob → deny.

    The refspec-explicit path was already covered by
    ``test_guard_denies_force_push_to_main_with_interrupt``; this is the
    enrichment path, where the classifier emitted ``branch=None`` and the guard
    filled it in.
    """
    _init_head(tmp_path, "main")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git push --force"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    assert guard.violations[-1].operation is Operation.FORCE_PUSH


@pytest.mark.anyio
async def test_guard_force_push_bare_non_repo_fails_closed(tmp_path: Path) -> None:
    """No repo → HEAD undecidable → UNKNOWN → deny (fail-closed).

    Pinned per msg-1068: a future op widened via ``branch_glob`` without an
    entry in ``_enrich`` will leave ``branch=None`` on this call and slip
    through ``_branch_matches``. This test only stays green if
    ``FORCE_PUSH`` is in the enrichment enumeration.
    """
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git push --force"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    assert guard.violations[-1].operation is Operation.UNKNOWN


@pytest.mark.anyio
async def test_guard_force_push_bare_detached_head_fails_closed(tmp_path: Path) -> None:
    """Detached HEAD → ``_current_branch`` returns None → UNKNOWN → deny.

    The other half of the enumeration pin: a real repo, but HEAD is not on a
    named branch, so enrichment cannot assign one. ``rev-parse --abbrev-ref``
    prints ``HEAD`` in that state and ``_current_branch`` returns None (which
    the enrichment downgrades to UNKNOWN → default-deny).
    """
    _init_detached_head(tmp_path)
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git push --force"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    assert guard.violations[-1].operation is Operation.UNKNOWN


@pytest.mark.anyio
async def test_guard_rebase_on_feature_allowed(tmp_path: Path) -> None:
    """``git rebase`` on a feature branch — HEAD → ``feature/x`` → allow.

    The exact call that was previously halting the loop (msg-1056 listed
    ``spirrow-voxelworld/T-slope-extension-dead-mode`` as denied on
    ``history_rewrite``). After the widening it now runs.
    """
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git rebase origin/develop"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow)
    assert guard.violations == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("label", "command"),
    [
        ("upstream-and-branch", "git rebase develop main"),
        ("remote-upstream", "git rebase origin/develop main"),
        ("onto-form", "git rebase --onto develop feature/y main"),
    ],
)
async def test_guard_rebase_naming_main_explicitly_is_denied(
    tmp_path: Path, label: str, command: str
) -> None:
    """``git rebase <upstream> <branch>`` rewrites ``<branch>``, not HEAD.

    Measured: standing on ``feature/x``, ``git rebase develop main`` checked out
    ``main`` and moved it. Reading every rebase as "targets HEAD" and filling in
    the current branch therefore let a rewrite of ``main`` borrow a feature
    branch's permission — the bypass Tier B found on PR #158. The classifier now
    names the target, so the glob sees ``main`` and refuses.
    """
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": command}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny), label


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("label", "command"),
    [
        # git stops parsing options at `--`, so these name `main` as <branch>.
        # Measured: git itself refuses the first two with `fatal: invalid
        # upstream`, so neither rewrote anything — but the parser agrees with git
        # rather than relying on which of git's errors happen to save us.
        ("dash-dash-then-flag", "git rebase -- -i main"),
        ("dash-dash-then-onto", "git rebase -- --onto main"),
        # And the one that really does rewrite `main`, measured: 7fec953 ->
        # 9751652 with HEAD left on `main`.
        ("dash-dash-then-two", "git rebase -- develop main"),
        ("dash-dash-after-upstream", "git rebase develop -- main"),
    ],
)
async def test_guard_rebase_after_a_double_dash_is_still_read_as_git_reads_it(
    tmp_path: Path, label: str, command: str
) -> None:
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": command}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny), label


@pytest.mark.anyio
async def test_guard_rebase_naming_a_feature_branch_is_allowed(tmp_path: Path) -> None:
    """The other side: an explicit target inside the glob still works."""
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git rebase develop feature/z"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow)


@pytest.mark.anyio
async def test_guard_rebase_with_an_unrecognised_flag_is_denied(tmp_path: Path) -> None:
    """An unconsumed flag value is indistinguishable from a branch name.

    ``git rebase --exec make develop`` reads as two positionals, which would name
    ``develop`` as the target while the real target is HEAD — fail-OPEN when HEAD
    is ``main``. Refusing what we do not recognise costs a denial the implementer
    can avoid by writing the simple form.
    """
    _init_head(tmp_path, "main")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard(
        "Bash", {"command": "git rebase --exec make develop"}, ToolPermissionContext()
    )
    assert isinstance(res, PermissionResultDeny)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("label", "command"),
    [
        # Every one measured against `main` from `feature/x`, and every one landed.
        ("force-move", "git branch -f main HEAD"),
        ("force-rename-onto-main", "git branch -M main"),
        ("delete", "git branch -D main"),
        ("rename-away", "git branch -m main renamed"),
        # `-C <src> <dst>` clobbers the SECOND name, which one `branch` field
        # cannot express — so rename and copy are refused outright.
        ("force-copy-onto-main", "git branch -C feature/x main"),
        # `-D a b` deletes both and only one fits in `branch`.
        ("delete-many", "git branch -D feature/a main"),
        ("force-after-double-dash", "git branch -f -- main"),
        # Round 4: git does not require the destructive flag to come first, and
        # an earlier streaming parse met `-v` while `delete` was still false,
        # called it a listing, and let git delete `main`. Every case above put
        # the destructive flag first, which is exactly why they all passed.
        ("unknown-flag-before-delete", "git branch -v -D main"),
        ("unknown-long-flag-before-force", "git branch --track -f main origin/main"),
        # Bundled shorts: measured, `-vD main` and `-Dv main` both delete `main`,
        # so a bundle has to be read as its letters.
        ("bundled-verbose-delete", "git branch -vD main"),
        ("bundled-delete-verbose", "git branch -Dv main"),
        ("delete-after-double-dash", "git branch -D -- main"),
        # A value-taking flag alongside a destructive one: the value would be
        # miscounted as a branch name, so it is refused rather than guessed.
        ("unmodelled-flag", "git branch -f --contains x main"),
    ],
)
async def test_guard_destructive_git_branch_on_main_is_denied(
    tmp_path: Path, label: str, command: str
) -> None:
    """`git branch -f main <commit>` is a history rewrite by another spelling.

    It classified as `exec.code` and ran at Tier A — pre-existing rather than
    introduced by the branch-glob widening, but it made this PR's promise
    ("history rewrites are denied on `main`") false as written.
    """
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": command}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny), label


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("label", "command"),
    [
        ("force-move-within-glob", "git branch -f feature/y HEAD"),
        ("delete-within-glob", "git branch -D feature/old"),
        ("bundled-quiet-delete-within-glob", "git branch -qD feature/old"),
        ("create", "git branch feature/new"),
        ("list", "git branch"),
        ("list-all", "git branch -a"),
        ("list-verbose", "git branch -vv"),
        ("filter-by-merged", "git branch --merged main"),
        # git: "'-f' is not a valid branch name". After `--` it is a NAME, so
        # this is a failing creation, not a destructive form — measured, `main`
        # was untouched. The parser reads it the way git does.
        ("double-dash-makes-it-a-name", "git branch -- -f main"),
    ],
)
async def test_guard_ordinary_git_branch_still_runs(
    tmp_path: Path, label: str, command: str
) -> None:
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": command}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow), label


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("label", "command"),
    [
        # Measured ALLOWED before this rule: validation saw `feature/x`, the
        # script then destroyed `main`.
        ("checkout-then-reset", "git checkout main && git reset --hard HEAD~1"),
        ("switch-then-reset", "git switch main && git reset --hard HEAD~1"),
        ("checkout-then-rebase", "git checkout main && git rebase origin/develop"),
        ("checkout-then-force-push", "git checkout main && git push --force"),
        # No checkout at all — the tracking change redirects the bare push.
        (
            "retarget-then-force-push",
            "git config push.default upstream && git branch -u origin/main && git push --force",
        ),
        # Round 6: the same trap catches every HEAD-enriched operation, not only
        # the two this PR widened. A push to `main` walks past the Tier C gate.
        ("checkout-then-push", "git checkout main && git push"),
        ("checkout-then-commit", "git checkout main && git commit -m x"),
        ("script-then-force-push", "bash s.sh && git push --force"),
        # Anything chained with one of the two operations this PR widened is
        # refused, whether or not it looks dangerous: they are rare enough that
        # the strict rule costs nothing the implementer cannot split into two
        # tool calls.
        ("read-only-prefix-force-push", "git status && git push --force"),
        ("commit-then-force-push", "git commit -m x && git push --force"),
        # A rebase that NAMES a branch checks it out first, so it is not
        # branch-preserving company for what follows.
        ("explicit-rebase-then-push", "git rebase develop main && git push"),
    ],
)
async def test_guard_a_chained_destructive_step_cannot_trust_ambient_head(
    tmp_path: Path, label: str, command: str
) -> None:
    """The guard reads HEAD before the shell runs; a chain can move it first."""
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": command}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny), label


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("label", "command"),
    [
        ("alone-reset", "git reset --hard HEAD~1"),
        ("alone-rebase", "git rebase origin/develop"),
        ("alone-force-push", "git push --force"),
        # Names its own target, so it never consults HEAD and chaining is moot.
        ("explicit-target", "git rebase develop feature/z"),
        # The loop's most common command. Denying it would be a gate people
        # route around, so for commit/push the rule turns on "can this step
        # switch branches", not "is the destructive step alone".
        ("stage-then-commit", "git add . && git commit -m x"),
        ("stage-commit-push", "git add -A && git commit -m x && git push"),
        ("diff-then-commit", "git diff --staged && git commit -m x"),
    ],
)
async def test_guard_a_lone_destructive_step_still_runs(
    tmp_path: Path, label: str, command: str
) -> None:
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": command}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow), label


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("label", "command"),
    [
        # `detail` is the classifier's tokens rejoined, so the quotes are gone
        # and shlex refused the bare apostrophe. That refusal used to deny the
        # chained commit — a denial with nothing behind it. (Tier B, round 7.)
        ("apostrophe-in-a-path", "git add " + _DQ + "don't.txt" + _DQ + " && git commit -m x"),
        ("space-in-a-path", "git add " + _DQ + "a b.txt" + _DQ + " && git commit -m x"),
        # A bare `-` is git's shorthand for the previous branch: measured,
        # `git rebase -` succeeds, so refusing it protected nothing.
        ("previous-branch-shorthand", "git rebase -"),
    ],
)
async def test_guard_quoting_and_shorthand_do_not_cost_a_denial(
    tmp_path: Path, label: str, command: str
) -> None:
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": command}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow), label


@pytest.mark.anyio
async def test_guard_previous_branch_shorthand_still_names_an_explicit_target(
    tmp_path: Path,
) -> None:
    """`-` is a positional, so a second one is still the branch being rewritten."""
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git rebase - main"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)


@pytest.mark.anyio
async def test_known_gap_an_opaque_step_before_a_plain_push_is_not_seen(
    tmp_path: Path,
) -> None:
    """Records a limit rather than a guarantee, so nobody reads one for the other.

    `_may_switch_branch` is a denylist, so a step it does not recognise passes.
    `make deploy` can check out `main` inside the Makefile and `git push` then
    runs there. The stricter rule — refuse any chain whose other steps are not
    provably branch-preserving — was written and measured: it failed 33 tests,
    because the classifier splits a heredoc's body and terminator into separate
    actions, so every `git commit -F - <<'EOF' … EOF` counted as a chain.

    The exposure is the one `git commit` / `git push` have carried since they
    were first branch-scoped; this PR neither widens nor closes it. The two
    operations this PR DOES widen get the strict rule, where it costs nothing.
    """
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "make deploy && git push"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow)


@pytest.mark.anyio
async def test_guard_filter_branch_stays_denied(tmp_path: Path) -> None:
    """`filter-branch` takes rev-list arguments, so any of them can name a branch.

    Recognising those shapes would be cost without a caller, so it keeps the
    Tier C denial it had before this widening — on a feature branch too.
    """
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard(
        "Bash",
        {"command": "git filter-branch --tree-filter true -- main"},
        ToolPermissionContext(),
    )
    assert isinstance(res, PermissionResultDeny)


@pytest.mark.anyio
async def test_guard_rebase_on_main_denied(tmp_path: Path) -> None:
    """``git rebase`` while on ``main`` — HEAD → ``main`` → outside glob → deny.

    The trap Bohr flagged in msg-1056 §2: ``git rebase`` carries no target
    argument, so a naïve reading of ``action.branch`` is ``None`` — and
    ``_branch_matches(None, ...)`` is fail-OPEN. This test only passes because
    ``_enrich`` fills the branch from HEAD before ``check`` runs.
    """
    _init_head(tmp_path, "main")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git rebase origin/develop"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    assert guard.violations[-1].operation is Operation.HISTORY_REWRITE


@pytest.mark.anyio
async def test_guard_history_rewrite_non_repo_fails_closed(tmp_path: Path) -> None:
    """No repo → HEAD undecidable → UNKNOWN → deny (fail-closed).

    Pin for ``HISTORY_REWRITE`` mirroring the ``FORCE_PUSH`` case above; both
    must be enumerated in ``_enrich`` for the widening to be safe.
    """
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git rebase origin/develop"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    assert guard.violations[-1].operation is Operation.UNKNOWN


@pytest.mark.anyio
async def test_guard_history_rewrite_detached_head_fails_closed(tmp_path: Path) -> None:
    """Detached HEAD → UNKNOWN → deny (fail-closed) for ``git rebase`` too."""
    _init_detached_head(tmp_path)
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git rebase origin/develop"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    assert guard.violations[-1].operation is Operation.UNKNOWN


@pytest.mark.anyio
async def test_guard_reset_hard_on_feature_allowed(tmp_path: Path) -> None:
    """``git reset --hard`` classifies as ``HISTORY_REWRITE``; allowed on a feature branch."""
    _init_head(tmp_path, "feature/x")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git reset --hard HEAD~1"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultAllow)
    assert guard.violations == []


@pytest.mark.anyio
async def test_guard_reset_hard_on_main_denied(tmp_path: Path) -> None:
    """``git reset --hard`` while on ``main`` — HEAD → ``main`` → deny."""
    _init_head(tmp_path, "main")
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard("Bash", {"command": "git reset --hard HEAD~1"}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    assert guard.violations[-1].operation is Operation.HISTORY_REWRITE


# --------------------------------------------------------------------------- #
# adapter lifecycle (fake SDK client)
# --------------------------------------------------------------------------- #


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="test-model")


def _result(*, is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="t",
        stop_reason="end_turn",
        result="ok",
    )


class _FakeSdkClient:
    """Structural stand-in that can drive the options' can_use_tool guard."""

    def __init__(
        self,
        options: Any,
        *,
        responses: list[Any],
        simulate_tool: tuple[str, dict[str, Any]] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.options = options
        self._can_use_tool = options.can_use_tool
        self._responses = responses
        self._simulate_tool = simulate_tool
        self._fail_on = fail_on
        self.connected = False
        self.disconnected = False
        self.interrupt_count = 0
        self.queries: list[str] = []

    async def connect(self) -> None:
        if self._fail_on == "connect":
            raise RuntimeError("connect boom")
        self.connected = True

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        if self._simulate_tool is not None:
            name, inp = self._simulate_tool
            await self._can_use_tool(name, inp, ToolPermissionContext())

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self._responses:
            yield message

    async def interrupt(self) -> None:
        self.interrupt_count += 1

    async def disconnect(self) -> None:
        self.disconnected = True


def _factory(
    *,
    responses: list[Any],
    simulate_tool: tuple[str, dict[str, Any]] | None = None,
    fail_on: str | None = None,
    capture: list[_FakeSdkClient] | None = None,
) -> Callable[[Any], _FakeSdkClient]:
    def make(options: Any) -> _FakeSdkClient:
        client = _FakeSdkClient(
            options, responses=responses, simulate_tool=simulate_tool, fail_on=fail_on
        )
        if capture is not None:
            capture.append(client)
        return client

    return make


def _thread_ref() -> ThreadRef:
    return ThreadRef(project_id="spirrow-mindwire", thread_id="01JTHREAD", chatroom_uri="mc://t/1")


def _ctx(captured: list[ReplyDraft]) -> SpawnContext:
    async def on_reply(draft: ReplyDraft) -> None:
        captured.append(draft)

    async def on_event_log(_event: Event) -> None:
        return None

    return SpawnContext(
        on_reply=on_reply,
        on_event_log=on_event_log,
        own_role=Role.IMPLEMENTER,
        own_instance_id="implementer-1",
    )


def _event(
    *, author: str = "human", body: str = "do it", event_type: EventType = EventType.NEW_MESSAGE
) -> ChatroomEvent:
    return ChatroomEvent(
        event_id="01JEVENT",
        event_type=event_type,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id="m1", author=author, body=body, parent_msg_id=None),
    )


def test_capabilities_execute_code_not_naysayer() -> None:
    caps = ImplementerSdkAdapter.capabilities
    assert Capability.EXECUTE_CODE in caps
    assert Capability.NAYSAYER_QUALIFIED not in caps


def test_satisfies_roleadapter_protocol(tmp_path: Path) -> None:
    adapter: RoleAdapter = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url="http://lx"
    )
    assert adapter.adapter_id == "implementer-sdk"


@pytest.mark.anyio
async def test_spawn_requires_inference_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MINDWIRE_IMPLEMENTER_BASE_URL", raising=False)
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, client_factory=_factory(responses=[])
    )
    with pytest.raises(ImplementerSdkSpawnError):
        await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))


@pytest.mark.anyio
async def test_spawn_routes_inference_via_base_url(tmp_path: Path) -> None:
    cap: list[_FakeSdkClient] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lexora:8110",
        client_factory=_factory(responses=[], capture=cap),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    opts = cap[0].options
    # never api.anthropic.com directly: ANTHROPIC_BASE_URL pinned to Lexora.
    assert opts.env["ANTHROPIC_BASE_URL"] == "http://lexora:8110"
    assert opts.can_use_tool is not None  # the allow-list guard is wired in
    assert opts.permission_mode == "default"  # NOT bypassPermissions
    assert handle.role is Role.IMPLEMENTER


@pytest.mark.anyio
async def test_make_options_exposes_builtins_and_isolates(tmp_path: Path) -> None:
    # T37: regression guard for the four wiring fixes. The whole "implementer
    # never ran for real" finding was that tools=[] disabled every built-in, so
    # pin the exposure + isolation + UTF-8 + guard wiring against silent drift.
    cap: list[_FakeSdkClient] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(responses=[], capture=cap),
    )
    await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    opts = cap[0].options
    # #1 built-ins exposed (NOT tools=[]): the core code/fs/exec/search tools.
    assert isinstance(opts.tools, list)
    assert opts.tools, "tools must not be empty (else no built-ins)"
    for tool in ("Read", "Write", "Edit", "Bash", "Glob", "Grep", "TodoWrite"):
        assert tool in opts.tools
    # the guard is still the single enforcement point — nothing auto-approved.
    assert opts.allowed_tools == []
    assert opts.can_use_tool is not None
    assert opts.permission_mode == "default"
    # #4 isolation: no host settings (connectors/CLAUDE.md/permissions) inherited,
    # and only explicitly-passed MCP servers are honored.
    assert opts.setting_sources == []
    assert opts.strict_mcp_config is True
    # #2 UTF-8 forced for the subprocess + any python the agent spawns.
    assert opts.env["PYTHONUTF8"] == "1"
    assert opts.env["PYTHONIOENCODING"] == "utf-8"
    # T40: the cwd grounding reaches the SDK system prompt (so the agent knows its working dir).
    assert str(tmp_path) in opts.system_prompt


def test_system_prompt_grounds_cwd(tmp_path: Path) -> None:
    # T40: the implementer runs with a custom system prompt (no claude_code working-dir section), so
    # it must be told its cwd explicitly — else it guesses an absolute path (observed on the
    # voxelworld conductor smoke: it targeted /home/user/<repo>/... and the guard fail-loud denied
    # the out-of-repo write). Grounding the cwd + mandating relative paths fixes it.
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url="http://lx"
    )
    sp = adapter._system_prompt
    assert str(tmp_path) in sp
    assert "WORKING DIRECTORY" in sp
    assert "relative path" in sp
    # grounding is appended, not a replacement — the role handoff guidance is preserved.
    assert "Conductor handoff protocol" in sp


# --- what the implementer may NOT read (2026-08-09) ------------------------- #
# Voxelworld PR #182: asked to "perform the ADR-2026-05-29-13 read-back", the session had neither
# the ADR body (separate docs repo) nor even the id->title map, so it reconstructed the ADR from
# context and stated the result as fact — three of five claims attributed to ADR-13 things it does
# not say. The failure was not ignorance but silent confident invention, so the fix is two halves:
# say what you cannot read, and know which ADRs exist. Neither half works alone.
#
# 2026-08-09 update (T-loop-readable-obligations): the DECLARE-UNREADABLE clause was MOVED to
# spec/process/obligations.yaml (OBL-DECLARE-UNREADABLE) and is now injected via the manifest.
# Per the Tier-C GO msg-737 ("delete the ping to the string literal? no — repoint at the rendered
# prompt"), this test was kept and its assertions repointed at the assembled ``_system_prompt``
# (which was already the target here). The check now verifies the WIRING — that the injection
# path lands the moved body in the rendered prompt — rather than the string literal that no
# longer exists in source.


def test_system_prompt_forbids_reconstructing_unreadable_documents(tmp_path: Path) -> None:
    sp = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url="http://lx"
    )._system_prompt
    assert "DOCUMENTS YOU CANNOT READ" in sp
    # The instruction must be to DECLARE the gap, not merely to be careful about it.
    assert "cannot read" in sp
    assert "do NOT reconstruct" in sp
    # And the injection wiring must actually be the one delivering it — the id label
    # travels alongside the body so a regression that dropped the injection path
    # (e.g. someone re-inlining the paragraph in source instead of using the manifest)
    # fires here as well as in the id-coverage canary.
    assert "[OBL-DECLARE-UNREADABLE]" in sp


def test_system_prompt_carries_the_adr_index_as_titles_only(tmp_path: Path) -> None:
    sp = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url="http://lx"
    )._system_prompt
    assert "ADR INDEX" in sp
    # A real id from the in-repo manifest — the map is present, not a placeholder.
    assert "ADR-2026-05-29-13" in sp
    # And it must be labelled for what it is. Handing over titles WITHOUT this caveat would invite
    # better-grounded confabulation, which is harder to catch than the original failure.
    assert "TITLES ONLY" in sp
    assert "NOT the ADRs" in sp


def test_adr_index_block_says_so_when_the_manifest_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing manifest is announced, never shipped as a silent gap (mirrors the naysayer)."""
    import spirrow_mindwire.adapters.implementer as impl

    monkeypatch.setattr(impl, "load_adr_index", lambda *a, **k: ())
    block = impl._adr_index_block()
    assert "UNAVAILABLE" in block
    assert "do not guess" in block


@pytest.mark.anyio
async def test_deliver_emits_reply_when_allowed(tmp_path: Path) -> None:
    captured: list[ReplyDraft] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(
            responses=[_assistant("done"), _result()],
            simulate_tool=("Bash", {"command": "pytest -q"}),
        ),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx(captured))
    await adapter.deliver_event(handle, _event(author="human"))
    assert len(captured) == 1
    assert captured[0].body == "done"
    assert (await adapter.health(handle)).state is SessionState.IDLE


@pytest.mark.anyio
async def test_deliver_allowlist_violation_fails_loud(tmp_path: Path) -> None:
    captured: list[ReplyDraft] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(
            responses=[_assistant("ignored"), _result()],
            # Force-pushing to ``main`` is still Tier C after 2026-08-15 (only the
            # branch-scoped feature/develop cases were widened) — this is the
            # allow-list violation we want the adapter lifecycle to surface fail-loud.
            simulate_tool=("Bash", {"command": "git push --force origin main"}),
        ),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx(captured))
    with pytest.raises(ImplementerAllowlistError):
        await adapter.deliver_event(handle, _event(author="human"))
    assert captured == []  # fail-loud: no reply posted
    hs = await adapter.health(handle)
    assert hs.state is SessionState.FAILED
    assert hs.error is not None
    assert hs.error.code == "adapter.allowlist_violation"


@pytest.mark.anyio
async def test_own_role_self_filter(tmp_path: Path) -> None:
    captured: list[ReplyDraft] = []
    cap: list[_FakeSdkClient] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(responses=[_assistant("x"), _result()], capture=cap),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx(captured))
    # I3 v2.2: the self-filter keys on instance_id ("implementer-1"), not the bare role.
    await adapter.deliver_event(handle, _event(author="implementer-1"))
    assert captured == []
    assert cap[0].queries == []


@pytest.mark.anyio
async def test_non_new_message_is_noop(tmp_path: Path) -> None:
    captured: list[ReplyDraft] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(responses=[_assistant("x"), _result()]),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx(captured))
    await adapter.deliver_event(handle, _event(event_type=EventType.THREAD_CLOSED))
    assert captured == []


@pytest.mark.anyio
async def test_halt_disconnects_and_is_idempotent(tmp_path: Path) -> None:
    cap: list[_FakeSdkClient] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(responses=[], capture=cap),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    await adapter.halt(handle)
    assert cap[0].disconnected is True
    assert (await adapter.health(handle)).state is SessionState.HALTED
    await adapter.halt(handle)  # idempotent no-op
    assert cap[0].interrupt_count == 1


@pytest.mark.anyio
async def test_deliver_on_halted_session_raises(tmp_path: Path) -> None:
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(responses=[]),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    await adapter.halt(handle)
    with pytest.raises(ImplementerSdkDeliveryError):
        await adapter.deliver_event(handle, _event())


def test_builtin_tool_set_is_self_consistent() -> None:
    # T37: the benign-whitelist set must be a subset of the exposed tools (a tool
    # the model can never call need not be whitelisted), and the core code/fs tools
    # the implementer's job needs must actually be exposed. Pins the two T37 #1/#3
    # constants against drifting apart.
    exposed = set(_IMPLEMENTER_BUILTIN_TOOLS)
    assert exposed >= _BENIGN_BUILTIN_TOOLS
    assert exposed >= {"Read", "Write", "Edit", "Bash", "Glob", "Grep"}
    # every exposed tool classifies to something the default allow-list can act on
    # (never UNKNOWN — that would silently halt the agent on a tool we handed it).
    for tool in exposed:
        assert classify_tool_call(tool, {}).operation is not Operation.UNKNOWN


# --------------------------------------------------------------------------- #
# manual SDK smoke (B3): verify the REAL SDK routes tool calls through the guard
# --------------------------------------------------------------------------- #


@pytest.mark.manual
@pytest.mark.anyio
async def test_manual_sdk_routes_tool_calls_through_guard(tmp_path: Path) -> None:
    """The whole gate rests on the real SDK calling can_use_tool for every tool.

    Run with a working Lexora Anthropic-compat endpoint:
        MINDWIRE_IMPLEMENTER_BASE_URL=... uv run pytest -m manual -k manual_sdk
    Asks the model to run a forbidden command and asserts the guard denied it
    (proving permission_mode=default + allowed_tools=[] route Bash through the
    guard, not auto-approval).
    """
    base = os.environ.get("MINDWIRE_IMPLEMENTER_BASE_URL")
    if not base:
        pytest.skip("set MINDWIRE_IMPLEMENTER_BASE_URL (Lexora Anthropic-compat) to run")
    _init_head(tmp_path, "feature/manual-smoke")
    adapter = ImplementerSdkAdapter(cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url=base)
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    try:
        with pytest.raises(ImplementerAllowlistError):
            await adapter.deliver_event(
                handle,
                _event(body="Run exactly this shell command and nothing else: rm -rf /tmp/denied"),
            )
        assert (await adapter.health(handle)).state is SessionState.FAILED
    finally:
        await adapter.halt(handle)


@pytest.mark.manual
@pytest.mark.anyio
async def test_manual_sdk_executes_allowed_tool_through_guard(tmp_path: Path) -> None:
    """The other half of the gate: an ALLOWED tool must actually EXECUTE (T37 #1).

    Before the tool-wiring fix the session was launched with ``tools=[]`` and the
    model had no built-in tools at all, so it could neither act nor trip the guard
    — the deny test above would pass for the wrong reason (model can't call Bash)
    while the implementer was in fact a no-op. This asks the model to create a
    file inside the repo (a Tier A ``fs.write``) and asserts the file really lands
    on disk, proving Write/Bash are exposed and the allow path runs end to end.

        MINDWIRE_IMPLEMENTER_BASE_URL=... uv run pytest -m manual -k executes_allowed
    """
    base = os.environ.get("MINDWIRE_IMPLEMENTER_BASE_URL")
    if not base:
        pytest.skip("set MINDWIRE_IMPLEMENTER_BASE_URL (Lexora Anthropic-compat) to run")
    _init_head(tmp_path, "feature/manual-exec")
    captured: list[ReplyDraft] = []
    adapter = ImplementerSdkAdapter(cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url=base)
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx(captured))
    marker = tmp_path / "t37_exec_proof.txt"
    try:
        await adapter.deliver_event(
            handle,
            _event(
                body=(
                    "Create a file named t37_exec_proof.txt in the current directory "
                    "containing exactly the text OK, using the Write tool. Then reply done."
                ),
            ),
        )
        assert marker.is_file(), "the allowed Write tool did not execute (tools likely still empty)"
        assert (await adapter.health(handle)).state is SessionState.IDLE
        assert captured, "expected a reply after the tool executed"
    finally:
        await adapter.halt(handle)


def test_default_system_prompt_includes_implementer_handoff_protocol() -> None:
    # PR-2b-1: the implementer ends every reply with a NEXT: line — hand back to the proposer for a
    # spec-review, or to the human for a Tier-C decision (it never merges to main itself).
    assert "Conductor handoff protocol" in _DEFAULT_IMPLEMENTER_SYSTEM_PROMPT
    assert "NEXT:" in _DEFAULT_IMPLEMENTER_SYSTEM_PROMPT
    assert "never merge" in _DEFAULT_IMPLEMENTER_SYSTEM_PROMPT
