"""The conductor must attach thread context to the event it dispatches (D-3 wiring).

Why the *event* and not ``spawn_instance``: the conductor spawns one session per
identity and reuses it for every round (``sessions`` in :meth:`Conductor.run`),
while the thread grows on every round. Context attached at spawn would be round
one's thread forever — the staleness this thread exists to remove, reintroduced
one layer down. The event is rebuilt per round, so that is where it belongs.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_conductor_core import (
    _GREEN,
    _RED,
    _conductor,
    _FakeChatroomMcp,
    _NoMsgIdMcp,
    _rollup,
    _ScriptedDispatcher,
    _ScriptedPrGate,
    _ScriptedRollupSource,
)

from spirrow_mindwire.conductor.core import Conductor, StopReason
from spirrow_mindwire.github.client import ReviewEvent
from spirrow_mindwire.value_objects import ChatroomEvent, Role, SessionHandle, ThreadRef

_TR = ThreadRef(project_id="p", thread_id="T-x", chatroom_uri="mc://t")
_ROSTER = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER, "Heisenberg": Role.IMPLEMENTER}


class _Mcp:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_get_thread":
            return {"messages": self.messages}
        return {"msg": {"msg_id": "msg-relay"}}


class _Dispatcher:
    def __init__(self) -> None:
        self.events: list[ChatroomEvent] = []

    async def spawn_instance(
        self, thread_ref: ThreadRef, role: Role, instance_id: str
    ) -> SessionHandle:
        from datetime import UTC, datetime

        return SessionHandle(
            session_id="s",
            instance_id=instance_id,
            adapter_id="a",
            thread_ref=thread_ref,
            role=role,
            started_at=datetime.now(UTC),
        )

    async def dispatch(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        self.events.append(event)


@pytest.mark.anyio
async def test_dispatched_event_carries_the_thread_not_just_the_trigger() -> None:
    messages = [
        {"msg_id": "msg-1", "author": "Bohr", "content": "the design", "timestamp": ""},
        {"msg_id": "msg-2", "author": "Einstein", "content": "an objection", "timestamp": ""},
        {"msg_id": "msg-3", "author": "Bohr", "content": "go\n\nNEXT: Einstein", "timestamp": ""},
    ]
    dispatcher = _Dispatcher()
    c = Conductor(
        mcp=_Mcp(messages),
        dispatcher=dispatcher,
        thread_ref=_TR,
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=1,
    )
    await c.run()
    assert dispatcher.events, "the conductor did not dispatch"
    ctx = dispatcher.events[0].thread_context
    assert ctx is not None, "the dispatched turn got the trigger message and nothing else"
    assert ctx.opener is not None and ctx.opener.msg_id == "msg-1"
    assert [m.msg_id for m in ctx.recent] == ["msg-2"]
    assert ctx.total_count == 3


@pytest.mark.anyio
async def test_context_is_rebuilt_each_round_not_frozen_at_spawn() -> None:
    """A session is reused across rounds; a spawn-time snapshot would go stale."""
    messages = [
        {"msg_id": "msg-1", "author": "Bohr", "content": "opener", "timestamp": ""},
        {"msg_id": "msg-2", "author": "Bohr", "content": "one\n\nNEXT: Einstein", "timestamp": ""},
    ]
    mcp = _Mcp(messages)
    dispatcher = _Dispatcher()

    real_dispatch = dispatcher.dispatch

    async def _dispatch(handle: SessionHandle, event: ChatroomEvent) -> None:
        await real_dispatch(handle, event)
        # simulate the dispatched role posting its reply.
        #
        # The reply hands to Bohr, not back to Einstein. It used to hand to Einstein, which is a
        # self-handoff (author == next) — a shape the conductor now refuses to spawn and stops on
        # (StopReason.SELF_HANDOFF, design §6.1). This fixture only ever needed *a second round*
        # to prove the context is rebuilt rather than frozen at spawn; which participant that
        # round goes to is incidental, and a handoff that the loop treats as a fault is the wrong
        # way to ask for one.
        mcp.messages = [
            *mcp.messages,
            {
                "msg_id": f"msg-{len(mcp.messages) + 1}",
                "author": "Einstein",
                "content": "review\n\nNEXT: Bohr",
                "timestamp": "",
            },
        ]

    dispatcher.dispatch = _dispatch  # type: ignore[method-assign]
    c = Conductor(
        mcp=mcp,
        dispatcher=dispatcher,
        thread_ref=_TR,
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=3,
    )
    await c.run()
    assert len(dispatcher.events) >= 2
    first = dispatcher.events[0].thread_context
    second = dispatcher.events[1].thread_context
    assert first is not None and second is not None
    assert second.total_count > first.total_count


# --------------------------------------------------------------------------- #
# R-1b — relay / CI-route triggers are posted AFTER the fetch (msg-4871 §3)
# --------------------------------------------------------------------------- #


def _capture_fetches(conductor: Conductor) -> list[list[dict[str, Any]]]:
    """Record every list the conductor fetched, by identity, so mutation is observable."""
    fetched: list[list[dict[str, Any]]] = []
    real = conductor._fetch_messages

    async def _fetch() -> list[dict[str, Any]]:
        got = await real()
        fetched.append(got)
        return got

    conductor._fetch_messages = _fetch  # type: ignore[method-assign]
    return fetched


def _assert_trigger_appended_nondestructively(
    event: ChatroomEvent, fetched: list[dict[str, Any]], snapshot: list[dict[str, Any]]
) -> None:
    ctx = event.thread_context
    assert ctx is not None
    # The trigger was posted after the fetch; the builder must have seen [*fetched, trigger],
    # so the thread at trigger time is the fetched list plus the trigger itself.
    assert ctx.total_count == len(snapshot) + 1
    assert ctx.opener is not None and ctx.opener.msg_id == snapshot[0]["msg_id"]
    assert [m.msg_id for m in ctx.recent] == [m["msg_id"] for m in snapshot[1:]]
    # ...and the round's own list was not mutated to get there.
    assert fetched == snapshot


@pytest.mark.anyio
async def test_relay_path_dispatches_with_the_trigger_appended_without_mutation() -> None:
    """R-3(c), REQUEST_CHANGES relay path."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="the design\n\nNEXT: Heisenberg")
    mcp.seed(author="Heisenberg", content="opened the PR\n\nNEXT: pr-review acme/widgets#7")
    gate = _ScriptedPrGate(mcp, ReviewEvent.REQUEST_CHANGES)
    disp = _ScriptedDispatcher(mcp, {})
    c = _conductor(mcp, disp, orchestrator=gate)
    fetched = _capture_fetches(c)
    snapshot = [dict(m) for m in (await mcp.call_tool("chatroom_get_thread", {}))["messages"]]
    await c.run()
    assert disp.events, "the implementer was not dispatched on the relay"
    _assert_trigger_appended_nondestructively(disp.events[0], fetched[0], snapshot)


@pytest.mark.anyio
async def test_ci_route_path_dispatches_with_the_trigger_appended_without_mutation() -> None:
    """R-3(c), CI-route (R4) path."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="the design\n\nNEXT: Heisenberg")
    mcp.seed(author="Heisenberg", content="opened\n\nNEXT: pr-review acme/widgets#7")
    gate = _ScriptedPrGate(mcp, ReviewEvent.APPROVE)
    disp = _ScriptedDispatcher(mcp, {})
    source = _ScriptedRollupSource(_rollup(*_RED))
    c = _conductor(mcp, disp, orchestrator=gate, rollup_source=source)
    fetched = _capture_fetches(c)
    snapshot = [dict(m) for m in (await mcp.call_tool("chatroom_get_thread", {}))["messages"]]
    await c.run()
    assert disp.events, "the implementer was not dispatched on the ci-route post"
    _assert_trigger_appended_nondestructively(disp.events[0], fetched[0], snapshot)


@pytest.mark.anyio
async def test_ci_route_without_msg_id_stops_at_human_before_the_builder() -> None:
    """R-1b: a ci-route post with no ``msg_id`` could not satisfy the builder's contract.

    It never reaches it: the existing empty-id fail-safe stops at the human first, so
    ThreadContextTriggerMissing is not the failure mode. (The relay twin is pinned by
    ``test_pr_gate_relay_without_msg_id_fails_safe_to_human`` in test_conductor_core.)
    """
    mcp = _NoMsgIdMcp()
    mcp.seed(author="Heisenberg", content="opened\n\nNEXT: pr-review acme/widgets#7")
    gate = _ScriptedPrGate(mcp, ReviewEvent.APPROVE)
    disp = _ScriptedDispatcher(mcp, {})
    source = _ScriptedRollupSource(_rollup(*_RED))
    outcome = await _conductor(mcp, disp, orchestrator=gate, rollup_source=source).run()
    assert outcome.stop_reason is StopReason.HUMAN
    assert disp.dispatches == []


@pytest.mark.anyio
async def test_a_relay_after_a_ci_route_sees_the_route_post_in_its_context() -> None:
    """PR-gate #371 objection 1: "route_msg then relay_msg in one round → stale context".

    The two cannot share a round: the CI-route branch ends in ``continue`` and the relay
    only fires on ``GateAdmission.INVOKE``, so a relay always comes from a LATER round, and
    every round re-fetches ``messages``. This drives exactly the scenario the objection
    names (red → ci-route → implementer re-nominates → green → REQUEST_CHANGES relay) and
    pins that the relay turn's history contains the ci-route post, without the conductor
    ever mutating a fetched list.
    """
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="the design\n\nNEXT: Heisenberg")
    mcp.seed(author="Heisenberg", content="opened\n\nNEXT: pr-review acme/widgets#7")
    gate = _ScriptedPrGate(mcp, ReviewEvent.REQUEST_CHANGES)
    disp = _ScriptedDispatcher(
        mcp, {Role.IMPLEMENTER: ["fixed CI\n\nNEXT: pr-review acme/widgets#7"]}
    )
    source = _ScriptedRollupSource(_rollup(*_RED), _rollup(*_GREEN))
    c = _conductor(mcp, disp, orchestrator=gate, rollup_source=source)
    fetched = _capture_fetches(c)
    await c.run()
    assert len(disp.events) >= 2, [e.payload.body[:40] for e in disp.events]
    route_event, relay_event = disp.events[0], disp.events[1]
    assert "ADMISSION: route_implementer" in route_event.payload.body
    assert "VERDICT: REQUEST_CHANGES" in relay_event.payload.body
    ctx = relay_event.thread_context
    assert ctx is not None
    history = ([ctx.opener] if ctx.opener else []) + list(ctx.recent)
    assert route_event.payload.msg_id in [m.msg_id for m in history]
    # the relay's context is built from a later fetch than the route's
    assert any(route_event.payload.msg_id in [m["msg_id"] for m in f] for f in fetched[1:])
