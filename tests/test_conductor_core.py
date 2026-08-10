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

from spirrow_mindwire.conductor.control import ControlState
from spirrow_mindwire.conductor.core import Conductor, ConductorDispatcher, StopReason
from spirrow_mindwire.dispatcher.core import Dispatcher
from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
from spirrow_mindwire.github.client import CiState, ReviewEvent
from spirrow_mindwire.magickit.gateway import MagickitChatroomGateway
from spirrow_mindwire.naysayer.pr_review import PrReviewOutcome
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
        self.events: list[ChatroomEvent] = []

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
        self.events.append(event)
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


class _FakeControl:
    """Loop-control fake: yields the given states in order, repeating the last one forever.

    Recording ``observed`` separately from ``reads`` is what lets a test assert the write-back
    happened on the very round the conductor acted on the state — including the round it stops on.
    """

    def __init__(self, *states: ControlState) -> None:
        self._states = states or (ControlState.SUPERVISED,)
        self.reads = 0
        self.observed: list[ControlState] = []

    async def read(self) -> ControlState:
        state = self._states[min(self.reads, len(self._states) - 1)]
        self.reads += 1
        return state

    async def report_observed(self, state: ControlState) -> None:
        self.observed.append(state)


def _conductor(
    mcp: _FakeChatroomMcp,
    dispatcher: ConductorDispatcher,
    *,
    max_rounds: int = 40,
    orchestrator: Any = None,
    force_naysayer_only_on_explicit_human: bool = False,
    control: Any = None,
) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=dispatcher,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=max_rounds,
        orchestrator=orchestrator,
        force_naysayer_only_on_explicit_human=force_naysayer_only_on_explicit_human,
        control=control,
    )


def _pr_outcome(verdict: ReviewEvent) -> PrReviewOutcome:
    return PrReviewOutcome(
        verdict=verdict, body="critique body", ci_state=CiState.SUCCESS, head_sha="abc123"
    )


class _ScriptedPrGate:
    """A fake :class:`~spirrow_mindwire.conductor.core.PrGate`: returns scripted verdicts in order.

    Records each fired pr_ref. With one verdict it repeats it; with several it pops one per call (so
    an RC→fix→re-gate→APPROVE cycle can be scripted).
    """

    def __init__(self, *verdicts: ReviewEvent) -> None:
        self._verdicts = list(verdicts)
        self.fired: list[str] = []

    async def fire_pr_review(
        self, *, project: str, pr_ref: str
    ) -> tuple[ThreadRef, PrReviewOutcome]:
        self.fired.append(pr_ref)
        verdict = self._verdicts.pop(0) if len(self._verdicts) > 1 else self._verdicts[0]
        return _thread_ref(), _pr_outcome(verdict)


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
async def test_cost_lever_skips_forced_consult_on_absent_qa() -> None:
    # force_naysayer_only_on_explicit_human=True (cost lever): an un-routed (ABSENT / Q-A)
    # content turn no longer forces a naysayer consult — it falls straight through to the human
    # (NO_HANDOFF). Same seed as test_content_absent_from_proposer_forces_naysayer_qa, lever ON.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="a detailed design that forgot its NEXT line")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["forced review\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp, force_naysayer_only_on_explicit_human=True).run()
    assert disp.dispatches == []  # no forced consult
    assert outcome.forced_naysayer_turns == 0
    assert outcome.stop_reason is StopReason.NO_HANDOFF


@pytest.mark.anyio
async def test_cost_lever_keeps_forced_consult_on_explicit_human() -> None:
    # The lever narrows the force to an explicit NEXT: human ONLY — there it still fires, so Obj2
    # at the genuine Tier-C handoff is preserved. Same seed as test_obj2_forces_*, lever ON.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design ready\n\nNEXT: human")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["forced review\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp, force_naysayer_only_on_explicit_human=True).run()
    assert disp.dispatches[0][0] is Role.NAYSAYER
    assert outcome.forced_naysayer_turns == 1
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_cost_lever_skips_forced_consult_on_guard_i_redirect() -> None:
    # A proposer routing straight to the implementer is the design→implement guard-(i) redirect (a
    # hard-reject to the human). With the lever ON it no longer forces a naysayer consult on that
    # redirect — only an explicit NEXT: human does. (Lever OFF would force one here.)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="skip review, just build it\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["forced review\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp, force_naysayer_only_on_explicit_human=True).run()
    assert disp.dispatches == []  # no forced consult; hard-reject goes to the human
    assert outcome.forced_naysayer_turns == 0
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_forced_naysayer_saveable_counts_non_explicit_human() -> None:
    # Shadow metric (lever OFF): a forced consult on a non-explicit-human terminal (here an
    # ABSENT / Q-A turn) is "saveable" — the count force_naysayer_only_on_explicit_human would drop.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="a detailed design that forgot its NEXT line")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["forced review\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert outcome.forced_naysayer_turns == 1
    assert outcome.forced_naysayer_turns_saveable == 1  # ABSENT terminal → saveable


@pytest.mark.anyio
async def test_forced_naysayer_saveable_counts_guard_i_redirect() -> None:
    # A guard-(i) design→implement redirect (the proposer routes straight to the implementer) forces
    # a naysayer consult on a non-explicit-human terminal — so it is "saveable" (lever OFF). Proves
    # the _human_terminal(explicit_human=False) saveable path is reachable: the condition is
    # ``explicit_human OR not force_only_on_explicit_human``, so it fires for explicit_human=False.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="skip review, just build it\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["forced review\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert outcome.forced_naysayer_turns == 1
    assert outcome.forced_naysayer_turns_saveable == 1  # guard-(i) redirect → saveable


@pytest.mark.anyio
async def test_forced_naysayer_explicit_human_not_saveable() -> None:
    # An explicit NEXT: human forced consult is NOT saveable (the lever keeps it).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design ready\n\nNEXT: human")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["forced review\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert outcome.forced_naysayer_turns == 1
    assert outcome.forced_naysayer_turns_saveable == 0  # explicit NEXT: human → not saveable


@pytest.mark.anyio
async def test_tier_c_marker_logged_when_present_on_explicit_human(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # T-human-terminal-overuse §7 C — non-blocking observability: when the author's
    # ``NEXT: human`` carries a ``TIER-C: <reason>`` marker on the immediately preceding
    # line, the conductor emits a log line naming the parsed reason so a downstream grep
    # can count the marker-present rate. The routing is unchanged (still HUMAN).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="approved\n\nNEXT: Einstein")
    mcp.seed(
        author="Einstein",
        content=(
            "review — this needs the human to merge PR #99\nTIER-C: merge-protected\nNEXT: human"
        ),
    )
    disp = _ScriptedDispatcher(mcp, {})
    caplog.set_level("INFO", logger="spirrow_mindwire.conductor.core")
    outcome = await _conductor(mcp, disp).run()
    assert outcome.stop_reason is StopReason.HUMAN
    records = [r.getMessage() for r in caplog.records]
    assert any("tier_c_marker=merge-protected" in r for r in records), records


@pytest.mark.anyio
async def test_tier_c_marker_logged_missing_when_absent_on_explicit_human(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The "MISSING" observation is the counting bucket for the pre-registered analysis: an
    # explicit ``NEXT: human`` with no ``TIER-C:`` marker is what we're trying to measure
    # (in-loop uncertainty reaching for the human unnecessarily). The routing is unchanged.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="approved\n\nNEXT: Einstein")
    mcp.seed(author="Einstein", content="review with no tier-c reason\n\nNEXT: human")
    disp = _ScriptedDispatcher(mcp, {})
    caplog.set_level("INFO", logger="spirrow_mindwire.conductor.core")
    outcome = await _conductor(mcp, disp).run()
    assert outcome.stop_reason is StopReason.HUMAN
    records = [r.getMessage() for r in caplog.records]
    assert any("tier_c_marker=MISSING" in r for r in records), records


@pytest.mark.anyio
async def test_tier_c_marker_not_logged_on_guard_i_redirect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A guard-(i) design→implement redirect is NOT an author-emitted ``NEXT: human`` — the
    # observation deliberately skips it (the author never claimed a Tier-C reason, so a
    # "missing" count on it would poison the rate). Only explicit ``NEXT: human`` is
    # measured. Same seed as test_forced_naysayer_saveable_counts_guard_i_redirect.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="skip review, just build it\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["forced review\n\nNEXT: human"]})
    caplog.set_level("INFO", logger="spirrow_mindwire.conductor.core")
    await _conductor(mcp, disp).run()
    records = [r.getMessage() for r in caplog.records]
    # The forced-consult reply DOES carry a NEXT: human — that reply IS an explicit human
    # handoff (routed through resolve_handoff → HandoffKind.HUMAN), so it may log MISSING.
    # The guard-(i) turn itself (Bohr's proposer→implementer redirect) must NOT log.
    # Assert on the FIRST log record: it corresponds to the Bohr redirect, not the naysayer
    # reply that comes after.
    tier_c_records = [r for r in records if "tier_c_marker" in r]
    # The Bohr message never emitted; only the forced-naysayer reply might. So at most one
    # tier_c_marker record (from the naysayer's NEXT: human), never two.
    assert len(tier_c_records) <= 1


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
    # Driven over legitimate handoffs only (proposer ⇄ naysayer); the naysayer is the role reused.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    disp = _ScriptedDispatcher(
        mcp,
        {
            Role.NAYSAYER: ["review a\n\nNEXT: Bohr", "review b\n\nNEXT: human"],
            Role.PROPOSER: ["revised\n\nNEXT: Einstein"],
        },
    )
    outcome = await _conductor(mcp, disp).run()
    nay_spawns = [s for s in disp.spawns if s[0] is Role.NAYSAYER]
    assert nay_spawns == [(Role.NAYSAYER, "Einstein")]  # spawned once despite two dispatches
    assert [role for role, _ in disp.dispatches] == [
        Role.NAYSAYER,
        Role.PROPOSER,
        Role.NAYSAYER,
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
# guard (i): design→implement Tier-C gate + carve-out ① (PR-2b-1; ② → -2, ③ → dedicated slice)
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
async def test_design_time_naysayer_to_implementer_is_gated_not_carved_out() -> None:
    # carve-out ② (the broad `author_role is naysayer` bypass) is removed in PR-2b-1 (Tier-C
    # msg-553): the structural gate must not trust an in-band design-time naysayer that emits
    # NEXT: <implementer> (hallucination / prompt violation). It is redirected to the human
    # terminal — and since the naysayer already spoke this segment, no second consult is forced.
    # The PR-gate fix relay re-enters as a properly marker-gated carve-out in PR-2b-2.
    # Regression: independent-naysayer RC on PR #102 (T-pr-review-102, msg-552).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="REQUEST_CHANGES: fix the thing\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["fixed\n\nNEXT: human"]})
    outcome = await _conductor(mcp, disp).run()
    assert disp.dispatches == []  # implementer NOT dispatched on the design-time naysayer's say-so
    assert outcome.forced_naysayer_turns == 0
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_proposer_to_implementer_stops_at_human_after_review() -> None:
    # Even after an independent naysayer review, the proposer's design→implement handoff stops at
    # the human for the Tier-C decision rather than proceeding to the implementer (carve-out ① — a
    # human-authored decide — is the only path to the implementer in PR-2b-1).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="kickoff\n\nNEXT: Bohr")
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


# --------------------------------------------------------------------------- #
# carve-out ③: the naysayer's proceed to the implementer, gated on loop control
# --------------------------------------------------------------------------- #


def _design_to_code_dispatcher(mcp: _FakeChatroomMcp) -> _ScriptedDispatcher:
    """proposer → naysayer → (proceed) implementer: the chain carve-out ③ decides."""
    return _ScriptedDispatcher(
        mcp,
        {
            Role.PROPOSER: ["design\n\nNEXT: Einstein"],
            Role.NAYSAYER: ["sound, build it\n\nNEXT: Heisenberg"],
            Role.IMPLEMENTER: ["built\n\nNEXT: none"],
        },
    )


@pytest.mark.anyio
async def test_run_state_allows_naysayer_proceed_to_implementer() -> None:
    # With loop control at `run`, the naysayer's OWN proceed-handoff to the implementer is honored
    # without a per-step human GO (carve-out ③).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="kickoff\n\nNEXT: Bohr")
    disp = _design_to_code_dispatcher(mcp)
    outcome = await _conductor(mcp, disp, control=_FakeControl(ControlState.RUN)).run()
    assert [role for role, _ in disp.dispatches] == [Role.PROPOSER, Role.NAYSAYER, Role.IMPLEMENTER]
    assert outcome.stop_reason is StopReason.SETTLED


@pytest.mark.anyio
async def test_run_state_does_not_let_proposer_self_advance() -> None:
    # Even at `run`, ONLY the naysayer may advance to code (Einstein msg-601 Fix-1). A
    # proposer→implementer handoff is redirected to the human terminal (forcing a naysayer consult
    # first), so the proposer can never bypass the independent review by asking for the implementer.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="kickoff\n\nNEXT: Bohr")
    disp = _ScriptedDispatcher(
        mcp,
        {
            Role.PROPOSER: ["design\n\nNEXT: Heisenberg"],  # proposer tries to self-advance
            Role.NAYSAYER: ["forced review\n\nNEXT: human"],
        },
    )
    outcome = await _conductor(mcp, disp, control=_FakeControl(ControlState.RUN)).run()
    roles = [role for role, _ in disp.dispatches]
    assert Role.IMPLEMENTER not in roles
    assert roles == [Role.PROPOSER, Role.NAYSAYER]
    assert outcome.forced_naysayer_turns == 1
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_supervised_state_stops_the_naysayer_proceed_at_the_human() -> None:
    # At `supervised` (the pre-inversion behaviour) the naysayer's proceed is still gated to the
    # human: carve-out ③ is closed, so nothing but a human decide or a PR-gate verdict reaches code.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="kickoff\n\nNEXT: Bohr")
    disp = _design_to_code_dispatcher(mcp)
    outcome = await _conductor(mcp, disp, control=_FakeControl(ControlState.SUPERVISED)).run()
    roles = [role for role, _ in disp.dispatches]
    assert Role.IMPLEMENTER not in roles
    assert roles == [Role.PROPOSER, Role.NAYSAYER]
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_no_control_source_holds_the_supervised_baseline() -> None:
    # A Conductor with no control plane wired must NOT read as "granted autonomy". It holds the
    # pre-inversion baseline: the design loop turns, the naysayer's proceed does not reach code.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="kickoff\n\nNEXT: Bohr")
    disp = _design_to_code_dispatcher(mcp)
    outcome = await _conductor(mcp, disp).run()
    assert Role.IMPLEMENTER not in [role for role, _ in disp.dispatches]
    assert outcome.stop_reason is StopReason.HUMAN


# --------------------------------------------------------------------------- #
# hold: the operator's stop switch
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_hold_stops_before_dispatching_or_even_reading_the_thread() -> None:
    # A held project costs one control read and nothing else — no thread fetch, no dispatch, no
    # inference. And the hold IS reported observed on the round that stops: that write-back is
    # exactly what tells the operator's dashboard their stop landed, so the stopping path must not
    # be the one path that skips it.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    disp = _ScriptedDispatcher(mcp, {})
    control = _FakeControl(ControlState.HOLD)
    outcome = await _conductor(mcp, disp, control=control).run()
    assert outcome.stop_reason is StopReason.HOLD
    assert outcome.rounds == 0
    assert disp.dispatches == []
    assert control.reads == 1
    assert control.observed == [ControlState.HOLD]


@pytest.mark.anyio
async def test_hold_set_mid_run_lands_at_the_next_round_boundary() -> None:
    # The latency promise: a HOLD set while a turn is in flight does not abort that turn, it stops
    # the round after. Re-reading per round (not per run) is what bounds this to one round.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="kickoff\n\nNEXT: Bohr")
    disp = _design_to_code_dispatcher(mcp)
    control = _FakeControl(ControlState.RUN, ControlState.HOLD)
    outcome = await _conductor(mcp, disp, control=control).run()
    assert [role for role, _ in disp.dispatches] == [Role.PROPOSER]  # the in-flight turn completed
    assert outcome.stop_reason is StopReason.HOLD
    assert control.observed == [ControlState.RUN, ControlState.HOLD]


@pytest.mark.anyio
async def test_control_state_is_reread_every_round() -> None:
    # Carve-out ③ follows the CURRENT state, not the state the run started with: a project flipped
    # to `run` mid-run advances to code, and one flipped away from it would not.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="kickoff\n\nNEXT: Bohr")
    disp = _design_to_code_dispatcher(mcp)
    control = _FakeControl(ControlState.SUPERVISED, ControlState.RUN)
    outcome = await _conductor(mcp, disp, control=control).run()
    assert [role for role, _ in disp.dispatches] == [Role.PROPOSER, Role.NAYSAYER, Role.IMPLEMENTER]
    assert outcome.stop_reason is StopReason.SETTLED
    assert control.reads == 4  # one per round, including the round that settles


# --------------------------------------------------------------------------- #
# PR-gate (PR-2b-2): NEXT: pr-review fires the Tier B review synchronously, routes by the verdict
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_pr_gate_approve_stops_at_human() -> None:
    # NEXT: pr-review <ref> fires the gate; APPROVE → stop at the human (Tier-C merge). The verdict
    # relay is posted under the reserved author, and the implementer is NOT dispatched.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content="opened the PR\n\nNEXT: pr-review acme/widgets#7")
    gate = _ScriptedPrGate(ReviewEvent.APPROVE)
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp, orchestrator=gate).run()
    assert gate.fired == ["acme/widgets#7"]
    assert outcome.stop_reason is StopReason.HUMAN
    assert disp.dispatches == []  # no implementer dispatch on APPROVE
    assert mcp.posts[-1]["author"] == "pr-gate-relay"  # the verdict relay
    assert "VERDICT: APPROVE" in mcp.posts[-1]["content"]


@pytest.mark.anyio
async def test_pr_gate_request_changes_dispatches_implementer_then_reapprove() -> None:
    # REQUEST_CHANGES → the implementer is dispatched to fix (carve-out ②: verdict-driven, so
    # guard (i) is never consulted and no design-time naysayer is forced). The implementer re-emits
    # the pr-review sentinel; the second gate APPROVEs → stop at the human.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content="opened the PR\n\nNEXT: pr-review acme/widgets#7")
    gate = _ScriptedPrGate(ReviewEvent.REQUEST_CHANGES, ReviewEvent.APPROVE)
    disp = _ScriptedDispatcher(
        mcp, {Role.IMPLEMENTER: ["pushed a fix\n\nNEXT: pr-review acme/widgets#7"]}
    )
    outcome = await _conductor(mcp, disp, orchestrator=gate).run()
    assert gate.fired == ["acme/widgets#7", "acme/widgets#7"]
    assert [role for role, _ in disp.dispatches] == [Role.IMPLEMENTER]
    assert outcome.forced_naysayer_turns == 0  # the PR-gate IS the naysayer; none forced
    assert outcome.stop_reason is StopReason.HUMAN
    # the implementer is woken on the RC relay (verdict + critique), not its own pr-review trigger
    assert "VERDICT: REQUEST_CHANGES" in disp.events[0].payload.body
    assert "critique body" in disp.events[0].payload.body


@pytest.mark.anyio
async def test_pr_gate_request_changes_without_implementer_routes_to_human() -> None:
    # A roster with no single implementer persona cannot auto-dispatch the fix → a RC routes to the
    # human (fail-safe) rather than guessing who fixes it.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="opened the PR\n\nNEXT: pr-review acme/widgets#7")
    gate = _ScriptedPrGate(ReviewEvent.REQUEST_CHANGES)
    disp = _ScriptedDispatcher(mcp, {})
    conductor = Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster={"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER},  # no implementer
        naysayer_identity="Einstein",
        orchestrator=gate,
    )
    outcome = await conductor.run()
    assert gate.fired == ["acme/widgets#7"]
    assert disp.dispatches == []
    assert outcome.stop_reason is StopReason.HUMAN


class _NoMsgIdMcp(_FakeChatroomMcp):
    """A chatroom whose post results omit the msg_id (schema-drift / ambiguous-return sim)."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await super().call_tool(name, arguments)
        if name == "chatroom_post_message":
            return {"msg": {}}  # no msg_id field
        return result


@pytest.mark.anyio
async def test_pr_gate_relay_without_msg_id_fails_safe_to_human() -> None:
    # If the relay post returns no msg_id, the conductor cannot track no-progress on the continue
    # path, so a RC fails safe to the human (no implementer dispatch) — Tier B msg-572 #2.
    mcp = _NoMsgIdMcp()
    mcp.seed(author="Heisenberg", content="opened\n\nNEXT: pr-review acme/widgets#7")
    gate = _ScriptedPrGate(ReviewEvent.REQUEST_CHANGES)
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp, orchestrator=gate).run()
    assert outcome.stop_reason is StopReason.HUMAN
    assert disp.dispatches == []


@pytest.mark.anyio
async def test_pr_gate_sentinel_without_orchestrator_routes_to_human() -> None:
    # Fail-safe: a pr-review sentinel with no orchestrator wired (PR-gate disabled) routes to the
    # human rather than silently stranding — nothing is fired or relayed.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content="opened the PR\n\nNEXT: pr-review acme/widgets#7")
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp).run()  # orchestrator=None
    assert outcome.stop_reason is StopReason.HUMAN
    assert disp.dispatches == []
    assert mcp.posts == []


@pytest.mark.anyio
async def test_pr_gate_malformed_ref_routes_to_human_without_firing() -> None:
    # An unparseable PR ref must not reach fire_pr_review (which would raise and crash the loop);
    # the conductor validates via parse_pr_ref and fails safe to the human (Tier B PR #103 round 4).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content="oops\n\nNEXT: pr-review not-a-valid-ref")
    gate = _ScriptedPrGate(ReviewEvent.APPROVE)
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp, orchestrator=gate).run()
    assert gate.fired == []  # never fired — the ref failed validation
    assert outcome.stop_reason is StopReason.HUMAN
    assert disp.dispatches == []


@pytest.mark.anyio
async def test_pr_gate_normalizes_url_ref_to_slug_before_firing() -> None:
    # A URL ref is normalized to the canonical owner/repo#n slug before the gate fires, so the
    # orchestrator / GitHub client always receives a canonical ref (Tier B PR #103 round 5).
    mcp = _FakeChatroomMcp()
    mcp.seed(
        author="Heisenberg",
        content="opened\n\nNEXT: pr-review https://github.com/acme/widgets/pull/7",
    )
    gate = _ScriptedPrGate(ReviewEvent.APPROVE)
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp, orchestrator=gate).run()
    assert gate.fired == ["acme/widgets#7"]  # normalized from the URL
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
