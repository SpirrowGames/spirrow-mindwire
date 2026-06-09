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
    adapter: RoleAdapter = ImplementerSdkAdapter(cwd=tmp_path, inference_base_url="http://lx")
    assert adapter.adapter_id == "implementer-sdk"


@pytest.mark.anyio
async def test_spawn_requires_inference_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MINDWIRE_IMPLEMENTER_BASE_URL", raising=False)
    adapter = ImplementerSdkAdapter(cwd=tmp_path, client_factory=_factory(responses=[]))
    with pytest.raises(ImplementerSdkSpawnError):
        await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))


@pytest.mark.anyio
async def test_spawn_routes_inference_via_base_url(tmp_path: Path) -> None:
    cap: list[_FakeSdkClient] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
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
    adapter = ImplementerSdkAdapter(cwd=tmp_path, inference_base_url="http://lx")
    sp = adapter._system_prompt
    assert str(tmp_path) in sp
    assert "WORKING DIRECTORY" in sp
    assert "relative path" in sp
    # grounding is appended, not a replacement — the role handoff guidance is preserved.
    assert "Conductor handoff protocol" in sp


@pytest.mark.anyio
async def test_deliver_emits_reply_when_allowed(tmp_path: Path) -> None:
    captured: list[ReplyDraft] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
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
    adapter = ImplementerSdkAdapter(cwd=tmp_path, inference_base_url=base)
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
    adapter = ImplementerSdkAdapter(cwd=tmp_path, inference_base_url=base)
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
