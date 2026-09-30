"""T43 follow-up — a spawn that times out is retried once, then reported in the thread.

Thread ``T-agmsg-transport-lessons-readiness-session-claim-board``: design Bohr msg-5053
(D-1..D-4), approved by Einstein msg-5054, authorised by the human. What is pinned here:

1. D-2: only :class:`AdapterSpawnTimeoutError` is retried, once; each timeout writes one
   ``spawn.timeout`` line; the turn then runs as if nothing had happened.
2. D-3: two timeouts post a notice under ``conductor-relay`` ending ``NEXT: human`` and stop on
   ``StopReason.HUMAN``; a notice that does not land re-raises the timeout itself.
3. Every other spawn failure is untouched: one attempt, no notice, the same exception.
4. D-1: all three spawn sites in ``Conductor.run`` go through the one helper.
5. D-4: the proposer's ``connect()`` is bounded by the implementer's budget and its timeout is
   the Port-level class, so it reaches the conductor's retry through the real ``Dispatcher``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import (
    _RED,
    _ROSTER,
    _conductor,
    _FakeChatroomMcp,
    _rollup,
    _ScriptedDispatcher,
    _ScriptedPrGate,
    _ScriptedRollupSource,
    _thread_ref,
)
from test_phase1_integration import _FakeGateway

from spirrow_mindwire.adapters import implementer as implementer_module
from spirrow_mindwire.adapters._connect_budget import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    SdkConnectTimeoutError,
    connect_bounded,
)
from spirrow_mindwire.adapters.claude_code_sdk import (
    ClaudeCodeSdkAdapter,
    ClaudeCodeSdkSpawnError,
    ClaudeCodeSdkSpawnTimeoutError,
)
from spirrow_mindwire.adapters.implementer import (
    ImplementerSdkAdapter,
    ImplementerSdkSpawnError,
    ImplementerSdkSpawnTimeoutError,
)
from spirrow_mindwire.conductor.core import CONDUCTOR_RELAY_AUTHOR, Conductor, StopReason
from spirrow_mindwire.conductor.head_skip import Decision, decide
from spirrow_mindwire.conductor.spawn_timeout import (
    EVENT_KIND_SPAWN_TIMEOUT,
    SPAWN_ATTEMPTS,
    spawn_timeout_event,
)
from spirrow_mindwire.dispatcher.core import Dispatcher
from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
from spirrow_mindwire.exceptions import AdapterSpawnError, AdapterSpawnTimeoutError
from spirrow_mindwire.github.client import ReviewEvent
from spirrow_mindwire.magickit.client import ThreadResolvedError
from spirrow_mindwire.obligations import load_manifest
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.value_objects import Event, ReplyDraft, Role, SessionHandle, ThreadRef

_SPAWN_TIMEOUT_LOGGER = "spirrow_mindwire.conductor.spawn_timeout"
_OBLIGATIONS = load_manifest()


def _timeout_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _SPAWN_TIMEOUT_LOGGER and r.getMessage().startswith(EVENT_KIND_SPAWN_TIMEOUT)
    ]


def _timeout() -> AdapterSpawnTimeoutError:
    return AdapterSpawnTimeoutError(
        "adapter.spawn_timeout: test", adapter_id="fault-injection", timeout_s=60.0
    )


class _FailingSpawnDispatcher(_ScriptedDispatcher):
    """Raises the queued exceptions from ``spawn_instance``, one per call, then spawns normally.

    ``spawns`` counts every attempt, failed or not, so a test can tell "retried" from "did not".
    """

    def __init__(
        self,
        mcp: _FakeChatroomMcp,
        replies: dict[Role, list[str]],
        failures: list[BaseException],
    ) -> None:
        super().__init__(mcp, replies)
        self._failures = list(failures)
        self.raised: list[BaseException] = []

    async def spawn_instance(
        self, thread_ref: ThreadRef, role: Role, instance_id: str
    ) -> SessionHandle:
        if self._failures:
            self.spawns.append((role, instance_id))
            exc = self._failures.pop(0)
            self.raised.append(exc)
            raise exc
        return await super().spawn_instance(thread_ref, role, instance_id)


class _RefusingMcp(_FakeChatroomMcp):
    """The thread is readable but was resolved underneath the run: every post is refused."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_post_message":
            raise ThreadResolvedError("thread resolved")
        return await super().call_tool(name, arguments)


class _BrokenPostMcp(_FakeChatroomMcp):
    """Posting fails for a reason that is not "thread resolved" (the transport is down)."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_post_message":
            raise ConnectionError("gateway unreachable")
        return await super().call_tool(name, arguments)


def _seed_proposer_turn(mcp: _FakeChatroomMcp) -> None:
    # Implementer → proposer: an ordinary AI-addressed handoff that routes straight to a spawn.
    mcp.seed(author="Heisenberg", content="PR is up.\n\nNEXT: Bohr")


# --------------------------------------------------------------------------- #
# D-2 — retry once, on a timeout only
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_one_timeout_then_success_runs_the_turn_and_logs_one_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    mcp = _FakeChatroomMcp()
    _seed_proposer_turn(mcp)
    disp = _FailingSpawnDispatcher(mcp, {Role.PROPOSER: ["done\n\nNEXT: none"]}, [_timeout()])

    outcome = await _conductor(mcp, disp).run()

    # Two attempts, one session, and the turn it was spawned for was delivered.
    assert disp.spawns == [(Role.PROPOSER, "Bohr"), (Role.PROPOSER, "Bohr")]
    assert disp.dispatches == [(Role.PROPOSER, "m1")]
    # The only post is the proposer's reply: a retry that worked is not a stop, and the
    # conductor says nothing about it in the thread.
    assert [p["author"] for p in mcp.posts] == ["Bohr"]
    assert outcome.stop_reason is StopReason.SETTLED
    (line,) = _timeout_lines(caplog)
    assert line == (
        "spawn.timeout adapter_id=fault-injection instance_id=Bohr role=proposer "
        "attempt=1 timeout_s=60.0"
    )


def test_the_event_carries_exactly_the_five_designed_fields() -> None:
    event = spawn_timeout_event(
        adapter_id="claude-code-sdk",
        instance_id="Bohr",
        role=Role.PROPOSER,
        attempt=2,
        timeout_s=60.0,
    )
    assert event.kind == "spawn.timeout"
    assert event.fields == {
        "adapter_id": "claude-code-sdk",
        "instance_id": "Bohr",
        "role": "proposer",
        "attempt": 2,
        "timeout_s": 60.0,
    }


# --------------------------------------------------------------------------- #
# D-3 — two timeouts: say so in the thread and stop at the human
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_two_timeouts_post_a_notice_and_stop_at_human(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    mcp = _FakeChatroomMcp()
    _seed_proposer_turn(mcp)
    disp = _FailingSpawnDispatcher(mcp, {}, [_timeout(), _timeout()])

    outcome = await _conductor(mcp, disp).run()

    assert SPAWN_ATTEMPTS == 2
    assert len(disp.spawns) == 2  # the first attempt and exactly one retry
    assert disp.dispatches == []  # the turn never ran
    assert outcome.stop_reason is StopReason.HUMAN  # exit 0: the stop was reported in the thread
    assert outcome.rounds == 0
    (post,) = mcp.posts
    assert post["author"] == CONDUCTOR_RELAY_AUTHOR == "conductor-relay"
    assert (post["project"], post["thread_id"]) == ("spirrow-mindwire", "T-cond")
    assert "role" not in post  # the relay holds no role (I-6)
    assert post["content"].rstrip().splitlines()[-1] == "NEXT: human"
    assert "Bohr" in post["content"] and "fault-injection" in post["content"]
    assert "60.0" in post["content"] and "spawn.timeout" in post["content"]
    assert outcome.last_msg_id == "m2"  # recorded against the notice a human will open
    assert [ln.split("attempt=")[1].split()[0] for ln in _timeout_lines(caplog)] == ["1", "2"]
    # The next sweep tick reads a stop token on the head and does not launch into it again.
    verdict = decide(
        now=datetime(2026, 10, 1, tzinfo=UTC),
        head_msg_id="m2",
        head_body=post["content"],
        control_state="run",
        record=None,
    )
    assert verdict.decision is Decision.SKIP


@pytest.mark.anyio
@pytest.mark.parametrize("mcp_type", [_RefusingMcp, _BrokenPostMcp])
async def test_a_notice_that_does_not_land_re_raises_the_timeout_itself(
    mcp_type: type[_FakeChatroomMcp], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    mcp = mcp_type()
    _seed_proposer_turn(mcp)
    first, second = _timeout(), _timeout()
    disp = _FailingSpawnDispatcher(mcp, {}, [first, second])

    with pytest.raises(AdapterSpawnTimeoutError) as excinfo:
        await _conductor(mcp, disp).run()

    # The very exception the last attempt raised: today's exit 1 → quarantine, unchanged.
    assert excinfo.value is second
    assert len(disp.spawns) == 2
    assert len(_timeout_lines(caplog)) == 2  # the reason still reaches the quarantine log tail


# --------------------------------------------------------------------------- #
# every other spawn failure is untouched
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        AdapterSpawnError("adapter.job_assign_missed: not in our Job"),
        ImplementerSdkSpawnError("no inference base URL configured"),
        RuntimeError("something else entirely"),
    ],
)
async def test_a_spawn_failure_that_is_not_a_timeout_is_neither_retried_nor_reported(
    failure: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    mcp = _FakeChatroomMcp()
    _seed_proposer_turn(mcp)
    disp = _FailingSpawnDispatcher(mcp, {}, [failure])

    with pytest.raises(type(failure)) as excinfo:
        await _conductor(mcp, disp).run()

    assert excinfo.value is failure
    assert len(disp.spawns) == 1  # no retry
    assert mcp.posts == []  # no notice
    assert _timeout_lines(caplog) == []


# --------------------------------------------------------------------------- #
# D-1 — one spawn path for all three sites
# --------------------------------------------------------------------------- #


def test_the_conductor_calls_spawn_instance_in_exactly_one_place() -> None:
    # A fourth spawn site added with a bare ``spawn_instance`` would skip the retry and the
    # notice without any behavioural test noticing. ``run`` itself must hold none.
    assert inspect.getsource(Conductor).count("spawn_instance(") == 1
    assert "spawn_instance(" not in inspect.getsource(Conductor.run)
    assert "spawn_instance(" in inspect.getsource(Conductor._spawn)


@pytest.mark.anyio
async def test_the_request_changes_fix_dispatch_gets_the_same_retry_and_notice() -> None:
    # Site 2: the PR-gate said REQUEST_CHANGES and the implementer is spawned to fix it.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content="opened the PR\n\nNEXT: pr-review acme/widgets#7")
    gate = _ScriptedPrGate(mcp, ReviewEvent.REQUEST_CHANGES)
    disp = _FailingSpawnDispatcher(mcp, {}, [_timeout(), _timeout()])

    outcome = await _conductor(mcp, disp, orchestrator=gate).run()

    assert disp.spawns == [(Role.IMPLEMENTER, "Heisenberg")] * 2
    assert disp.dispatches == []
    assert outcome.stop_reason is StopReason.HUMAN
    # The relay asked for the implementer; the notice after it is what the head now says.
    assert [p["author"] for p in mcp.posts] == ["pr-gate-relay", "conductor-relay"]
    assert mcp.posts[-1]["content"].rstrip().splitlines()[-1] == "NEXT: human"
    assert outcome.last_msg_id == "m3"


@pytest.mark.anyio
async def test_the_ci_route_dispatch_gets_the_same_retry_and_notice() -> None:
    # Site 1: pre-gate admission saw the first red on this head (R4) and spawns the implementer.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content="opened\n\nNEXT: pr-review acme/widgets#7")
    gate = _ScriptedPrGate(mcp, ReviewEvent.APPROVE)
    disp = _FailingSpawnDispatcher(mcp, {}, [_timeout()])
    source = _ScriptedRollupSource(_rollup(*_RED))

    await _conductor(mcp, disp, orchestrator=gate, rollup_source=source).run()

    # One timeout, one retry, and the implementer was then woken on the ci-route post.
    assert disp.spawns == [(Role.IMPLEMENTER, "Heisenberg")] * 2
    assert [role for role, _ in disp.dispatches] == [Role.IMPLEMENTER]
    assert "ADMISSION: route_implementer" in disp.events[0].payload.body


@pytest.mark.anyio
async def test_a_forced_consult_that_never_spawned_is_not_counted_as_one() -> None:
    # A proposer's own ``NEXT: human`` forces a naysayer consult. If that spawn gives up, the
    # outcome must not report a forced naysayer turn: none ran.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="Here is the design.\n\nNEXT: human")
    disp = _FailingSpawnDispatcher(mcp, {}, [_timeout(), _timeout()])

    outcome = await _conductor(mcp, disp).run()

    assert disp.spawns == [(Role.NAYSAYER, "Einstein")] * 2
    assert outcome.stop_reason is StopReason.HUMAN
    assert outcome.forced_naysayer_turns == 0


# --------------------------------------------------------------------------- #
# D-4 — the proposer's connect is bounded, and by the implementer's number
# --------------------------------------------------------------------------- #


class _Client:
    """Fake SDK client whose ``connect`` hangs, raises its own ``TimeoutError``, or returns."""

    def __init__(self, *, hang: bool = False, own_timeout: bool = False) -> None:
        self._hang = hang
        self._own_timeout = own_timeout
        self.connects = 0
        self.disconnected = False

    async def connect(self) -> None:
        self.connects += 1
        if self._own_timeout:
            raise TimeoutError("the SDK gave up on its own")
        if self._hang:
            await asyncio.Event().wait()

    async def query(self, prompt: str) -> None:
        return None

    async def receive_response(self) -> Any:
        return
        yield  # pragma: no cover

    async def interrupt(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnected = True


def _clients(*clients: _Client) -> Callable[[Any], _Client]:
    queue = list(clients)
    return lambda _options: queue.pop(0)


def _ctx(role: Role) -> SpawnContext:
    async def on_reply(_draft: ReplyDraft) -> None:
        return None

    async def on_event_log(_event: Event) -> None:
        return None

    return SpawnContext(
        on_reply=on_reply, on_event_log=on_event_log, own_role=role, own_instance_id="Bohr"
    )


def test_the_proposer_and_the_implementer_share_one_connect_budget() -> None:
    assert DEFAULT_CONNECT_TIMEOUT_SECONDS == 60.0
    assert implementer_module._DEFAULT_SPAWN_TIMEOUT_SECONDS is DEFAULT_CONNECT_TIMEOUT_SECONDS
    default = inspect.signature(ClaudeCodeSdkAdapter.__init__).parameters["spawn_timeout_seconds"]
    assert default.default is DEFAULT_CONNECT_TIMEOUT_SECONDS


@pytest.mark.anyio
async def test_a_proposer_connect_that_hangs_raises_the_port_level_timeout(tmp_path: Path) -> None:
    client = _Client(hang=True)
    adapter = ClaudeCodeSdkAdapter(
        cwd=tmp_path, client_factory=_clients(client), spawn_timeout_seconds=0.05
    )

    with pytest.raises(ClaudeCodeSdkSpawnTimeoutError) as excinfo:
        await asyncio.wait_for(adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx(Role.PROPOSER)), 5)

    exc = excinfo.value
    assert isinstance(exc, AdapterSpawnTimeoutError) and isinstance(exc, ClaudeCodeSdkSpawnError)
    assert (exc.adapter_id, exc.timeout_s) == ("claude-code-sdk", 0.05)
    assert "adapter.spawn_timeout" in str(exc)
    assert client.disconnected  # the abandoned client was asked to let go of its subprocess
    assert adapter._sessions == {}  # and no half-made session was registered


@pytest.mark.anyio
async def test_a_timeout_the_sdk_raised_itself_is_a_plain_spawn_failure(tmp_path: Path) -> None:
    # Not our budget running out, so not the retryable class: a connect that failed is not a
    # connect that was still going.
    adapter = ClaudeCodeSdkAdapter(
        cwd=tmp_path, client_factory=_clients(_Client(own_timeout=True)), spawn_timeout_seconds=5
    )
    with pytest.raises(ClaudeCodeSdkSpawnError) as excinfo:
        await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx(Role.PROPOSER))
    assert not isinstance(excinfo.value, AdapterSpawnTimeoutError)


@pytest.mark.anyio
async def test_connect_bounded_tells_its_own_deadline_from_the_sdks() -> None:
    with pytest.raises(SdkConnectTimeoutError) as ours:
        await connect_bounded(_Client(hang=True), 0.05)
    assert ours.value.timeout_seconds == 0.05
    with pytest.raises(TimeoutError) as theirs:
        await connect_bounded(_Client(own_timeout=True), 5)
    assert not isinstance(theirs.value, SdkConnectTimeoutError)
    await connect_bounded(_Client(), 5)  # a connect that returns is left alone


@pytest.mark.anyio
async def test_the_implementer_timeout_is_the_port_level_class_with_its_fields(
    tmp_path: Path,
) -> None:
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_clients(_Client(hang=True)),
        spawn_timeout_seconds=0.05,
    )
    with pytest.raises(ImplementerSdkSpawnTimeoutError) as excinfo:
        await asyncio.wait_for(
            adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx(Role.IMPLEMENTER)), 5
        )
    exc = excinfo.value
    assert isinstance(exc, AdapterSpawnTimeoutError) and isinstance(exc, ImplementerSdkSpawnError)
    assert (exc.adapter_id, exc.timeout_s) == (adapter.adapter_id, 0.05)


# --------------------------------------------------------------------------- #
# through the real Dispatcher: the timeout arrives un-wrapped
# --------------------------------------------------------------------------- #


def _real_dispatcher(adapter: ClaudeCodeSdkAdapter) -> Dispatcher:
    registry = InMemoryAdapterRegistry()
    registry.register(adapter)
    return Dispatcher(registry=registry, gateway=_FakeGateway())


@pytest.mark.anyio
async def test_a_hanging_proposer_connect_reaches_the_notice_through_the_real_dispatcher(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # msg-5053's open question: does ``Dispatcher.spawn_instance`` hand the adapter's exception to
    # the conductor as it was raised? If it ever starts wrapping it, the type test in ``_spawn``
    # stops matching and this goes red.
    caplog.set_level(logging.INFO)
    mcp = _FakeChatroomMcp()
    _seed_proposer_turn(mcp)
    first, second = _Client(hang=True), _Client(hang=True)
    adapter = ClaudeCodeSdkAdapter(
        cwd=tmp_path, client_factory=_clients(first, second), spawn_timeout_seconds=0.05
    )
    conductor = Conductor(
        mcp=mcp,
        dispatcher=_real_dispatcher(adapter),
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
    )

    outcome = await asyncio.wait_for(conductor.run(), 5)

    assert (first.connects, second.connects) == (1, 1)  # a fresh client per attempt
    assert first.disconnected and second.disconnected
    assert outcome.stop_reason is StopReason.HUMAN
    (post,) = mcp.posts
    assert post["author"] == "conductor-relay"
    assert "claude-code-sdk" in post["content"]
    assert [ln.split("attempt=")[1].split()[0] for ln in _timeout_lines(caplog)] == ["1", "2"]
