"""Tests for T19 ``ImplementerSdkAdapter`` + the SDK-tool classifier.

The classifier (SDK tool call → allow-list :class:`Operation`) is the
safety-critical mapping and is tested exhaustively. The adapter lifecycle is
exercised with a fake SDK client that drives the ``can_use_tool`` guard, so the
fail-loud allow-list-violation path is covered without the real CLI.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)

from spirrow_mindwire.adapters.implementer import (
    ImplementerSdkAdapter,
    ImplementerSdkDeliveryError,
    ImplementerSdkSpawnError,
)
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


# --------------------------------------------------------------------------- #
# T27: indirection is unified via recursion (direct == wrapped), not a regex
# mirror. These cover the shell-extraction edge cases (nesting, multiple -c,
# $() nesting, tokenizer-defeating quoting, depth bound).
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# guard
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# manual SDK smoke (B3): verify the REAL SDK routes tool calls through the guard
# --------------------------------------------------------------------------- #
