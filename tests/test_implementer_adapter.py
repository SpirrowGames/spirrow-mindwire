"""Tests for T19 ``ImplementerSdkAdapter`` + the SDK-tool classifier.

The classifier (SDK tool call → allow-list :class:`Operation`) is the
safety-critical mapping and is tested exhaustively. The adapter lifecycle is
exercised with a fake SDK client that drives the ``can_use_tool`` guard, so the
fail-loud allow-list-violation path is covered without the real CLI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
    ],
)
def test_classify_bash(cmd: str, expected: Operation) -> None:
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
):
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

    return SpawnContext(on_reply=on_reply, on_event_log=on_event_log, own_role=Role.IMPLEMENTER)


def _event(
    *, author: str = "human", event_type: EventType = EventType.NEW_MESSAGE
) -> ChatroomEvent:
    return ChatroomEvent(
        event_id="01JEVENT",
        event_type=event_type,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id="m1", author=author, body="do it", parent_msg_id=None),
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
    await adapter.deliver_event(handle, _event(author="implementer"))
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
