"""T-decider-conductor-hook step 2 — adapter, result, wire, hook, Conductor wiring, replay.

Spec: Bohr msg-4180 (wire / Null rule / extraction / transport / policy / replay --endpoint),
msg-4182 (``DecisionResult``), msg-4184 (``actionable_verdict`` + invariants), msg-4186 (gate =
``is_grey_zone``; 4-valued outcome), msg-4188 (always call ``/v1/decide``, then branch),
msg-4196 (DECIDED 1: ``gate_result is None`` → no call; DECIDED 2: gate columns + not-called line).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_conductor_core import _ROSTER as ROSTER
from test_conductor_core import _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire import decider as decider_facade
from spirrow_mindwire.adapters import decider_lexora
from spirrow_mindwire.adapters.decider_lexora import (
    DECIDER_TIMEOUT_SECONDS,
    DeciderLexoraAdapter,
    build_decider,
    decide_once,
)
from spirrow_mindwire.conductor.core import Conductor, StopReason
from spirrow_mindwire.decider.hook import (
    ThreadMessage,
    log_decision,
    run_tierc_hook,
    turn_from_messages,
)
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
from spirrow_mindwire.tier_c_admission_gate import AdmissionVerdict, LogKind
from spirrow_mindwire.value_objects import Role

ROOT = Path(__file__).resolve().parent.parent
ALL_KEYS = TIER_C_GENUINE_KEYS + TIER_C_SPURIOUS_KEYS

GREY = AdmissionGateResult(
    verdict=AdmissionVerdict.ADMIT, kind=LogKind.ADMIT_UNSURE, label="unsure:goal?"
)
ADMIT_LABELLED = AdmissionGateResult(verdict=AdmissionVerdict.ADMIT, kind=None, label="goal")


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
    d = build_decider(config_backend="off", tierc_mode="shadow")
    assert isinstance(d, DeciderLexoraAdapter)


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


def test_log_decision_not_called_line_has_empty_outcome_and_same_keys() -> None:
    """msg-4196 DECIDED 2: the "targeted but not sent" line — outcome empty, gate columns None."""
    called = log_decision(
        thread_id="T",
        round_index=1,
        stop="human",
        dr=_dr(DecisionOutcome.NO_VERDICT_NULL, None),
        gate_result=GREY,
    )
    rec = log_decision(thread_id="T", round_index=1, stop="human", dr=None, gate_result=None)
    assert set(rec) == set(called)
    assert rec["outcome"] is None and rec["decision_id"] is None
    assert rec["gate_kind"] is None and rec["gate_is_grey_zone"] is None
    assert rec["stop"] == "human"


class _StubDecider:
    def __init__(self, result: DecisionResult | None = None, exc: Exception | None = None) -> None:
        self.tierc_mode = "shadow"
        self.result = result
        self.exc = exc
        self.states: list[DecisionState] = []

    async def evaluate(self, state: DecisionState) -> DecisionResult | None:
        self.states.append(state)
        if self.exc is not None:
            raise self.exc
        return self.result


def _msgs() -> list[ThreadMessage]:
    return [
        ThreadMessage("m1", "Einstein", "critique\n\nNEXT: Bohr", "Bohr"),
        ThreadMessage("m2", "Bohr", "x" * 900 + "\n\nNEXT: human", "human"),
    ]


def test_turn_from_messages_shape() -> None:
    t = turn_from_messages(thread_id="T", round_index=1, roster={}, messages=_msgs())
    assert t.parsed_next == "human"
    assert t.prev_next == "Bohr"
    assert [e.msg_id for e in t.recent_events] == ["m2", "m1"]  # newest first
    assert len(t.head_summary) == 500
    assert t.gate_result is None


@pytest.mark.anyio
async def test_hook_with_preexisting_stop_logs_both_and_keeps_dr() -> None:
    """msg-4186 §2 (c): a rule stop does not rewrite ``dr``; both land on the log line."""
    dr = _dr(DecisionOutcome.EVALUATED, _v(TierCScope.IN_GATE))
    got = await run_tierc_hook(
        _StubDecider(dr), thread_id="T", round_index=1, roster={}, messages=_msgs(), stop="human"
    )
    assert got is dr


def _decider_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(r.getMessage().split("decider_decision ", 1)[1])
        for r in caplog.records
        if r.getMessage().startswith("decider_decision ")
    ]


@pytest.mark.anyio
async def test_hook_logs_not_called_line_for_ungated_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """msg-4196 DECIDED 2: gate did not run → evaluate None → still one line, outcome empty."""
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        got = await run_tierc_hook(
            _StubDecider(None),
            thread_id="T",
            round_index=1,
            roster={},
            messages=_msgs(),
            stop="human",
        )
    assert got is None
    lines = _decider_lines(caplog)
    assert len(lines) == 1
    assert lines[0]["outcome"] is None and lines[0]["gate_is_grey_zone"] is None
    assert lines[0]["stop"] == "human"


@pytest.mark.anyio
async def test_hook_writes_no_line_for_non_human_head(caplog: pytest.LogCaptureFixture) -> None:
    """PR-gate on #345: ``evaluate`` → None on a non-human head is not a "missed" turn."""
    msgs = [
        ThreadMessage("m1", "Bohr", "design\n\nNEXT: Einstein", "Einstein"),
        ThreadMessage("m2", "Einstein", "critique\n\nNEXT: Bohr", "Bohr"),
    ]
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        got = await run_tierc_hook(
            _StubDecider(None),
            thread_id="T",
            round_index=1,
            roster={},
            messages=msgs,
            stop=None,
        )
    assert got is None
    assert _decider_lines(caplog) == []


@pytest.mark.anyio
async def test_hook_writes_no_line_when_decider_mode_off(caplog: pytest.LogCaptureFixture) -> None:
    """PR-gate on #345: a Decider in mode ``off`` targets nothing, so it logs nothing."""
    decider = _StubDecider(None)
    decider.tierc_mode = "off"
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        got = await run_tierc_hook(
            decider,
            thread_id="T",
            round_index=1,
            roster={},
            messages=_msgs(),
            stop="human",
        )
    assert got is None
    assert _decider_lines(caplog) == []


@pytest.mark.anyio
async def test_hook_with_gate_logs_gate_columns(caplog: pytest.LogCaptureFixture) -> None:
    dr = _dr(DecisionOutcome.EVALUATED, _v(TierCScope.IN_GATE))
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        await run_tierc_hook(
            _StubDecider(dr),
            thread_id="T",
            round_index=1,
            roster={},
            messages=_msgs(),
            stop="human",
            gate_result=GREY,
        )
    (line,) = _decider_lines(caplog)
    assert line["outcome"] == "evaluated"
    assert line["gate_kind"] == "ADMIT_UNSURE" and line["gate_is_grey_zone"] is True


@pytest.mark.anyio
async def test_hook_swallows_decider_exceptions() -> None:
    got = await run_tierc_hook(
        _StubDecider(exc=RuntimeError("boom")),
        thread_id="T",
        round_index=1,
        roster={},
        messages=_msgs(),
        stop="human",
    )
    assert got is None


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
    assert all(fn == "log_decision" for fn, _ in _verdict_reads(hook)), _verdict_reads(hook)
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


async def _run(dec: Any) -> tuple[Any, _ScriptedDispatcher, _FakeChatroomMcp]:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: ["critique\n\nNEXT: human"]})
    outcome = await _conductor_with(mcp, disp, dec).run()
    return outcome, disp, mcp


@pytest.mark.anyio
async def test_conductor_calls_decider_on_explicit_human_and_stop_is_unchanged() -> None:
    baseline, bdisp, bmcp = await _run(None)
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
        assert st.gate_result is None  # the conductor does not run the admission gate


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
