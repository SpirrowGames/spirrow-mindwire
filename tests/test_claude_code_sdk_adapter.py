"""Tests for T11 ``ClaudeCodeSdkAdapter`` (ADR-2026-05-21-06 §3.1).

The Claude Agent SDK session is replaced by a fake client injected via
``client_factory``, exercising the adapter's lifecycle / reply wiring /
exception mapping without spinning up the real CLI.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from spirrow_mindwire.adapters.claude_code_sdk import (
    _DEFAULT_SYSTEM_PROMPT,
    ClaudeCodeSdkAdapter,
    ClaudeCodeSdkDeliveryError,
    ClaudeCodeSdkHaltError,
    ClaudeCodeSdkHealthError,
    ClaudeCodeSdkSpawnError,
    _PathScopeGuard,
)
from spirrow_mindwire.ports import RoleAdapter, SpawnContext
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    Event,
    EventType,
    NewMessagePayload,
    ReplyDraft,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

_TS = datetime(2026, 5, 22, tzinfo=UTC)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="test-model")


def _result(*, is_error: bool = False, result: str | None = "ok") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=10,
        is_error=is_error,
        num_turns=1,
        session_id="test",
        stop_reason="end_turn",
        result=result,
    )


class _FakeClient:
    """Structural stand-in for claude_agent_sdk.ClaudeSDKClient."""

    def __init__(
        self,
        responses: list[Any],
        *,
        fail_on: str | None = None,
        block: asyncio.Event | None = None,
        block_method: str | None = None,
    ) -> None:
        self._responses = responses
        self._fail_on = fail_on
        self._block = block
        self._block_method = block_method
        self.connected = False
        self.disconnected = False
        self.interrupt_count = 0
        self.queries: list[str] = []

    async def _maybe_block(self, method: str) -> None:
        if self._block is not None and self._block_method == method:
            await self._block.wait()

    async def connect(self) -> None:
        if self._fail_on == "connect":
            raise RuntimeError("connect boom")
        self.connected = True

    async def query(self, prompt: str) -> None:
        if self._fail_on == "query":
            raise RuntimeError("query boom")
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self._responses:
            yield message

    async def interrupt(self) -> None:
        if self._fail_on == "interrupt":
            raise RuntimeError("interrupt boom")
        await self._maybe_block("interrupt")
        self.interrupt_count += 1

    async def disconnect(self) -> None:
        await self._maybe_block("disconnect")
        self.disconnected = True


def _factory(client: _FakeClient) -> Callable[[Any], Any]:
    def make(_options: Any) -> _FakeClient:
        return client

    return make


def _ctx(captured: list[ReplyDraft], *, own_role: Role = Role.PROPOSER) -> SpawnContext:
    async def on_reply(draft: ReplyDraft) -> None:
        captured.append(draft)

    async def on_event_log(_event: Event) -> None:
        return None

    return SpawnContext(
        on_reply=on_reply,
        on_event_log=on_event_log,
        own_role=own_role,
        own_instance_id=f"{own_role.value}-1",
    )


def _thread_ref() -> ThreadRef:
    return ThreadRef(project_id="spirrow-mindwire", thread_id="01JTHREAD", chatroom_uri="mc://t/1")


def _event(
    *,
    author: str = "human",
    body: str = "hi",
    msg_id: str = "m1",
    event_type: EventType = EventType.NEW_MESSAGE,
) -> ChatroomEvent:
    return ChatroomEvent(
        event_id="01JEVENT",
        event_type=event_type,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id=msg_id, author=author, body=body, parent_msg_id=None),
    )


def _stray_handle() -> SessionHandle:
    return SessionHandle(
        session_id="01JSTRAY",
        instance_id="proposer-1",
        adapter_id="claude-code-sdk",
        thread_ref=_thread_ref(),
        role=Role.PROPOSER,
        started_at=_TS,
    )


@pytest.mark.anyio
async def test_spawn_connects_and_returns_idle_handle(tmp_path: Path) -> None:
    client = _FakeClient([])
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    assert client.connected is True
    assert handle.adapter_id == "claude-code-sdk"
    assert handle.role is Role.PROPOSER
    assert handle.instance_id == "proposer-1"  # T24: stamped from ctx.own_instance_id
    hs = await adapter.health(handle)
    assert hs.state is SessionState.IDLE
    assert hs.error is None


# --------------------------------------------------------------------------- #
# _PathScopeGuard: exposure is not approval
# --------------------------------------------------------------------------- #


async def _verdict(guard: _PathScopeGuard, tool: str, tool_input: dict[str, Any]) -> bool:
    """True when the call is allowed."""
    result = await guard(tool, tool_input, None)  # type: ignore[arg-type]
    return type(result).__name__ == "PermissionResultAllow"


@pytest.mark.anyio
@pytest.mark.parametrize("key", ["file_path", "path"])
async def test_a_read_inside_the_repository_is_allowed(tmp_path: Path, key: str) -> None:
    inside = tmp_path / "src" / "thing.py"
    inside.parent.mkdir(parents=True)
    inside.write_text("x", encoding="utf-8")
    guard = _PathScopeGuard(root=tmp_path)
    assert await _verdict(guard, "Read", {key: str(inside)}) is True
    assert guard.denials == []


@pytest.mark.anyio
async def test_a_relative_path_is_resolved_against_the_root(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    guard = _PathScopeGuard(root=tmp_path)
    assert await _verdict(guard, "Read", {"file_path": "a.py"}) is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("label", "target"),
    [
        ("absolute-escape", "{parent}/secrets.env"),
        ("dot-dot-escape", "../secrets.env"),
        ("nested-dot-dot", "src/../../secrets.env"),
    ],
)
async def test_a_read_outside_the_repository_is_refused(
    tmp_path: Path, label: str, target: str
) -> None:
    """The whole point of the guard. A role whose input is text written by other
    agents must not be able to quote a credential file back into the chatroom,
    which is replicated off this host and forwarded to an external model.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "secrets.env").write_text("TOKEN=1", encoding="utf-8")
    guard = _PathScopeGuard(root=root)
    resolved = target.format(parent=tmp_path.as_posix())
    assert await _verdict(guard, "Read", {"file_path": resolved}) is False, label
    assert len(guard.denials) == 1


@pytest.mark.anyio
async def test_a_sibling_that_merely_shares_a_prefix_is_refused(tmp_path: Path) -> None:
    """Why the check is ``is_relative_to`` and not ``startswith``."""
    root = tmp_path / "repo"
    root.mkdir()
    sibling = tmp_path / "repo-secrets"
    sibling.mkdir()
    (sibling / "x.env").write_text("TOKEN=1", encoding="utf-8")
    guard = _PathScopeGuard(root=root)
    assert await _verdict(guard, "Read", {"file_path": str(sibling / "x.env")}) is False


@pytest.mark.anyio
async def test_a_call_carrying_no_path_is_left_alone(tmp_path: Path) -> None:
    guard = _PathScopeGuard(root=tmp_path)
    assert await _verdict(guard, "Grep", {"pattern": "def "}) is True


@pytest.mark.anyio
async def test_no_builtins_means_no_guard_to_run(tmp_path: Path) -> None:
    """Nothing is exposed, so there is nothing to scope."""
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(_FakeClient([])))
    assert adapter._path_guard is None


@pytest.mark.anyio
async def test_exposing_builtins_installs_the_guard(tmp_path: Path) -> None:
    """Exposure and approval are separate, and the guard is what re-joins them.

    ``allowed_tools`` auto-approves, so without this the SDK would never ask.
    """
    captured: list[Any] = []

    def factory(options: Any) -> Any:
        captured.append(options)
        return _FakeClient([])

    adapter = ClaudeCodeSdkAdapter(
        cwd=tmp_path,
        builtin_tools=("Read",),
        allowed_tools=["Read"],
        client_factory=factory,
    )
    await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    guard = captured[0].can_use_tool
    assert isinstance(guard, _PathScopeGuard)
    assert guard.root == tmp_path


@pytest.mark.anyio
async def test_builtin_tools_default_to_none_exposed(tmp_path: Path) -> None:
    """The default stays "no built-ins", so nothing gains hands by accident."""
    captured: list[Any] = []

    def factory(options: Any) -> Any:
        captured.append(options)
        return _FakeClient([])

    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=factory)
    await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    assert list(captured[0].tools) == []


@pytest.mark.anyio
async def test_builtin_tools_are_what_the_sdk_is_asked_to_expose(tmp_path: Path) -> None:
    """``tools=`` is the exposure list, and it is NOT ``allowed_tools``.

    ``tools=[]`` disables every built-in in the SDK; reading it as "expose only
    what allowed_tools names" is the mistake that left the implementer with no
    hands (T37 #1) and the proposer unable to open a file (msg-1197). This pins
    the two as separate inputs so they cannot be conflated again.
    """
    captured: list[Any] = []

    def factory(options: Any) -> Any:
        captured.append(options)
        return _FakeClient([])

    adapter = ClaudeCodeSdkAdapter(
        cwd=tmp_path,
        builtin_tools=("Read", "Grep"),
        allowed_tools=["Read"],
        client_factory=factory,
    )
    await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    assert list(captured[0].tools) == ["Read", "Grep"]
    assert list(captured[0].allowed_tools) == ["Read"]


@pytest.mark.anyio
async def test_spawn_binds_options_and_exposes_them_via_source_marker_options(
    tmp_path: Path,
) -> None:
    """Pin the source-marker seam on ``ClaudeCodeSdkAdapter``.

    ``spawn`` must (1) build ``ClaudeAgentOptions`` and hand it to the
    client factory, and (2) store that same instance on the session so
    :meth:`ClaudeCodeSdkAdapter.source_marker_options` yields it. If a
    future refactor drops the local ``options`` binding — the failure mode
    PR #139 Tier B Finding 1 speculated about — ``spawn`` would either
    ``NameError`` (running the test) or return ``None`` from the getter,
    both of which this test catches loudly. The getter's return matches
    what the client factory received, so the marker the dispatcher
    derives is guaranteed to describe the SDK's actual options object
    (msg-834 §2 (a)).
    """
    client = _FakeClient([])
    captured_factory_options: list[Any] = []

    def factory(options: Any) -> Any:
        captured_factory_options.append(options)
        return client

    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=factory)
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))

    # 1) The factory received a ClaudeAgentOptions object.
    assert len(captured_factory_options) == 1
    factory_options = captured_factory_options[0]
    assert factory_options is not None
    # 2) The session's stored options are the SAME instance (identity —
    # msg-834 §2 (a): input to the marker builder is the SDK's actual
    # options instance, not a re-declared copy).
    exposed = adapter.source_marker_options(handle)
    assert exposed is factory_options


@pytest.mark.anyio
async def test_spawn_failure_raises_spawn_error(tmp_path: Path) -> None:
    client = _FakeClient([], fail_on="connect")
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    with pytest.raises(ClaudeCodeSdkSpawnError):
        await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))


@pytest.mark.anyio
async def test_deliver_new_message_emits_reply(tmp_path: Path) -> None:
    client = _FakeClient([_assistant("hi "), _assistant("there"), _result()])
    captured: list[ReplyDraft] = []
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx(captured))
    await adapter.deliver_event(handle, _event(author="human", msg_id="m7"))
    assert len(captured) == 1
    assert captured[0].body == "hi there"
    assert captured[0].reply_to_msg_id == "m7"
    assert captured[0].adapter_metadata["adapter_id"] == "claude-code-sdk"
    assert len(client.queries) == 1
    hs = await adapter.health(handle)
    assert hs.state is SessionState.IDLE


@pytest.mark.anyio
async def test_own_role_self_filter_skips(tmp_path: Path) -> None:
    client = _FakeClient([_assistant("x"), _result()])
    captured: list[ReplyDraft] = []
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx(captured))
    # author == our instance_id ("proposer-1") → our own post echoed back, filtered
    # out (I3 v2.2: the self-filter keys on instance_id, not the bare role).
    await adapter.deliver_event(handle, _event(author="proposer-1"))
    assert captured == []
    assert client.queries == []


@pytest.mark.anyio
async def test_non_new_message_event_is_noop(tmp_path: Path) -> None:
    client = _FakeClient([_assistant("x"), _result()])
    captured: list[ReplyDraft] = []
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx(captured))
    await adapter.deliver_event(handle, _event(event_type=EventType.THREAD_CLOSED))
    assert captured == []
    assert client.queries == []


@pytest.mark.anyio
async def test_deliver_failure_sets_failed_and_raises(tmp_path: Path) -> None:
    client = _FakeClient([], fail_on="query")
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    with pytest.raises(ClaudeCodeSdkDeliveryError):
        await adapter.deliver_event(handle, _event())
    hs = await adapter.health(handle)
    assert hs.state is SessionState.FAILED
    assert hs.error is not None
    assert hs.error.code == "adapter.delivery_failed"
    # §3.4 / I2: the failure code is in error.code, not duplicated in details.
    assert "error_code" not in hs.details


@pytest.mark.anyio
async def test_deliver_on_terminal_session_raises(tmp_path: Path) -> None:
    client = _FakeClient([])
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    await adapter.halt(handle)
    with pytest.raises(ClaudeCodeSdkDeliveryError):
        await adapter.deliver_event(handle, _event())


@pytest.mark.anyio
async def test_halt_disconnects_and_is_idempotent(tmp_path: Path) -> None:
    client = _FakeClient([])
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    await adapter.halt(handle)
    assert client.disconnected is True
    assert client.interrupt_count == 1
    assert (await adapter.health(handle)).state is SessionState.HALTED
    # Idempotent no-op (I8): second halt does nothing, no extra interrupt, no raise.
    await adapter.halt(handle)
    assert client.interrupt_count == 1
    assert (await adapter.health(handle)).state is SessionState.HALTED


@pytest.mark.anyio
async def test_halt_failure_sets_failed_and_raises(tmp_path: Path) -> None:
    client = _FakeClient([], fail_on="interrupt")
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    with pytest.raises(ClaudeCodeSdkHaltError):
        await adapter.halt(handle)
    hs = await adapter.health(handle)
    assert hs.state is SessionState.FAILED
    assert hs.error is not None
    assert hs.error.code == "adapter.halt_failed"


@pytest.mark.anyio
async def test_halt_unknown_handle_is_noop(tmp_path: Path) -> None:
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(_FakeClient([])))
    await adapter.halt(_stray_handle())  # no raise


@pytest.mark.anyio
async def test_health_unknown_handle_raises(tmp_path: Path) -> None:
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(_FakeClient([])))
    with pytest.raises(ClaudeCodeSdkHealthError):
        await adapter.health(_stray_handle())


def test_capabilities_exclude_naysayer_qualified() -> None:
    caps = ClaudeCodeSdkAdapter.capabilities
    # claude-code is the same model family as main → must not be naysayer-qualified.
    assert Capability.NAYSAYER_QUALIFIED not in caps
    assert Capability.EXECUTE_CODE in caps
    assert Capability.POST_REPLY in caps


def test_satisfies_roleadapter_protocol(tmp_path: Path) -> None:
    # Static structural conformance (mypy) is the real assertion.
    adapter: RoleAdapter = ClaudeCodeSdkAdapter(cwd=tmp_path)
    assert adapter.adapter_id == "claude-code-sdk"


@pytest.mark.anyio
async def test_deliver_missing_result_message_marks_failed(tmp_path: Path) -> None:
    # SDK stream ends without a ResultMessage → protocol error, not silent success.
    client = _FakeClient([_assistant("partial")])  # no _result()
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    with pytest.raises(ClaudeCodeSdkDeliveryError):
        await adapter.deliver_event(handle, _event())
    hs = await adapter.health(handle)
    assert hs.state is SessionState.FAILED
    assert hs.error is not None
    assert hs.error.code == "adapter.delivery_failed"


@pytest.mark.anyio
async def test_deliver_on_reply_failure_marks_failed(tmp_path: Path) -> None:
    client = _FakeClient([_assistant("hi"), _result()])

    async def on_reply(_draft: ReplyDraft) -> None:
        raise RuntimeError("downstream boom")

    async def on_event_log(_event: Event) -> None:
        return None

    ctx = SpawnContext(
        on_reply=on_reply,
        on_event_log=on_event_log,
        own_role=Role.PROPOSER,
        own_instance_id="proposer-1",
    )
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, ctx)
    with pytest.raises(ClaudeCodeSdkDeliveryError):
        await adapter.deliver_event(handle, _event())
    # A raising on_reply must not leave the session stuck in PROCESSING.
    hs = await adapter.health(handle)
    assert hs.state is SessionState.FAILED
    assert hs.error is not None
    assert hs.error.code == "adapter.delivery_failed"


@pytest.mark.anyio
async def test_deliver_rejected_while_halting(tmp_path: Path) -> None:
    block = asyncio.Event()  # gates disconnect so halt() stalls mid-shutdown
    client = _FakeClient([_assistant("x"), _result()], block=block, block_method="disconnect")
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    halt_task = asyncio.create_task(adapter.halt(handle))
    # Let halt() reach the blocked disconnect (state == HALTING).
    for _ in range(1000):
        if (await adapter.health(handle)).state is SessionState.HALTING:
            break
        await asyncio.sleep(0)
    assert (await adapter.health(handle)).state is SessionState.HALTING
    with pytest.raises(ClaudeCodeSdkDeliveryError):
        await adapter.deliver_event(handle, _event())
    block.set()
    await halt_task
    assert (await adapter.health(handle)).state is SessionState.HALTED


@pytest.mark.anyio
async def test_halt_grace_timeout_marks_failed(tmp_path: Path) -> None:
    block = asyncio.Event()  # never set → disconnect hangs past grace
    client = _FakeClient([], block=block, block_method="disconnect")
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_factory(client))
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx([]))
    with pytest.raises(ClaudeCodeSdkHaltError):
        await adapter.halt(handle, grace=timedelta(seconds=0.05))
    hs = await adapter.health(handle)
    assert hs.state is SessionState.FAILED
    assert hs.error is not None
    assert hs.error.code == "adapter.halt_failed"


def test_default_system_prompt_includes_proposer_handoff_protocol() -> None:
    # PR-2b-1: the loop proposer is taught to end every reply with a NEXT: line so the conductor
    # chains, and to route a ready design to the naysayer / human (not straight to the implementer).
    assert "Conductor handoff protocol" in _DEFAULT_SYSTEM_PROMPT
    assert "NEXT:" in _DEFAULT_SYSTEM_PROMPT
    assert "Do NOT hand a design straight to the implementer" in _DEFAULT_SYSTEM_PROMPT
