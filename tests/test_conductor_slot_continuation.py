"""A4 (msg-9400 §3-§4): the conductor's CONTINUE decision in slot mode.

The ledger check replaces the clone check; a CONTINUE posted in this run is continued without it
(this run still holds that turn's slot); a refusal propagates as SlotContinuationRefused.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from test_conductor_core import _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire.conductor.continuation import render_relay
from spirrow_mindwire.conductor.core import CONDUCTOR_RELAY_AUTHOR, Conductor
from spirrow_mindwire.slot_continuation import SlotContinuationRefused, SlotContinuationVerdict
from spirrow_mindwire.turn_ref_snapshot import ContinuationScope
from spirrow_mindwire.value_objects import Role

_ROSTER: Mapping[str, Role] = {
    "Bohr": Role.PROPOSER,
    "Heisenberg": Role.IMPLEMENTER,
    "Einstein": Role.NAYSAYER,
}
_ENTRY = "設計 OK\n\nTIER-C-CHECK: none\nNEXT: Heisenberg"


def _report(body: str = "step 2 of 3") -> str:
    return f"step 1 done\n\nCONTINUE: {body}\nNEXT: Heisenberg"


class _SlotCheck:
    def __init__(self, *, refuse: bool = False) -> None:
        self.calls = 0
        self.refuse = refuse

    def __call__(self) -> None:
        self.calls += 1
        if self.refuse:
            raise SlotContinuationRefused(
                SlotContinuationVerdict(False, "not_saved", path="/q", detail="rescue_failed"),
                project="p",
                thread="t",
            )


def _clone_check_must_not_run(scope: ContinuationScope) -> None:
    raise AssertionError("the shared-clone check ran in slot mode")


def _conductor(
    mcp: _FakeChatroomMcp, disp: _ScriptedDispatcher, slot_check: _SlotCheck
) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=10,
        continue_cap=3,
        continuation_clone_check=None,
        slot_continuation_check=slot_check,
    )


@pytest.mark.anyio
async def test_continue_from_an_earlier_run_is_judged_by_the_ledger_check() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})
    check = _SlotCheck()

    await _conductor(mcp, disp, check).run()

    assert check.calls == 1
    assert disp.dispatches[0] == (Role.IMPLEMENTER, "m3")


@pytest.mark.anyio
async def test_refused_ledger_check_propagates_and_posts_no_relay() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})

    with pytest.raises(SlotContinuationRefused):
        await _conductor(mcp, disp, _SlotCheck(refuse=True)).run()

    assert not [p for p in mcp.posts if p.get("author") == CONDUCTOR_RELAY_AUTHOR]
    assert disp.dispatches == []


@pytest.mark.anyio
async def test_recovered_relay_head_is_judged_by_the_ledger_check() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    mcp.seed(
        author=CONDUCTOR_RELAY_AUTHOR,
        content=render_relay(n=1, cap=3, continue_body="step 2 of 3", implementer="Heisenberg"),
    )
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})

    with pytest.raises(SlotContinuationRefused):
        await _conductor(mcp, disp, _SlotCheck(refuse=True)).run()
    assert disp.dispatches == []


@pytest.mark.anyio
async def test_continue_posted_in_this_run_skips_the_ledger_check() -> None:
    """The slot of the turn that posted CONTINUE is still this run's: no ledger row exists yet,
    and none is needed, since the next turn works in the same clone."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="A で進める。\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(
        mcp, {Role.IMPLEMENTER: [_report("step 2"), "all done\n\nNEXT: Bohr"]}
    )
    check = _SlotCheck(refuse=True)  # would refuse if it were consulted

    await _conductor(mcp, disp, check).run()

    assert check.calls == 0
    assert [r for r, _ in disp.dispatches].count(Role.IMPLEMENTER) == 2


def test_shared_clone_check_is_not_used_in_slot_mode() -> None:
    """Both wired is not a configuration loop_runner builds; slot mode wins if it happens."""
    mcp = _FakeChatroomMcp()
    disp = _ScriptedDispatcher(mcp, {})
    conductor = Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        continuation_clone_check=_clone_check_must_not_run,
        slot_continuation_check=_SlotCheck(),
    )
    conductor._judge_continuation([], 0)
