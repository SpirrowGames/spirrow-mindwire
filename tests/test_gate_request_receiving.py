"""The review-request receiving thread settles (T-gate-fires-on-review-request v1.1 §A, §C-9).

A ``T-gate-request-<repo>-<n>`` thread has no AI implementer, whatever the roster says, and no
verdict there may hand a member's PR to the human: every gate post ends in ``STOP: done`` and
the conductor dispatches nobody.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_conductor_core import (
    _ROSTER,
    _FakeChatroomMcp,
    _ScriptedDispatcher,
    _ScriptedPrGate,
)
from test_relay_route import _HEAD, _outcome, _relay_with, _ThreadMcp

from spirrow_mindwire.conductor.core import Conductor, StopReason
from spirrow_mindwire.conductor.gate_records import (
    RECEIVING_TERMINAL,
    RelayRoute,
    is_receiving_thread,
)
from spirrow_mindwire.conductor.handoff import settles_by_terminal_form
from spirrow_mindwire.gate_admission import AdmissionResult, GateAdmission
from spirrow_mindwire.github.client import ReviewEvent
from spirrow_mindwire.orchestrator import PrReviewOrchestrator
from spirrow_mindwire.value_objects import ThreadRef

_RECEIVING = "T-gate-request-widgets-7"
_PR_SLUG = "acme/widgets#7"


def test_receiving_thread_predicate() -> None:
    assert is_receiving_thread(_RECEIVING)
    assert not is_receiving_thread("T-cond")
    assert not is_receiving_thread("T-pr-review-widgets-7")


# ---- the orchestrator's verdict relay ---------------------------------------------------------


class _Driver:
    async def fetch_pr_base_ref(self, pr: Any) -> str:  # pragma: no cover - not reached
        return "develop"

    async def aclose(self) -> None:  # pragma: no cover - never reached
        return None


async def _receiving_relay(outcome: Any, implementer: str | None) -> dict[str, Any]:
    orch = PrReviewOrchestrator(_ThreadMcp(), driver=_Driver())  # type: ignore[arg-type]
    return await orch._post_design_relay(
        project="p",
        design_thread=_RECEIVING,
        pr_ref="o/r#7",
        outcome=outcome,
        implementer=implementer,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("event", "objections", "base"),
    [
        (ReviewEvent.REQUEST_CHANGES, None, "main"),
        (ReviewEvent.APPROVE, "[]", "main"),
        (ReviewEvent.APPROVE, "[]", "develop"),  # would be MERGE elsewhere
        (ReviewEvent.COMMENT, None, "main"),
    ],
)
@pytest.mark.parametrize("implementer", ["Heisenberg", None])
async def test_receiving_relay_settles_whatever_the_verdict(
    event: ReviewEvent, objections: str | None, base: str, implementer: str | None
) -> None:
    relay = await _receiving_relay(_outcome(event, objections, _HEAD, base_ref=base), implementer)
    body: str = relay["content"]
    assert relay["route"] is RelayRoute.HUMAN  # never IMPLEMENTER / MERGE
    assert body.rstrip().endswith(RECEIVING_TERMINAL)
    assert "NEXT:" not in body.split("VERDICT:", 1)[1].rsplit("\n\n", 1)[-1]
    assert settles_by_terminal_form(body)


@pytest.mark.anyio
async def test_a_design_thread_relay_is_unchanged() -> None:
    outcome = _outcome(ReviewEvent.REQUEST_CHANGES, None, _HEAD)
    relay = await _relay_with(_ThreadMcp(), outcome, _Driver())  # type: ignore[arg-type]
    assert relay["route"] is RelayRoute.IMPLEMENTER
    assert RECEIVING_TERMINAL not in relay["content"]


# ---- the conductor -----------------------------------------------------------------------------


def _receiving_ref() -> ThreadRef:
    return ThreadRef(
        project_id="spirrow-mindwire",
        thread_id=_RECEIVING,
        chatroom_uri=f"magickit://chatroom/thread/{_RECEIVING}",
    )


def _conductor(mcp: _FakeChatroomMcp, disp: Any, gate: Any) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_receiving_ref(),
        roster=_ROSTER,  # carries Heisenberg as implementer: the receiving thread ignores it
        naysayer_identity="Einstein",
        orchestrator=gate,
    )


class _RecordingGate(_ScriptedPrGate):
    def __init__(self, mcp: _FakeChatroomMcp, *verdicts: ReviewEvent) -> None:
        super().__init__(mcp, *verdicts)
        self.implementers: list[str | None] = []

    async def fire_pr_review(self, **kwargs: Any) -> Any:
        self.implementers.append(kwargs.get("implementer"))
        return await super().fire_pr_review(**kwargs)


@pytest.mark.anyio
@pytest.mark.parametrize("verdict", [ReviewEvent.REQUEST_CHANGES, ReviewEvent.APPROVE])
async def test_receiving_thread_verdict_dispatches_nobody_and_settles(
    verdict: ReviewEvent,
) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="pr-event-relay", content=f"review-request\n\nNEXT: pr-review {_PR_SLUG}")
    gate = _RecordingGate(mcp, verdict)
    disp = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, disp, gate).run()
    assert gate.fired == [_PR_SLUG]
    assert gate.implementers == [None]
    assert disp.dispatches == []
    assert outcome.stop_reason is StopReason.SETTLED


@pytest.mark.anyio
async def test_receiving_thread_admission_posts_settle() -> None:
    mcp = _FakeChatroomMcp()
    conductor = _conductor(mcp, _ScriptedDispatcher(mcp, {}), None)
    decision = AdmissionResult(admission=GateAdmission.ROUTE_HUMAN, rule="R3", reason="stuck")
    await conductor._post_admission_escalation(_PR_SLUG, decision)
    body = str(mcp.posts[-1]["content"])
    assert body.rstrip().endswith(RECEIVING_TERMINAL)
    assert "NEXT: human" not in body
