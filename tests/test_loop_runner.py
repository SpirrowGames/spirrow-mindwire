"""Stage 3 ``mindwire-loop`` daemon wiring — T-stage3-loop-wiring (msg-385, Option A).

Proves the runner composes the existing Stage 3 components correctly without
touching any core (watcher / dispatcher / adapter):

- **role resolution**: with the three production adapters registered, the
  registry routes PROPOSER → text-only ``Stage3ProposerAdapter``, IMPLEMENTER →
  the allow-list-gated ``ImplementerSdkAdapter``, NAYSAYER → the independent
  ``NaysayerPrReviewAdapter`` (and *only* it — ADR-05 §5). This is the wrinkle
  the runner solves: the base ``ClaudeCodeSdkAdapter`` declares EXECUTE_CODE and
  would otherwise shadow the gated implementer under first-qualified.
- **config**: ``[loop].watches`` parse into ``WatchSpec``s with the right role.
- **assembly**: a fully built loop dispatches a proposer reply over a fake
  chatroom (the registry→dispatcher→watcher→gateway composition is sound).
- **guards**: an unset ``repo_dir`` fails loud; a non-independent naysayer is
  rejected at registry-build time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from spirrow_mindwire.adapters.claude_code_sdk import ClaudeCodeSdkAdapter
from spirrow_mindwire.adapters.implementer import ImplementerSdkAdapter
from spirrow_mindwire.adapters.naysayer_pr_review import NaysayerPrReviewAdapter
from spirrow_mindwire.config import LoopWatchConfig, MindwireSettings, Stage3LoopConfig
from spirrow_mindwire.loop_runner import (
    Stage3ProposerAdapter,
    build_loop,
    build_registry,
    build_watches,
)
from spirrow_mindwire.magickit.watcher import WatchSpec
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.ulid_util import new_ulid
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    HealthStatus,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

# --------------------------------------------------------------------------- #
# minimal fakes
# --------------------------------------------------------------------------- #


class _FakeChatroom:
    """In-memory chatroom shared by the fake watcher-read and gateway-write."""

    def __init__(self) -> None:
        self._msgs: list[dict[str, Any]] = []
        self._seq = 0

    def post(self, *, author: str, content: str, reply_to: str | None = None) -> str:
        self._seq += 1
        msg_id = f"m{self._seq}"
        ts = (datetime(2026, 6, 3, tzinfo=UTC) + timedelta(seconds=self._seq)).isoformat()
        self._msgs.append(
            {
                "msg_id": msg_id,
                "author": author,
                "content": content,
                "timestamp": ts,
                "reply_to": reply_to,
            }
        )
        return msg_id

    def messages(self) -> list[dict[str, Any]]:
        return list(self._msgs)

    def authors(self) -> list[str]:
        return [str(m["author"]) for m in self._msgs]


class _FakeMcp:
    """McpToolCaller over a single-thread :class:`_FakeChatroom`."""

    def __init__(self, chatroom: _FakeChatroom) -> None:
        self._chatroom = chatroom

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_get_thread":
            return {"messages": self._chatroom.messages()}
        if name == "chatroom_post_message":
            msg_id = self._chatroom.post(
                author=str(arguments["author"]),
                content=str(arguments["content"]),
                reply_to=arguments.get("reply_to"),
            )
            return {"msg": {"msg_id": msg_id}}
        raise AssertionError(f"unexpected tool {name!r}")


class _ScriptedSdkClient:
    """Fake claude_agent_sdk client yielding a fixed proposer reply."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def connect(self) -> None: ...

    async def query(self, prompt: str) -> None: ...

    async def receive_response(self) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[TextBlock(text=self._text)], model="stub")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            stop_reason="end_turn",
            result="ok",
        )

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


class _StubAdapter:
    """RoleAdapter stub with configurable id/capabilities (never spawned here)."""

    def __init__(self, adapter_id: str, capabilities: frozenset[Capability]) -> None:
        self.adapter_id = adapter_id
        self.capabilities = capabilities

    async def spawn(self, thread_ref: ThreadRef, role: Role, ctx: SpawnContext) -> SessionHandle:
        return SessionHandle(
            session_id=new_ulid(),
            instance_id=ctx.own_instance_id,
            adapter_id=self.adapter_id,
            thread_ref=thread_ref,
            role=role,
            started_at=datetime.now(UTC),
        )

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        raise AssertionError("stub adapter should not be delivered to in this test")

    async def halt(self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)) -> None:
        return None

    async def health(self, handle: SessionHandle) -> HealthStatus:
        return HealthStatus(
            state=SessionState.IDLE, last_active_at=datetime.now(UTC), error=None, details={}
        )


def _exec_caps() -> frozenset[Capability]:
    return frozenset({Capability.READ_THREAD, Capability.POST_REPLY, Capability.EXECUTE_CODE})


def _naysayer_caps() -> frozenset[Capability]:
    return frozenset({Capability.READ_THREAD, Capability.POST_REPLY, Capability.NAYSAYER_QUALIFIED})


def _real_adapters(tmp_path: Path) -> tuple[Stage3ProposerAdapter, ImplementerSdkAdapter, Any]:
    """The three production adapter classes, built without network/SDK I/O."""
    proposer = Stage3ProposerAdapter(cwd=tmp_path)
    implementer = ImplementerSdkAdapter(cwd=tmp_path, inference_base_url="http://lexora.local")
    naysayer = NaysayerPrReviewAdapter(lexora=_FakeLexora(), github=_FakeGitHub())
    return proposer, implementer, naysayer


class _FakeLexora:
    async def chat_completion(self, *, model: str, messages: Any, max_tokens: int) -> Any:
        raise AssertionError("not called")

    async def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    async def aclose(self) -> None: ...


class _FakeGitHub:
    async def fetch_pr_diff(self, pr: Any) -> str:
        raise AssertionError("not called")

    async def fetch_ci_status(self, pr: Any) -> Any:
        raise AssertionError("not called")

    async def submit_review(self, pr: Any, *, event: Any, body: str) -> Any:
        raise AssertionError("not called")

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------- #
# Stage3ProposerAdapter: text-only (drops EXECUTE_CODE)
# --------------------------------------------------------------------------- #


def test_stage3_proposer_is_text_only() -> None:
    caps = Stage3ProposerAdapter.capabilities
    assert Capability.READ_THREAD in caps
    assert Capability.POST_REPLY in caps
    # Dropping EXECUTE_CODE is what lets the IMPLEMENTER slot resolve to the
    # gated adapter rather than the proposer (registry first-qualified).
    assert Capability.EXECUTE_CODE not in caps
    # Same-model family as main → never qualifies as naysayer (ADR-05 §5).
    assert Capability.NAYSAYER_QUALIFIED not in caps
    # adapter_id distinct from the base so logs/registry don't collide.
    assert Stage3ProposerAdapter.adapter_id != ClaudeCodeSdkAdapter.adapter_id


# --------------------------------------------------------------------------- #
# build_registry: role resolution + independence
# --------------------------------------------------------------------------- #


def test_build_registry_resolves_each_role(tmp_path: Path) -> None:
    proposer, implementer, naysayer = _real_adapters(tmp_path)
    registry = build_registry(proposer=proposer, implementer=implementer, naysayer=naysayer)

    assert registry.qualified_for(Role.PROPOSER)[0] is proposer
    assert registry.qualified_for(Role.IMPLEMENTER)[0] is implementer
    # Independence: the naysayer slot resolves to EXACTLY the independent adapter.
    assert registry.qualified_for(Role.NAYSAYER) == [naysayer]


def test_build_registry_rejects_non_independent_naysayer(tmp_path: Path) -> None:
    proposer, implementer, _naysayer = _real_adapters(tmp_path)
    # A same-model adapter (no NAYSAYER_QUALIFIED) must not fill the naysayer slot.
    bad_naysayer = _StubAdapter("not-independent", _exec_caps())
    with pytest.raises(RuntimeError, match="NAYSAYER"):
        build_registry(proposer=proposer, implementer=implementer, naysayer=bad_naysayer)


def test_build_registry_rejects_proposer_that_shadows_implementer(tmp_path: Path) -> None:
    # If the "proposer" advertises EXECUTE_CODE it also qualifies for IMPLEMENTER
    # and, registered first, would shadow the gated implementer — caught fail-loud.
    exec_proposer = _StubAdapter("exec-proposer", _exec_caps())
    implementer = ImplementerSdkAdapter(cwd=tmp_path, inference_base_url="http://lexora.local")
    naysayer = _StubAdapter("nay", _naysayer_caps())
    with pytest.raises(RuntimeError, match="IMPLEMENTER"):
        build_registry(proposer=exec_proposer, implementer=implementer, naysayer=naysayer)


# --------------------------------------------------------------------------- #
# config: watches parse with Role + build_watches
# --------------------------------------------------------------------------- #


def test_loop_config_parses_role_from_string() -> None:
    cfg = Stage3LoopConfig(
        watches=(
            LoopWatchConfig(thread_id="T-work-1", role="proposer"),  # type: ignore[arg-type]
            LoopWatchConfig(thread_id="T-pr-review-1", role="naysayer"),  # type: ignore[arg-type]
        )
    )
    assert cfg.watches[0].role is Role.PROPOSER
    assert cfg.watches[1].role is Role.NAYSAYER
    assert cfg.watches[0].baseline is True  # default


def test_build_watches_maps_config_to_watchspecs() -> None:
    cfg = Stage3LoopConfig(
        project="spirrow-mindwire",
        watches=(
            LoopWatchConfig(thread_id="T-work-1", role=Role.PROPOSER),
            LoopWatchConfig(
                thread_id="T-impl-1", role=Role.IMPLEMENTER, instance_id="implementer-7"
            ),
        ),
    )
    specs = build_watches(cfg)
    assert len(specs) == 2
    assert specs[0].thread_ref.project_id == "spirrow-mindwire"
    assert specs[0].thread_ref.thread_id == "T-work-1"
    assert specs[0].role is Role.PROPOSER
    assert specs[0].instance_id == "proposer-1"  # blank → minted default
    assert specs[1].instance_id == "implementer-7"  # explicit override preserved


# --------------------------------------------------------------------------- #
# build_loop guards
# --------------------------------------------------------------------------- #


def test_build_loop_requires_repo_dir_when_building_sdk_adapters() -> None:
    settings = MindwireSettings()  # loop.repo_dir is None by default
    chatroom = _FakeChatroom()
    with pytest.raises(SystemExit, match="repo_dir"):
        build_loop(settings, mcp=_FakeMcp(chatroom))


def test_build_loop_skips_repo_dir_when_adapters_injected(tmp_path: Path) -> None:
    settings = MindwireSettings()
    proposer, implementer, naysayer = _real_adapters(tmp_path)
    loop = build_loop(
        settings,
        mcp=_FakeMcp(_FakeChatroom()),
        proposer=proposer,
        implementer=implementer,
        naysayer=naysayer,
    )
    assert loop.orchestrator is not None
    assert loop.registry.qualified_for(Role.IMPLEMENTER)[0] is implementer


# --------------------------------------------------------------------------- #
# assembled loop dispatches a proposer reply (composition smoke)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_built_loop_dispatches_proposer_reply(tmp_path: Path) -> None:
    chatroom = _FakeChatroom()
    chatroom.post(author="human", content="How should we speed up hot-path reads?")
    mcp = _FakeMcp(chatroom)

    proposer = Stage3ProposerAdapter(
        cwd=tmp_path,
        client_factory=lambda _opts: _ScriptedSdkClient("PROPOSAL: add a write-through cache."),
    )
    implementer = _StubAdapter("fake-implementer", _exec_caps())
    naysayer = _StubAdapter("fake-naysayer", _naysayer_caps())

    loop = build_loop(
        MindwireSettings(), mcp=mcp, proposer=proposer, implementer=implementer, naysayer=naysayer
    )

    work = ThreadRef(
        project_id="spirrow-mindwire",
        thread_id="T-work-1",
        chatroom_uri="magickit://chatroom/thread/T-work-1",
    )
    # baseline=False so the proposer answers the question already in the thread.
    await loop.watcher.add_watch(WatchSpec(work, Role.PROPOSER), baseline=False)
    dispatched = await loop.watcher.poll_once()
    await loop.watcher.stop()

    assert dispatched == 1
    # The proposer (instance "proposer-1") posted its proposal back to the thread.
    assert chatroom.authors() == ["human", "proposer-1"]
    assert chatroom.messages()[-1]["content"] == "PROPOSAL: add a write-through cache."


@pytest.mark.anyio
async def test_run_loop_idles_cleanly_with_no_watches(tmp_path: Path) -> None:
    # No watches → run_loop must still build, log a warning, and (when cancelled)
    # stop cleanly. We approximate "cancelled immediately" by a zero-watch poll.
    settings = MindwireSettings(loop=Stage3LoopConfig(repo_dir=tmp_path, watches=()))
    proposer, implementer, naysayer = _real_adapters(tmp_path)
    loop = build_loop(
        settings,
        mcp=_FakeMcp(_FakeChatroom()),
        proposer=proposer,
        implementer=implementer,
        naysayer=naysayer,
    )
    # Nothing watched → a poll dispatches nothing and stop() is a no-op-safe halt.
    assert await loop.watcher.poll_once() == 0
    await loop.watcher.stop()
