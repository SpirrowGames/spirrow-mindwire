"""Stage 3 ``mindwire-loop`` daemon wiring — T-stage3-loop-wiring (msg-385, Option A).

Proves the runner composes the existing Stage 3 components correctly without
touching any core (watcher / dispatcher / adapter):

- **role resolution**: with the three production adapters registered, the
  registry routes PROPOSER → read-only ``Stage3ProposerAdapter``, IMPLEMENTER →
  the allow-list-gated ``ImplementerSdkAdapter``, NAYSAYER → the independent
  design-time ``NaysayerSdkAdapter`` (and *only* it — ADR-05 §5; ADR-19 driver-化
  unify made it the sole registry NAYSAYER, the PR-gate being a driver). This is
  the wrinkle the runner solves: the base ``ClaudeCodeSdkAdapter`` declares
  EXECUTE_CODE and would otherwise shadow the gated implementer under
  first-qualified.
- **config**: ``[loop].watches`` parse into ``WatchSpec``s with the right role.
- **assembly**: a fully built loop dispatches a proposer reply over a fake
  chatroom (the registry→dispatcher→watcher→gateway composition is sound).
- **guards**: an unset ``repo_dir`` fails loud; a non-independent naysayer is
  rejected at registry-build time.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from spirrow_mindwire import loop_runner
from spirrow_mindwire.adapters.claude_code_sdk import ClaudeCodeSdkAdapter, _PathScopeGuard
from spirrow_mindwire.adapters.implementer import ImplementerSdkAdapter
from spirrow_mindwire.adapters.naysayer_sdk import NaysayerSdkAdapter
from spirrow_mindwire.conductor import StopReason
from spirrow_mindwire.config import (
    ConductorConfig,
    LoopWatchConfig,
    MindwireSettings,
    Stage3LoopConfig,
)
from spirrow_mindwire.loop_runner import (
    _PROPOSER_BUILTIN_TOOLS,
    Stage3Conductor,
    Stage3ProposerAdapter,
    build_conductor,
    build_loop,
    build_proposer,
    build_registry,
    build_watches,
    run_conductor,
)
from spirrow_mindwire.magickit.client import MagickitMcpError
from spirrow_mindwire.magickit.watcher import WatchSpec
from spirrow_mindwire.naysayer.pr_review import NaysayerPrReviewDriver
from spirrow_mindwire.obligations import load_manifest
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.ulid_util import new_ulid
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    HealthStatus,
    ReplyDraft,
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
    """McpToolCaller over a single-thread :class:`_FakeChatroom`, plus loop control.

    ``control_state`` is served to ``loop_control_get``; the conductor reads it every round and
    stops on ``hold``, so a fake that did not answer this tool would fail closed and every
    conductor test here would stop at round 0 (which is exactly what happened when the control
    plane was added — the composition root wires it unconditionally). ``control_state=None``
    simulates a control plane that cannot be reached at all.
    """

    def __init__(
        self, chatroom: _FakeChatroom, *, control_state: str | None = "supervised"
    ) -> None:
        self._chatroom = chatroom
        self._control_state = control_state
        self.observed: list[str] = []

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
        if name in ("loop_control_get", "loop_control_report_observed"):
            if self._control_state is None:
                # Production wraps every transport failure into ``MagickitMcpError`` at the client
                # boundary (``StreamableHttpChatroomMcp.call_tool``), so a fake that raises the raw
                # underlying ``ConnectionError`` puts a class no caller can catch on the wire.
                # Under msg-1496 §4.1's narrow catch, the older ``ConnectionError`` fake leaked out
                # of ``report_observed``; using the class production actually produces keeps the
                # simulation faithful without hiding the failure.
                raise MagickitMcpError(f"{name}: control plane unreachable")
            if name == "loop_control_get":
                return {"desired_state": self._control_state, "configured": True}
            self.observed.append(str(arguments["state"]))
            return {}
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


def _proposer_caps() -> frozenset[Capability]:
    return frozenset({Capability.READ_THREAD, Capability.POST_REPLY})


def _exec_caps() -> frozenset[Capability]:
    return frozenset({Capability.READ_THREAD, Capability.POST_REPLY, Capability.EXECUTE_CODE})


def _naysayer_caps() -> frozenset[Capability]:
    return frozenset({Capability.READ_THREAD, Capability.POST_REPLY, Capability.NAYSAYER_QUALIFIED})


_OBLIGATIONS = load_manifest()


def _real_adapters(tmp_path: Path) -> tuple[Stage3ProposerAdapter, ImplementerSdkAdapter, Any]:
    """The three production adapter classes, built without network/SDK I/O.

    The registry naysayer is the design-time ``NaysayerSdkAdapter`` (ADR-19 driver-化 unify);
    the PR-gate is the separate :func:`_pr_review_driver`, not a registered adapter.
    """
    proposer = Stage3ProposerAdapter(cwd=tmp_path)
    implementer = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url="http://lexora.local"
    )
    naysayer = NaysayerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url="http://lexora.local"
    )
    return proposer, implementer, naysayer


def _pr_review_driver() -> NaysayerPrReviewDriver:
    """A PR-review driver with fake clients (never called here; avoids env/network)."""
    return NaysayerPrReviewDriver(lexora=_FakeLexora(), github=_FakeGitHub())


class _FakeLexora:
    def __init__(self) -> None:
        self.closed = False

    async def chat_completion(self, *, model: str, messages: Any, max_tokens: int) -> Any:
        raise AssertionError("not called")

    async def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    async def aclose(self) -> None:
        self.closed = True


class _FakeGitHub:
    def __init__(self) -> None:
        self.closed = False

    async def fetch_pr_diff(self, pr: Any) -> str:
        raise AssertionError("not called")

    async def fetch_ci_status(self, pr: Any) -> Any:
        raise AssertionError("not called")

    async def fetch_pr_reviews(self, pr: Any) -> Any:
        raise AssertionError("not called")

    async def submit_review(self, pr: Any, *, event: Any, body: str) -> Any:
        raise AssertionError("not called")

    async def aclose(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# Stage3ProposerAdapter: read-only (drops EXECUTE_CODE)
# --------------------------------------------------------------------------- #


def test_the_proposer_can_read_the_repository_it_designs_against() -> None:
    """It could not, and that stopped the loop rather than a design.

    On T-fs-delete-path-scope msg-1197 the proposer reported that no read tool
    was permitted, declined to design a security gate from quoted excerpts, and
    handed back to a human; the naysayer's review endorsed the refusal. Nothing
    then moved for a day. Read / Glob / Grep are what "check the claim before
    designing against it" costs.
    """
    assert set(_PROPOSER_BUILTIN_TOOLS) == {"Read", "Glob", "Grep"}
    proposer = build_proposer(Path("."))
    assert list(proposer._builtin_tools) == list(_PROPOSER_BUILTIN_TOOLS)
    # Auto-approved because running headless means an un-approved call reaches a
    # prompt no one can answer. The bound is the guard, asserted separately.
    assert list(proposer._allowed_tools) == list(_PROPOSER_BUILTIN_TOOLS)


def test_the_proposer_is_scoped_to_the_repository_it_was_given() -> None:
    """Exposing a read tool is not approving every path it could name.

    `allowed_tools` auto-approves, and `Read` takes an absolute path, so without
    a guard the proposer could be talked into quoting a credential file into the
    chatroom — which leaves this host and reaches an external model. Widening
    permissions on an isolated box bounds what a deletion costs; it does not
    bound where a secret travels. (Tier B, PR #157.)
    """
    repo = Path("/tmp/some-repo")
    proposer = build_proposer(repo)
    guard = proposer._can_use_tool
    assert isinstance(guard, _PathScopeGuard)
    assert guard.root == repo


def test_the_proposer_still_cannot_write_or_run_anything() -> None:
    """Reading is the widening; writing and executing are not.

    A proposer that can change the tree is an implementer, and the Stage 3 split
    puts every such call behind the allow-list-gated adapter.
    """
    forbidden = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "Task", "WebFetch"}
    assert forbidden.isdisjoint(set(_PROPOSER_BUILTIN_TOOLS))
    proposer = build_proposer(Path("."))
    assert forbidden.isdisjoint(set(proposer._allowed_tools))


def test_reading_does_not_make_the_proposer_qualify_as_the_implementer() -> None:
    """The reason the class drops EXECUTE_CODE, restated as a guard.

    ``capabilities`` is a class attribute and independent of the tool list, so
    widening the tools cannot make first-qualified hand the IMPLEMENTER slot to
    the un-gated adapter. That was the whole point of the split.
    """
    assert Capability.EXECUTE_CODE not in Stage3ProposerAdapter.capabilities


def test_stage3_proposer_capabilities_are_unchanged() -> None:
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
    implementer = ImplementerSdkAdapter(
        cwd=tmp_path, obligations=_OBLIGATIONS, inference_base_url="http://lexora.local"
    )
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
        pr_review_driver=_pr_review_driver(),
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
        MindwireSettings(),
        mcp=mcp,
        proposer=proposer,
        implementer=implementer,
        naysayer=naysayer,
        pr_review_driver=_pr_review_driver(),
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
    posted = chatroom.messages()[-1]["content"]
    # The dispatcher stamps the harness-derived source marker on the posted body
    # (msg-805 D3 / msg-834 §2). The agent's proposal is preserved verbatim; the
    # marker is the trailing HTML-comment line derived from the SDK options.
    assert posted.startswith("PROPOSAL: add a write-through cache.")
    assert posted.rstrip().splitlines()[-1].startswith("<!-- source:")


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
        pr_review_driver=_pr_review_driver(),
    )
    # Nothing watched → a poll dispatches nothing and stop() is a no-op-safe halt.
    assert await loop.watcher.poll_once() == 0
    await loop.watcher.stop()


@pytest.mark.anyio
async def test_loop_aclose_closes_pr_review_driver(tmp_path: Path) -> None:
    # Tier B #93 round-4: the driver is orchestrator-held (not in the registry, so no adapter
    # sweep closes it), so daemon teardown (loop.aclose) must close its HTTP clients.
    lexora, github = _FakeLexora(), _FakeGitHub()
    driver = NaysayerPrReviewDriver(lexora=lexora, github=github)
    proposer, implementer, naysayer = _real_adapters(tmp_path)
    loop = build_loop(
        MindwireSettings(),
        mcp=_FakeMcp(_FakeChatroom()),
        proposer=proposer,
        implementer=implementer,
        naysayer=naysayer,
        pr_review_driver=driver,
    )
    await loop.aclose()  # watcher.stop() + orchestrator.aclose() → driver.aclose()
    assert lexora.closed and github.closed


# --------------------------------------------------------------------------- #
# PR-2a: conductor mode (mindwire-loop --mode conductor, Tier-C decide msg-523)
# --------------------------------------------------------------------------- #

_CONDUCTOR_ROSTER = {
    "Bohr": Role.PROPOSER,
    "Heisenberg": Role.IMPLEMENTER,
    "Einstein": Role.NAYSAYER,
}


def _conductor_settings(
    tmp_path: Path | None = None,
    *,
    task_thread_id: str = "T-cond",
    roster: dict[str, Role] | None = None,
    naysayer_identity: str = "Einstein",
    human_identity: str = "human",
    max_rounds: int = 40,
) -> MindwireSettings:
    return MindwireSettings(
        loop=Stage3LoopConfig(repo_dir=tmp_path),
        conductor=ConductorConfig(
            task_thread_id=task_thread_id,
            roster=_CONDUCTOR_ROSTER if roster is None else roster,
            naysayer_identity=naysayer_identity,
            human_identity=human_identity,
            max_rounds=max_rounds,
        ),
    )


class _ScriptedReplyAdapter:
    """RoleAdapter that emits scripted replies (each ending with a ``NEXT:`` line) + tracks halt."""

    def __init__(
        self, adapter_id: str, capabilities: frozenset[Capability], replies: list[str]
    ) -> None:
        self.adapter_id = adapter_id
        self.capabilities = capabilities
        self._replies = list(replies)
        self._ctx: SpawnContext | None = None
        self.halted = False

    async def spawn(self, thread_ref: ThreadRef, role: Role, ctx: SpawnContext) -> SessionHandle:
        self._ctx = ctx
        return SessionHandle(
            session_id=new_ulid(),
            instance_id=ctx.own_instance_id,
            adapter_id=self.adapter_id,
            thread_ref=thread_ref,
            role=role,
            started_at=datetime.now(UTC),
        )

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        if event.payload.author == handle.instance_id or not self._replies:
            return
        assert self._ctx is not None
        await self._ctx.on_reply(
            ReplyDraft(
                body=self._replies.pop(0), reply_to_msg_id=event.payload.msg_id, adapter_metadata={}
            )
        )

    async def halt(self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)) -> None:
        self.halted = True

    async def health(self, handle: SessionHandle) -> HealthStatus:
        return HealthStatus(
            state=SessionState.IDLE, last_active_at=datetime.now(UTC), error=None, details={}
        )


def test_build_conductor_wires_conductor_from_config(tmp_path: Path) -> None:
    # Injected adapters skip repo_dir; the conductor is wired over [conductor].task_thread_id with
    # the same registry→dispatcher core as build_loop (role resolution holds).
    proposer = _StubAdapter("fake-proposer", _proposer_caps())
    implementer = _StubAdapter("fake-implementer", _exec_caps())
    naysayer = _StubAdapter("fake-naysayer", _naysayer_caps())
    cond = build_conductor(
        _conductor_settings(human_identity="takahito"),
        mcp=_FakeMcp(_FakeChatroom()),
        proposer=proposer,
        implementer=implementer,
        naysayer=naysayer,
    )
    assert isinstance(cond, Stage3Conductor)
    assert cond.registry.qualified_for(Role.IMPLEMENTER)[0] is implementer
    assert cond.registry.qualified_for(Role.NAYSAYER) == [naysayer]
    # PR-2b-3 D-1: [conductor].human_identity is wired into the Conductor (carve-out ① identity).
    assert cond.conductor._human_identity == "takahito"


def test_build_conductor_requires_task_thread_id(tmp_path: Path) -> None:
    proposer, implementer, naysayer = _real_adapters(tmp_path)
    with pytest.raises(SystemExit, match="task_thread_id"):
        build_conductor(
            _conductor_settings(task_thread_id="  "),
            mcp=_FakeMcp(_FakeChatroom()),
            proposer=proposer,
            implementer=implementer,
            naysayer=naysayer,
        )


def test_build_conductor_requires_roster(tmp_path: Path) -> None:
    proposer, implementer, naysayer = _real_adapters(tmp_path)
    with pytest.raises(SystemExit, match="roster"):
        build_conductor(
            _conductor_settings(roster={}),
            mcp=_FakeMcp(_FakeChatroom()),
            proposer=proposer,
            implementer=implementer,
            naysayer=naysayer,
        )


def test_build_conductor_requires_naysayer_identity(tmp_path: Path) -> None:
    proposer, implementer, naysayer = _real_adapters(tmp_path)
    with pytest.raises(SystemExit, match="naysayer_identity"):
        build_conductor(
            _conductor_settings(naysayer_identity=""),
            mcp=_FakeMcp(_FakeChatroom()),
            proposer=proposer,
            implementer=implementer,
            naysayer=naysayer,
        )


def test_build_conductor_rejects_naysayer_identity_not_mapping_to_naysayer_role(
    tmp_path: Path,
) -> None:
    # The Conductor ctor's fail-loud invariant (Tier B msg-529) surfaces as a friendly SystemExit
    # at daemon startup rather than a raw ValueError.
    proposer, implementer, naysayer = _real_adapters(tmp_path)
    with pytest.raises(SystemExit, match="misconfigured"):
        build_conductor(
            _conductor_settings(naysayer_identity="Bohr"),  # Bohr maps to PROPOSER, not NAYSAYER
            mcp=_FakeMcp(_FakeChatroom()),
            proposer=proposer,
            implementer=implementer,
            naysayer=naysayer,
        )


def test_build_conductor_requires_repo_dir_when_building_adapters() -> None:
    # No adapters injected + loop.repo_dir unset → fail loud (same guard as build_loop).
    settings = _conductor_settings(tmp_path=None)
    with pytest.raises(SystemExit, match="repo_dir"):
        build_conductor(settings, mcp=_FakeMcp(_FakeChatroom()))


@pytest.mark.anyio
async def test_run_conductor_drives_round_trip_and_closes_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end over a fake chatroom: a proposer NEXT:Einstein → the naysayer replies NEXT:human →
    # the conductor stops at HUMAN; run_conductor then aclose()s, halting the spawned session.
    chatroom = _FakeChatroom()
    chatroom.post(author="Bohr", content="design proposal\n\nNEXT: Einstein")
    mcp = _FakeMcp(chatroom)
    proposer = _StubAdapter("fake-proposer", _proposer_caps())
    implementer = _StubAdapter("fake-implementer", _exec_caps())
    naysayer = _ScriptedReplyAdapter(
        "fake-naysayer", _naysayer_caps(), ["independent critique\n\nNEXT: human"]
    )

    # run_conductor calls build_conductor(settings) with no injection; patch it to inject our fakes.
    real_build = loop_runner.build_conductor

    def _build(settings: MindwireSettings) -> Stage3Conductor:
        return real_build(
            settings, mcp=mcp, proposer=proposer, implementer=implementer, naysayer=naysayer
        )

    monkeypatch.setattr(loop_runner, "build_conductor", _build)
    outcome = await run_conductor(_conductor_settings())

    assert outcome.stop_reason is StopReason.HUMAN
    assert outcome.forced_naysayer_turns == 0  # NEXT named Einstein explicitly
    assert chatroom.authors() == ["Bohr", "Einstein"]  # naysayer posted under its persona name
    assert naysayer.halted  # aclose() in run_conductor's finally halted the spawned session
    assert mcp.observed == ["supervised"]  # the loop reported the state it acted on


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("control_state", "expect_reads"),
    [("hold", True), (None, False)],
    ids=["operator hold", "control plane unreachable"],
)
async def test_run_conductor_stops_on_hold_through_the_real_composition_root(
    monkeypatch: pytest.MonkeyPatch, control_state: str | None, expect_reads: bool
) -> None:
    # Both ways a project ends up stopped, proven through the wiring rather than the Conductor
    # alone: an operator HOLD, and a control plane that cannot be read at all (``None`` makes the
    # fake reject loop_control_get, the same shape as magickit being down). The second case is the
    # one worth pinning — build_conductor wires the reader unconditionally, so a regression that
    # dropped the reader would show up as this test going green on the WRONG path (HUMAN, having
    # driven the thread) rather than as an import error.
    chatroom = _FakeChatroom()
    chatroom.post(author="Bohr", content="design proposal\n\nNEXT: Einstein")
    mcp = _FakeMcp(chatroom, control_state=control_state)
    naysayer = _ScriptedReplyAdapter(
        "fake-naysayer", _naysayer_caps(), ["independent critique\n\nNEXT: human"]
    )
    real_build = loop_runner.build_conductor

    def _build(settings: MindwireSettings) -> Stage3Conductor:
        return real_build(
            settings,
            mcp=mcp,
            proposer=_StubAdapter("fake-proposer", _proposer_caps()),
            implementer=_StubAdapter("fake-implementer", _exec_caps()),
            naysayer=naysayer,
        )

    monkeypatch.setattr(loop_runner, "build_conductor", _build)
    outcome = await run_conductor(_conductor_settings())

    assert outcome.stop_reason is StopReason.HOLD
    assert outcome.rounds == 0
    assert chatroom.authors() == ["Bohr"]  # nothing was dispatched, nothing was posted
    assert bool(mcp.observed) is expect_reads


@pytest.mark.anyio
async def test_run_conductor_closes_sessions_even_when_run_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The aclose() teardown runs in finally even if the drive loop raises (no session leak).
    closed = False

    class _BoomConductor:
        async def run(self) -> None:
            raise RuntimeError("drive boom")

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(loop_runner, "build_conductor", lambda _s: _BoomConductor())
    with pytest.raises(RuntimeError, match="drive boom"):
        await run_conductor(_conductor_settings())
    assert closed


def test_main_routes_to_conductor_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def _fake_run_conductor(_settings: MindwireSettings) -> None:
        calls.append("conductor")

    async def _fake_run_loop(_settings: MindwireSettings) -> None:
        calls.append("watcher")

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "run_loop", _fake_run_loop)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: MindwireSettings())
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])
    loop_runner.main()
    assert calls == ["conductor"]


def test_main_defaults_to_watcher_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def _fake_run_conductor(_settings: MindwireSettings) -> None:
        calls.append("conductor")

    async def _fake_run_loop(_settings: MindwireSettings) -> None:
        calls.append("watcher")

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "run_loop", _fake_run_loop)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: MindwireSettings())
    monkeypatch.setattr("sys.argv", ["mindwire-loop"])
    loop_runner.main()
    assert calls == ["watcher"]  # backward-compatible default


# --------------------------------------------------------------------------- #
# T39: UTF-8 runtime for the daemon entrypoint
# --------------------------------------------------------------------------- #


def test_ensure_utf8_runtime_exports_child_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # T39: the daemon must export UTF-8 so the CLI subprocesses / the agent's own python don't crash
    # on em-dash / 日本語 under the Windows cp932 default (the adapters also set it per-spawn; the
    # entrypoint export is the parent-level belt). monkeypatch auto-restores os.environ after.
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    loop_runner._ensure_utf8_runtime()
    assert os.environ["PYTHONUTF8"] == "1"
    assert os.environ["PYTHONIOENCODING"] == "utf-8"


def test_ensure_utf8_runtime_respects_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # setdefault, not overwrite: an operator who deliberately set PYTHONUTF8=0 is honoured.
    monkeypatch.setenv("PYTHONUTF8", "0")
    loop_runner._ensure_utf8_runtime()
    assert os.environ["PYTHONUTF8"] == "0"


# --------------------------------------------------------------------------- #
# T-sdk-is-error-loses-the-reason S-6 — exit-time marker re-emission
# --------------------------------------------------------------------------- #


def test_main_reemits_sdk_error_marker_on_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the run raises with an ``SdkIsErrorSignal`` in the chain, ``main``
    prints the traceback THEN emits the marker as the last stdout line, then
    exits non-zero via ``SystemExit``.

    The naive earlier version just re-emitted the marker and re-raised — that
    let Python's default ``sys.excepthook`` write a 30-plus-line traceback to
    stderr AFTER the marker, which PowerShell's ``*>&1`` merged capture then
    dropped into the tail window and pushed the marker out. See PR #181
    naysayer review; this test pins the fix so a future refactor cannot
    silently regress it.
    """
    from spirrow_mindwire.adapters._sdk_result import SdkIsErrorSignal

    signal_detail = {
        "reason_source": "result",
        "message": "429 rate limit",
        "captured_fields": {"session_id": "exit-test-sid"},
    }

    async def _fake_run_conductor(_settings: MindwireSettings) -> None:
        try:
            raise SdkIsErrorSignal(signal_detail)
        except SdkIsErrorSignal as sig:
            # Mirrors what ``deliver_event`` does — wraps the signal in the
            # adapter's typed delivery error while preserving ``__cause__``, so
            # the ``find_sdk_error_signal`` walker has to actually walk.
            raise RuntimeError("wrapped delivery failure") from sig

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: MindwireSettings())
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])

    # ``main`` now exits via ``SystemExit(1)`` instead of re-raising, so the
    # default excepthook does NOT print the traceback AFTER our marker. The
    # exit code stays 1 so the wrapper's ``if ($code -ne 0)`` branch is
    # unchanged.
    with pytest.raises(SystemExit) as excinfo:
        loop_runner.main()
    assert excinfo.value.code == 1

    out = capsys.readouterr().out
    # 1) Marker is present and carries the signal's detail.
    assert "sdk_error_detail=" in out
    assert "exit-test-sid" in out

    # 2) The traceback was printed BEFORE the marker (not swallowed). Without
    # this the exit-time write would be silent about what actually happened,
    # which would trade one silent-degradation defect for another.
    assert "Traceback" in out
    assert "wrapped delivery failure" in out

    # 3) The marker is the LAST non-empty line on stdout. This is what the
    # ``session_log_tail`` window depends on to keep the marker inside its
    # 50-line horizon — the exact regression PR #181 review identified.
    nonempty = [line for line in out.splitlines() if line.strip()]
    marker_indices = [i for i, line in enumerate(nonempty) if line.startswith("sdk_error_detail=")]
    assert marker_indices, "no marker line found on stdout"
    assert marker_indices[-1] == len(nonempty) - 1, (
        "marker is not the LAST non-empty stdout line — traceback / other output "
        "leaked after it, exactly the regression the fix exists to prevent. "
        f"Last three lines: {nonempty[-3:]}"
    )

    # 4) Every traceback line appears BEFORE the marker in the stream — since
    # the wrapper merges via ``*>&1``, an in-stream ordering assertion pins
    # the merge order too (both writes are to the same file descriptor).
    marker_index = marker_indices[-1]
    traceback_indices = [i for i, line in enumerate(nonempty) if "Traceback" in line]
    assert all(i < marker_index for i in traceback_indices), (
        "a Traceback line appeared AFTER the marker — the fix's ordering invariant is broken."
    )


def test_main_does_nothing_extra_when_the_error_has_no_sdk_signal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exit handler is a no-op when the failure was not an SDK is_error.

    Crucially, the fix for the sdk_is_error case (traceback-then-marker-then-
    ``sys.exit``) must NOT extend to unrelated exceptions. An unrelated
    ``RuntimeError`` must still propagate unchanged so a future default-hook
    surface (Python 3.14 tracebacks, IDE integrations, etc.) does not lose
    fidelity for the 99 % case that has nothing to do with the SDK.
    """

    async def _fake_run_conductor(_settings: MindwireSettings) -> None:
        raise RuntimeError("unrelated crash")

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: MindwireSettings())
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])

    with pytest.raises(RuntimeError):
        loop_runner.main()

    assert "sdk_error_detail=" not in capsys.readouterr().out
