"""T-tier-c-six-articles-security-boundary, part 1b — Jev's per-hold answers acted on.

Bohr msg-9661 §1.1⁵ (one question per hold), msg-9663 §1.1⁶ (history appended to every
touched hold; ``unheld_unbased = yes`` opens a new hold), msg-9665 §1.1⁷ (structural holds are
asked about too), msg-9779 (release order: delivered → ``ESCALATED`` written → released; a failed
write keeps the holds), msg-9886 §2 (delivered only when **every** open hold is answered
``supplemented`` and the Decider runs in ``bounce`` mode; anything else is fail-closed) and §3 (the
release rides in the ``ESCALATED`` row's ``holds_released`` stamp, the one replay path a human's
release also uses). Every scenario runs through the conductor, not ``gate.escalated()`` directly
(PR #595 gate Obj-1-2). Harness: ``test_tierc_jev_bounce``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import _thread_ref
from test_tierc_jev_bounce import _bounces, _Decider, _gate, _rows, _run

from spirrow_mindwire.conductor.tierc_gate import TierCGate, bounced_msg_id
from spirrow_mindwire.decider.questions import (
    TIERC_ESCALATION_QUESTIONS_VERSION,
    TIERC_HOLD_QUESTIONS_VERSION,
    UNHELD_UNBASED_KEY,
    HoldRef,
    hold_relation_key,
)
from spirrow_mindwire.decider.result import DecisionOutcome, DecisionResult
from spirrow_mindwire.decider.state import DecisionState
from spirrow_mindwire.decider.verdict import TierCV2Verdict, TierCVerdictKind
from spirrow_mindwire.tier_c_admission_gate import BounceReason, LogKind
from spirrow_mindwire.tier_c_security_holds import read_hold_answers
from spirrow_mindwire.value_objects import Role

NOW = datetime(2026, 11, 20, tzinfo=UTC)
THREAD = _thread_ref().thread_id
HEAD = "A と B の場所を補った\nSECURITY-BASIS: exposure src/a.py:3\nTIER-C: security\nNEXT: human"


def _hold(gate: TierCGate, text: str, msg_id: str) -> str:
    """Open a structural hold for Bohr in the scenario thread; return its id."""
    decision = gate.admit(
        body=f"{text}\nTIER-C: security\nNEXT: human",
        author="Bohr",
        thread=THREAD,
        msg_id=msg_id,
        now=NOW,
    )
    return str(decision.log_entries[0].payload["retry_uuid"])


def _result(
    relations: dict[str, str],
    *,
    kind: TierCVerdictKind = TierCVerdictKind.CONFIRMED,
    unheld: str | None = "no",
) -> DecisionResult:
    raw: dict[str, Any] = {
        hold_relation_key(h): {"choice": r, "confidence": 0.9} for h, r in relations.items()
    }
    if unheld is not None:
        raw[UNHELD_UNBASED_KEY] = {"choice": unheld}
    return DecisionResult(
        outcome=DecisionOutcome.EVALUATED,
        decision_id="d",
        provider="jev",
        raw_answers=raw,
        verdict=TierCV2Verdict(kind=kind, ask_score=0.9 if kind.value != "likely_not" else 0.1),
        policy="p",
    )


class _SeeingDecider(_Decider):
    def __init__(self, result: DecisionResult | None, mode: str = "bounce") -> None:
        super().__init__(result, mode)
        self.states: list[DecisionState] = []

    async def evaluate(self, state: DecisionState) -> DecisionResult | None:
        self.states.append(state)
        return await super().evaluate(state)


def _open(gate: TierCGate) -> list[str]:
    return [h.hold_id for h in gate.holds(thread=THREAD, author="Bohr")]


def _escalated(gate: TierCGate) -> list[dict[str, Any]]:
    return [r for r in _rows(gate) if r["kind"] == LogKind.ESCALATED.value]


@pytest.mark.anyio
async def test_the_decider_is_asked_about_every_open_hold(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    a, b = _hold(gate, "A の漏洩", "m0a"), _hold(gate, "B の漏洩", "m0b")
    decider = _SeeingDecider(_result({a: "supplemented", b: "supplemented"}))
    await _run(gate, decider, head=HEAD)
    (state,) = decider.states
    assert [h.hold_id for h in state.security_holds] == [a, b]
    assert [h.held_msg_id for h in state.security_holds] == ["m0a", "m0b"]


def _stamped(gate: TierCGate) -> list[str]:
    """Every hold id any log row releases through ``holds_released``."""
    return [h for r in _rows(gate) for h in r.get("holds_released") or ()]


@pytest.mark.anyio
async def test_both_supplemented_delivers_and_releases_both(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    a, b = _hold(gate, "A", "m0a"), _hold(gate, "B", "m0b")
    _, _, mcp = await _run(
        gate, _Decider(_result({a: "supplemented", b: "supplemented"})), head=HEAD
    )
    assert _bounces(mcp) == []
    (row,) = _escalated(gate)
    assert row["holds_released"] == sorted([a, b])
    assert "released_holds" not in row  # msg-9886 §3: the old field is gone
    assert _open(gate) == []


@pytest.mark.anyio
async def test_a_supplemented_hold_is_released_by_replay_without_the_thread_line(
    tmp_path: Path,
) -> None:
    """msg-9886 §4: supplement A → delivered → A released; ``gate.holds()`` with no release passed
    in returns nothing, and the same body refused after the release opens a hold of its own
    (the round 2 ordering applied to the ``ESCALATED`` stamp)."""
    gate = _gate(tmp_path)
    a = _hold(gate, "A", "m0a")
    _hold(gate, "A", "m0a2")  # identical re-send before the release: history of ``a``
    assert _open(gate) == [a]
    _, _, mcp = await _run(gate, _Decider(_result({a: "supplemented"})), head=HEAD)
    assert _bounces(mcp) == []
    assert _escalated(gate)[0]["holds_released"] == [a]
    assert gate.holds(thread=THREAD, author="Bohr") == []
    later = _hold(gate, "A", "m9")
    assert _open(gate) == [later]
    assert later != a


@pytest.mark.anyio
async def test_only_one_of_two_supplemented_bounces_and_releases_neither(tmp_path: Path) -> None:
    """msg-9886 §2 / §4: A supplemented, B ``unrelated`` → the whole head bounces and nothing is
    released (every open hold must be answered ``supplemented``)."""
    gate = _gate(tmp_path)
    a, b = _hold(gate, "A", "m0a"), _hold(gate, "B", "m0b")
    _, _, mcp = await _run(gate, _Decider(_result({a: "supplemented", b: "unrelated"})), head=HEAD)
    (notice,) = _bounces(mcp)
    assert BounceReason.SECURITY_HOLD.value in notice["content"]
    assert _escalated(gate) == []
    assert _stamped(gate) == []
    assert _open(gate) == [a, b]


@pytest.mark.anyio
async def test_one_unsupplemented_stops_the_whole_handoff(tmp_path: Path) -> None:
    """msg-9660 Obj-6-1: B supplemented and A carried unsupplemented in one body → nothing
    delivered, nothing released, both holds get history."""
    gate = _gate(tmp_path)
    a, b = _hold(gate, "A", "m0a"), _hold(gate, "B", "m0b")
    _, _, mcp = await _run(
        gate, _Decider(_result({a: "unsupplemented", b: "supplemented"})), head=HEAD
    )
    (notice,) = _bounces(mcp)
    assert BounceReason.SECURITY_HOLD.value in notice["content"]
    # the notice must not promise that a RETRY gets through while held
    assert "人へは通りません" in notice["content"]
    assert "1回だけ、ラベルに関係なく人へ通します" not in notice["content"]
    assert _escalated(gate) == []
    holds = gate.holds(thread=THREAD, author="Bohr")
    assert [h.hold_id for h in holds] == [a, b]
    assert [h.history[-1]["relation"] for h in holds] == ["unsupplemented", "supplemented"]


@pytest.mark.anyio
async def test_supplemented_but_likely_not_keeps_the_hold(tmp_path: Path) -> None:
    """msg-9886 §2: ``LIKELY_NOT`` bounces ``security-hold``; history is appended (§1.1⁶ (a))."""
    gate = _gate(tmp_path)
    a = _hold(gate, "A", "m0a")
    result = _result({a: "supplemented"}, kind=TierCVerdictKind.LIKELY_NOT)
    _, _, mcp = await _run(gate, _Decider(result), head=HEAD)
    (notice,) = _bounces(mcp)
    assert BounceReason.SECURITY_HOLD.value in notice["content"]
    assert "人へは通りません" in notice["content"]
    (hold,) = gate.holds(thread=THREAD, author="Bohr")
    assert hold.history[-1] == {
        "hold_id": a,
        "relation": "supplemented",
        "outcome": "security-hold",
        "msg_id": "m3",
    }


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["shadow", "annotate"])
async def test_bounce_mode_off_is_not_delivered_even_when_supplemented(
    tmp_path: Path, mode: str
) -> None:
    """msg-9886 §2: outside ``bounce`` mode the Decider's answers are not acted on, so a held head
    is never delivered on them — fail-closed, not the ordinary pass-through."""
    gate = _gate(tmp_path)
    a = _hold(gate, "A", "m0a")
    _, _, mcp = await _run(gate, _Decider(_result({a: "supplemented"}), mode=mode), head=HEAD)
    (notice,) = _bounces(mcp)
    assert BounceReason.SECURITY_HOLD_NO_VERDICT.value in notice["content"]
    assert _escalated(gate) == []
    assert _stamped(gate) == []
    assert _open(gate) == [a]


@pytest.mark.anyio
async def test_unheld_unbased_opens_a_new_hold_beside_the_appended_one(tmp_path: Path) -> None:
    """msg-9663 §6⁵: A carried unsupplemented plus an unbased B in one body → A appended, B's
    hold opened. A later head that supplements only A still bounces (B is open and not
    supplemented, msg-9886 §2); one that supplements both releases both."""
    gate = _gate(tmp_path)
    a = _hold(gate, "A", "m0a")
    await _run(gate, _Decider(_result({a: "unsupplemented"}, unheld="yes")), head=HEAD)
    holds = gate.holds(thread=THREAD, author="Bohr")
    assert [h.held_msg_id for h in holds] == ["m0a", "m3"]
    new = holds[1].hold_id
    gate2 = TierCGate(log_path=gate.log_path, uuid_factory=gate.uuid_factory)
    await _run(gate2, _Decider(_result({a: "supplemented", new: "unrelated"})), head=HEAD)
    assert _open(gate2) == [a, new]
    await _run(gate2, _Decider(_result({a: "supplemented", new: "supplemented"})), head=HEAD)
    assert _open(gate2) == []


@pytest.mark.anyio
async def test_all_supplemented_plus_a_new_unbased_request_is_not_delivered(
    tmp_path: Path,
) -> None:
    """PR #599 gate round 1: every open hold answered ``supplemented`` but ``unheld_unbased =
    yes`` (the head also carries another unbased security request) → the whole head bounces
    ``security-hold``, nothing is delivered or released, and a hold opens on the new request."""
    gate = _gate(tmp_path)
    a, b = _hold(gate, "A", "m0a"), _hold(gate, "B", "m0b")
    result = _result({a: "supplemented", b: "supplemented"}, unheld="yes")
    _, _, mcp = await _run(gate, _Decider(result), head=HEAD)
    (notice,) = _bounces(mcp)
    assert BounceReason.SECURITY_HOLD.value in notice["content"]
    assert _escalated(gate) == []
    assert _stamped(gate) == []
    holds = gate.holds(thread=THREAD, author="Bohr")
    assert [h.held_msg_id for h in holds] == ["m0a", "m0b", "m3"]
    assert [h.hold_id for h in holds][:2] == [a, b]
    assert [h.history[-1]["relation"] for h in holds[:2]] == ["supplemented", "supplemented"]


@pytest.mark.anyio
async def test_no_answer_for_a_hold_delivers_and_releases_nothing(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    a, b = _hold(gate, "A", "m0a"), _hold(gate, "B", "m0b")
    _, _, mcp = await _run(gate, _Decider(_result({a: "supplemented"})), head=HEAD)  # b missing
    (notice,) = _bounces(mcp)
    assert BounceReason.SECURITY_HOLD_NO_VERDICT.value in notice["content"]
    assert _escalated(gate) == []
    assert _open(gate) == [a, b]


@pytest.mark.anyio
@pytest.mark.parametrize("decider", ["none", "no-result"])
async def test_no_decider_result_while_held_is_not_delivered(tmp_path: Path, decider: str) -> None:
    """msg-9886 §2: no decider, or a decider that returned nothing (unavailable) → bounce."""
    gate = _gate(tmp_path)
    a = _hold(gate, "A", "m0a")
    _, _, mcp = await _run(gate, None if decider == "none" else _Decider(None), head=HEAD)
    (notice,) = _bounces(mcp)
    assert BounceReason.SECURITY_HOLD_NO_VERDICT.value in notice["content"]
    assert _escalated(gate) == []
    assert _stamped(gate) == []
    assert _open(gate) == [a]


@pytest.mark.anyio
async def test_a_held_head_never_takes_the_ordinary_path_when_its_holds_read_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """msg-9886 §2: ``admit`` saw the hold (``RH-recheck``) but the conductor's own read came back
    empty (a read failure) → the head is bounced, never delivered on the ordinary path."""
    from spirrow_mindwire.conductor.core import Conductor

    gate = _gate(tmp_path)
    a = _hold(gate, "A", "m0a")
    monkeypatch.setattr(Conductor, "_open_security_holds", lambda self, messages: [])
    _, _, mcp = await _run(gate, _Decider(_result({a: "supplemented"})), head=HEAD)
    (notice,) = _bounces(mcp)
    assert BounceReason.SECURITY_HOLD_NO_VERDICT.value in notice["content"]
    assert _escalated(gate) == []
    assert _open(gate) == [a]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        pytest.param("likely_not", id="jev-vetoes-delivery"),
        pytest.param("no_unheld_answer", id="malformed-answer-set"),
        pytest.param("shadow", id="bounce-mode-off"),
    ],
)
async def test_delivery_failure_releases_nothing(tmp_path: Path, case: str) -> None:
    """msg-9886 §4 / Einstein: the open hold is answered ``supplemented``, yet the head is not
    delivered — it does not stop at the human: the conductor posts a bounce and dispatches the
    author on it. Then no row carries a ``holds_released`` stamp and the hold stays open.

    In this conductor "delivery" is the gate letting the head stop at the human; nothing is sent
    after the ``ESCALATED`` row that could fail (that row's own failure is
    ``test_escalated_write_failure_keeps_the_holds``). So the delivery that fails here is the one
    refused after supplementation was answered."""
    gate = _gate(tmp_path)
    a = _hold(gate, "A", "m0a")
    if case == "likely_not":
        decider = _Decider(_result({a: "supplemented"}, kind=TierCVerdictKind.LIKELY_NOT))
    elif case == "no_unheld_answer":
        decider = _Decider(_result({a: "supplemented"}, unheld=None))
    else:
        decider = _Decider(_result({a: "supplemented"}), mode="shadow")
    _, disp, mcp = await _run(gate, decider, head=HEAD)
    (notice,) = _bounces(mcp)
    assert bounced_msg_id(notice["content"]) == "m3"
    assert disp.dispatches[-1][0] is Role.PROPOSER  # sent back, not stopped at the human
    assert _escalated(gate) == []
    assert _stamped(gate) == []
    assert [h.hold_id for h in gate.holds(thread=THREAD, author="Bohr")] == [a]


@dataclass(frozen=True)
class _FailingEscalated(TierCGate):
    def escalated(self, **_: Any) -> None:
        raise OSError("disk full")


@pytest.mark.anyio
async def test_escalated_write_failure_keeps_the_holds(tmp_path: Path) -> None:
    """msg-9779: delivered, but the ESCALATED row did not land → the holds stay."""
    base = _gate(tmp_path)
    a = _hold(base, "A", "m0a")
    gate = _FailingEscalated(log_path=base.log_path, uuid_factory=base.uuid_factory)
    _, _, mcp = await _run(gate, _Decider(_result({a: "supplemented"})), head=HEAD)
    assert _bounces(mcp) == []  # still delivered
    assert _open(gate) == [a]
    assert _stamped(gate) == []


# --------------------------------------------------------------------------- wire / reading


def test_request_without_holds_is_tierc_v5_and_with_holds_is_tierc_v5_hold() -> None:
    from test_decider_tierc_v2 import RULES, _state

    from spirrow_mindwire.decider.wire import POLICY_LIVE_TIERC, build_decide_request

    plain = build_decide_request(_state(), policy=POLICY_LIVE_TIERC, rules=RULES)
    assert plain["questions_version"] == TIERC_ESCALATION_QUESTIONS_VERSION
    held_state = _state()
    from dataclasses import replace

    held_state = replace(held_state, security_holds=(HoldRef("h1", "m1", "SECURITY-BASIS: x"),))
    held = build_decide_request(held_state, policy=POLICY_LIVE_TIERC, rules=RULES)
    assert held["questions_version"] == TIERC_HOLD_QUESTIONS_VERSION
    assert held["state"] == plain["state"]  # the holds never ride in the state
    extra = set(held["questions"]) - set(plain["questions"])
    assert extra == {hold_relation_key("h1"), UNHELD_UNBASED_KEY}
    q = held["questions"][hold_relation_key("h1")]
    assert q["type"] == "choice"
    assert set(q["criteria"]) == {"supplemented", "unsupplemented", "unrelated"}
    assert "m1" in q["criteria"]["supplemented"]


def test_reading_rejects_unknown_choices_as_no_verdict() -> None:
    from spirrow_mindwire.tier_c_security_holds import SecurityHold

    hold = SecurityHold("h", "T", "Bohr", "m", None, "structural", None)
    answers = read_hold_answers(
        [hold], {hold_relation_key("h"): {"choice": "maybe"}, UNHELD_UNBASED_KEY: {"choice": "no"}}
    )
    assert not answers.complete and answers.relations == {}
