"""T-next-names-unspawnable-identity PR-1 (D2): the stop for a ``NEXT:`` nobody can spawn.

Bohr msg-8709 (確定設計 v0.4; approved by Einstein msg-8708 / msg-8710). What this pins:

* the stop reason is ``unspawnable_target_to_human`` (``StopReason.UNSPAWNABLE_TARGET``), not
  ``human``, so the notification can say the request was not delivered instead of "判断待ち";
* the stop line carries ``blocked_target=<ADR-11 partition key>`` after ``last_msg=``, the same
  for ``NEXT: Fermi`` and ``NEXT: fermi`` (msg-8707 v0.4 item 1);
* ``NEXT: Fermii`` is not this stop: it is not in the embodiment table, so it takes the
  ``IDENTITY_UNRESOLVED`` path and the line has no ``blocked_target=`` (msg-8705);
* an operator embodiment key that normalises to something holding ``=`` fails at load, and the
  shipped defaults pass (msg-8707 v0.4 item 2);
* the thread notice says the request was not delivered (msg-8709 ``_terminal_notice``);
* the Decider hook still labels the stop ``spawn_blocked`` rather than raising.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import pytest
from test_conductor_core import _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire.conductor.core import Conductor, StopReason
from spirrow_mindwire.conductor.gate_records import RELAY_AUTHOR
from spirrow_mindwire.decider.hook import (
    ROUTED_SPAWN_BLOCKED,
    ROUTED_STOP,
    RoutingInvariantError,
    routed_from_route,
)
from spirrow_mindwire.identity.embodiment import (
    DEFAULT_IDENTITY_EMBODIMENT,
    normalize_embodiment_table,
)
from spirrow_mindwire.identity.normalize import normalize_identity_key
from spirrow_mindwire.value_objects import Role

_ROSTER: Mapping[str, Role] = {
    "Bohr": Role.PROPOSER,
    "Heisenberg": Role.IMPLEMENTER,
    "Einstein": Role.NAYSAYER,
}


def _fermi_thread() -> _FakeChatroomMcp:
    """A thread Fermi owns: since PR #562 (ADR-2026-09-14-21 §7) ``NEXT: Fermi`` anywhere else is
    refused as ``fermi_outside_own_thread`` before the spawnability check, so the stop this file
    pins only happens on a Fermi-owned thread."""
    return _FakeChatroomMcp(owner="Fermi")


def _conductor(mcp: _FakeChatroomMcp) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=_ScriptedDispatcher(mcp, {}),
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=40,
    )


def _stop_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "conductor stopped:" in r.getMessage()]


def _relay_posts(mcp: _FakeChatroomMcp) -> list[dict[str, Any]]:
    return [p for p in mcp.posts if p.get("author") == RELAY_AUTHOR]


@pytest.mark.anyio
@pytest.mark.parametrize("token", ["Fermi", "fermi"])
async def test_stop_line_carries_the_normalised_blocked_target(
    token: str, caplog: pytest.LogCaptureFixture
) -> None:
    mcp = _fermi_thread()
    mcp.seed(author="Bohr", content=f"ask the chat\n\nNEXT: {token}")
    caplog.set_level(logging.INFO, logger="spirrow_mindwire.conductor.core")

    outcome = await _conductor(mcp).run()

    assert outcome.stop_reason is StopReason.UNSPAWNABLE_TARGET
    assert outcome.stop_reason.value == "unspawnable_target_to_human"
    (line,) = _stop_lines(caplog)
    assert "reason=unspawnable_target_to_human" in line
    fields = line.split()
    assert "blocked_target=fermi" in fields
    # Directly after last_msg= (msg-8709 D2), so last_msg's \S+ value is unchanged.
    last = next(i for i, f in enumerate(fields) if f.startswith("last_msg="))
    assert fields[last] == f"last_msg={outcome.last_msg_id}"
    assert fields[last + 1] == "blocked_target=fermi"


@pytest.mark.anyio
async def test_a_typo_is_unresolved_not_unspawnable(caplog: pytest.LogCaptureFixture) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="ask the chat\n\nNEXT: Fermii")
    caplog.set_level(logging.INFO)

    outcome = await _conductor(mcp).run()

    # Not in the embodiment table → not spawn-blocked; the unknown-name path handles it (here the
    # Q-A naysayer consult first, whose silent fake ends the run on NO_PROGRESS).
    assert outcome.stop_reason is not StopReason.UNSPAWNABLE_TARGET
    assert not any("spawn-unavailable" in r.getMessage() for r in caplog.records)
    for line in _stop_lines(caplog):
        assert "blocked_target=" not in line


@pytest.mark.anyio
async def test_other_stops_carry_no_blocked_target(caplog: pytest.LogCaptureFixture) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="review done\n\nNEXT: human")
    caplog.set_level(logging.INFO, logger="spirrow_mindwire.conductor.core")

    outcome = await _conductor(mcp).run()

    assert outcome.stop_reason is StopReason.HUMAN
    (line,) = _stop_lines(caplog)
    assert "blocked_target=" not in line


@pytest.mark.anyio
async def test_the_thread_notice_says_the_request_was_not_delivered() -> None:
    mcp = _fermi_thread()
    mcp.seed(author="Bohr", content="ask the chat\n\nNEXT: Fermi")

    await _conductor(mcp).run()

    (post,) = _relay_posts(mcp)
    body = post["content"]
    assert "依頼は `Fermi` に配送されていません" in body
    assert body.rstrip().endswith("NEXT: human")


def test_an_operator_key_holding_equals_fails_at_load() -> None:
    with pytest.raises(ValueError, match="="):
        normalize_embodiment_table({"Fer=mi": "web_ai_chat"})


def test_the_shipped_defaults_pass_the_equals_check() -> None:
    table = normalize_embodiment_table(None)
    assert table == {"fermi": "web_ai_chat"}
    for name in DEFAULT_IDENTITY_EMBODIMENT:
        key = normalize_identity_key(name)
        assert "=" not in key and not any(c.isspace() for c in key)


def test_an_operator_key_with_whitespace_normalises_and_loads() -> None:
    """Whitespace is removed by normalisation, so only ``=`` needs the load-time check."""
    assert normalize_embodiment_table({"Some Web": "web_ai_chat"}, include_defaults=False) == {
        "some-web": "web_ai_chat"
    }


def test_decider_labels_the_new_stop_spawn_blocked() -> None:
    kw: dict[str, Any] = {"is_forced": False, "target_role": None, "naysayer_role": Role.NAYSAYER}
    assert (
        routed_from_route(stop="unspawnable_target_to_human", spawn_blocked=True, **kw)
        == ROUTED_SPAWN_BLOCKED
    )
    assert routed_from_route(stop="human", spawn_blocked=False, **kw) == ROUTED_STOP
    # The new reason without the router's own flag is a broken invariant, not a label.
    with pytest.raises(RoutingInvariantError):
        routed_from_route(stop="unspawnable_target_to_human", spawn_blocked=False, **kw)
