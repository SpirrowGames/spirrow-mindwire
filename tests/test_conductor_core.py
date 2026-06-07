"""Tests for the conductor core drive loop (msg-520 / msg-522 disposition / Tier-C decide msg-523).

Two levels: scripted-dispatcher tests pin the routing / stop / Obj2 / Obj3 logic precisely, and one
integration test drives the conductor through the **real** ``Dispatcher`` + gateway over a fake MCP
transport (only the chatroom + models are faked) to prove the production round-trip.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from spirrow_mindwire.conductor.core import Conductor, ConductorDispatcher, StopReason
from spirrow_mindwire.dispatcher.core import Dispatcher
from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
from spirrow_mindwire.magickit.gateway import MagickitChatroomGateway
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

_TS = datetime(2026, 6, 7, tzinfo=UTC)
_ROSTER: Mapping[str, Role] = {
    "Bohr": Role.PROPOSER,
    "Heisenberg": Role.IMPLEMENTER,
    "Einstein": Role.NAYSAYER,
}


def _thread_ref() -> ThreadRef:
    return ThreadRef(
        project_id="spirrow-mindwire",
        thread_id="T-cond",
        chatroom_uri="magickit://chatroom/thread/T-cond",
    )


class _FakeChatroomMcp:
    """In-memory chatroom: serves ``chatroom_get_thread`` and grows on ``chatroom_post_message``."""

    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []
        self._counter = 0
        self.posts: list[dict[str, Any]] = []

    def seed(self, *, author: str, content: str) -> None:
        self._counter += 1
        self._messages.append(
            {
                "msg_id": f"m{self._counter}",
                "author": author,
                "content": content,
                "reply_to": None,
                "timestamp": "2026-06-07T00:00:00Z",
            }
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_get_thread":
            return {"messages": list(self._messages)}
        if name == "chatroom_post_message":
            self._counter += 1
            msg = {
                "msg_id": f"m{self._counter}",
                "author": arguments["author"],
                "content": arguments["content"],
                "reply_to": arguments.get("reply_to"),
                "timestamp": "2026-06-07T00:00:00Z",
            }
            self._messages.append(msg)
            self.posts.append(arguments)
            return {"msg": {"msg_id": msg["msg_id"]}}
        raise AssertionError(f"unexpected tool {name!r}")


class _ScriptedDispatcher:
    """Records spawns/dispatches; simulates a role's reply by posting to the fake chatroom.

    ``replies`` maps a role to the bodies it posts on successive dispatches (each should end with a
    ``NEXT:`` line). A role with an empty / exhausted queue posts nothing — simulating silence /
    self-filter, which the conductor treats as no-progress.
    """

    def __init__(self, mcp: _FakeChatroomMcp, replies: Mapping[Role, list[str]]) -> None:
        self._mcp = mcp
        self._replies: dict[Role, list[str]] = {r: list(b) for r, b in replies.items()}
        self.spawns: list[tuple[Role, str]] = []
        self.dispatches: list[tuple[Role, str]] = []

    async def spawn_instance(
        self, thread_ref: ThreadRef, role: Role, instance_id: str
    ) -> SessionHandle:
        self.spawns.append((role, instance_id))
        return SessionHandle(
            session_id=new_ulid(),
            instance_id=instance_id,
            adapter_id="scripted",
            thread_ref=thread_ref,
            role=role,
            started_at=_TS,
        )

    async def dispatch(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        self.dispatches.append((handle.role, event.payload.msg_id))
        queue = self._replies.get(handle.role)
        if queue:
            body = queue.pop(0)
            await self._mcp.call_tool(
                "chatroom_post_message",
                {
                    "project": handle.thread_ref.project_id,
                    "thread_id": handle.thread_ref.thread_id,
                    "msg_type": "report",
                    "author": handle.instance_id,
                    "content": body,
                    "reply_to": event.payload.msg_id,
                },
            )


def _conductor(
    mcp: _FakeChatroomMcp, dispatcher: ConductorDispatcher, *, max_rounds: int = 40
) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=dispatcher,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=max_rounds,
    )


# --------------------------------------------------------------------------- #
# routing / stop conditions (scripted dispatcher)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_round_trip_named_naysayer_then_human() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design proposal\n\nNEXT: Einstein")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["critique\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert disp.spawns == [(Role.NAYSAYER, "Einstein")]
    assert [role for role, _ in disp.dispatches] == [Role.NAYSAYER]
    assert outcome.stop_reason is StopReason.HUMAN
    assert outcome.forced_naysayer_turns == 0  # NEXT named Einstein explicitly — not forced
    assert mcp.posts[-1]["author"] == "Einstein"  # authored under the persona name


@pytest.mark.anyio
async def test_obj2_forces_naysayer_before_human() -> None:
    # Proposer hands straight to human with no prior naysayer → conductor inserts a naysayer turn.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design ready\n\nNEXT: human")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["forced review\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert disp.dispatches[0][0] is Role.NAYSAYER  # naysayer consulted first
    assert disp.spawns == [(Role.NAYSAYER, "Einstein")]
    assert outcome.forced_naysayer_turns == 1
    assert outcome.stop_reason is StopReason.HUMAN  # after the forced consult, the human gets it


@pytest.mark.anyio
async def test_obj2_does_not_re_force_when_already_consulted() -> None:
    # design → review → disposition(→human): the naysayer already spoke this segment, so the
    # disposition proceeds to the human with no second forced review (matches the real msg-522).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    mcp.seed(author="Einstein", content="review\n\nNEXT: Bohr")
    mcp.seed(author="Bohr", content="disposition\n\nNEXT: human")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp).run()
    assert outcome.stop_reason is StopReason.HUMAN
    assert outcome.forced_naysayer_turns == 0
    assert disp.dispatches == []  # latest already at human; nothing dispatched


@pytest.mark.anyio
async def test_absent_handoff_routes_to_human() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="a reply with no NEXT line")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp).run()
    assert outcome.stop_reason is StopReason.NO_HANDOFF
    assert disp.dispatches == []


@pytest.mark.anyio
async def test_next_none_settles() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="closing out\n\nNEXT: none")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp).run()
    assert outcome.stop_reason is StopReason.SETTLED


@pytest.mark.anyio
async def test_no_progress_when_dispatched_role_is_silent() -> None:
    # The named role posts nothing → latest is unchanged next round → no-progress stop (not a spin).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="ping\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(mcp, {})  # implementer has no scripted reply
    outcome = await _conductor(mcp, disp).run()
    assert disp.dispatches == [(Role.IMPLEMENTER, "m1")]
    assert outcome.stop_reason is StopReason.NO_PROGRESS


@pytest.mark.anyio
async def test_round_cap_backstops_a_nonconverging_loop() -> None:
    # proposer ↔ implementer bounce forever; the round cap stops the runaway.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="start\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(
        mcp,
        {
            Role.IMPLEMENTER: [f"impl {i}\n\nNEXT: Bohr" for i in range(10)],
            Role.PROPOSER: [f"prop {i}\n\nNEXT: Heisenberg" for i in range(10)],
        },
    )
    outcome = await _conductor(mcp, disp, max_rounds=3).run()
    assert outcome.stop_reason is StopReason.ROUND_CAP
    assert outcome.rounds == 3
    assert len(disp.dispatches) == 3


@pytest.mark.anyio
async def test_session_is_reused_across_turns() -> None:
    # A role dispatched twice in one run is spawned once (session reuse → accumulated context).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="kick\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(
        mcp,
        {
            Role.IMPLEMENTER: ["impl a\n\nNEXT: Einstein", "impl b\n\nNEXT: human"],
            Role.NAYSAYER: ["review\n\nNEXT: Heisenberg"],
        },
    )
    outcome = await _conductor(mcp, disp).run()
    impl_spawns = [s for s in disp.spawns if s[0] is Role.IMPLEMENTER]
    assert impl_spawns == [(Role.IMPLEMENTER, "Heisenberg")]  # spawned once despite two dispatches
    assert [role for role, _ in disp.dispatches] == [
        Role.IMPLEMENTER,
        Role.NAYSAYER,
        Role.IMPLEMENTER,
    ]
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_distinct_identities_same_role_get_distinct_sessions() -> None:
    # Regression for the Tier B naysayer finding (msg-526): a sessions cache keyed by Role conflated
    # two personas of the same role; keyed by identity each persona spawns + authors distinctly.
    # (With the bug, Schrodinger's turn would be misrouted to Heisenberg's session.)
    roster: Mapping[str, Role] = {
        "Bohr": Role.PROPOSER,
        "Heisenberg": Role.IMPLEMENTER,
        "Schrodinger": Role.IMPLEMENTER,
        "Einstein": Role.NAYSAYER,
    }
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="kick\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(
        mcp, {Role.IMPLEMENTER: ["impl A\n\nNEXT: Schrodinger", "impl B\n\nNEXT: none"]}
    )
    outcome = await Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=roster,
        naysayer_identity="Einstein",
    ).run()
    # Both implementers spawn as distinct instances (the Role-keyed bug spawned only the first).
    assert disp.spawns == [(Role.IMPLEMENTER, "Heisenberg"), (Role.IMPLEMENTER, "Schrodinger")]
    # The second turn is authored under Schrodinger — not misrouted to Heisenberg's session.
    assert [p["author"] for p in mcp.posts] == ["Heisenberg", "Schrodinger"]
    assert outcome.stop_reason is StopReason.SETTLED


def test_ctor_rejects_bad_args() -> None:
    mcp = _FakeChatroomMcp()
    disp = _ScriptedDispatcher(mcp, {})
    with pytest.raises(ValueError, match="max_rounds"):
        Conductor(
            mcp=mcp,
            dispatcher=disp,
            thread_ref=_thread_ref(),
            roster=_ROSTER,
            naysayer_identity="Einstein",
            max_rounds=0,
        )
    with pytest.raises(ValueError, match="naysayer_identity"):
        Conductor(
            mcp=mcp,
            dispatcher=disp,
            thread_ref=_thread_ref(),
            roster=_ROSTER,
            naysayer_identity="   ",
        )


# --------------------------------------------------------------------------- #
# integration: real Dispatcher + real gateway over a fake MCP transport
# --------------------------------------------------------------------------- #


class _ScriptedAdapter:
    """A fake RoleAdapter that emits scripted replies (each ending with a ``NEXT:`` line)."""

    def __init__(
        self, adapter_id: str, capabilities: frozenset[Capability], replies: list[str]
    ) -> None:
        self.adapter_id = adapter_id
        self.capabilities = capabilities
        self._replies = list(replies)
        self._ctx: SpawnContext | None = None

    async def spawn(self, thread_ref: ThreadRef, role: Role, ctx: SpawnContext) -> SessionHandle:
        self._ctx = ctx
        return SessionHandle(
            session_id=new_ulid(),
            instance_id=ctx.own_instance_id,
            adapter_id=self.adapter_id,
            thread_ref=thread_ref,
            role=role,
            started_at=_TS,
        )

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        if event.payload.author == handle.instance_id:
            return  # self-filter (defensive against a self-reply loop)
        if not self._replies:
            return
        assert self._ctx is not None
        await self._ctx.on_reply(
            ReplyDraft(
                body=self._replies.pop(0),
                reply_to_msg_id=event.payload.msg_id,
                adapter_metadata={},
            )
        )

    async def halt(self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)) -> None:
        return None

    async def health(self, handle: SessionHandle) -> HealthStatus:
        return HealthStatus(state=SessionState.IDLE, last_active_at=_TS, error=None, details={})


@pytest.mark.anyio
async def test_integration_real_dispatcher_round_trip() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design proposal\n\nNEXT: Einstein")
    proposer = _ScriptedAdapter(
        "fake-proposer", frozenset({Capability.READ_THREAD, Capability.POST_REPLY}), []
    )
    naysayer = _ScriptedAdapter(
        "fake-naysayer",
        frozenset({Capability.READ_THREAD, Capability.POST_REPLY, Capability.NAYSAYER_QUALIFIED}),
        ["independent critique\n\nNEXT: human"],
    )
    registry = InMemoryAdapterRegistry()
    registry.register(proposer)  # first qualified for the PROPOSER slot
    registry.register(naysayer)
    dispatcher = Dispatcher(registry=registry, gateway=MagickitChatroomGateway(mcp))
    outcome = await Conductor(
        mcp=mcp,
        dispatcher=dispatcher,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
    ).run()
    assert outcome.stop_reason is StopReason.HUMAN
    assert len(mcp.posts) == 1
    post = mcp.posts[0]
    assert post["author"] == "Einstein"  # I3 v2.2: author = instance_id (the persona name)
    assert "independent critique" in post["content"]
