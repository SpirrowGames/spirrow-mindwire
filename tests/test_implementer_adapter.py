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
        ("git filter-branch --tree-filter true HEAD", Operation.HISTORY_REWRITE),
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


def test_force_push_command_denied_end_to_end(tmp_path: Path) -> None:
    al = default_allowlist(repo_root=tmp_path)
    action = classify_tool_call("Bash", {"command": "git push --force origin feature/x"})
    assert al.check(action).allowed is False


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
async def test_guard_denies_force_push_with_interrupt(tmp_path: Path) -> None:
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    res = await guard(
        "Bash", {"command": "git push --force origin feature/x"}, ToolPermissionContext()
    )
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
            simulate_tool=("Bash", {"command": "git push --force origin feature/x"}),
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


# --------------------------------------------------------------------------- #
# SPEC-2026-08-11-denial-detail-and-overdeny — PR-1 observation
#
# These exercise the four observation fields on ``ClassifiedAction`` and the
# ``format_denial_detail`` reason wrapping. The classifier's verdicts are
# untouched by this PR (D-1); only the observation surface is new.
# --------------------------------------------------------------------------- #


from spirrow_mindwire.adapters.implementer import (  # noqa: E402  — after top-level tests
    _find_heredoc_bodies,
    _redact_secrets,
    format_denial_detail,
)
from spirrow_mindwire.allowlist import AllowlistDecision, ClassifiedAction  # noqa: E402


def test_ac3_backward_compat_classifiedaction_default_ctor() -> None:
    # AC3: ClassifiedAction still constructs with just an Operation — no callers
    # need to know about the new observation fields.
    a = ClassifiedAction(Operation.EXEC_CODE)
    assert a.raw_command is None
    assert a.heredoc_bodies == ()
    assert a.match_span is None
    assert a.corroborated == "unknown"


def test_ac6_non_bash_tool_is_unknown() -> None:
    # AC6: any classification that does not run through the Bash pipeline stays
    # ``unknown`` — nothing to corroborate against.
    for name, inp in [
        ("Write", {"file_path": "x.py"}),
        ("Read", {"file_path": "x.py"}),
        ("mcp__github__delete_file", {}),
    ]:
        a = classify_tool_call(name, inp)
        assert a.corroborated == "unknown", name
        assert a.raw_command is None, name


def test_bash_direct_tier_c_is_structural_only() -> None:
    # Direct `rm -rf x` — no indirection ∴ the coarse gate never opens, verdict
    # comes from the structural pass alone.
    a = classify_tool_call("Bash", {"command": "rm -rf x"})
    assert a.operation is Operation.FS_DELETE
    assert a.corroborated == "structural_only"
    assert a.raw_command == "rm -rf x"
    assert a.match_span is None  # no coarse-floor match (gate closed)


def test_bash_wrapped_tier_c_both_paths_agree() -> None:
    # `bash -c "rm -rf x"` — structural recursion extracts inner ∴ FS_DELETE;
    # the coarse floor also fires. They agree at FS_DELETE → structural_and_coarse.
    a = classify_tool_call("Bash", {"command": 'bash -c "rm -rf x"'})
    assert a.operation is Operation.FS_DELETE
    assert a.corroborated == "structural_and_coarse"
    assert a.match_span is not None  # coarse floor recorded WHERE it matched


def test_bash_coarse_only_on_ansi_c_quoting() -> None:
    # ANSI-C $'...' quoting defeats shlex tokenisation, so the structural pass
    # never surfaces the inner `rm` — only the coarse floor does. This is the
    # exact "coarse fires alone" case the PR-1 measurement wants to count.
    a = classify_tool_call("Bash", {"command": "eval $'rm -rf x'"})
    assert a.operation is Operation.FS_DELETE
    assert a.corroborated == "coarse_only"


def test_bash_benign_verdict_is_unknown() -> None:
    # A benign EXEC_CODE Bash command has no deny to corroborate. Keep it as
    # ``unknown`` so PR-1 measurements do not treat "nothing to say" as a
    # corroboration signal.
    a = classify_tool_call("Bash", {"command": "pytest -q"})
    assert a.operation is Operation.EXEC_CODE
    assert a.corroborated == "unknown"


# ---- heredoc detection -------------------------------------------------------


def test_find_heredoc_bodies_single_body() -> None:
    cmd = "cat > f.ps1 << 'EOF'\nline 1\nline 2\nEOF\necho done"
    bodies = _find_heredoc_bodies(cmd)
    assert len(bodies) == 1
    s, e = bodies[0]
    # Body starts after the opener line and ends just before the closing EOF line.
    assert cmd[s:e] == "line 1\nline 2\n"


def test_find_heredoc_bodies_no_close_reports_to_eof() -> None:
    # Under-detection is worse than over-reporting: an unclosed heredoc extends
    # its body to end-of-string, so the reader never mistakes "no body found" for
    # "match was outside a body".
    cmd = "cat << EOF\nnever closed\nRemove-Item x"
    bodies = _find_heredoc_bodies(cmd)
    assert len(bodies) == 1
    s, e = bodies[0]
    assert e == len(cmd)
    assert "Remove-Item x" in cmd[s:e]


def test_find_heredoc_bodies_none_when_absent() -> None:
    assert _find_heredoc_bodies("echo hi") == ()


# ---- AC2: PowerShell fixture (the halted session's failure shape) -----------


_PS_HEREDOC = (
    "cat > tests/Test-SweepQuarantine.ps1 << 'PS1'\n"
    "$parseErrors | ForEach-Object {\n"
    '  Write-Host "PARSE ERROR line $($_.Extent.StartLineNumber): $($_.Message)"\n'
    "}\n"
    "Check \"fresh (0h) -> quarantined\" 'quarantined' `\n"
    "$state = Get-Content foo.json\n"
    "Remove-Item -Recurse -Force $tmpDir\n"
    "PS1\n"
)


def test_ac2_powershell_heredoc_with_remove_item_records_observation() -> None:
    # AC2 fixture: `$(` in PowerShell body opens _INDIRECTION_RE; `Remove-Item`
    # inside the same heredoc fires the coarse floor; the STRUCTURAL classifier
    # also fires here because ``_BASH_SEP`` splits on ``\n`` regardless of
    # heredoc context — the Remove-Item line becomes a sibling fragment. That
    # is itself an interesting finding for PR-1 measurement (both paths cross
    # heredoc boundaries), so the corroborated value is expected to be
    # ``structural_and_coarse`` on this input; the PR-1 promise is that the
    # match_span + heredoc_bodies are recorded so a reader can tell the
    # match landed INSIDE the heredoc body (AC1).
    a = classify_tool_call("Bash", {"command": _PS_HEREDOC})
    assert a.operation is Operation.FS_DELETE
    assert a.corroborated in ("structural_and_coarse", "coarse_only")
    assert a.match_span is not None
    ms, me = a.match_span
    assert _PS_HEREDOC[ms:me] == "Remove-Item"
    assert len(a.heredoc_bodies) == 1


def test_ac1_match_inside_heredoc_is_machine_readable() -> None:
    # AC1: the halt reason string carries enough for a reader to tell that the
    # coarse-floor match fell INSIDE the heredoc body (structural over-deny
    # candidate) rather than in the outer command (a genuine `rm` on the shell).
    a = classify_tool_call("Bash", {"command": _PS_HEREDOC})
    reason = format_denial_detail(a, "test-forbidden-reason")
    assert reason.splitlines()[0] == "test-forbidden-reason"  # AC7
    assert f"corroborated={a.corroborated}" in reason
    assert "match=" in reason and "heredoc=" in reason
    ms, me = a.match_span  # type: ignore[misc]
    assert f"match={ms}..{me}" in reason
    # Machine-read: the match span must fall inside at least one heredoc body span.
    h_start, h_end = a.heredoc_bodies[0]
    assert h_start <= ms and me <= h_end, "AC1: match must land inside the heredoc body"


# ---- T1 secret redaction (AC4/AC5) ------------------------------------------


def test_ac4_ghp_token_redacted() -> None:
    token = "ghp_" + "A" * 36
    cmd = f"echo {token} > /tmp/x"
    a = classify_tool_call("Bash", {"command": cmd})
    reason = format_denial_detail(a, "test-forbidden-reason")
    assert token not in reason
    assert "<REDACTED:github_pat>" in reason


@pytest.mark.parametrize(
    "kind,token",
    [
        ("github_pat", "ghp_" + "A" * 36),
        ("github_fine_pat", "github_pat_" + "A" * 30),
        ("slack", "xoxb-" + "A" * 20),
        ("jwt", "eyJabc." + "A" * 10 + "." + "B" * 10),
        ("aws_access_key", "AKIA" + "A" * 16),
    ],
)
def test_redact_secrets_covers_known_shapes(kind: str, token: str) -> None:
    text = f"prefix {token} suffix"
    out = _redact_secrets(text)
    assert token not in out
    assert f"<REDACTED:{kind}>" in out


def test_ac5_redaction_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # AC5: if redaction throws, `command:` does NOT leak the raw text — it says
    # "(redacted; redaction error)" instead. Simulated by patching the redaction
    # function to raise; the format helper must catch and refuse to print raw.
    from spirrow_mindwire.adapters import implementer as impl_mod

    def boom(_text: str) -> str:
        raise RuntimeError("simulated redaction failure")

    monkeypatch.setattr(impl_mod, "_redact_secrets", boom)
    cmd = "echo very-secret-thing-do-not-leak > /tmp/x"
    a = ClassifiedAction(
        operation=Operation.FS_DELETE,
        detail=cmd,
        raw_command=cmd,
        corroborated="structural_only",
    )
    reason = impl_mod.format_denial_detail(a, "test-forbidden-reason")
    assert "very-secret-thing-do-not-leak" not in reason
    assert "(redacted; redaction error)" in reason


def test_ac7_reason_first_line_is_original() -> None:
    # AC7: existing callers that read only the first line of the reason (the
    # original Tier C forbidden.reason) keep working. The new detail is
    # additive lines below.
    a = classify_tool_call("Bash", {"command": "rm -rf x"})
    reason = format_denial_detail(a, "ファイル削除は Tier C (不可逆)。")
    assert reason.startswith("ファイル削除は Tier C (不可逆)。")


def test_non_bash_denial_prints_tool_name_hint() -> None:
    # A non-Bash denial (e.g. an unknown MCP tool) has no raw_command; the
    # helper prints the tool_name so the human still knows what was called.
    a = ClassifiedAction(operation=Operation.UNKNOWN)
    reason = format_denial_detail(a, "unlisted operation", tool_name="mcp__unknown__frobnicate")
    assert "command: (non-bash tool: mcp__unknown__frobnicate)" in reason
    assert "corroborated=unknown" in reason


# ---- guard integration: violations carry the enriched reason ---------------


@pytest.mark.anyio
async def test_guard_deny_message_carries_detail(tmp_path: Path) -> None:
    guard = _AllowlistGuard(default_allowlist(repo_root=tmp_path))
    cmd = 'bash -c "rm -rf x"'
    res = await guard("Bash", {"command": cmd}, ToolPermissionContext())
    assert isinstance(res, PermissionResultDeny)
    # The SDK-facing message and the stored violation reason are the enriched
    # form: line 1 = original Tier C reason, then detail: and command:.
    assert isinstance(res.message, str)
    lines = res.message.splitlines()
    assert len(lines) >= 3
    assert "detail:" in lines[1]
    assert "command:" in lines[2]
    # And the stored decision carries the same enriched text, so downstream
    # ErrorInfo.message (fed from decision.reason) reads the detail too.
    stored = guard.violations[-1]
    assert isinstance(stored, AllowlistDecision)
    assert stored.reason == res.message
