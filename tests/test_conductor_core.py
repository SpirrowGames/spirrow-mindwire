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
async def test_content_absent_from_proposer_forces_naysayer_qa() -> None:
    # Q-A reversal (msg-542 Demand 2): a content-bearing turn that forgets its NEXT line still
    # terminates at the human — but the un-reviewed design gets a forced naysayer consult first
    # (NEXT-presence is no longer the trigger; un-reviewed content reaching the human is).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="a detailed design that forgot its NEXT line")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["forced review\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert disp.dispatches[0][0] is Role.NAYSAYER
    assert outcome.forced_naysayer_turns == 1
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_absent_from_naysayer_routes_to_human() -> None:
    # The naysayer's own un-routed turn is not re-reviewed: ABSENT from the naysayer falls straight
    # to the human fallback (Obj3), no forced consult.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="a critique that forgot its NEXT line")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp).run()
    assert outcome.stop_reason is StopReason.NO_HANDOFF
    assert disp.dispatches == []


@pytest.mark.anyio
async def test_empty_absent_turn_routes_to_human_without_forcing() -> None:
    # An empty turn that also fails to route carries no design content to review → plain human
    # fallback (the naysayer consult is for un-reviewed *content*, not for empty turns).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="   ")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp).run()
    assert outcome.stop_reason is StopReason.NO_HANDOFF
    assert outcome.forced_naysayer_turns == 0
    assert disp.dispatches == []


@pytest.mark.anyio
async def test_explicit_human_terminal_from_human_author_does_not_force_naysayer() -> None:
    # Obj2 protects the Tier-C gate from un-reviewed *agent* proposals; it must not police the
    # human's own message. An explicit NEXT: human from the human stops at the human directly, even
    # with no naysayer in the segment — symmetric with guard (i)'s human carve-out.
    # Regression: independent-naysayer REQUEST_CHANGES on PR #102 (T-pr-review-102 msg-548).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="let's pause here\n\nNEXT: human")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp).run()
    assert outcome.stop_reason is StopReason.HUMAN
    assert outcome.forced_naysayer_turns == 0
    assert disp.dispatches == []


@pytest.mark.anyio
async def test_content_absent_from_human_author_does_not_force_naysayer() -> None:
    # The likely case the naysayer flagged: the human posts "approved, go" and omits a NEXT line
    # (ABSENT). The human carve-out keeps the Q-A reversal from forcing an AI naysayer to review the
    # human's own instruction. Regression: PR #102 RC (T-pr-review-102 msg-548).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="approved, go")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp).run()
    assert outcome.stop_reason is StopReason.NO_HANDOFF
    assert outcome.forced_naysayer_turns == 0
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
    # A human Tier-C decide directs the implementer (carve-out ①); the implementer posts nothing →
    # latest is unchanged next round → no-progress stop (not a spin).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="approved, go\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(mcp, {})  # implementer has no scripted reply
    outcome = await _conductor(mcp, disp).run()
    assert disp.dispatches == [(Role.IMPLEMENTER, "m1")]
    assert outcome.stop_reason is StopReason.NO_PROGRESS


@pytest.mark.anyio
async def test_round_cap_backstops_a_nonconverging_loop() -> None:
    # proposer ↔ naysayer bounce forever (neither hands to the human / implementer, so no guard
    # intercept); the round cap stops the runaway.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="start\n\nNEXT: Einstein")
    disp = _ScriptedDispatcher(
        mcp,
        {
            Role.NAYSAYER: [f"nay {i}\n\nNEXT: Bohr" for i in range(10)],
            Role.PROPOSER: [f"prop {i}\n\nNEXT: Einstein" for i in range(10)],
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
    # (Driven through two proposers — the design→implement guard does not gate proposer handoffs —
    # so the test isolates the identity-keyed-session mechanism; the implementer carve-outs are
    # covered separately below.)
    roster: Mapping[str, Role] = {
        "Einstein": Role.NAYSAYER,
        "Bohr": Role.PROPOSER,
        "Dirac": Role.PROPOSER,
    }
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="kick\n\nNEXT: Bohr")
    disp = _ScriptedDispatcher(
        mcp, {Role.PROPOSER: ["prop A\n\nNEXT: Dirac", "prop B\n\nNEXT: none"]}
    )
    outcome = await Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=roster,
        naysayer_identity="Einstein",
    ).run()
    # Both proposers spawn as distinct instances (the Role-keyed bug spawned only the first).
    assert disp.spawns == [(Role.PROPOSER, "Bohr"), (Role.PROPOSER, "Dirac")]
    # The second turn is authored under Dirac — not misrouted to Bohr's session.
    assert [p["author"] for p in mcp.posts] == ["Bohr", "Dirac"]
    assert outcome.stop_reason is StopReason.SETTLED


# --------------------------------------------------------------------------- #
# guard (i): design→implement Tier-C gate + carve-outs + delegation (PR-2b-1)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_guard_i_proposer_to_implementer_is_redirected_to_human() -> None:
    # guard (i): the proposer cannot hand a design straight to the implementer; the conductor
    # redirects to the human terminal, which (Obj2) forces an independent naysayer consult first —
    # the implementer is never dispatched on the proposer's say-so.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["forced review\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert disp.dispatches[0][0] is Role.NAYSAYER
    assert all(role is not Role.IMPLEMENTER for role, _ in disp.dispatches)
    assert outcome.forced_naysayer_turns == 1
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_guard_i_redirect_stops_at_human_when_already_consulted() -> None:
    # If the naysayer already reviewed this segment, the gated proposer→implementer handoff goes
    # straight to the human for the Tier-C decision (no second forced review, no dispatch).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    mcp.seed(author="Einstein", content="review\n\nNEXT: Bohr")
    mcp.seed(author="Bohr", content="revised, ready to build\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp).run()
    assert disp.dispatches == []
    assert outcome.forced_naysayer_turns == 0
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_carveout_human_decide_directs_implementer() -> None:
    # carve-out ①: a human-authored Tier-C decide may direct the implementer directly.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="approved for implementation\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(
        mcp,
        {
            Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"],
            Role.PROPOSER: ["spec ok\n\nNEXT: none"],
        },
    )
    outcome = await _conductor(mcp, disp).run()
    assert disp.dispatches[0] == (Role.IMPLEMENTER, "m1")
    assert outcome.forced_naysayer_turns == 0
    assert outcome.stop_reason is StopReason.SETTLED


@pytest.mark.anyio
async def test_carveout_naysayer_relay_directs_implementer() -> None:
    # carve-out ②: the naysayer-name relay of a PR-gate REQUEST_CHANGES → fix loop may direct the
    # implementer (the conductor posts the RC verdict as the naysayer with NEXT: <implementer>).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="REQUEST_CHANGES: fix the thing\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["fixed\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert disp.dispatches[0] == (Role.IMPLEMENTER, "m1")
    assert outcome.forced_naysayer_turns == 0
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_delegation_lifts_human_stop_but_keeps_naysayer_consult() -> None:
    # carve-out ③: a human DELEGATE declaration lets the proposer hand to the implementer WITHOUT
    # stopping at the human — but the naysayer consult still runs (advisory), and only after it has
    # spoken does a later proposer→implementer proceed instead of stopping at the human.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="DELEGATE: design→impl thread\n\nNEXT: Bohr")
    disp = _ScriptedDispatcher(
        mcp,
        {
            Role.PROPOSER: ["design\n\nNEXT: Heisenberg", "revised\n\nNEXT: Heisenberg"],
            Role.NAYSAYER: ["advisory review\n\nNEXT: Bohr"],
            Role.IMPLEMENTER: ["built it\n\nNEXT: none"],
        },
    )
    outcome = await _conductor(mcp, disp).run()
    assert [role for role, _ in disp.dispatches] == [
        Role.PROPOSER,  # human → Bohr
        Role.NAYSAYER,  # Bohr → Heisenberg gated; consult forced (advisory) even under delegation
        Role.PROPOSER,  # naysayer → Bohr
        Role.IMPLEMENTER,  # Bohr → Heisenberg now proceeds (delegation lifted the human stop)
    ]
    assert outcome.forced_naysayer_turns == 1
    assert outcome.stop_reason is StopReason.SETTLED


@pytest.mark.anyio
async def test_without_delegation_proposer_to_implementer_stops_at_human_after_review() -> None:
    # Contrast with the delegation case: absent a DELEGATE, the same post-review proposer→impl
    # handoff stops at the human for the Tier-C decision rather than proceeding to the implementer.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="kickoff\n\nNEXT: Bohr")  # no DELEGATE line
    disp = _ScriptedDispatcher(
        mcp,
        {
            Role.PROPOSER: ["design\n\nNEXT: Heisenberg", "revised\n\nNEXT: Heisenberg"],
            Role.NAYSAYER: ["advisory review\n\nNEXT: Bohr"],
        },
    )
    outcome = await _conductor(mcp, disp).run()
    assert [role for role, _ in disp.dispatches] == [Role.PROPOSER, Role.NAYSAYER, Role.PROPOSER]
    assert outcome.forced_naysayer_turns == 1
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_delegation_ignored_when_not_human_authored() -> None:
    # A non-human author cannot self-delegate: a proposer-authored DELEGATE line does not lift
    # guard (i), so the design→implement handoff is still gated.
    mcp = _FakeChatroomMcp()
    mcp.seed(
        author="Bohr",
        content="DELEGATE: design→impl thread\n\ndesign\n\nNEXT: Heisenberg",
    )
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["review\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert disp.dispatches[0][0] is Role.NAYSAYER
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_empty_human_identity_disables_human_carveout() -> None:
    # Fail-safe: with no configured human identity, even a "human"-authored handoff to the
    # implementer is gated (the operator must set human_identity to enable carve-out ①).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="go\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["review\n\nNEXT: human"]})
    outcome = await Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        human_identity="",
    ).run()
    assert disp.dispatches[0][0] is Role.NAYSAYER
    assert outcome.stop_reason is StopReason.HUMAN


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


def test_ctor_rejects_naysayer_identity_not_mapping_to_naysayer_role() -> None:
    # Tier B msg-529: if naysayer_identity is absent from the roster (or maps to the wrong role),
    # _naysayer_consulted never recognises the naysayer's posts → a forced naysayer every round to
    # ROUND_CAP. Fail-fast at construction instead.
    mcp = _FakeChatroomMcp()
    disp = _ScriptedDispatcher(mcp, {})
    with pytest.raises(ValueError, match="must map to role"):
        Conductor(  # "Nobody" is absent from the roster
            mcp=mcp,
            dispatcher=disp,
            thread_ref=_thread_ref(),
            roster=_ROSTER,
            naysayer_identity="Nobody",
        )
    with pytest.raises(ValueError, match="must map to role"):
        Conductor(  # "Bohr" is in the roster but maps to PROPOSER, not NAYSAYER
            mcp=mcp,
            dispatcher=disp,
            thread_ref=_thread_ref(),
            roster=_ROSTER,
            naysayer_identity="Bohr",
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
