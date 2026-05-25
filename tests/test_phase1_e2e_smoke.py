"""T16 — Phase 1 end-to-end smoke (1 thread + 2-thread 並走).

Proves the **assembled Phase 1 stack** runs a full proposer → naysayer →
proposer(revise+decide) → implementer cycle end-to-end, in-process and with
no real models / network, against the ADR-2026-05-21-06 §8 acceptance
criteria and the ADR-2026-05-21-04 Phase 0/1 completion bar ("複数 thread 並走
しても人間スキップ可能" — minimal human input).

What is real here (the point of a composition smoke): the
:class:`~spirrow_mindwire.dispatcher.core.Dispatcher`, the
:class:`~spirrow_mindwire.dispatcher.registry.InMemoryAdapterRegistry`, the
:class:`~spirrow_mindwire.magickit.watcher.ChatroomWatcher`, the concrete
:class:`~spirrow_mindwire.magickit.gateway.MagickitChatroomGateway`, and the
real :class:`~spirrow_mindwire.adapters.claude_code_sdk.ClaudeCodeSdkAdapter`
(filling **both** the proposer and implementer roles — same model family, per
the T11/T16 design) are all the production objects. Only two things are faked:
the Claude Agent SDK client (scripted by prompt content) and the magickit
chatroom (a shared in-memory store behind the :class:`McpToolCaller` the watcher
reads and the gateway writes — so a posted reply genuinely becomes the next
message a peer observes, closing the loop).

The **independent naysayer** is a distinct mock adapter (the "Gemini stub")
carrying :attr:`~spirrow_mindwire.value_objects.Capability.NAYSAYER_QUALIFIED`.
Per ADR-05 §5 the same-model ``claude-code`` adapter deliberately omits that
capability, so the architecture can never silently assign it the naysayer slot —
this smoke fills the naysayer slot only with the mock and asserts the exclusion,
so the "1 adapter しか動かない段階で独立性が形骸化する" risk in the T16 DoD is
exercised rather than waved away.

Two properties of the current Phase 1 mechanism shape the topology (and are
themselves worth pinning):

1. The real ``ClaudeCodeSdkAdapter`` replies to **every** non-self message it is
   delivered (it has no content-level "stay silent" path). So two auto-replying
   roles sharing one thread would ping-pong forever.
2. :class:`ChatroomWatcher` dedups per ``(thread, msg_id)`` — **not** per
   instance — so on a single thread each message is consumed by exactly one
   watch (the first to poll it after it appears). A third same-thread role would
   "steal" a peer's message.

Both push toward the real Stage-3 topology (cf. ``orchestrator.py`` / T20, where
the naysayer runs on its own ``T-pr-review`` thread): **one auto-replying role
per thread**, bridged by an orchestrator. Phase 1 ships no automatic
convergence-detection / cross-thread relay engine, so here the test harness
plays that orchestrator (and the human) — exactly the two manual touch-points
the Phase 1 bar allows: the opening question and the final approval.

Scope note (review): this is an *in-process composition* proof. It drives
``ChatroomWatcher.poll_once`` directly for determinism, so the production daemon
loop itself (``ChatroomWatcher.run``'s poll-interval sleep + continue-on-error
guard) is **not** on this path — its resilience is the T14 unit tests' job, not
this smoke's. ``_drive_until`` is a fail-loud liveness cap, not a timing
assertion.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from spirrow_mindwire.adapters.claude_code_sdk import ClaudeCodeSdkAdapter
from spirrow_mindwire.dispatcher.core import Dispatcher
from spirrow_mindwire.dispatcher.event_log import EVENT_FIELD_AUTHOR, EVENT_KIND_REPLY_SENT
from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
from spirrow_mindwire.magickit.client import MagickitMcpError
from spirrow_mindwire.magickit.gateway import MagickitChatroomGateway
from spirrow_mindwire.magickit.watcher import ChatroomWatcher, WatchSpec
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.ulid_util import new_ulid
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    Event,
    HealthStatus,
    ReplyDraft,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

_PROJECT = "spirrow-mindwire"
_BASE_TS = datetime(2026, 5, 25, tzinfo=UTC)

# Scripted conversation content. The markers (PROPOSAL / REQUEST_CHANGES /
# DECIDE / BUILD REQUEST) are the only things the fakes branch on, so the flow
# is fully deterministic.
_QUESTION = "How should we make hot-path reads faster?"
_PROPOSAL = "PROPOSAL: add a write-through cache in front of the store."
_CRITIQUE = "CRITIQUE: the proposal ignores cache invalidation. VERDICT: REQUEST_CHANGES."
_REVISED = "REVISED PROPOSAL: write-through cache with TTL invalidation. DECIDE: converged."
_BUILD = "BUILD REQUEST: implement the converged design. DECIDE: converged."
_PR_OPENED = "PR opened: implements the converged cache design."
_APPROVAL = "APPROVED — merge to main."


# --------------------------------------------------------------------------- #
# Fakes: in-memory chatroom + the McpToolCaller over it
# --------------------------------------------------------------------------- #


class _FakeChatroom:
    """A minimal in-memory stand-in for the magickit chatroom.

    Shared by the watcher (reads via ``chatroom_get_thread``) and the gateway
    (writes via ``chatroom_post_message``) so a posted reply is observed by the
    next poll. Messages keep insertion order = chronological order, which is the
    order the watcher dispatches in.
    """

    def __init__(self) -> None:
        self._threads: dict[str, list[dict[str, Any]]] = {}
        self._closed: set[str] = set()
        self._seq = 0

    def open_thread(self, thread_id: str) -> None:
        self._threads.setdefault(thread_id, [])

    def post(
        self, thread_id: str, *, author: str, content: str, reply_to: str | None = None
    ) -> str:
        self._seq += 1
        msg_id = f"m{self._seq}"
        timestamp = (_BASE_TS + timedelta(seconds=self._seq)).isoformat().replace("+00:00", "Z")
        self._threads.setdefault(thread_id, []).append(
            {
                "msg_id": msg_id,
                "author": author,
                "content": content,
                "timestamp": timestamp,
                "reply_to": reply_to,
            }
        )
        return msg_id

    def messages(self, thread_id: str) -> list[dict[str, Any]]:
        return list(self._threads.get(thread_id, []))

    def timeline(self) -> list[tuple[str, str]]:
        """All messages across all threads in global post order → (msg_id, thread_id).

        ``msg_id`` is ``f"m{seq}"`` from a single monotonic counter, so sorting by
        it reconstructs the true temporal order across threads — which lets a test
        observe whether two concurrently-driven streams actually interleaved or
        ran one-after-the-other.
        """
        items = [(str(msg["msg_id"]), tid) for tid, msgs in self._threads.items() for msg in msgs]
        return sorted(items, key=lambda it: int(it[0][1:]))  # "m12" -> 12

    def authors(self, thread_id: str) -> list[str]:
        return [str(msg["author"]) for msg in self._threads.get(thread_id, [])]

    def latest(self, thread_id: str) -> dict[str, Any]:
        return self._threads[thread_id][-1]

    def close(self, thread_id: str) -> None:
        self._closed.add(thread_id)

    def is_closed(self, thread_id: str) -> bool:
        return thread_id in self._closed


class _FakeMcp:
    """A :class:`~spirrow_mindwire.magickit.client.McpToolCaller` over a chatroom.

    Implements only the two tools the production stack actually calls in this
    path: ``chatroom_get_thread`` (watcher) and ``chatroom_post_message``
    (gateway). Any other tool is a wiring error and raises.
    """

    def __init__(self, chatroom: _FakeChatroom) -> None:
        self._chatroom = chatroom
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        # Yield once on every chatroom read/write so concurrent streams genuinely
        # interleave on the shared Dispatcher (Copilot review): with no suspension
        # point in the fakes, asyncio.gather would run the streams effectively
        # sequentially and the 2-thread test would not exercise real concurrency.
        await asyncio.sleep(0)
        if name == "chatroom_get_thread":
            return {
                "messages": self._chatroom.messages(str(arguments["thread_id"])),
                "mode": arguments.get("mode"),
            }
        if name == "chatroom_post_message":
            msg_id = self._chatroom.post(
                str(arguments["thread_id"]),
                author=str(arguments["author"]),
                content=str(arguments["content"]),
                reply_to=arguments.get("reply_to"),
            )
            return {"msg": {"msg_id": msg_id}}
        raise MagickitMcpError(f"unexpected tool {name!r}")


# --------------------------------------------------------------------------- #
# Fakes: the scripted Claude Agent SDK client (proposer + implementer)
# --------------------------------------------------------------------------- #


def _script_reply(prompt: str) -> str:
    """Map a ``ClaudeCodeSdkAdapter`` prompt to the scripted assistant text.

    The adapter's prompt embeds the role ("acting as the {role} role") and the
    incoming message ("New message from {author}:\\n\\n{body}"), which is all the
    branching the smoke needs.
    """
    if "acting as the proposer role" in prompt:
        if "New message from human" in prompt:
            return _PROPOSAL
        if "REQUEST_CHANGES" in prompt:  # relayed naysayer critique → revise + decide
            return _REVISED
        return "ACK: proposer has nothing to add."
    if "acting as the implementer role" in prompt:
        if "BUILD REQUEST" in prompt or "DECIDE" in prompt:
            return _PR_OPENED
        return "ACK: implementer idle."
    return "ACK."


class _ScriptedSdkClient:
    """Fake ``claude_agent_sdk.ClaudeSDKClient`` driven by prompt content.

    One fresh instance per spawn (so concurrent sessions never share the
    last-prompt slot); the reply is a pure function of the prompt, so the same
    class serves both the proposer and implementer roles.
    """

    def __init__(self) -> None:
        self._reply = ""
        self.disconnected = False

    async def connect(self) -> None: ...

    async def query(self, prompt: str) -> None:
        self._reply = _script_reply(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[TextBlock(text=self._reply)], model="claude-code-stub")
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

    async def disconnect(self) -> None:
        self.disconnected = True


def _sdk_factory(_options: Any) -> _ScriptedSdkClient:
    return _ScriptedSdkClient()


# --------------------------------------------------------------------------- #
# Fake: the independent naysayer (the "Gemini stub", NAYSAYER_QUALIFIED)
# --------------------------------------------------------------------------- #


class _MockNaysayerAdapter:
    """A distinct, independent naysayer adapter (RoleAdapter, ADR-05 §5).

    Carries ``NAYSAYER_QUALIFIED`` — which ``ClaudeCodeSdkAdapter`` deliberately
    does not — so it is the *only* adapter the registry will route to the
    naysayer slot. It objects once to a proposal and otherwise stays silent
    (self-filtering its own echoed posts like the real adapters do).
    """

    adapter_id: str = "naysayer-gemini-stub"
    capabilities: frozenset[Capability] = frozenset(
        {Capability.READ_THREAD, Capability.POST_REPLY, Capability.NAYSAYER_QUALIFIED}
    )

    def __init__(self) -> None:
        self._sessions: dict[SessionHandle, SpawnContext] = {}

    async def spawn(self, thread_ref: ThreadRef, role: Role, ctx: SpawnContext) -> SessionHandle:
        handle = SessionHandle(
            session_id=new_ulid(),
            instance_id=ctx.own_instance_id,
            adapter_id=self.adapter_id,
            thread_ref=thread_ref,
            role=role,
            started_at=datetime.now(UTC),
        )
        self._sessions[handle] = ctx
        return handle

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        ctx = self._sessions[handle]
        payload = event.payload
        if payload.author == handle.instance_id:
            return  # instance self-filter (Gap-2 (b) / I3 v2.2): drop our own echo
        if "PROPOSAL" in payload.body:
            await ctx.on_reply(
                ReplyDraft(
                    body=_CRITIQUE,
                    reply_to_msg_id=payload.msg_id,
                    adapter_metadata={"model_id": "gemini-stub"},
                )
            )

    async def halt(self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)) -> None:
        self._sessions.pop(handle, None)

    async def health(self, handle: SessionHandle) -> HealthStatus:
        return HealthStatus(
            state=SessionState.IDLE,
            last_active_at=datetime.now(UTC),
            error=None,
            details={"adapter_id": self.adapter_id},
        )


# --------------------------------------------------------------------------- #
# Harness + driver (the human + the manual Phase-1 orchestrator)
# --------------------------------------------------------------------------- #


@dataclass
class _Harness:
    chatroom: _FakeChatroom
    mcp: _FakeMcp
    dispatcher: Dispatcher
    registry: InMemoryAdapterRegistry
    events: list[Event]
    claude_code: ClaudeCodeSdkAdapter
    naysayer: _MockNaysayerAdapter


def _build_harness(tmp_path: Path) -> _Harness:
    chatroom = _FakeChatroom()
    mcp = _FakeMcp(chatroom)
    gateway = MagickitChatroomGateway(mcp)
    registry = InMemoryAdapterRegistry()
    claude_code = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=_sdk_factory)
    naysayer = _MockNaysayerAdapter()
    # claude-code registered first → first qualified for the proposer/implementer
    # slots; the mock is the sole NAYSAYER_QUALIFIED candidate.
    registry.register(claude_code)
    registry.register(naysayer)
    events: list[Event] = []

    async def sink(event: Event) -> None:
        events.append(event)

    dispatcher = Dispatcher(registry=registry, gateway=gateway, event_sink=sink)
    return _Harness(chatroom, mcp, dispatcher, registry, events, claude_code, naysayer)


async def _drive_until(
    watcher: ChatroomWatcher, predicate: Callable[[], bool], *, max_polls: int = 20
) -> None:
    """Poll the watcher until ``predicate`` holds (adapters reply synchronously
    within a poll, so this is normally one iteration; the cap fails loud rather
    than hanging if the scripted flow ever stalls)."""
    for _ in range(max_polls):
        await watcher.poll_once()
        if predicate():
            return
    raise AssertionError(f"predicate not satisfied within {max_polls} polls")


def _thread_ref(kind: str, sid: str) -> ThreadRef:
    return ThreadRef(
        project_id=_PROJECT,
        thread_id=f"T-{kind}-{sid}",
        chatroom_uri=f"magickit://chatroom/thread/T-{kind}-{sid}",
    )


async def _run_stream(h: _Harness, sid: str) -> dict[str, ThreadRef]:
    """Drive one full proposer→naysayer→proposer→implementer cycle.

    The two manual touch-points (= the only human inputs the Phase 1 bar allows)
    are the opening question and the final approval; everything between is the
    automated dispatcher/watcher/adapter machinery, with the harness standing in
    for the not-yet-built convergence orchestrator (opening the review/impl
    threads and relaying across them).
    """
    work = _thread_ref("work", sid)
    review = _thread_ref("review", sid)
    impl = _thread_ref("impl", sid)
    # Stream-specific instance ids (Copilot review): with two concurrent streams
    # sharing one Dispatcher, distinct authors per stream make a cross-stream
    # routing leak observable — identical "{role}-1" labels across streams would
    # mask it. For the single-stream test (sid="1") these read back as the Phase 1
    # default "{role}-1" mint, so its audit-trail assertions are unchanged.
    proposer_id = f"proposer-{sid}"
    naysayer_id = f"naysayer-{sid}"
    implementer_id = f"implementer-{sid}"

    h.chatroom.open_thread(work.thread_id)
    watcher = ChatroomWatcher(
        h.mcp, h.dispatcher, [WatchSpec(work, Role.PROPOSER, instance_id=proposer_id)]
    )
    await watcher.start(baseline=False)  # spawn the proposer; thread is empty so far

    # (human #1) the opening question → proposer posts a proposal.
    h.chatroom.post(work.thread_id, author="human", content=_QUESTION)
    await _drive_until(watcher, lambda: h.chatroom.authors(work.thread_id)[-1:] == [proposer_id])
    proposal_body = h.chatroom.latest(work.thread_id)["content"]
    assert "PROPOSAL" in proposal_body

    # (orchestrator) open the independent-review thread, wire the naysayer, relay
    # the proposal → the independent naysayer objects.
    h.chatroom.open_thread(review.thread_id)
    await watcher.add_watch(
        WatchSpec(review, Role.NAYSAYER, instance_id=naysayer_id), baseline=False
    )
    h.chatroom.post(review.thread_id, author="orchestrator", content=proposal_body)
    await _drive_until(watcher, lambda: h.chatroom.authors(review.thread_id)[-1:] == [naysayer_id])
    critique_body = h.chatroom.latest(review.thread_id)["content"]
    assert "REQUEST_CHANGES" in critique_body

    # (orchestrator) relay the objection back → proposer revises and converges.
    h.chatroom.post(work.thread_id, author="orchestrator", content=critique_body)
    await _drive_until(watcher, lambda: h.chatroom.authors(work.thread_id).count(proposer_id) == 2)
    revised_body = h.chatroom.latest(work.thread_id)["content"]
    assert "DECIDE" in revised_body

    # (orchestrator) on the decision, open the impl thread, wire the implementer,
    # post the build request → implementer "opens the PR".
    h.chatroom.open_thread(impl.thread_id)
    await watcher.add_watch(
        WatchSpec(impl, Role.IMPLEMENTER, instance_id=implementer_id), baseline=False
    )
    h.chatroom.post(impl.thread_id, author="orchestrator", content=_BUILD)
    await _drive_until(watcher, lambda: h.chatroom.authors(impl.thread_id)[-1:] == [implementer_id])
    assert h.chatroom.latest(impl.thread_id)["content"].startswith("PR opened")

    # (human #2) the final approval, then the threads close. No further polling,
    # so the proposer never auto-replies to the approval (it would, if delivered).
    h.chatroom.post(work.thread_id, author="human", content=_APPROVAL)
    for ref in (work, review, impl):
        h.chatroom.close(ref.thread_id)
    await watcher.stop()
    return {"work": work, "review": review, "impl": impl}


# --------------------------------------------------------------------------- #
# §8 acceptance — 1 thread
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_e2e_single_thread_full_cycle(tmp_path: Path) -> None:
    h = _build_harness(tmp_path)
    refs = await _run_stream(h, "1")
    work, review, impl = refs["work"], refs["review"], refs["impl"]

    # Audit trail — the chatroom thread histories carry every step, attributed
    # to the right author (= instance_id, I3 v2.2).
    assert h.chatroom.authors(work.thread_id) == [
        "human",  # opening question
        "proposer-1",  # proposal
        "orchestrator",  # relayed objection
        "proposer-1",  # revised proposal + decide
        "human",  # final approval
    ]
    assert h.chatroom.authors(review.thread_id) == ["orchestrator", "naysayer-1"]
    assert h.chatroom.authors(impl.thread_id) == ["orchestrator", "implementer-1"]

    # The substance of the cycle.
    assert "DECIDE" in h.chatroom.messages(work.thread_id)[3]["content"]
    assert "REQUEST_CHANGES" in h.chatroom.latest(review.thread_id)["content"]
    assert h.chatroom.latest(impl.thread_id)["content"].startswith("PR opened")
    assert h.chatroom.is_closed(work.thread_id)

    # Minimal human input: exactly the opening question + the final approval —
    # counted across all of the stream's threads (MINOR-3: don't bake in the
    # assumption that human touch-points only ever land on the work thread).
    assert (
        sum(h.chatroom.authors(ref.thread_id).count("human") for ref in (work, review, impl)) == 2
    )

    # Observational audit channel (I7 sink): one reply.sent per adapter reply, in
    # order, no delivery.failed.
    assert {e.kind for e in h.events} == {EVENT_KIND_REPLY_SENT}
    assert [e.fields[EVENT_FIELD_AUTHOR] for e in h.events] == [
        "proposer-1",
        "naysayer-1",
        "proposer-1",
        "implementer-1",
    ]

    # Architecture-enforced independence (ADR-05 §5): only the distinct mock can
    # fill the naysayer slot; the same-model claude-code adapter is excluded.
    assert [a.adapter_id for a in h.registry.qualified_for(Role.NAYSAYER)] == [
        "naysayer-gemini-stub"
    ]
    assert h.registry.qualified_for(Role.PROPOSER)[0].adapter_id == "claude-code-sdk"
    assert h.registry.qualified_for(Role.IMPLEMENTER)[0].adapter_id == "claude-code-sdk"


# --------------------------------------------------------------------------- #
# §8 acceptance — 2 threads 並走 (concurrent, role-isolated)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_e2e_two_threads_concurrent_role_isolation(tmp_path: Path) -> None:
    h = _build_harness(tmp_path)

    # One shared Dispatcher/registry/gateway/chatroom; the two streams run
    # concurrently (each with its own watcher) — "dispatcher が並走できる". The
    # fakes yield on every chatroom read/write, so gather genuinely interleaves
    # the streams; that interleaving is asserted below (not just assumed).
    a, b = await asyncio.gather(_run_stream(h, "A"), _run_stream(h, "B"))

    for refs, sid in ((a, "A"), (b, "B")):
        work, review, impl = refs["work"], refs["review"], refs["impl"]
        other = "B" if sid == "A" else "A"
        # Role purity per thread, with stream-specific instance ids so a
        # cross-stream routing leak is actually observable (Copilot review).
        assert set(h.chatroom.authors(work.thread_id)) == {
            "human",
            "orchestrator",
            f"proposer-{sid}",
        }
        assert set(h.chatroom.authors(review.thread_id)) == {"orchestrator", f"naysayer-{sid}"}
        assert set(h.chatroom.authors(impl.thread_id)) == {"orchestrator", f"implementer-{sid}"}
        # The OTHER stream's roles never leak onto this stream's threads — this is
        # the assertion a same-label test could not make.
        assert f"proposer-{other}" not in h.chatroom.authors(work.thread_id)
        assert f"naysayer-{other}" not in h.chatroom.authors(review.thread_id)
        assert f"implementer-{other}" not in h.chatroom.authors(impl.thread_id)
        assert h.chatroom.authors(work.thread_id).count("human") == 2

    # The two streams are genuinely independent threads.
    assert a["work"].thread_id != b["work"].thread_id

    # DoD: total human input across the 2 streams = 2 questions + 2 approvals.
    total_human = sum(
        h.chatroom.authors(refs[kind].thread_id).count("human")
        for refs in (a, b)
        for kind in ("work", "review", "impl")
    )
    assert total_human == 4

    # Audit channel stays clean under concurrency: 8 replies (4 per stream), all
    # reply.sent, each attributed to its own stream's instances (no merge across
    # the concurrently-running streams).
    assert {e.kind for e in h.events} == {EVENT_KIND_REPLY_SENT}
    assert len(h.events) == 8
    assert {e.fields[EVENT_FIELD_AUTHOR] for e in h.events} == {
        "proposer-A",
        "naysayer-A",
        "implementer-A",
        "proposer-B",
        "naysayer-B",
        "implementer-B",
    }

    # SHOULD-1 (naysayer #73): make "concurrent" a *verified* property, not a
    # claim. The role-isolation checks above pass whether the streams interleave
    # or run back-to-back, so on their own they only prove "two streams on one
    # Dispatcher don't mix" — not isolation *under interleaving*. Prove genuine
    # interleaving from the global post order: a run that degenerated to sequential
    # (all of A, then all of B) is a single block boundary ("A…A B…B" → 2 blocks);
    # real interleaving alternates the streams many times. This assertion FAILS if
    # the harness ever silently degrades to sequential, so the isolation above is
    # exercised against actually-interleaved traffic on the shared Dispatcher.
    stream_tags = [tid.rsplit("-", 1)[-1] for _msg_id, tid in h.chatroom.timeline()]
    assert stream_tags.count("A") == stream_tags.count("B")  # symmetric work per stream
    blocks = 1 + sum(1 for x, y in pairwise(stream_tags) if x != y)
    assert blocks > 2, f"streams did not interleave — sequential degradation: {stream_tags}"


# --------------------------------------------------------------------------- #
# Independence is structural, not incidental
# --------------------------------------------------------------------------- #


def test_naysayer_independence_is_architecture_enforced(tmp_path: Path) -> None:
    h = _build_harness(tmp_path)
    # The naysayer slot resolves to the independent mock and nothing else.
    assert [a.adapter_id for a in h.registry.qualified_for(Role.NAYSAYER)] == [
        "naysayer-gemini-stub"
    ]
    # The same-model adapter cannot qualify (it lacks NAYSAYER_QUALIFIED).
    assert Capability.NAYSAYER_QUALIFIED not in h.claude_code.capabilities
    assert all(a.adapter_id != "claude-code-sdk" for a in h.registry.qualified_for(Role.NAYSAYER))
