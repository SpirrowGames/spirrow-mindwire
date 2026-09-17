"""Self-handoff detection and spawn-unavailable targets (design §6.1 / §6.4, ADR-2026-09-14-21).

Both cases used to end the same way — a session was spawned, nothing was delivered, the round
ended on ``NO_PROGRESS``, and the thread sat there. The fix is in :meth:`Conductor._route`, one
layer above the adapter's silent self-filter, so these tests assert on what the conductor did
*instead of spawning*: the stop reason, that no spawn happened at all, and that the reason was
written where a human will find it.

Two measured threads are the reason this file exists:
``spirrow-magickit/T-human-outage-degrade-close-only`` (head msg-244, author=Bohr, both the body
``NEXT:`` and the ``next_participant`` field saying Bohr) and
``spirrow-mindwire/T-scoped-driver-verdict-never-reaches-chatroom`` (head msg-2775, same shape,
field absent). The two differ in exactly the thing that decides which resolver path runs, so both
paths get a test.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from test_conductor_core import _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire.conductor.core import Conductor, ConductorDispatcher, StopReason
from spirrow_mindwire.conductor.gate_records import RELAY_AUTHOR
from spirrow_mindwire.value_objects import Role

_ROSTER: Mapping[str, Role] = {
    "Bohr": Role.PROPOSER,
    "Heisenberg": Role.IMPLEMENTER,
    "Einstein": Role.NAYSAYER,
}


def _conductor(
    mcp: _FakeChatroomMcp,
    dispatcher: ConductorDispatcher,
    *,
    identity_embodiment: dict[str, str] | None = None,
    max_rounds: int = 40,
) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=dispatcher,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=max_rounds,
        identity_embodiment=identity_embodiment,
    )


def _relay_posts(mcp: _FakeChatroomMcp) -> list[dict[str, Any]]:
    """Only the conductor's OWN posts — the fake records dispatched roles' replies too."""
    return [p for p in mcp.posts if p.get("author") == RELAY_AUTHOR]


# --------------------------------------------------------------------------------------------
# §6.1 — author == next
# --------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_self_handoff_stops_without_spawning() -> None:
    """(a) The head hands to its own author → SELF_HANDOFF, and nothing is started."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="F-1 起票\n\nNEXT: Bohr")
    disp = _ScriptedDispatcher(mcp, {})

    outcome = await _conductor(mcp, disp).run()

    assert outcome.stop_reason is StopReason.SELF_HANDOFF
    # The point of moving the detection into _route: no session is created at all. Before this,
    # spawn_instance ran and only the delivery was dropped.
    assert disp.spawns == []
    assert disp.dispatches == []


@pytest.mark.anyio
async def test_self_handoff_is_detected_through_the_envelope_field_too() -> None:
    """msg-244's shape: the structured field names the author, not just the body line."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="carve out ②\n\nNEXT: Bohr", next_participant="Bohr")
    disp = _ScriptedDispatcher(mcp, {})

    outcome = await _conductor(mcp, disp).run()

    assert outcome.stop_reason is StopReason.SELF_HANDOFF
    assert disp.spawns == []


@pytest.mark.anyio
async def test_self_handoff_comparison_is_case_insensitive() -> None:
    """ADR-2026-05-29-11 keys, not raw strings — ``bohr`` and ``Bohr`` are one identity."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="bohr", content="same actor, different spelling\n\nNEXT: Bohr")
    disp = _ScriptedDispatcher(mcp, {})

    assert (await _conductor(mcp, disp).run()).stop_reason is StopReason.SELF_HANDOFF


@pytest.mark.anyio
async def test_self_handoff_writes_the_reason_into_the_thread() -> None:
    """The stop is not silent: a reader of the thread can see why nothing happened."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="F-1 起票\n\nNEXT: Bohr")
    disp = _ScriptedDispatcher(mcp, {})

    outcome = await _conductor(mcp, disp).run()

    posts = _relay_posts(mcp)
    assert len(posts) == 1, "exactly one record, not one per round"
    body = posts[0]["content"]
    assert "自己ハンドオフ" in body
    assert "Bohr" in body
    # It ends on a stop token, which is what parks head_skip's Stage 1 instead of feeding the
    # retry loop a fresh head to chase.
    assert body.rstrip().endswith("NEXT: human")
    # The stop is recorded against the posted message — the one a human opens.
    assert outcome.last_msg_id == "m2"


@pytest.mark.anyio
async def test_a_handoff_to_a_different_participant_is_untouched() -> None:
    """The guard is narrow: an ordinary handoff still dispatches."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="proposal\n\nNEXT: Einstein")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["review\n\nNEXT: none"]})

    outcome = await _conductor(mcp, disp).run()

    assert outcome.stop_reason is StopReason.SETTLED
    assert disp.dispatches == [(Role.NAYSAYER, "m1")]
    assert _relay_posts(mcp) == []


# --------------------------------------------------------------------------------------------
# §6.4 / ADR-2026-09-14-21 — spawn-unavailable identities
# --------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_next_fermi_stops_exactly_where_next_human_stops() -> None:
    """(b) ``NEXT: Fermi`` is the same stop condition as ``NEXT: human`` (ADR D-3).

    Asserted as a comparison rather than against a literal so the two cannot drift: whatever
    reason an explicit human handoff produces from this fixture is the reason the Fermi handoff
    has to produce.
    """
    human_mcp = _FakeChatroomMcp()
    human_mcp.seed(author="Einstein", content="review done\n\nNEXT: human")
    human_outcome = await _conductor(human_mcp, _ScriptedDispatcher(human_mcp, {})).run()

    fermi_mcp = _FakeChatroomMcp()
    fermi_mcp.seed(author="Einstein", content="review done\n\nNEXT: Fermi")
    fermi_disp = _ScriptedDispatcher(fermi_mcp, {})
    fermi_outcome = await _conductor(fermi_mcp, fermi_disp).run()

    assert human_outcome.stop_reason is StopReason.HUMAN
    assert fermi_outcome.stop_reason is human_outcome.stop_reason
    assert fermi_disp.spawns == [], "the ADR forbids an adapter for web_ai_chat"


@pytest.mark.anyio
async def test_fermi_is_blocked_without_any_configuration() -> None:
    """The ADR's entry ships as a default; a host that wrote no config still refuses."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="ask the chat\n\nNEXT: Fermi")
    disp = _ScriptedDispatcher(mcp, {})

    outcome = await _conductor(mcp, disp, identity_embodiment=None).run()

    assert outcome.stop_reason is StopReason.HUMAN
    assert disp.spawns == []


@pytest.mark.anyio
async def test_spawn_unavailable_writes_the_general_rule_into_the_thread() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="ask the chat\n\nNEXT: Fermi")
    disp = _ScriptedDispatcher(mcp, {})

    await _conductor(mcp, disp).run()

    posts = _relay_posts(mcp)
    assert len(posts) == 1
    body = posts[0]["content"]
    assert "Fermi" in body and "web_ai_chat" in body
    assert "terminal_coding_agent" in body
    assert body.rstrip().endswith("NEXT: human")


@pytest.mark.anyio
async def test_the_rule_is_general_not_a_fermi_special_case() -> None:
    """Any non-terminal embodiment blocks — the ADR's D-3 is a rule, not an entry."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="hand over\n\nNEXT: Sagan")
    disp = _ScriptedDispatcher(mcp, {})

    outcome = await _conductor(mcp, disp, identity_embodiment={"Sagan": "web_ai_chat"}).run()

    assert outcome.stop_reason is StopReason.HUMAN
    assert disp.spawns == []


@pytest.mark.anyio
async def test_an_unknown_embodiment_blocks_rather_than_falling_through() -> None:
    """A value nobody here can interpret is not one any adapter here can run."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="hand over\n\nNEXT: Sagan")
    disp = _ScriptedDispatcher(mcp, {})

    outcome = await _conductor(mcp, disp, identity_embodiment={"Sagan": "carrier-pigeon"}).run()

    assert outcome.stop_reason is StopReason.HUMAN
    assert disp.spawns == []


@pytest.mark.anyio
async def test_an_operator_entry_can_unblock_a_shipped_default() -> None:
    """Config is merged OVER the defaults, so a shipped row can be corrected in place.

    Fermi is put in the roster here too, because unblocking it only means anything if there is
    a role to dispatch — which is exactly what a deployment that gave Fermi a terminal
    embodiment would have had to do.
    """
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="hand over\n\nNEXT: Fermi")
    disp = _ScriptedDispatcher(mcp, {Role.PROPOSER: ["from fermi\n\nNEXT: none"]})
    conductor = Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster={**_ROSTER, "Fermi": Role.PROPOSER},
        naysayer_identity="Einstein",
        identity_embodiment={"Fermi": "terminal_coding_agent"},
    )

    outcome = await conductor.run()

    assert outcome.stop_reason is StopReason.SETTLED
    assert disp.spawns == [(Role.PROPOSER, "Fermi")]


@pytest.mark.anyio
async def test_roster_identities_are_spawnable_by_default() -> None:
    """Silence about an identity is not a claim that it is web-driven."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="proposal\n\nNEXT: Einstein")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["ok\n\nNEXT: none"]})

    outcome = await _conductor(mcp, disp, identity_embodiment={}).run()

    assert outcome.stop_reason is StopReason.SETTLED
    assert disp.spawns == [(Role.NAYSAYER, "Einstein")]
