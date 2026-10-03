"""``[decider.tierc] mode = "bounce"`` — the Tier-C Decider (Jev) acting on label-admitted turns.

Takahito 2026-10-03: put the Jev Tier-C judgement into operation. Under ``[tierc_gate] mode =
"enforce"`` a role-authored ``NEXT: human`` that the label gate *admitted* is bounced once more,
back to its author, when the Decider's actionable tierc-v2 verdict is ``LIKELY_NOT``. A ``RETRY:``
of that bounce always reaches the human; any other verdict, a missing result, shadow mode or a
missing gate leaves the turn exactly as before.

Scenario shape follows ``test_tierc_gate_enforce``: Bohr design → Einstein critique (attested) →
Bohr's ``NEXT: human`` head, which ``_route`` stops at the human.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import _ROSTER as ROSTER
from test_conductor_core import _attested, _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire.conductor.core import CONDUCTOR_RELAY_AUTHOR, Conductor, StopReason
from spirrow_mindwire.conductor.handoff import parse_next_token
from spirrow_mindwire.conductor.tierc_gate import (
    TierCGate,
    bounced_msg_id,
    is_bounce_notice,
    render_jev_bounce_body,
)
from spirrow_mindwire.decider.result import DecisionOutcome, DecisionResult
from spirrow_mindwire.decider.state import DecisionState
from spirrow_mindwire.decider.verdict import TierCV2Verdict, TierCVerdictKind
from spirrow_mindwire.tier_c_admission_gate import BounceReason, LogKind
from spirrow_mindwire.tier_c_decisions_log import build_retry_lookup
from spirrow_mindwire.value_objects import Role

LABELLED = "revised\n\nTIER-C: goal\nNEXT: human"
MERGE = "revised\n\nTIER-C: merge-protected\nNEXT: human"
UNSURE = "revised\n\nTIER-C: unsure:goal?\nNEXT: human"


class _Uuids:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"u-{self.n}"


def _gate(tmp_path: Path) -> TierCGate:
    return TierCGate(
        log_path=tmp_path / "state" / "tier_c_decisions_log.jsonl", uuid_factory=_Uuids()
    )


def _rows(gate: TierCGate) -> list[dict[str, Any]]:
    if not gate.log_path.exists():
        return []
    return [json.loads(line) for line in gate.log_path.read_text(encoding="utf-8").splitlines()]


def _result(kind: TierCVerdictKind, score: float) -> DecisionResult:
    return DecisionResult(
        outcome=DecisionOutcome.EVALUATED,
        decision_id="d",
        provider="jev",
        raw_answers={},
        verdict=TierCV2Verdict(kind=kind, ask_score=score),
        policy="p",
    )


LIKELY_NOT = _result(TierCVerdictKind.LIKELY_NOT, 0.2)


class _Decider:
    def __init__(self, result: DecisionResult | None, mode: str = "bounce") -> None:
        self.tierc_mode = mode
        self.result = result
        self.calls = 0

    def is_target(self, state: DecisionState) -> bool:
        return state.parsed_next == "human"

    async def evaluate(self, state: DecisionState) -> DecisionResult | None:
        self.calls += 1
        return self.result

    async def clear_proceed(self, state: DecisionState) -> DecisionResult | None:
        return None


async def _run(
    gate: TierCGate | None,
    decider: _Decider | None,
    *,
    head: str = LABELLED,
    proposer_replies: list[str] | None = None,
) -> tuple[Any, _ScriptedDispatcher, _FakeChatroomMcp]:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    mcp.seed(author="Einstein", content=_attested("critique\n\nNEXT: Bohr"))
    mcp.seed(author="Bohr", content=head)
    disp = _ScriptedDispatcher(mcp, {Role.PROPOSER: list(proposer_replies or [])})
    conductor = Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=ROSTER,
        naysayer_identity="Einstein",
        max_rounds=12,
        decider=decider,
        tierc_gate=gate,
    )
    return await conductor.run(), disp, mcp


def _bounces(mcp: _FakeChatroomMcp) -> list[dict[str, Any]]:
    return [
        p
        for p in mcp.posts
        if p["author"] == CONDUCTOR_RELAY_AUTHOR and is_bounce_notice(p["content"])
    ]


@pytest.mark.anyio
async def test_likely_not_bounces_a_label_admitted_human_to_its_author(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    _, disp, mcp = await _run(gate, _Decider(LIKELY_NOT))
    (notice,) = _bounces(mcp)
    body = notice["content"]
    assert parse_next_token(body) == "Bohr"
    # ``admit`` mints a token on every call (unused on an admit), so the Jev bounce gets u-2.
    assert "RETRY: u-2" in body.splitlines()
    assert bounced_msg_id(body) == "m3"
    assert BounceReason.JEV_LIKELY_NOT.value in body
    (row,) = _rows(gate)
    assert row["kind"] == LogKind.BOUNCED.value
    assert row["reason"] == BounceReason.JEV_LIKELY_NOT.value
    assert row["label"] == "goal"
    assert row["retry_uuid"] == "u-2"
    assert row["author"] == "Bohr"
    assert row["msg_id"] == "m3"
    # The author is dispatched on the notice itself.
    assert disp.dispatches[-1][0] is Role.PROPOSER


@pytest.mark.anyio
async def test_retry_of_a_jev_bounce_always_reaches_the_human(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    decider = _Decider(LIKELY_NOT)
    retry = "still needed\n\nRETRY: u-2\nTIER-C: goal\nNEXT: human"
    outcome, _, mcp = await _run(gate, decider, proposer_replies=[retry])
    assert outcome.stop_reason is StopReason.HUMAN
    assert len(_bounces(mcp)) == 1
    assert [r["kind"] for r in _rows(gate)] == [LogKind.BOUNCED.value, LogKind.RETRY_ADMIT.value]
    assert decider.calls == 2  # Jev still judged the retry; it just may not bounce it


@pytest.mark.anyio
@pytest.mark.parametrize(
    "result",
    [
        _result(TierCVerdictKind.UNSURE, 0.5),
        _result(TierCVerdictKind.CONFIRMED, 0.9),
        DecisionResult(
            outcome=DecisionOutcome.NO_VERDICT_NULL,
            decision_id="d-null",
            provider="null",
            raw_answers={},
            verdict=None,
            policy="p",
        ),
        None,
    ],
)
async def test_anything_but_likely_not_reaches_the_human(
    tmp_path: Path, result: DecisionResult | None
) -> None:
    gate = _gate(tmp_path)
    outcome, _, mcp = await _run(gate, _Decider(result))
    assert outcome.stop_reason is StopReason.HUMAN
    assert _bounces(mcp) == []
    assert _rows(gate) == []


@pytest.mark.anyio
async def test_shadow_mode_never_acts(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    outcome, _, mcp = await _run(gate, _Decider(LIKELY_NOT, mode="shadow"))
    assert outcome.stop_reason is StopReason.HUMAN
    assert _bounces(mcp) == []
    assert _rows(gate) == []


@pytest.mark.anyio
async def test_bounce_mode_without_the_enforced_gate_does_not_act(tmp_path: Path) -> None:
    outcome, _, mcp = await _run(None, _Decider(LIKELY_NOT))
    assert outcome.stop_reason is StopReason.HUMAN
    assert _bounces(mcp) == []


@pytest.mark.anyio
async def test_unsure_label_is_not_bounced_by_jev(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    outcome, _, mcp = await _run(gate, _Decider(LIKELY_NOT), head=UNSURE)
    assert outcome.stop_reason is StopReason.HUMAN
    assert _bounces(mcp) == []
    assert [r["kind"] for r in _rows(gate)] == [LogKind.ADMIT_UNSURE.value]


@pytest.mark.anyio
async def test_jev_bounce_write_failure_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = _gate(tmp_path)

    def boom(*args: Any, **kwargs: Any) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(TierCGate, "jev_bounce", boom)
    outcome, _, mcp = await _run(gate, _Decider(LIKELY_NOT))
    assert outcome.stop_reason is StopReason.HUMAN
    assert _bounces(mcp) == []


def test_jev_bounce_row_is_redeemable_by_retry(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    from datetime import UTC, datetime

    uuid_ = gate.jev_bounce(
        author="Bohr",
        label="cost",
        ask_score=0.1,
        thread="T",
        msg_id="m9",
        now=datetime(2026, 10, 3, tzinfo=UTC),
    )
    lookup = build_retry_lookup(gate.log_path)
    assert lookup(uuid_, "Bohr") is True
    assert lookup(uuid_, "Heisenberg") is False


def test_jev_notice_parse_is_hijack_safe() -> None:
    body = render_jev_bounce_body(
        author="Heisenberg", bounced_msg_id="m7", retry_uuid="u-9", label="cost", ask_score=0.12
    )
    assert is_bounce_notice(body)
    assert bounced_msg_id(body) == "m7"
    assert parse_next_token(body) == "Heisenberg"
    line_start_next = [ln for ln in body.splitlines() if ln.startswith("NEXT:")]
    assert line_start_next == ["NEXT: Heisenberg"]
    assert not any(ln.startswith("TIER-C:") for ln in body.splitlines())


@pytest.mark.anyio
async def test_merge_protected_is_never_bounced_by_jev(tmp_path: Path) -> None:
    """None of the tierc-v2 rules names a protected-branch merge, so Jev scores it low; the
    label is excluded from the Jev bounce (``JEV_BOUNCEABLE_LABELS``)."""
    gate = _gate(tmp_path)
    outcome, _, mcp = await _run(gate, _Decider(LIKELY_NOT), head=MERGE)
    assert outcome.stop_reason is StopReason.HUMAN
    assert _bounces(mcp) == []
    assert _rows(gate) == []
