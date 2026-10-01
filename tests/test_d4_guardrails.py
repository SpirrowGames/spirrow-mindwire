"""D-4' guardrails on carve-out ③ — predicate, parsers and the Decider's proceed veto.

Spec (thread T-pr-2b-3-human-identity-delegate): Bohr msg-4856 §3 (G1 Tier-C-declared segment
closes ③, G2 ``TIER-C-CHECK: none`` required on the naysayer's proceed, G3 Decider — originally a
clearance, since Takahito's msg-5219 "a" decide a VETO: Bohr msg-5227 R1-R4 / msg-5229 R3',
endorsed by Einstein msg-5228 / msg-5230),
msg-4858 §2 (G1 segment resets ONLY on a human-authored message, latches, no merge reset;
§4 the four pinned scenarios — those drive the conductor and live in ``test_conductor_core.py``),
endorsed by the naysayer in msg-4857 / msg-4859; Takahito's "B" decide selects G1+G2+G3 and
indefinite (latched) delegation (G4 (i)).

Everything here is fail-closed by construction: each guard can only keep the door to code
closed, never open a route the pre-D-4' guard would have closed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from spirrow_mindwire.adapters.decider_lexora import DeciderLexoraAdapter, decide_once
from spirrow_mindwire.conductor.handoff import (
    _ROLE_HANDOFF_GUIDANCE,
    TIER_C_CHECK_KEYWORD,
    TIER_C_CHECK_NONE,
    build_handoff_protocol_block,
    declares_no_tier_c,
    declares_tier_c,
)
from spirrow_mindwire.decider.hook import ThreadMessage, proceed_vetoed, run_proceed_veto
from spirrow_mindwire.decider.questions import (
    SHOULD_ASK_HUMAN_KEY,
    TIERC_V2_PROCEED_QUESTIONS_VERSION,
    load_tierc_rules,
    tierc_rules_template_path,
    tierc_v2_questions,
)
from spirrow_mindwire.decider.result import DecisionOutcome, DecisionResult
from spirrow_mindwire.decider.state import DecisionState, SimpleTurn, state_builder
from spirrow_mindwire.decider.verdict import (
    TierCScope,
    TierCV2Verdict,
    TierCVerdict,
    TierCVerdictKind,
)
from spirrow_mindwire.decider.wire import POLICY_LIVE_PROCEED, build_decide_request
from spirrow_mindwire.lexora.client import LexoraTimeoutError
from spirrow_mindwire.routing import (
    GuardIVerdict,
    carve_out_iii_admissible,
    guard_proposer_to_implementer,
)
from spirrow_mindwire.value_objects import Role

RULES = load_tierc_rules(tierc_rules_template_path())
ROSTER = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER, "Heisenberg": Role.IMPLEMENTER}


class _Thunk:
    def __init__(self, value: bool) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.value


def _guard(
    *,
    human: bool = False,
    naysayer: bool = True,
    run: bool = True,
    attested: bool = True,
    declared: bool = False,
    checked: bool = True,
    vetoed: bool = False,
) -> tuple[GuardIVerdict, dict[str, _Thunk]]:
    thunks = {
        "attest": _Thunk(attested),
        "declared": _Thunk(declared),
        "checked": _Thunk(checked),
        "vetoed": _Thunk(vetoed),
    }
    verdict = guard_proposer_to_implementer(
        author_is_human=human,
        author_is_naysayer=naysayer,
        control_state_is_run=run,
        message_is_attested=thunks["attest"],
        segment_declares_tier_c=thunks["declared"],
        naysayer_declared_no_tier_c=thunks["checked"],
        decider_vetoes=thunks["vetoed"],
    )
    return verdict, thunks


# --------------------------------------------------------------------------- #
# Predicate
# --------------------------------------------------------------------------- #


def test_all_guards_open_honours() -> None:
    verdict, thunks = _guard()
    assert verdict is GuardIVerdict.HONOR
    assert all(t.calls == 1 for t in thunks.values())


@pytest.mark.parametrize(
    "closing",
    [
        {"declared": True},  # G1
        {"checked": False},  # G2
        {"vetoed": True},  # G3 (veto)
        {"attested": False},
        {"run": False},
        {"naysayer": False},
    ],
)
def test_any_single_closed_guard_redirects(closing: dict[str, bool]) -> None:
    verdict, _ = _guard(**closing)
    assert verdict is GuardIVerdict.REDIRECT


def test_human_author_honours_even_with_a_declared_segment_and_no_decider() -> None:
    # carve-out ① is the Tier-C gate itself; none of the D-4' thunks is read.
    verdict, thunks = _guard(human=True, declared=True, checked=False, vetoed=True)
    assert verdict is GuardIVerdict.HONOR
    assert all(t.calls == 0 for t in thunks.values())


def test_decider_is_consulted_last_and_only_when_everything_else_holds() -> None:
    # The Decider is the one network-bound thunk: a closed G1 / G2 / attest must spare the call.
    for closing in ({"declared": True}, {"checked": False}, {"attested": False}, {"run": False}):
        _, thunks = _guard(**closing)
        assert thunks["vetoed"].calls == 0, closing


def test_segment_scan_is_not_read_for_an_unattested_post() -> None:
    _, thunks = _guard(attested=False)
    assert thunks["declared"].calls == 0 and thunks["checked"].calls == 0


def test_admissible_is_the_guard_minus_the_decider() -> None:
    assert carve_out_iii_admissible(
        author_is_naysayer=True,
        control_state_is_run=True,
        message_is_attested=lambda: True,
        segment_declares_tier_c=lambda: False,
        naysayer_declared_no_tier_c=lambda: True,
    )
    assert not carve_out_iii_admissible(
        author_is_naysayer=True,
        control_state_is_run=True,
        message_is_attested=lambda: True,
        segment_declares_tier_c=lambda: True,
        naysayer_declared_no_tier_c=lambda: True,
    )


# --------------------------------------------------------------------------- #
# G1 parser — any line, any label, line-anchored
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        "design\nTIER-C: scope\nNEXT: Heisenberg",
        "TIER-C: cost",
        "a\n  TIER-C:goal  \nb",
        "x\nTIER-C: other: needs a new vendor\nNEXT: Einstein",
        "x\ntier-c: irreversible\r\nNEXT: Einstein",
        "TIER-C: unsure:goal?",
    ],
)
def test_declares_tier_c_on_any_line(body: str) -> None:
    assert declares_tier_c(body)


@pytest.mark.parametrize(
    "body",
    [
        "the proposer wrote TIER-C: scope in round 1",  # prose mention, not a line
        "TIER-C: banana",  # not a label
        "TIER-C:",  # no label
        "TIER-C-CHECK: none\nNEXT: Heisenberg",  # G2's line is not a declaration
        "nothing here\nNEXT: Heisenberg",
    ],
)
def test_declares_tier_c_ignores_non_declarations(body: str) -> None:
    assert not declares_tier_c(body)


# --------------------------------------------------------------------------- #
# G2 parser — exactly the line above the final NEXT:
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        "sound\n\nTIER-C-CHECK: none\nNEXT: Heisenberg",
        "sound\n\ntier-c-check:NONE\nNEXT: Heisenberg",
        "sound\n\n  TIER-C-CHECK: none  \nNEXT: Heisenberg\n\n<!-- attest: x -->",
        "sound\r\nTIER-C-CHECK: none\r\nNEXT: Heisenberg",
    ],
)
def test_declares_no_tier_c_accepts_the_exact_line(body: str) -> None:
    assert declares_no_tier_c(body)


@pytest.mark.parametrize(
    "body",
    [
        "sound\n\nNEXT: Heisenberg",  # missing
        "TIER-C-CHECK: none\n\nNEXT: Heisenberg",  # blank line in between: not directly above
        "sound\nTIER-C-CHECK: scope\nNEXT: Heisenberg",  # a label, not none
        "sound\nTIER-C-CHECK: none, I think\nNEXT: Heisenberg",
        "sound\n**TIER-C-CHECK: none**\nNEXT: Heisenberg",  # decorated
        "quoted: TIER-C-CHECK: none\nNEXT: Heisenberg",
        "TIER-C-CHECK: none\nNEXT: Heisenberg\nlater\nNEXT: Bohr",  # not above the FINAL NEXT
        "TIER-C-CHECK: none",  # no NEXT at all
    ],
)
def test_declares_no_tier_c_rejects_everything_else(body: str) -> None:
    assert not declares_no_tier_c(body)


def test_naysayer_guidance_teaches_the_check_line() -> None:
    block = build_handoff_protocol_block(Role.NAYSAYER)
    assert f"`{TIER_C_CHECK_KEYWORD}: {TIER_C_CHECK_NONE}`" in block
    # The proposer's OWN guidance never teaches the check line (it is the naysayer's proceed
    # declaration). The shared protocol does carry it since T-next-role-name-stands-down-to-human
    # D4''' (msg-5426): it is the middle line of the ``NEXT: operator`` form every role may use,
    # and it has no effect on guard (i) there (carve-out ③ reads it only from a naysayer).
    assert f"{TIER_C_CHECK_KEYWORD}" not in _ROLE_HANDOFF_GUIDANCE[Role.PROPOSER]


# --------------------------------------------------------------------------- #
# G3 — the Decider's proceed veto (msg-5219)
# --------------------------------------------------------------------------- #


def _result(
    kind: TierCVerdictKind | None, outcome: DecisionOutcome, p: float = 0.5
) -> DecisionResult:
    return DecisionResult(
        outcome=outcome,
        decision_id=None if outcome is DecisionOutcome.TRANSPORT_ERROR else "d-1",
        provider=None if outcome is DecisionOutcome.TRANSPORT_ERROR else "jev",
        raw_answers=None,
        verdict=TierCV2Verdict(kind=kind, ask_score=p) if kind is not None else None,
        policy=POLICY_LIVE_PROCEED,
    )


def test_proceed_vetoed_only_on_confirmed() -> None:
    # msg-5227 R2: only an actionable tierc-v2 CONFIRMED vetoes.
    assert proceed_vetoed(_result(TierCVerdictKind.CONFIRMED, DecisionOutcome.EVALUATED))
    assert not proceed_vetoed(_result(TierCVerdictKind.UNSURE, DecisionOutcome.EVALUATED))
    assert not proceed_vetoed(_result(TierCVerdictKind.LIKELY_NOT, DecisionOutcome.EVALUATED))
    assert not proceed_vetoed(_result(None, DecisionOutcome.NO_VERDICT_NULL))
    assert not proceed_vetoed(_result(None, DecisionOutcome.NO_VERDICT_MALFORMED))
    assert not proceed_vetoed(_result(None, DecisionOutcome.TRANSPORT_ERROR))
    assert not proceed_vetoed(None)


def test_proceed_veto_ignores_a_v1_verdict() -> None:
    # msg-5227 R2: a v1 configuration never vetoes, even with a v1 CONFIRMED in-gate verdict.
    v1 = TierCVerdict(
        kind=TierCVerdictKind.CONFIRMED,
        genuine_score=0.9,
        spurious_score=0.1,
        fired_reason=None,
        scope=TierCScope.IN_GATE,
    )
    dr = DecisionResult(
        outcome=DecisionOutcome.EVALUATED,
        decision_id="d-1",
        provider="jev",
        raw_answers=None,
        verdict=v1,
        policy=POLICY_LIVE_PROCEED,
    )
    assert dr.actionable_verdict is not None
    assert not proceed_vetoed(dr)


class _FakeClient:
    def __init__(self, payload: dict[str, Any] | None = None, exc: Exception | None = None) -> None:
        self.payload = payload
        self.exc = exc
        self.bodies: list[dict[str, Any]] = []

    async def decide(self, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        if self.exc is not None:
            raise self.exc
        assert self.payload is not None
        return self.payload

    async def aclose(self) -> None:
        return None


def _payload(p: float) -> dict[str, Any]:
    return {
        "answers": {
            SHOULD_ASK_HUMAN_KEY: {"noul": p},
            "matched_rule": {"choice": "none", "probabilities": {}, "confidence": 0.9},
        },
        "provider": "jev",
        "decision_id": "d-7",
        "latency_ms": 3,
    }


def _proceed_msgs() -> list[ThreadMessage]:
    return [
        ThreadMessage("m1", "Bohr", "design\n\nNEXT: Einstein", "Einstein"),
        ThreadMessage(
            "m2", "Einstein", "sound\n\nTIER-C-CHECK: none\nNEXT: Heisenberg", "Heisenberg"
        ),
    ]


def _proceed_state() -> DecisionState:
    return state_builder(
        SimpleTurn(
            thread_id="T",
            round_index=0,
            roster=ROSTER,
            head_summary="sound",
            recent_events=(),
            parsed_next="heisenberg",
            prev_next="Einstein",
        )
    )


def test_proceed_request_uses_its_own_question_frame_and_version() -> None:
    body = build_decide_request(
        _proceed_state(), policy=POLICY_LIVE_PROCEED, rules=RULES, proceed=True
    )
    assert body["questions_version"] == TIERC_V2_PROCEED_QUESTIONS_VERSION
    assert body["policy"] == POLICY_LIVE_PROCEED
    proceed_q = body["questions"][SHOULD_ASK_HUMAN_KEY]["instructions"]
    escalation_q = tierc_v2_questions(RULES)[SHOULD_ASK_HUMAN_KEY]["instructions"]
    assert proceed_q != escalation_q
    assert "NEXT: implementer" in proceed_q and "NEXT: human" not in proceed_q
    # same five rules in both frames
    assert all(r.text in proceed_q for r in RULES.rules)


def test_proceed_request_refuses_v1() -> None:
    with pytest.raises(ValueError, match="tierc-v2"):
        build_decide_request(_proceed_state(), policy=POLICY_LIVE_PROCEED, rules=None, proceed=True)


@pytest.mark.anyio
async def test_decide_once_proceed_records_the_proceed_version() -> None:
    dr = await decide_once(
        _proceed_state(),
        client=_FakeClient(_payload(0.1)),
        policy=POLICY_LIVE_PROCEED,
        rules=RULES,
        proceed=True,
    )
    assert dr.outcome is DecisionOutcome.EVALUATED
    assert dr.questions_version == TIERC_V2_PROCEED_QUESTIONS_VERSION
    assert dr.actionable_verdict is not None
    assert dr.actionable_verdict.kind is TierCVerdictKind.LIKELY_NOT


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("p", "vetoed"),
    [
        (0.1, False),  # LIKELY_NOT
        (0.5, False),  # UNSURE (grey zone) — msg-5219: G3 does not close
        (0.5999, False),  # boundary, msg-5227 §3 test 5
        (0.60, True),  # tierc_v2_ask_min — CONFIRMED vetoes
        (0.9, True),
    ],
)
async def test_adapter_proceed_veto_end_to_end(
    p: float, vetoed: bool, caplog: pytest.LogCaptureFixture
) -> None:
    client = _FakeClient(_payload(p))
    adapter = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: client, rules=RULES)
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        got = await run_proceed_veto(
            adapter, thread_id="T", round_index=1, roster=ROSTER, messages=_proceed_msgs()
        )
    assert got == (vetoed, p)
    assert client.bodies[0]["questions_version"] == TIERC_V2_PROCEED_QUESTIONS_VERSION
    sent_state = json.loads(client.bodies[0]["state"])
    assert sent_state["parsed_next"] == "heisenberg"
    (line,) = [
        json.loads(r.getMessage().split(" ", 1)[1])
        for r in caplog.records
        if r.getMessage().startswith("decider_proceed_clearance ")
    ]
    assert line["vetoed"] is vetoed and line["head_msg_id"] == "m2"
    assert line["ask_score"] == p
    # never mixed into the §6 escalation tally
    assert not any(r.getMessage().startswith("decider_decision ") for r in caplog.records)


@pytest.mark.anyio
async def test_no_verdict_does_not_veto() -> None:
    # msg-5219: off / no verdict / error / v1 / null → G3 does not close (G1 / G2 decide).
    kw: dict[str, Any] = {
        "thread_id": "T",
        "round_index": 1,
        "roster": ROSTER,
        "messages": _proceed_msgs(),
    }
    # backend=off: no Decider at all
    assert await run_proceed_veto(None, **kw) == (False, None)
    # mode off / v1 rules: the adapter declines to call
    off = DeciderLexoraAdapter(
        tierc_mode="off", client_factory=lambda: _FakeClient(_payload(0.0)), rules=RULES
    )
    assert await run_proceed_veto(off, **kw) == (False, None)
    v1 = DeciderLexoraAdapter(
        tierc_mode="shadow", client_factory=lambda: _FakeClient(_payload(0.0))
    )
    assert await run_proceed_veto(v1, **kw) == (False, None)
    # transport failure
    broken = DeciderLexoraAdapter(
        tierc_mode="shadow",
        client_factory=lambda: _FakeClient(exc=LexoraTimeoutError("slow")),
        rules=RULES,
    )
    assert await run_proceed_veto(broken, **kw) == (False, None)
    # null provider
    null = _payload(0.0)
    null["provider"] = "null"
    nullp = DeciderLexoraAdapter(
        tierc_mode="shadow", client_factory=lambda: _FakeClient(null), rules=RULES
    )
    assert await run_proceed_veto(nullp, **kw) == (False, None)


@pytest.mark.anyio
async def test_veto_swallows_a_raising_decider() -> None:
    class _Raises:
        tierc_mode = "shadow"

        def is_target(self, state: DecisionState) -> bool:
            return False

        async def evaluate(self, state: DecisionState) -> DecisionResult | None:
            return None

        async def clear_proceed(self, state: DecisionState) -> DecisionResult | None:
            raise RuntimeError("boom")

    got = await run_proceed_veto(
        _Raises(), thread_id="T", round_index=1, roster=ROSTER, messages=_proceed_msgs()
    )
    assert got == (False, None)
