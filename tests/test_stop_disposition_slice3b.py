"""T-next-line-carries-who-not-why Slice 3b, S3b-2 (Bohr msg-6523 §2 / msg-6525).

A ``STOP: blocked-on`` wake naming the implementer is MALFORMED, whoever wrote the line: a fired
park is posted by ``park-wake-relay`` (a machine), so such a wake always meets guard (i) and lands
at the human terminal — the second route from an agent's STOP line to the human that B-5 closed
for ``human``. What this file pins:

1. the parser: an implementer wake (any spelling) is MALFORMED, a proposer / naysayer wake is
   BLOCKED_ON, and acceptance follows :data:`STOP_WAKE_ROLES` for every :class:`Role`;
2. nothing is sent for it (no ``disposition``), and the park-wake tick neither parks nor wakes it:
   an agent's line counts as ``unclassified``, a human's as ``human_close`` (no author exemption);
3. the prompt names exactly the roles the parser accepts, and its example forms still parse.
"""

from __future__ import annotations

import pytest
from test_stop_disposition_slice3 import _ROSTER, _Gh, _post, _PostMcp, _TickMcp

from spirrow_mindwire.conductor.disposition import disposition_payload
from spirrow_mindwire.conductor.handoff import (
    STOP_WAKE_ROLES,
    StopStatus,
    build_handoff_protocol_block,
    resolve_handoff,
)
from spirrow_mindwire.magickit.gateway import MagickitChatroomGateway
from spirrow_mindwire.park_wake.runner import run_tick
from spirrow_mindwire.value_objects import Role


def _status(line: str, roster: dict[str, Role] = _ROSTER) -> StopStatus:
    stop = resolve_handoff(f"x\n\n{line}\nNEXT: none", roster).stop_line
    assert stop is not None
    return stop.status


def test_wake_roles_are_proposer_and_naysayer_only() -> None:
    assert STOP_WAKE_ROLES == (Role.PROPOSER, Role.NAYSAYER)
    assert Role.IMPLEMENTER not in STOP_WAKE_ROLES


@pytest.mark.parametrize("wake", ["Heisenberg", "heisenberg", "HEISENBERG"])
def test_an_implementer_wake_is_malformed(wake: str) -> None:
    line = f"STOP: blocked-on thread:T-a wake:{wake}"
    assert _status(line) is StopStatus.MALFORMED
    stop = resolve_handoff(f"x\n\n{line}\nNEXT: none", _ROSTER).stop_line
    assert stop is not None and disposition_payload(stop) is None


@pytest.mark.parametrize("wake", ["Bohr", "Einstein", "einstein"])
def test_a_proposer_or_naysayer_wake_is_still_blocked_on(wake: str) -> None:
    assert _status(f"STOP: blocked-on thread:T-a wake:{wake}") is StopStatus.BLOCKED_ON


@pytest.mark.parametrize("role", list(Role))
def test_acceptance_follows_the_wake_roles_for_every_role(role: Role) -> None:
    roster = {"Someone": role}
    expected = StopStatus.BLOCKED_ON if role in STOP_WAKE_ROLES else StopStatus.MALFORMED
    assert _status("STOP: blocked-on pr:acme/w#1 wake:Someone", roster) is expected


@pytest.mark.anyio
async def test_gateway_sends_no_disposition_for_an_implementer_wake() -> None:
    mcp = _PostMcp()
    body = "x\n\nSTOP: blocked-on thread:T-1 wake:Heisenberg\nNEXT: none"
    await _post(MagickitChatroomGateway(mcp, roster=_ROSTER), body)
    (call,) = mcp.calls
    assert "disposition" not in call
    assert call["content"] == body


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("author", "roles", "expected"),
    [("Bohr", ["proposer"], "unclassified"), ("human", ["human"], "human_close")],
)
async def test_tick_neither_parks_nor_wakes_an_implementer_wake(
    author: str, roles: list[str], expected: str
) -> None:
    """No author exemption (msg-6525, Objection 2): a person's line is MALFORMED too, and is
    counted as ``human_close`` — the runner's v11 §1 rule — rather than raised as an accident."""
    mcp = _TickMcp()
    mcp.identities[author] = roles
    mcp.thread("p", "T-a", status="resolved")  # the trigger has fired
    mcp.thread(
        "p",
        "T-1",
        author=author,
        content="w\n\nSTOP: blocked-on thread:T-a wake:Heisenberg\nNEXT: none",
    )
    report = (await run_tick(mcp=mcp, gh=_Gh({}), project="p", roster=_ROSTER)).as_dict()
    assert report["counts"][expected] == 1
    assert report["counts"]["blocked_on"] == 0
    assert report["woken"] == [] and report["held"] == [] and mcp.posts == []


@pytest.mark.parametrize("role", list(Role))
def test_prompt_names_exactly_the_wake_roles(role: Role) -> None:
    block = build_handoff_protocol_block(role)
    taught = " or ".join(r.value for r in STOP_WAKE_ROLES)
    assert f"who holds the {taught} role" in block
    assert "never the implementer" in block
    assert "wake the proposer" in block
