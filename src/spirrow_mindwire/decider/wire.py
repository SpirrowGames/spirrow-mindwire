"""Decider wire format — what goes to ``POST /v1/decide`` (Bohr msg-4180 §2-1).

**One builder for live and replay.** ``DecideRequest.state`` on the Lexora side is a ``str``, so
the :class:`~spirrow_mindwire.decider.state.DecisionState` has to be serialised somewhere. Before
step 2 that serialisation lived only in ``scripts/decider_replay.py`` (the private
``_state_to_json``). It is moved here, and **both** the live adapter
(:mod:`spirrow_mindwire.adapters.decider_lexora`) and the replay script call
:func:`build_decide_request` — so a live turn and the same turn replayed from a fixture send
byte-identical requests (the §1 "same builder" rule carried one step further, and what makes the
§6 replay-vs-live comparison meaningful).

The serialisation is ``json.dumps(..., sort_keys=True, ensure_ascii=False,
separators=(",", ":"))``: key order and whitespace are fixed, so the bytes do not depend on dict
insertion order.

**``gate_result`` is on the wire.** The design (v3.4 §3.2 / §6.4: "state builder は
``DecisionState.gate_result`` を feature として持つ") treats the admission-gate result as a
Decider feature, and msg-4180 §2-1 moves the replay serialisation — which already carried it —
here unchanged. The step-1 note in :mod:`.state` that anticipated stripping it in the adapter is
superseded by that decision; if the admission label is later judged to anchor the provider, the
change is one line here and applies to live and replay together.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from spirrow_mindwire.decider.questions import (
    TIERC_ESCALATION_QUESTIONS_VERSION,
    TIERC_QUESTIONS_V1,
    TIERC_QUESTIONS_VERSION,
    TIERC_V2_PROCEED_QUESTIONS_VERSION,
    TierCRules,
    tierc_v2_questions,
)
from spirrow_mindwire.decider.state import AdmissionGateResult, DecisionState

POLICY_LIVE_TIERC = "mindwire.conductor.tierc"
"""``policy`` tag for the live Conductor hook (msg-4180 §2-5)."""

POLICY_REPLAY_TIERC = "mindwire.replay.tierc"
"""``policy`` tag for ``scripts/decider_replay.py`` (msg-4180 §2-5) — keeps replay traffic
separable from live traffic in Lexora's decision log."""

POLICY_LIVE_PROCEED = "mindwire.conductor.proceed"
"""D-4' G3: the policy tag on a naysayer proceed-clearance call, kept apart from
``POLICY_LIVE_TIERC`` so the §6 tally of ``NEXT: human`` escalations is not mixed with it."""


def gate_result_to_dict(gate: AdmissionGateResult | None) -> dict[str, Any] | None:
    """``AdmissionGateResult`` → plain dict (``is_grey_zone`` included, as the replay did)."""
    if gate is None:
        return None
    return {
        "verdict": gate.verdict.value,
        "kind": gate.kind.value if gate.kind is not None else None,
        "label": gate.label,
        "retry_admit_reason": (gate.retry_admit_reason.value if gate.retry_admit_reason else None),
        "bounce_reason": gate.bounce_reason.value if gate.bounce_reason else None,
        "is_grey_zone": gate.is_grey_zone,
    }


def state_to_dict(state: DecisionState) -> dict[str, Any]:
    """``DecisionState`` → plain dict. Moved verbatim from the replay's ``_state_to_json``.

    ``dispute_rounds`` (v2 rule_5 feature, msg-4380 Δ5) is emitted **only when computed**: a
    state without it (every pre-v2 replay fixture) serialises to the same bytes as before, so
    hash-locked v1 replays stay byte-identical.
    """
    d: dict[str, Any] = {
        "thread_id": state.thread_id,
        "round_index": state.round_index,
        "roster": {k: v.value for k, v in state.roster.items()},
        "head_summary": state.head_summary,
        "recent_events": [asdict(e) for e in state.recent_events],
        "parsed_next": state.parsed_next,
        "prev_next": state.prev_next,
        "diff_stat": asdict(state.diff_stat) if state.diff_stat else None,
        "gate_result": gate_result_to_dict(state.gate_result),
    }
    if state.dispute_rounds is not None:
        d["dispute_rounds"] = state.dispute_rounds
    return d


def state_to_wire(state: DecisionState) -> str:
    """``DecisionState`` → the ``state`` string Lexora's ``DecideRequest`` takes (§2-1)."""
    return json.dumps(
        state_to_dict(state), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def questions_to_wire() -> dict[str, dict[str, Any]]:
    """``TIERC_QUESTIONS_V1`` → ``{key: {type, instructions, criteria}}`` (§2-1).

    Note the shape: a dict keyed by question name, **not** the list the step-1 dry-run record
    emits. Insertion order follows ``TIERC_QUESTIONS_V1`` (declaration order).
    """
    return {
        q.key: {"type": "noul", "instructions": q.prompt, "criteria": None}
        for q in TIERC_QUESTIONS_V1
    }


def build_decide_request(
    state: DecisionState, *, policy: str, rules: TierCRules | None = None, proceed: bool = False
) -> dict[str, Any]:
    """The full ``/v1/decide`` request body — the one builder live and replay share.

    ``rules=None`` → the v1 set (bytes unchanged from before v2). ``rules`` given → the v2 set
    (``should_ask_human`` + ``matched_rule``, built from the rules file) with
    ``questions_version="tierc-v2"``. ``proceed=True`` (D-4' G3) asks the same keys of a
    naysayer's proceed handoff (``questions_version="tierc-v2-proceed"``); it requires ``rules``
    — v1 has no proceed variant.
    """
    if proceed:
        if rules is None:
            raise ValueError("a proceed-clearance request requires the tierc-v2 rules")
        return {
            "state": state_to_wire(state),
            "questions": tierc_v2_questions(rules, proceed=True),
            "policy": policy,
            "questions_version": TIERC_V2_PROCEED_QUESTIONS_VERSION,
        }
    if rules is None:
        return {
            "state": state_to_wire(state),
            "questions": questions_to_wire(),
            "policy": policy,
            "questions_version": TIERC_QUESTIONS_VERSION,
        }
    return {
        "state": state_to_wire(state),
        "questions": tierc_v2_questions(rules),
        "policy": policy,
        "questions_version": TIERC_ESCALATION_QUESTIONS_VERSION,
    }


__all__ = [
    "POLICY_LIVE_PROCEED",
    "POLICY_LIVE_TIERC",
    "POLICY_REPLAY_TIERC",
    "build_decide_request",
    "gate_result_to_dict",
    "questions_to_wire",
    "state_to_dict",
    "state_to_wire",
]
