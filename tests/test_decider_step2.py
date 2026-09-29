"""T-decider-conductor-hook step 2 — adapter, result, wire, hook, Conductor wiring, replay.

Spec: Bohr msg-4180 (wire / Null rule / extraction / transport / policy / replay --endpoint),
msg-4182 (``DecisionResult``), msg-4184 (``actionable_verdict`` + invariants), msg-4186 (gate =
``is_grey_zone``; 4-valued outcome), msg-4188 (always call ``/v1/decide``, then branch),
msg-4196 (DECIDED 1: ``gate_result is None`` → no call; DECIDED 2: gate columns + not-called line),
msg-4200 / msg-4203 (step 2b: compute-only admission gate, proposer-only entry; tests 1-10).

T-decider-tierc-v2-all-escalations (msg-4360 / 4361 / 4380-4384) changed three things pinned here:
the entry roles are proposer / implementer / naysayer (tests 6-7 now enter), the msg-4196
DECIDED 2 empty-``outcome`` line is gone, and ``build_decider`` defaults to tierc-v2 (a rules
file). The v2 behaviour itself is tested in ``test_decider_tierc_v2.py``.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_conductor_core import _ROSTER as ROSTER
from test_conductor_core import _attested, _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire import decider as decider_facade
from spirrow_mindwire import tier_c_admission_gate
from spirrow_mindwire.adapters import decider_lexora
from spirrow_mindwire.adapters.decider_lexora import (
    DECIDER_TIMEOUT_SECONDS,
    DeciderLexoraAdapter,
    build_decider,
    decide_once,
)
from spirrow_mindwire.conductor.core import Conductor, StopReason
from spirrow_mindwire.decider import hook as hook_mod
from spirrow_mindwire.decider.hook import (
    ThreadMessage,
    compute_gate_result,
    is_tierc_entry,
    log_decision,
    never_retry,
    run_tierc_hook,
    turn_from_messages,
)
from spirrow_mindwire.decider.questions import tierc_rules_template_path
from spirrow_mindwire.decider.result import DecisionOutcome, DecisionResult
from spirrow_mindwire.decider.state import (
    AdmissionGateResult,
    DecisionState,
    EventSummary,
    SimpleTurn,
    state_builder,
)
from spirrow_mindwire.decider.verdict import (
    TIER_C_GENUINE_KEYS,
    TIER_C_SPURIOUS_KEYS,
    TierCScope,
    TierCVerdict,
    TierCVerdictKind,
    evaluate_tierc,
)
from spirrow_mindwire.decider.wire import (
    POLICY_LIVE_TIERC,
    POLICY_REPLAY_TIERC,
    build_decide_request,
    questions_to_wire,
    state_to_dict,
    state_to_wire,
)
from spirrow_mindwire.lexora.client import LexoraClient, LexoraHTTPError, LexoraTimeoutError
from spirrow_mindwire.tier_c_admission_gate import AdmissionVerdict, LogKind, RetryAdmitReason
from spirrow_mindwire.value_objects import Role

ROOT = Path(__file__).resolve().parent.parent
ALL_KEYS = TIER_C_GENUINE_KEYS + TIER_C_SPURIOUS_KEYS

GREY = AdmissionGateResult(
    verdict=AdmissionVerdict.ADMIT, kind=LogKind.ADMIT_UNSURE, label="unsure:goal?"
)
ADMIT_LABELLED = AdmissionGateResult(verdict=AdmissionVerdict.ADMIT, kind=None, label="goal")
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _state(
    gate: AdmissionGateResult | None = GREY, parsed_next: str | None = "human"
) -> DecisionState:
    return state_builder(
        SimpleTurn(
            thread_id="T-x",
            round_index=3,
            roster={"Bohr": Role.PROPOSER},
            head_summary="日本語 head",
            recent_events=(EventSummary("m1", "Bohr", parsed_next, "body"),),
            parsed_next=parsed_next,
            prev_next="Einstein",
            diff_stat=None,
            gate_result=gate,
        )
    )


def _answers(**over: float) -> dict[str, dict[str, float]]:
    vals = dict.fromkeys(ALL_KEYS, 0.0)
    vals.update(over)
    return {k: {"noul": v} for k, v in vals.items()}


def _payload(
    provider: str = "jev", answers: Any = None, decision_id: Any = "d-1"
) -> dict[str, Any]:
    return {
        "answers": _answers(answerable_from_thread=0.9) if answers is None else answers,
        "provider": provider,
        "decision_id": decision_id,
        "latency_ms": 42,
    }


class FakeClient:
    def __init__(self, payload: dict[str, Any] | None = None, exc: Exception | None = None) -> None:
        self.payload = payload
        self.exc = exc
        self.bodies: list[dict[str, Any]] = []
        self.closed = 0

    async def decide(self, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        if self.exc is not None:
            raise self.exc
        assert self.payload is not None
        return self.payload

    async def aclose(self) -> None:
        self.closed += 1


# --------------------------------------------------------------------------- wire (§2-1)


def test_wire_state_is_canonical_json_string() -> None:
    s = _state()
    wire = state_to_wire(s)
    assert isinstance(wire, str)
    assert wire == json.dumps(
        state_to_dict(s), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    assert "日本語" in wire  # ensure_ascii=False
    assert json.loads(wire)["gate_result"]["is_grey_zone"] is True


def test_questions_to_wire_is_named_dict_of_noul() -> None:
    q = questions_to_wire()
    assert list(q) == list(ALL_KEYS)
    for v in q.values():
        assert v["type"] == "noul"
        assert v["criteria"] is None
        assert isinstance(v["instructions"], str) and v["instructions"]


def test_request_body_shape_and_policy_tags() -> None:
    body = build_decide_request(_state(), policy=POLICY_LIVE_TIERC)
    assert set(body) == {"state", "questions", "policy", "questions_version"}
    assert body["questions_version"] == "tierc-v1"
    assert POLICY_LIVE_TIERC == "mindwire.conductor.tierc"
    assert POLICY_REPLAY_TIERC == "mindwire.replay.tierc"


def _load_replay() -> Any:
    script = ROOT / "scripts" / "decider_replay.py"
    spec = importlib.util.spec_from_file_location("decider_replay_step2", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["decider_replay_step2"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_replay_and_live_send_identical_bytes() -> None:
    """msg-4180 §4: the wire output is the same byte string for replay and live."""
    replay = _load_replay()
    row = {
        "thread_id": "T-x",
        "round_index": 3,
        "roster": {"Bohr": "proposer"},
        "head_summary": "日本語 head",
        "recent_events": [
            {"msg_id": "m1", "author": "Bohr", "parsed_next": "human", "body_head": "body"}
        ],
        "parsed_next": "human",
        "prev_next": "Einstein",
        "diff_stat": None,
        "gate_result": {"verdict": "admit", "kind": "ADMIT_UNSURE", "label": "unsure:goal?"},
    }
    replay_state = state_builder(replay.parse_turn(row))
    assert state_to_wire(replay_state).encode() == state_to_wire(_state()).encode()
    # and the replay record's "state" is exactly the wire dict
    rec = replay.build_tierc_record(replay_state)
    assert json.dumps(rec["state"], sort_keys=True, ensure_ascii=False, separators=(",", ":")) == (
        state_to_wire(replay_state)
    )
    assert not hasattr(replay, "_state_to_json")  # moved, not duplicated


# --------------------------------------------------------------------------- result invariants


def _v(scope: TierCScope) -> TierCVerdict:
    return TierCVerdict(TierCVerdictKind.LIKELY_NOT, 0.0, 0.9, "answerable_from_thread", scope)


def _dr(
    outcome: DecisionOutcome, verdict: TierCVerdict | None, did: str | None = "d"
) -> DecisionResult:
    return DecisionResult(
        outcome=outcome,
        decision_id=did,
        provider="jev",
        raw_answers={},
        verdict=verdict,
        policy="p",
    )


def test_actionable_verdict_in_gate() -> None:
    v = _v(TierCScope.IN_GATE)
    assert _dr(DecisionOutcome.EVALUATED, v).actionable_verdict is v


def test_actionable_verdict_none_for_out_of_gate() -> None:
    """Regression for Einstein msg-4183: an out-of-gate record must never be actionable."""
    assert _dr(DecisionOutcome.EVALUATED, _v(TierCScope.OUT_OF_GATE)).actionable_verdict is None


@pytest.mark.parametrize(
    ("outcome", "verdict", "did"),
    [
        (DecisionOutcome.EVALUATED, None, "d"),
        (DecisionOutcome.NO_VERDICT_NULL, _v(TierCScope.IN_GATE), "d"),
        (DecisionOutcome.NO_VERDICT_MALFORMED, _v(TierCScope.OUT_OF_GATE), "d"),
        (DecisionOutcome.TRANSPORT_ERROR, _v(TierCScope.IN_GATE), None),
        (DecisionOutcome.NO_VERDICT_NULL, None, None),
        (DecisionOutcome.EVALUATED, _v(TierCScope.OUT_OF_GATE), None),
    ],
)
def test_invariant_violations_raise(
    outcome: DecisionOutcome, verdict: TierCVerdict | None, did: str | None
) -> None:
    with pytest.raises(ValueError):
        _dr(outcome, verdict, did)


def test_transport_error_may_lack_decision_id() -> None:
    assert _dr(DecisionOutcome.TRANSPORT_ERROR, None, None).actionable_verdict is None


def test_outcome_has_exactly_four_values() -> None:
    assert {o.value for o in DecisionOutcome} == {
        "evaluated",
        "no_verdict_null",
        "no_verdict_malformed",
        "transport_error",
    }


def test_facade_exports_step2_symbols() -> None:
    for name in ("DecisionResult", "DecisionOutcome", "state_to_wire", "build_decide_request"):
        assert name in decider_facade.__all__
        assert hasattr(decider_facade, name)


# --------------------------------------------------------------------------- decide_once (msg-4188)


@pytest.mark.anyio
async def test_null_provider_is_no_verdict_with_decision_id() -> None:
    c = FakeClient(_payload(provider="null", answers=_answers(**dict.fromkeys(ALL_KEYS, 0.5))))
    dr = await decide_once(_state(), client=c, policy="p")
    assert dr.outcome is DecisionOutcome.NO_VERDICT_NULL
    assert dr.decision_id == "d-1"
    assert dr.verdict is None
    assert dr.raw_answers is not None
    assert len(c.bodies) == 1


@pytest.mark.anyio
async def test_jev_in_grey_zone_synthesises_in_gate_verdict() -> None:
    c = FakeClient(_payload())
    dr = await decide_once(_state(GREY), client=c, policy="p")
    assert dr.outcome is DecisionOutcome.EVALUATED
    assert dr.verdict is not None and dr.verdict.scope is TierCScope.IN_GATE
    assert dr.verdict == evaluate_tierc(
        {k: v["noul"] for k, v in _answers(answerable_from_thread=0.9).items()}
    )
    assert dr.actionable_verdict is dr.verdict
    assert dr.latency_ms == 42 and dr.provider == "jev"


@pytest.mark.anyio
async def test_out_of_grey_zone_still_calls_and_is_out_of_gate() -> None:
    c = FakeClient(_payload())
    dr = await decide_once(_state(ADMIT_LABELLED), client=c, policy="p")
    assert len(c.bodies) == 1  # always called once a gate result exists (msg-4188 step 1)
    assert dr.outcome is DecisionOutcome.EVALUATED
    assert dr.decision_id == "d-1"
    assert dr.verdict is not None and dr.verdict.scope is TierCScope.OUT_OF_GATE
    assert dr.actionable_verdict is None


@pytest.mark.anyio
async def test_gate_none_makes_zero_http_and_evaluate_returns_none() -> None:
    """msg-4196 DECIDED 1 (replaces msg-4188's "None → call → OUT_OF_GATE")."""
    c = FakeClient(_payload())
    shadow = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: c)
    assert await shadow.evaluate(_state(None)) is None
    assert c.bodies == []
    assert c.closed == 0  # no client was even built


@pytest.mark.anyio
async def test_decide_once_refuses_gate_none_before_any_http() -> None:
    """msg-4196 DECIDED 1: the ``None → OUT_OF_GATE`` path is withdrawn, not just unused."""
    c = FakeClient(_payload())
    with pytest.raises(ValueError, match="gate_result"):
        await decide_once(_state(None), client=c, policy="p")
    assert c.bodies == []


@pytest.mark.anyio
async def test_null_out_of_gate_never_builds_out_of_gate_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("build_out_of_gate_verdict must not run on a null provider")

    monkeypatch.setattr(decider_lexora, "build_out_of_gate_verdict", boom)
    dr = await decide_once(_state(ADMIT_LABELLED), client=FakeClient(_payload("null")), policy="p")
    assert dr.outcome is DecisionOutcome.NO_VERDICT_NULL


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answers",
    [
        {k: v for k, v in _answers().items() if k != "irreversible"},  # missing
        {**_answers(), "incurs_cost": {"noul": "0.3"}},  # wrong type
        {**_answers(), "incurs_cost": {"noul": True}},  # bool is not a probability
        {**_answers(), "incurs_cost": {"noul": 1.5}},  # out of range
        {**_answers(), "incurs_cost": 0.3},  # not the {"noul": p} shape
        ["not", "an", "object"],
    ],
)
async def test_malformed_answers(answers: Any) -> None:
    dr = await decide_once(_state(), client=FakeClient(_payload(answers=answers)), policy="p")
    assert dr.outcome is DecisionOutcome.NO_VERDICT_MALFORMED
    assert dr.decision_id == "d-1"
    assert dr.error
    assert dr.verdict is None
    if isinstance(answers, dict):
        assert dr.raw_answers == answers


@pytest.mark.anyio
@pytest.mark.parametrize("exc", [LexoraTimeoutError("t"), LexoraHTTPError("503", status_code=503)])
async def test_transport_errors(exc: Exception) -> None:
    dr = await decide_once(_state(), client=FakeClient(exc=exc), policy="p")
    assert dr.outcome is DecisionOutcome.TRANSPORT_ERROR
    assert dr.decision_id is None
    assert dr.error


@pytest.mark.anyio
@pytest.mark.parametrize("bad", [{"decision_id": None}, {"decision_id": ""}, {"provider": None}])
async def test_unusable_envelope_is_transport_error(bad: dict[str, Any]) -> None:
    dr = await decide_once(_state(), client=FakeClient({**_payload(), **bad}), policy="p")
    assert dr.outcome is DecisionOutcome.TRANSPORT_ERROR


@pytest.mark.anyio
@pytest.mark.parametrize("status", [201, 302, 500])
async def test_real_client_non_200_is_transport_error(status: int) -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(status, json=_payload()))
    async with LexoraClient("http://lexora.test", transport=transport) as client:
        dr = await decide_once(_state(), client=client, policy="p")
    assert dr.outcome is DecisionOutcome.TRANSPORT_ERROR


@pytest.mark.anyio
async def test_real_client_posts_wire_body_to_v1_decide() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_payload())

    async with LexoraClient("http://lexora.test", transport=httpx.MockTransport(handler)) as c:
        dr = await decide_once(_state(), client=c, policy=POLICY_LIVE_TIERC)
    assert dr.outcome is DecisionOutcome.EVALUATED
    assert seen[0].url.path == "/v1/decide"
    assert json.loads(seen[0].content) == build_decide_request(_state(), policy=POLICY_LIVE_TIERC)


# --------------------------------------------------------------------------- adapter gating / build


@pytest.mark.anyio
async def test_evaluate_off_or_not_human_makes_no_call() -> None:
    c = FakeClient(_payload())
    off = DeciderLexoraAdapter(tierc_mode="off", client_factory=lambda: c)
    assert await off.evaluate(_state()) is None
    shadow = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: c)
    assert await shadow.evaluate(_state(parsed_next="Bohr")) is None
    assert c.bodies == []
    dr = await shadow.evaluate(_state())
    assert dr is not None and c.closed == 1


def test_build_decider_off_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDWIRE_DECIDER_BACKEND", raising=False)
    assert build_decider(config_backend="off", tierc_mode="shadow") is None
    assert build_decider(config_backend="lexora", tierc_mode="off") is None


def test_build_decider_env_backend_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWIRE_DECIDER_BACKEND", "lexora")
    monkeypatch.setenv("MINDWIRE_LEXORA_URL", "http://lexora.test")
    d = build_decider(
        config_backend="off", tierc_mode="shadow", rules_path=tierc_rules_template_path()
    )
    assert isinstance(d, DeciderLexoraAdapter)
    assert d.rules is not None  # tierc-v2 is the default question set
    v1 = build_decider(config_backend="off", tierc_mode="shadow", questions="tierc-v1")
    assert isinstance(v1, DeciderLexoraAdapter) and v1.rules is None


def test_build_decider_lexora_requires_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDWIRE_DECIDER_BACKEND", raising=False)
    monkeypatch.delenv("MINDWIRE_LEXORA_URL", raising=False)
    with pytest.raises(ValueError, match="MINDWIRE_LEXORA_URL"):
        build_decider(config_backend="lexora", tierc_mode="shadow")


@pytest.mark.parametrize("mode", ["annotate", "bounce"])
def test_build_decider_refuses_unimplemented_acting_modes(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.delenv("MINDWIRE_DECIDER_BACKEND", raising=False)
    monkeypatch.setenv("MINDWIRE_LEXORA_URL", "http://lexora.test")
    with pytest.raises(ValueError, match="not implemented"):
        build_decider(config_backend="lexora", tierc_mode=mode)


def test_timeout_is_five_seconds() -> None:
    assert DECIDER_TIMEOUT_SECONDS == 5.0
    client = decider_lexora._default_client_factory("http://lexora.test")()
    assert isinstance(client, LexoraClient)
    assert client._client.timeout.read == 5.0


# --------------------------------------------------------------------------- hook / log_decision


@pytest.mark.parametrize(
    "dr",
    [
        _dr(DecisionOutcome.EVALUATED, _v(TierCScope.IN_GATE)),
        _dr(DecisionOutcome.EVALUATED, _v(TierCScope.OUT_OF_GATE)),
        _dr(DecisionOutcome.NO_VERDICT_NULL, None),
        _dr(DecisionOutcome.NO_VERDICT_MALFORMED, None),
        _dr(DecisionOutcome.TRANSPORT_ERROR, None, None),
    ],
)
def test_log_decision_always_writes_id_and_outcome(
    dr: DecisionResult, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        rec = log_decision(thread_id="T-x", round_index=3, stop="human", dr=dr, gate_result=GREY)
    assert rec["outcome"] == dr.outcome.value
    assert rec["gate_kind"] == "ADMIT_UNSURE" and rec["gate_is_grey_zone"] is True
    assert "decision_id" in rec and rec["decision_id"] == dr.decision_id
    assert rec["stop"] == "human"
    line = next(r.getMessage() for r in caplog.records if "decider_decision" in r.getMessage())
    logged = json.loads(line.split("decider_decision ", 1)[1])
    assert logged["outcome"] == dr.outcome.value and logged["decision_id"] == dr.decision_id


def test_log_decision_gate_columns_for_labelled_admit() -> None:
    """msg-4196 DECIDED 2: ``gate_kind`` may be ``None`` even when the gate ran."""
    dr = _dr(DecisionOutcome.EVALUATED, _v(TierCScope.OUT_OF_GATE))
    rec = log_decision(
        thread_id="T", round_index=1, stop="human", dr=dr, gate_result=ADMIT_LABELLED
    )
    assert rec["gate_kind"] is None and rec["gate_is_grey_zone"] is False


def test_log_decision_gate_columns_none_when_gate_did_not_run() -> None:
    """The msg-4196 DECIDED 2 not-called line is gone (tierc-v2 msg-4380 Δ2: a gate-less turn is
    sent); the gate columns of a called line are ``None`` when the gate did not run."""
    rec = log_decision(
        thread_id="T",
        round_index=1,
        stop="human",
        dr=_dr(DecisionOutcome.NO_VERDICT_NULL, None),
        gate_result=None,
    )
    assert rec["outcome"] == "no_verdict_null"
    assert rec["gate_kind"] is None and rec["gate_is_grey_zone"] is None
    assert rec["stop"] == "human"
    assert not hasattr(hook_mod, "_NOT_CALLED_FIELDS")


class _StubDecider:
    def __init__(self, result: DecisionResult | None = None, exc: Exception | None = None) -> None:
        self.tierc_mode = "shadow"
        self.result = result
        self.exc = exc
        self.states: list[DecisionState] = []

    def is_target(self, state: DecisionState) -> bool:
        return self.tierc_mode != "off" and state.parsed_next == "human"

    async def evaluate(self, state: DecisionState) -> DecisionResult | None:
        self.states.append(state)
        if self.exc is not None:
            raise self.exc
        return self.result


def _msgs(head_author: str = "Bohr", head_body: str | None = None) -> list[ThreadMessage]:
    body = "x" * 900 + "\n\nNEXT: human" if head_body is None else head_body
    return [
        ThreadMessage("m1", "Einstein", "critique\n\nNEXT: Bohr", "Bohr"),
        ThreadMessage("m2", head_author, body, "human"),
    ]


async def _hook(
    decider: Any,
    *,
    messages: list[ThreadMessage] | None = None,
    stop: str | None = "human",
    author_wrote_next_human: bool = True,
) -> DecisionResult | None:
    return await run_tierc_hook(
        decider,
        thread_id="T",
        round_index=1,
        roster=ROSTER,
        messages=_msgs() if messages is None else messages,
        stop=stop,
        author_wrote_next_human=author_wrote_next_human,
        now=NOW,
    )


class _GateSpy:
    """Counts ``decide_admission`` calls made through the hook (msg-4203 tests 6-8)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, exc: Exception | None = None) -> None:
        self.calls = 0
        self.decisions: list[Any] = []
        real = tier_c_admission_gate.decide_admission

        def spy(**kw: Any) -> Any:
            self.calls += 1
            if exc is not None:
                raise exc
            d = real(**kw)
            self.decisions.append(d)
            return d

        monkeypatch.setattr(hook_mod, "decide_admission", spy)


def test_turn_from_messages_shape() -> None:
    t = turn_from_messages(thread_id="T", round_index=1, roster={}, messages=_msgs())
    assert t.parsed_next == "human"
    assert t.prev_next == "Bohr"
    assert [e.msg_id for e in t.recent_events] == ["m2", "m1"]  # newest first
    assert len(t.head_summary) == 500
    assert t.gate_result is None


@pytest.mark.parametrize(
    ("original_stop", "wrote", "role", "expected"),
    [
        ("human", True, Role.PROPOSER, True),
        (None, True, Role.PROPOSER, False),  # forced naysayer consult: no rule stop yet
        ("settled", True, Role.PROPOSER, False),
        ("human", False, Role.PROPOSER, False),  # field/body mismatch safety valve
        ("human", True, Role.IMPLEMENTER, True),  # tierc-v2 msg-4360 / msg-4382
        ("human", True, Role.NAYSAYER, True),  # tierc-v2 msg-4360 / msg-4382
        (None, True, Role.IMPLEMENTER, False),
        ("human", False, Role.NAYSAYER, False),
        ("human", True, None, False),  # off-roster author (pr-gate-relay): notification, msg-4361
    ],
)
def test_is_tierc_entry(
    original_stop: str | None, wrote: bool, role: Role | None, expected: bool
) -> None:
    """msg-4203 DECIDED 2b-3 (revised), roles widened by tierc-v2: all three conditions."""
    assert (
        is_tierc_entry(original_stop=original_stop, author_wrote_next_human=wrote, author_role=role)
        is expected
    )


@pytest.mark.parametrize(
    ("body", "verdict", "kind", "label", "grey"),
    [
        ("x\nTIER-C: goal\nNEXT: human", AdmissionVerdict.ADMIT, None, "goal", False),
        (
            "x\nTIER-C: unsure:goal?\nNEXT: human",
            AdmissionVerdict.ADMIT,
            LogKind.ADMIT_UNSURE,
            "unsure:goal?",
            True,
        ),
        ("x\nNEXT: human", AdmissionVerdict.BOUNCE, LogKind.BOUNCED, None, False),
    ],
)
def test_compute_gate_result_maps_decision(
    body: str, verdict: AdmissionVerdict, kind: LogKind | None, label: str | None, grey: bool
) -> None:
    g = compute_gate_result(body=body, author="Bohr", now=NOW)
    assert g is not None
    assert (g.verdict, g.kind, g.label, g.is_grey_zone) == (verdict, kind, label, grey)


def test_compute_gate_result_retry_prefix_falls_through_to_labels() -> None:
    """Test 3 (msg-4200): retry_lookup is always False, so a ``RETRY:`` body is label-evaluated."""
    body = "RETRY: 1234-abcd\nx\nTIER-C: unsure:goal?\nNEXT: human"
    g = compute_gate_result(body=body, author="Bohr", now=NOW)
    assert g is not None
    assert g.kind is LogKind.ADMIT_UNSURE and g.retry_admit_reason is None
    assert never_retry("1234-abcd", "Bohr") is False


def test_compute_gate_result_retry_admit_reason_when_lookup_matches() -> None:
    """The mapping carries ``retry_admit_reason`` (for when a real lookup is wired)."""
    body = "RETRY: u-1\nx\nTIER-C: release-cross-repo\nNEXT: human"
    g = compute_gate_result(body=body, author="Bohr", now=NOW, retry_lookup=lambda u, a: True)
    assert g is not None and g.kind is LogKind.RETRY_ADMIT
    assert g.retry_admit_reason is RetryAdmitReason.SECOND_TIME_FORCE_ADMIT and g.is_grey_zone


def test_compute_gate_result_exception_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _GateSpy(monkeypatch, exc=RuntimeError("gate down"))
    assert compute_gate_result(body="NEXT: human", author="Bohr", now=NOW) is None


@pytest.mark.anyio
async def test_hook_proposer_human_builds_gate_result_and_calls_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Test 1 (msg-4200, read as "proposer-written NEXT: human" per msg-4203): gate → one HTTP."""
    c = FakeClient(_payload())
    adapter = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: c)
    spy = _GateSpy(monkeypatch)
    body = "x\nTIER-C: unsure:goal?\nNEXT: human"
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        dr = await _hook(adapter, messages=_msgs(head_body=body))
    assert spy.calls == 1
    assert len(c.bodies) == 1
    assert dr is not None and dr.outcome is DecisionOutcome.EVALUATED
    (line,) = _decider_lines(caplog)
    assert line["gate_kind"] == "ADMIT_UNSURE" and line["gate_is_grey_zone"] is True
    assert line["stop"] == "human"


@pytest.mark.anyio
async def test_hook_with_preexisting_stop_logs_both_and_keeps_dr() -> None:
    """msg-4186 §2 (c): a rule stop does not rewrite ``dr``; both land on the log line."""
    dr = _dr(DecisionOutcome.EVALUATED, _v(TierCScope.IN_GATE))
    got = await _hook(_StubDecider(dr))
    assert got is dr


def _decider_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(r.getMessage().split("decider_decision ", 1)[1])
        for r in caplog.records
        if r.getMessage().startswith("decider_decision ")
    ]


@pytest.mark.anyio
async def test_hook_gate_exception_v1_decider_no_http_no_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Test 4 (msg-4200 2b-2) under a **v1** Decider: gate raises → gate_result None → 0 HTTP.
    The msg-4196 DECIDED 2 empty-``outcome`` line is gone (msg-4380 Δ2), so no line either; the
    v2 counterpart (the turn IS sent) is in ``test_decider_tierc_v2.py``."""
    _GateSpy(monkeypatch, exc=RuntimeError("gate down"))
    c = FakeClient(_payload())
    adapter = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: c)
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        got = await _hook(adapter)
    assert got is None
    assert c.bodies == []
    assert _decider_lines(caplog) == []


@pytest.mark.parametrize(
    ("head_author", "head_body"),
    [
        # Test 6: the implementer's merge handoff — entered since tierc-v2 (msg-4360 / 4382).
        ("Heisenberg", "PR #9 opened.\nTIER-C: merge-protected\nNEXT: human"),
        # Test 7: the naysayer's own in-thread NEXT: human — entered since tierc-v2.
        ("Einstein", "VERDICT: APPROVE\n\nNEXT: human"),
    ],
)
@pytest.mark.anyio
async def test_hook_implementer_and_naysayer_human_are_entered(
    head_author: str,
    head_body: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tests 6-7 flipped by tierc-v2 (msg-4203 2b-3 overridden): gate once, HTTP once, one line."""
    spy = _GateSpy(monkeypatch)
    c = FakeClient(_payload())
    adapter = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: c)
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        got = await _hook(adapter, messages=_msgs(head_author=head_author, head_body=head_body))
    assert got is not None
    assert spy.calls == 1
    assert len(c.bodies) == 1
    assert len(_decider_lines(caplog)) == 1


@pytest.mark.parametrize(
    ("head_author", "head_body"),
    [
        # Test 8: an off-roster infra author — its APPROVE → human is a notification (msg-4361).
        ("pr-gate-relay", "VERDICT: APPROVE (ci=success)\n\nNEXT: human"),
    ],
)
@pytest.mark.anyio
async def test_hook_off_roster_human_is_not_entered(
    head_author: str,
    head_body: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test 8 (msg-4203, kept by msg-4361): gate 0 calls, HTTP 0, no ``decider_decision`` line."""
    spy = _GateSpy(monkeypatch)
    c = FakeClient(_payload())
    adapter = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: c)
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        got = await _hook(adapter, messages=_msgs(head_author=head_author, head_body=head_body))
    assert got is None
    assert spy.calls == 0
    assert c.bodies == []
    assert _decider_lines(caplog) == []


@pytest.mark.parametrize(
    ("stop", "wrote"),
    [(None, True), ("settled", True), ("human", False)],
)
@pytest.mark.anyio
async def test_hook_proposer_without_human_stop_or_own_handoff_is_not_entered(
    stop: str | None, wrote: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spy = _GateSpy(monkeypatch)
    stub = _StubDecider(_dr(DecisionOutcome.EVALUATED, _v(TierCScope.IN_GATE)))
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        got = await _hook(stub, stop=stop, author_wrote_next_human=wrote)
    assert got is None and spy.calls == 0 and stub.states == []
    assert _decider_lines(caplog) == []


@pytest.mark.anyio
async def test_hook_asks_decider_is_target_and_writes_no_line_when_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PR-gate advisory 1 on #345: the hook asks ``decider.is_target``; not a target → nothing."""
    decider = _StubDecider(None)
    decider.tierc_mode = "off"
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        got = await _hook(decider)
    assert got is None
    assert decider.states == []
    assert _decider_lines(caplog) == []


def test_hook_does_not_restate_the_target_rule() -> None:
    """Advisory 1 on #345: the "mode / head" rule lives in the Decider, not in hook.py."""
    src = (ROOT / "src" / "spirrow_mindwire" / "decider" / "hook.py").read_text(encoding="utf-8")
    assert "_is_tierc_target" not in src
    assert 'tierc_mode != "off"' not in src


def test_adapter_is_target() -> None:
    shadow = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: FakeClient())
    off = DeciderLexoraAdapter(tierc_mode="off", client_factory=lambda: FakeClient())
    assert shadow.is_target(_state()) is True
    assert shadow.is_target(_state(parsed_next="Bohr")) is False
    assert off.is_target(_state()) is False


@pytest.mark.anyio
async def test_hook_annotate_branch_entered_on_proposer_human_stop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test 9 (msg-4203): proposer ``NEXT: human`` + HUMAN stop + injected annotate → the acting
    branch is reached (the old ``stop is None`` condition would have skipped it)."""
    stub = _StubDecider(_dr(DecisionOutcome.EVALUATED, _v(TierCScope.IN_GATE)))
    stub.tierc_mode = "annotate"  # build_decider refuses annotate; injected for this test only
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        got = await _hook(stub)
    assert got is not None
    assert any("has no acting implementation yet" in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_hook_swallows_decider_exceptions() -> None:
    assert await _hook(_StubDecider(exc=RuntimeError("boom"))) is None


def _verdict_reads(path: Path) -> list[tuple[str, int]]:
    """``(enclosing function, line)`` for every ``<expr>.verdict`` attribute read in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []

    def visit(node: ast.AST, fn: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else fn
            if isinstance(child, ast.Attribute) and child.attr == "verdict":
                hits.append((name, child.lineno))
            visit(child, name)

    visit(tree, "<module>")
    return hits


def test_hook_code_never_reads_raw_verdict() -> None:
    """msg-4184 §2-3: acting code uses ``actionable_verdict``; ``.verdict`` only in log_decision."""
    hook = ROOT / "src" / "spirrow_mindwire" / "decider" / "hook.py"
    # ``gate_result_from_decision`` reads ``AdmissionDecision.verdict`` (admit / bounce — the
    # admission gate's own answer, copied into the Decider's input), not a Decider verdict.
    allowed = {"log_decision", "gate_result_from_decision"}
    assert all(fn in allowed for fn, _ in _verdict_reads(hook)), _verdict_reads(hook)
    core = ROOT / "src" / "spirrow_mindwire" / "conductor" / "core.py"
    assert all(fn != "_decider_hook" for fn, _ in _verdict_reads(core))
    assert "dr.verdict" not in core.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- Conductor wiring


def _conductor_with(mcp: _FakeChatroomMcp, disp: _ScriptedDispatcher, dec: Any) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=ROSTER,
        naysayer_identity="Einstein",
        decider=dec,
    )


_PROPOSER_HUMAN = "revised\n\nNEXT: human"  # no TIER-C label → the gate bounces (no-label)


async def _run(
    dec: Any,
    *,
    seed: tuple[str, str] = ("Bohr", "design\n\nNEXT: Einstein"),
    replies: dict[Role, list[str]] | None = None,
) -> tuple[Any, _ScriptedDispatcher, _FakeChatroomMcp]:
    """Default: Bohr → Einstein critique → Bohr ``NEXT: human`` (naysayer already consulted, so
    the rule stop is HUMAN on the proposer's own head)."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author=seed[0], content=seed[1])
    if replies is None:
        replies = {
            Role.NAYSAYER: [_attested("critique\n\nNEXT: Bohr")],
            Role.PROPOSER: [_PROPOSER_HUMAN],
        }
    disp = _ScriptedDispatcher(mcp, replies)
    outcome = await _conductor_with(mcp, disp, dec).run()
    return outcome, disp, mcp


@pytest.mark.anyio
async def test_conductor_calls_decider_on_proposer_human_and_stop_is_unchanged() -> None:
    """Test 5 (msg-4200): the gate bounces this head, yet stop / dispatch / posts are unchanged."""
    baseline, bdisp, bmcp = await _run(None)
    assert baseline.stop_reason is StopReason.HUMAN
    for dr in (
        _dr(DecisionOutcome.EVALUATED, _v(TierCScope.IN_GATE)),
        _dr(DecisionOutcome.TRANSPORT_ERROR, None, None),
        None,
    ):
        stub = _StubDecider(dr)
        outcome, disp, mcp = await _run(stub)
        assert outcome == baseline
        assert disp.dispatches == bdisp.dispatches
        assert mcp.posts == bmcp.posts
        assert len(stub.states) == 1
        st = stub.states[0]
        assert st.parsed_next == "human"
        assert st.gate_result is not None
        assert st.gate_result.verdict is AdmissionVerdict.BOUNCE  # bounce changes nothing


@pytest.mark.anyio
async def test_conductor_decider_exception_does_not_change_stop() -> None:
    baseline, _, _ = await _run(None)
    outcome, _, _ = await _run(_StubDecider(exc=RuntimeError("down")))
    assert outcome == baseline
    assert outcome.stop_reason is StopReason.HUMAN


@pytest.mark.anyio
async def test_conductor_does_not_call_decider_on_non_human_heads() -> None:
    stub = _StubDecider(None)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="done\n\nNEXT: none")
    await _conductor_with(mcp, _ScriptedDispatcher(mcp, {}), stub).run()
    assert stub.states == []


@pytest.mark.anyio
async def test_conductor_naysayer_exit_human_is_evaluated_and_stop_unchanged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Test 7 at Conductor level, flipped by tierc-v2 (msg-4360 / 4382): the naysayer's own
    ``NEXT: human`` enters the hook; the stop stays exactly what the rules made it (D20)."""
    baseline, _, _ = await _run(
        None, replies={Role.NAYSAYER: [_attested("VERDICT: APPROVE\n\nNEXT: human")]}
    )
    spy = _GateSpy(monkeypatch)
    stub = _StubDecider(_dr(DecisionOutcome.EVALUATED, _v(TierCScope.IN_GATE)))
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        outcome, _, _ = await _run(
            stub, replies={Role.NAYSAYER: [_attested("VERDICT: APPROVE\n\nNEXT: human")]}
        )
    assert outcome == baseline
    assert outcome.stop_reason is StopReason.HUMAN
    assert spy.calls == 1 and len(stub.states) == 1
    assert len(_decider_lines(caplog)) == 1


@pytest.mark.anyio
async def test_conductor_forced_naysayer_escalation_leaves_one_proposer_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test 10 (msg-4203): Bohr ``NEXT: human`` → forced Einstein consult (no rule stop yet, not
    entered) → Bohr ``NEXT: human`` again → HUMAN stop → exactly one line, for the proposer."""
    stub = _StubDecider(_dr(DecisionOutcome.EVALUATED, _v(TierCScope.OUT_OF_GATE)))
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        outcome, disp, _ = await _run(
            stub,
            seed=("Bohr", "design\n\nNEXT: human"),
            replies={
                Role.NAYSAYER: [_attested("critique\n\nNEXT: Bohr")],
                Role.PROPOSER: [_PROPOSER_HUMAN],
            },
        )
    assert outcome.stop_reason is StopReason.HUMAN
    # the forced consult did happen, on the proposer's first NEXT: human (m1)
    assert disp.dispatches[0] == (Role.NAYSAYER, "m1")
    assert outcome.forced_naysayer_turns == 1
    assert len(stub.states) == 1
    assert stub.states[0].recent_events[0].author == "Bohr"  # newest first: the proposer's head
    (line,) = _decider_lines(caplog)
    assert line["stop"] == "human"


@pytest.mark.anyio
async def test_conductor_never_writes_the_decisions_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test 2 (msg-4200 2b-1): the compute-only gate writes nothing, even on a bounce."""
    from spirrow_mindwire import tier_c_decisions_log as dlog

    writes: list[str] = []
    for name in ("append_log_entry", "append_log_entries", "build_retry_lookup"):
        monkeypatch.setattr(dlog, name, lambda *a, _n=name, **k: writes.append(_n))
    monkeypatch.chdir(tmp_path)
    spy = _GateSpy(monkeypatch)
    c = FakeClient(_payload())
    adapter = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: c)
    await _run(adapter)
    assert spy.calls == 1
    # the gate DID produce entries (a BOUNCED row) — they were discarded, not written
    assert spy.decisions[0].log_entries
    assert writes == []
    assert list(tmp_path.iterdir()) == []
    assert len(c.bodies) == 1  # test 1 at Conductor level: one HTTP call


# --------------------------------------------------------------------------- replay --endpoint


def test_replay_endpoint_appends_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replay = _load_replay()
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=_payload(provider="null"))

    real: type[LexoraClient] = replay.LexoraClient

    def patched(url: str, **kw: Any) -> LexoraClient:
        return real(url, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(replay, "LexoraClient", patched)
    row = {
        "thread_id": "T",
        "round_index": 1,
        "roster": {},
        "recent_events": [],
        "parsed_next": "human",
        "gate_result": {"verdict": "admit", "kind": "ADMIT_UNSURE"},
    }
    fixture = tmp_path / "f.jsonl"
    fixture.write_text(json.dumps(row) + "\n", encoding="utf-8")
    out = tmp_path / "o.jsonl"
    rc = replay.main(
        ["--track", "tierc", "--fixture", str(fixture), "--out", str(out), "--endpoint", "http://x"]
    )
    assert rc == 0
    rec = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert rec["decision"]["outcome"] == "no_verdict_null"
    assert rec["decision"]["decision_id"] == "d-1"
    assert rec["decision"]["policy"] == POLICY_REPLAY_TIERC
    assert seen[0]["policy"] == POLICY_REPLAY_TIERC


def test_replay_without_endpoint_is_unchanged_dry_run(tmp_path: Path) -> None:
    replay = _load_replay()
    row = {
        "thread_id": "T",
        "round_index": 1,
        "roster": {},
        "recent_events": [],
        "gate_result": {"verdict": "admit", "kind": "ADMIT_UNSURE"},
    }
    fixture = tmp_path / "f.jsonl"
    fixture.write_text(json.dumps(row) + "\n", encoding="utf-8")
    out = tmp_path / "o.jsonl"
    assert replay.main(["--track", "tierc", "--fixture", str(fixture), "--out", str(out)]) == 0
    rec = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert "decision" not in rec


def test_default_settings_build_no_decider(monkeypatch: pytest.MonkeyPatch) -> None:
    """tierc default is shadow (msg-4180 §4) but backend default is off → no Decider, no HTTP."""
    from spirrow_mindwire.config import MindwireSettings

    monkeypatch.delenv("MINDWIRE_DECIDER_BACKEND", raising=False)
    cfg = MindwireSettings().decider
    assert cfg.tierc.mode == "shadow"
    assert build_decider(config_backend=cfg.backend, tierc_mode=cfg.tierc.mode) is None
