"""T-decider-tierc-v2-all-escalations — tierc-v2 (五ヶ条) question set, verdict, adapter, hook.

Spec: Fermi msg-4360 (Takahito's decision: every ``NEXT: human`` from Bohr / Heisenberg /
Einstein goes through Jev; rules + "should this be asked of the human" framing), msg-4361 (the
五ヶ条 replace the draft rules; criteria; ``matched_rule`` = ``rule_1``..``rule_5`` / ``none``;
pr-gate-relay excluded; rules in one editable file), Bohr msg-4380 (Δ1-Δ6) as amended by
msg-4382 (Role members only; ``matched_rule`` must be logged; rules at
``<data_dir>/config/tierc_rules.toml``) and msg-4384 (read once at startup, no hot-reload),
Einstein-approved.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire import cli
from spirrow_mindwire.adapters.decider_lexora import (
    MATCHED_RULE_SOURCE_CHOICE,
    DeciderLexoraAdapter,
    build_decider,
    decide_once,
)
from spirrow_mindwire.config import (
    DeciderThresholdsConfig,
    MindwireSettings,
    resolve_tierc_rules_path,
)
from spirrow_mindwire.decider.hook import (
    TIERC_ENTRY_ROLES,
    ThreadMessage,
    count_dispute_rounds,
    run_tierc_hook,
)
from spirrow_mindwire.decider.questions import (
    MATCHED_RULE_KEY,
    SHOULD_ASK_HUMAN_KEY,
    TIERC_ESCALATION_QUESTIONS_VERSION,
    TIERC_V2_PROCEED_QUESTIONS_VERSION,
    TierCRules,
    TierCRulesError,
    load_tierc_rules,
    parse_tierc_rules,
    tierc_rules_template_path,
    tierc_v2_questions,
)
from spirrow_mindwire.decider.result import DecisionOutcome, decision_result_to_dict
from spirrow_mindwire.decider.state import (
    AdmissionGateResult,
    DecisionState,
    EventSummary,
    SimpleTurn,
    state_builder,
)
from spirrow_mindwire.decider.verdict import (
    TierCScope,
    TierCV2Thresholds,
    TierCV2Verdict,
    TierCVerdictKind,
    evaluate_tierc_v2,
)
from spirrow_mindwire.decider.wire import POLICY_LIVE_TIERC, build_decide_request, state_to_dict
from spirrow_mindwire.lexora.client import LexoraTimeoutError
from spirrow_mindwire.tier_c_admission_gate import AdmissionVerdict, LogKind
from spirrow_mindwire.value_objects import Role

TEMPLATE = tierc_rules_template_path()
RULES = load_tierc_rules(TEMPLATE)

# msg-4361 "人間に問うべきもの (五ヶ条)" — the rule texts, verbatim (bold markers dropped).
FIVE_ARTICLES = (
    "金銭的コストの増加が見込まれる変更、アイデアの承認",
    "対象となるプロジェクトに対して決めた仕様の追加、削除、変更を伴う変更の承認",
    "取り消せない操作や外部への公開（データ・リポジトリの削除、force push などの履歴書き換え、"  # noqa: RUF001
    "ストア / SNS など外部への公開）",  # noqa: RUF001
    "人間にしかできない作業の依頼（PAT 等の資格情報の更新、課金・クレジット追加、Takahito の PC "  # noqa: RUF001
    "での操作、Takahito の手元にしかない情報の提供）",  # noqa: RUF001
    "AI 同士で決着がつかない対立の裁定（proposer と naysayer が合意に至らないとき）",  # noqa: RUF001
)

GREY = AdmissionGateResult(
    verdict=AdmissionVerdict.ADMIT, kind=LogKind.ADMIT_UNSURE, label="unsure:goal?"
)
ADMIT_LABELLED = AdmissionGateResult(verdict=AdmissionVerdict.ADMIT, kind=None, label="goal")
ROSTER = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER, "Heisenberg": Role.IMPLEMENTER}


def _state(
    gate: AdmissionGateResult | None = GREY, dispute_rounds: int | None = None
) -> DecisionState:
    return state_builder(
        SimpleTurn(
            thread_id="T-v2",
            round_index=2,
            roster=ROSTER,
            head_summary="head",
            recent_events=(EventSummary("m1", "Bohr", "human", "body"),),
            parsed_next="human",
            prev_next="Einstein",
            gate_result=gate,
            dispute_rounds=dispute_rounds,
        )
    )


def _payload(
    p: Any = 0.8, choice: Any = "rule_2", provider: str = "jev", drop: str | None = None
) -> dict[str, Any]:
    answers: dict[str, Any] = {
        SHOULD_ASK_HUMAN_KEY: {"noul": p},
        MATCHED_RULE_KEY: {
            "choice": choice,
            "probabilities": {"rule_2": 0.9},
            "confidence": 0.8,
        },
    }
    if drop is not None:
        del answers[drop]
    return {"answers": answers, "provider": provider, "decision_id": "d-9", "latency_ms": 7}


class FakeClient:
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


# --------------------------------------------------------------------------- rules file


def test_template_carries_the_five_articles_verbatim() -> None:
    assert [r.id for r in RULES.rules] == [f"rule_{i}" for i in range(1, 6)]
    assert tuple(r.text for r in RULES.rules) == FIVE_ARTICLES
    assert RULES.matched_rule_options == ("rule_1", "rule_2", "rule_3", "rule_4", "rule_5", "none")
    assert RULES.sha256 == hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()


def test_rule_5_carries_the_three_round_guideline() -> None:
    """msg-4380 Δ5: the round guideline is in the rules file (Takahito can edit it)."""
    note = RULES.rules[4].note
    assert note is not None and "3 回以上" in note and "VERDICT: APPROVE" in note
    assert all(r.note is None for r in RULES.rules[:4])


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "at least one"),
        ("[[rule]]\nid = 'rule_1'\n", "non-empty text"),
        ("[[rule]]\nid = 'r1'\ntext = 'x'\n", "rule_<number>"),
        ("[[rule]]\nid = 'none'\ntext = 'x'\n", "rule_<number>"),
        ("[[rule]]\nid='rule_1'\ntext='a'\n[[rule]]\nid='rule_1'\ntext='b'\n", "duplicate"),
        ("[[rule]]\nid = 'rule_1'\ntext = 'x'\ntxet = 'y'\n", "unknown keys"),
        ("title = 'x'\n[[rule]]\nid = 'rule_1'\ntext = 'x'\n", "unknown top-level"),
        ("[[rule]]\nid = 'rule_1'\ntext = 'x'\nnote = ''\n", "note"),
        ("[[rule\n", "cannot parse"),
    ],
)
def test_parse_rejects_malformed_rules(text: str, match: str) -> None:
    with pytest.raises(TierCRulesError, match=match):
        parse_tierc_rules(text.encode("utf-8"), source="t.toml")


def test_load_missing_file_is_a_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="init-config tierc-rules"):
        load_tierc_rules(tmp_path / "absent.toml")


# --------------------------------------------------------------------------- questions / wire


def test_should_ask_human_question_shape() -> None:
    q = tierc_v2_questions(RULES)
    assert list(q) == [SHOULD_ASK_HUMAN_KEY, MATCHED_RULE_KEY]
    sa = q[SHOULD_ASK_HUMAN_KEY]
    assert sa["type"] == "noul"
    # msg-4361 criteria, verbatim
    assert sa["criteria"] == {
        "true": "五ヶ条のいずれかに当たる",
        "false": "五ヶ条のどれにも当たらない",
    }
    for r in RULES.rules:
        assert f"- {r.id}: {r.text}" in sa["instructions"]
    assert "人間に問うべき" in sa["instructions"]  # the framing question (msg-4360 decision 2)


def test_matched_rule_is_a_choice_over_rule_ids_and_none() -> None:
    mr = tierc_v2_questions(RULES)[MATCHED_RULE_KEY]
    assert mr["type"] == "choice"
    # TypeSafe's choice criteria is a {option: description} map; Jev answers an array with a 422
    assert isinstance(mr["criteria"], dict)
    assert list(mr["criteria"]) == list(RULES.matched_rule_options)
    assert list(mr["criteria"].values())[:5] == list(FIVE_ARTICLES)
    # the rule texts ride on the options, not repeated in the instructions
    assert not any(text in mr["instructions"] for text in FIVE_ARTICLES)


def test_questions_follow_the_rules_file(tmp_path: Path) -> None:
    """The rule text comes from the file, not the code (msg-4360: Takahito edits the file)."""
    f = tmp_path / "r.toml"
    f.write_text("[[rule]]\nid = 'rule_1'\ntext = 'ほげ'\n", encoding="utf-8")
    rules = load_tierc_rules(f)
    q = tierc_v2_questions(rules)
    assert "- rule_1: ほげ" in q[SHOULD_ASK_HUMAN_KEY]["instructions"]
    assert q[MATCHED_RULE_KEY]["criteria"] == {
        "rule_1": "ほげ",
        "none": "五ヶ条のどれにも当たらない",
    }


def test_v2_request_body() -> None:
    body = build_decide_request(_state(), policy=POLICY_LIVE_TIERC, rules=RULES)
    assert body["questions_version"] == TIERC_ESCALATION_QUESTIONS_VERSION == "tierc-v3"
    assert body["questions"] == tierc_v2_questions(RULES)
    v1 = build_decide_request(_state(), policy=POLICY_LIVE_TIERC)
    assert v1["questions_version"] == "tierc-v1"


def test_fix_now_vs_follow_up_exclusion_is_in_the_should_ask_frame_only() -> None:
    """T-fix-now-vs-followup-is-mechanical (msg-5234 §3, msg-5241): the exclusion lives in the
    frame sentence (code), not in the rules file; the proceed frame is untouched; keys and
    criteria are unchanged, so old records read the same way."""
    exclusion = (
        "ただし、指摘を今の PR で直すか follow-up PR で直すかの"
        "順序・タイミングだけを問うハンドオフは、"
        "どの条にも当たらない(修正自体が仕様の追加・削除・変更を伴う場合は、その中身で判断する)。"
    )
    q = tierc_v2_questions(RULES)
    instructions = q[SHOULD_ASK_HUMAN_KEY]["instructions"]
    assert instructions.endswith(exclusion)
    assert exclusion not in TEMPLATE.read_text(encoding="utf-8")
    proceed = tierc_v2_questions(RULES, proceed=True)
    assert "follow-up" not in proceed[SHOULD_ASK_HUMAN_KEY]["instructions"]
    assert set(q) == {SHOULD_ASK_HUMAN_KEY, MATCHED_RULE_KEY}
    assert q[SHOULD_ASK_HUMAN_KEY]["criteria"] == {
        "true": "五ヶ条のいずれかに当たる",
        "false": "五ヶ条のどれにも当たらない",
    }
    assert TIERC_V2_PROCEED_QUESTIONS_VERSION == "tierc-v2-proceed"


def test_dispute_rounds_on_wire_only_when_computed() -> None:
    """msg-4380 Δ5 feature; absent when not computed so pre-v2 replay bytes do not move."""
    assert "dispute_rounds" not in state_to_dict(_state())
    assert state_to_dict(_state(dispute_rounds=3))["dispute_rounds"] == 3


def test_gate_none_goes_out_as_null() -> None:
    body = build_decide_request(_state(None), policy=POLICY_LIVE_TIERC, rules=RULES)
    assert json.loads(body["state"])["gate_result"] is None


# --------------------------------------------------------------------------- verdict


@pytest.mark.parametrize(
    ("p", "kind"),
    [
        (1.0, TierCVerdictKind.CONFIRMED),
        (0.60, TierCVerdictKind.CONFIRMED),
        (0.5999, TierCVerdictKind.UNSURE),
        (0.40, TierCVerdictKind.UNSURE),
        (0.3999, TierCVerdictKind.LIKELY_NOT),
        (0.0, TierCVerdictKind.LIKELY_NOT),
    ],
)
def test_evaluate_tierc_v2_boundaries(p: float, kind: TierCVerdictKind) -> None:
    v = evaluate_tierc_v2(p)
    assert v.kind is kind and v.ask_score == p
    assert v.scope is TierCScope.IN_GATE and v.fired_reason is None
    assert v.questions_version == "tierc-v3"


def test_v2_threshold_defaults_are_the_preregistered_values() -> None:
    """msg-4380 Δ4: 0.60 / 0.40 (v1's), fixed before the replay."""
    th = TierCV2Thresholds()
    assert (th.ask_min, th.not_ask_max) == (0.60, 0.40)
    cfg = DeciderThresholdsConfig()
    assert (cfg.tierc_v2_ask_min, cfg.tierc_v2_not_ask_max) == (0.60, 0.40)


@pytest.mark.parametrize(("ask_min", "not_ask_max"), [(0.4, 0.6), (1.1, 0.4), (0.6, -0.1)])
def test_v2_thresholds_reject_bad_bands(ask_min: float, not_ask_max: float) -> None:
    with pytest.raises(ValueError):
        TierCV2Thresholds(ask_min=ask_min, not_ask_max=not_ask_max)


def test_config_rejects_overlapping_v2_bands() -> None:
    with pytest.raises(ValueError, match="tierc_v2_not_ask_max"):
        DeciderThresholdsConfig(tierc_v2_ask_min=0.3, tierc_v2_not_ask_max=0.5)


def test_evaluate_tierc_v2_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        evaluate_tierc_v2(1.5)


# --------------------------------------------------------------------------- decide_once (v2)


@pytest.mark.anyio
@pytest.mark.parametrize("gate", [GREY, ADMIT_LABELLED, None])
async def test_v2_every_gate_result_is_sent_and_in_gate(gate: AdmissionGateResult | None) -> None:
    """msg-4360 (D18 withdrawn) + msg-4380 Δ2 (``gate_result is None`` is sent too)."""
    c = FakeClient(_payload(p=0.1))
    dr = await decide_once(_state(gate), client=c, policy="p", rules=RULES)
    assert len(c.bodies) == 1
    assert dr.outcome is DecisionOutcome.EVALUATED
    assert isinstance(dr.verdict, TierCV2Verdict)
    assert dr.verdict.scope is TierCScope.IN_GATE
    assert dr.actionable_verdict is dr.verdict
    assert dr.verdict.kind is TierCVerdictKind.LIKELY_NOT
    assert dr.matched_rule == "rule_2"
    assert dr.matched_rule_source == MATCHED_RULE_SOURCE_CHOICE == "choice"
    assert dr.matched_rule_error is None
    assert dr.rules_sha256 == RULES.sha256
    assert dr.questions_version == "tierc-v3"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        _payload(p=1.5),
        _payload(p="0.8"),
        _payload(p=True),
        _payload(drop=SHOULD_ASK_HUMAN_KEY),
    ],
)
async def test_v2_bad_should_ask_human_is_malformed(payload: dict[str, Any]) -> None:
    dr = await decide_once(_state(), client=FakeClient(payload), policy="p", rules=RULES)
    assert dr.outcome is DecisionOutcome.NO_VERDICT_MALFORMED
    assert dr.verdict is None and dr.error is not None
    assert dr.rules_sha256 == RULES.sha256 and dr.questions_version == "tierc-v3"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [_payload(choice="rule_9"), _payload(choice=3), _payload(drop=MATCHED_RULE_KEY)],
)
async def test_v2_bad_matched_rule_keeps_the_verdict(
    payload: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """msg-4380 Δ3: display-only — verdict kept, ``matched_rule=None`` + error logged."""
    with caplog.at_level(logging.WARNING):
        dr = await decide_once(_state(), client=FakeClient(payload), policy="p", rules=RULES)
    assert dr.outcome is DecisionOutcome.EVALUATED
    assert dr.verdict is not None and dr.verdict.kind is TierCVerdictKind.CONFIRMED
    assert dr.matched_rule is None and dr.matched_rule_error is not None
    # msg-4629 §2: no value → no source; ``matched_rule_error`` carries the reason.
    assert dr.matched_rule_source is None
    assert "matched_rule unusable" in caplog.text


@pytest.mark.anyio
async def test_v2_none_is_a_valid_matched_rule() -> None:
    dr = await decide_once(
        _state(), client=FakeClient(_payload(choice="none")), policy="p", rules=RULES
    )
    assert dr.matched_rule == "none" and dr.matched_rule_error is None
    assert dr.matched_rule_source == "choice"


@pytest.mark.anyio
async def test_v2_null_provider_and_transport_error_keep_version_fields() -> None:
    dr = await decide_once(
        _state(), client=FakeClient(_payload(provider="null")), policy="p", rules=RULES
    )
    assert dr.outcome is DecisionOutcome.NO_VERDICT_NULL
    assert dr.matched_rule is None and dr.rules_sha256 == RULES.sha256
    assert dr.matched_rule_source is None  # msg-4629 §2: no answer, no source
    te = await decide_once(
        _state(), client=FakeClient(exc=LexoraTimeoutError("t")), policy="p", rules=RULES
    )
    assert te.outcome is DecisionOutcome.TRANSPORT_ERROR
    assert te.questions_version == "tierc-v3" and te.rules_sha256 == RULES.sha256
    assert te.matched_rule is None and te.matched_rule_source is None


@pytest.mark.anyio
async def test_v2_thresholds_are_applied() -> None:
    dr = await decide_once(
        _state(),
        client=FakeClient(_payload(p=0.5)),
        policy="p",
        rules=RULES,
        v2_thresholds=TierCV2Thresholds(ask_min=0.5, not_ask_max=0.5),
    )
    assert dr.verdict is not None and dr.verdict.kind is TierCVerdictKind.CONFIRMED


def test_v2_record_carries_matched_rule_and_rules_sha() -> None:
    from spirrow_mindwire.decider.result import DecisionResult

    dr = DecisionResult(
        outcome=DecisionOutcome.EVALUATED,
        decision_id="d",
        provider="jev",
        raw_answers={},
        verdict=evaluate_tierc_v2(0.7),
        policy="p",
        questions_version="tierc-v2",
        matched_rule="rule_1",
        matched_rule_source="choice",
        rules_sha256="abc",
    )
    d = decision_result_to_dict(dr)
    assert d["verdict"] == {
        "kind": "CONFIRMED",
        "scope": "in_gate",
        "ask_score": 0.7,
        "fired_reason": None,
    }
    assert (d["matched_rule"], d["matched_rule_source"], d["rules_sha256"]) == (
        "rule_1",
        "choice",
        "abc",
    )
    assert d["matched_rule_error"] is None


# --------------------------------------------------------------------------- build_decider / config


def test_build_decider_v2_reads_rules_once_and_refuses_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MINDWIRE_DECIDER_BACKEND", raising=False)
    monkeypatch.setenv("MINDWIRE_LEXORA_URL", "http://lexora.test")
    with pytest.raises(ValueError, match="not readable"):
        build_decider(
            config_backend="lexora", tierc_mode="shadow", rules_path=tmp_path / "missing.toml"
        )
    bad = tmp_path / "bad.toml"
    bad.write_text("[[rule]]\nid = 'x'\ntext = 'y'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="rule_<number>"):
        build_decider(config_backend="lexora", tierc_mode="shadow", rules_path=bad)
    good = tmp_path / "good.toml"
    good.write_bytes(TEMPLATE.read_bytes())
    d = build_decider(config_backend="lexora", tierc_mode="shadow", rules_path=good)
    assert d is not None and d.rules is not None and d.rules.sha256 == RULES.sha256
    # read once: editing the file afterwards does not change the loaded rules (msg-4384)
    good.write_text("[[rule]]\nid = 'rule_1'\ntext = 'changed'\n", encoding="utf-8")
    assert d.rules.sha256 == RULES.sha256


def test_build_decider_off_does_not_need_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDWIRE_DECIDER_BACKEND", raising=False)
    assert build_decider(config_backend="off", tierc_mode="shadow", rules_path=None) is None


def test_config_defaults_and_rules_path(tmp_path: Path) -> None:
    s = MindwireSettings()
    assert s.decider.tierc.questions == "tierc-v2"
    assert s.decider.tierc.rules_path is None
    assert resolve_tierc_rules_path(s) == s.paths.data_dir / "config" / "tierc_rules.toml"
    s2 = MindwireSettings.model_validate(
        {"decider": {"tierc": {"rules_path": str(tmp_path / "x.toml")}}}
    )
    assert resolve_tierc_rules_path(s2) == tmp_path / "x.toml"


def test_template_is_not_read_as_a_runtime_fallback() -> None:
    """msg-4382: one canonical copy; the adapter module never points at the template."""
    root = Path(__file__).resolve().parent.parent / "src" / "spirrow_mindwire"
    src = (root / "adapters" / "decider_lexora.py").read_text(encoding="utf-8")
    assert "tierc_rules_template_path" not in src


# --------------------------------------------------------------------------- CLI init-config


def test_init_config_tierc_rules_copies_template_and_never_overwrites(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MINDWIRE_PATHS__DATA_DIR", str(tmp_path))
    cli.main(["init-config", "tierc-rules", "--config", str(tmp_path / "none.toml")])
    dest = tmp_path / "config" / "tierc_rules.toml"
    assert dest.read_bytes() == TEMPLATE.read_bytes()
    dest.write_text("# edited by Takahito\n", encoding="utf-8")
    cli.main(["init-config", "tierc-rules", "--config", str(tmp_path / "none.toml")])
    assert dest.read_text(encoding="utf-8") == "# edited by Takahito\n"
    assert "left unchanged" in capsys.readouterr().err


def test_init_tierc_rules_helper(tmp_path: Path) -> None:
    dest = tmp_path / "a" / "b.toml"
    assert cli.init_tierc_rules(dest) is True
    assert cli.init_tierc_rules(dest) is False


# --------------------------------------------------------------------------- hook


def test_entry_roles_are_exactly_the_three_roles() -> None:
    """msg-4382 Objection 1: existing ``Role`` members only; a new Role breaks this test so the
    entry decision is made explicitly."""
    assert TIERC_ENTRY_ROLES == {Role.PROPOSER, Role.IMPLEMENTER, Role.NAYSAYER} == set(Role)


def _m(i: int, author: str, body: str) -> ThreadMessage:
    return ThreadMessage(f"m{i}", author, body, None)


@pytest.mark.parametrize(
    ("seq", "expected"),
    [
        ([], 0),
        ([("Bohr", "design")], 0),
        ([("Bohr", "d"), ("Einstein", "Objection 1")], 1),
        ([("Bohr", "d"), ("Einstein", "o"), ("Bohr", "r"), ("Einstein", "o")], 2),
        (
            [
                ("Bohr", "d"),
                ("Einstein", "o"),
                ("Bohr", "r"),
                ("Einstein", "o"),
                ("Bohr", "r"),
                ("Einstein", "o"),
            ],
            3,
        ),
        # an APPROVE resets; later rounds count from 0
        (
            [
                ("Bohr", "d"),
                ("Einstein", "o"),
                ("Bohr", "r"),
                ("Einstein", "VERDICT: APPROVE"),
                ("Bohr", "d2"),
                ("Einstein", "o"),
            ],
            1,
        ),
        (
            [("Bohr", "d"), ("Einstein", "o"), ("Bohr", "r"), ("Einstein", "**VERDICT: APPROVE**")],
            0,
        ),
        # two naysayer messages in a row are one push-back; other authors are ignored
        ([("Bohr", "d"), ("Einstein", "o"), ("Einstein", "o2")], 1),
        ([("Bohr", "d"), ("Heisenberg", "impl"), ("Fermi", "x"), ("Einstein", "o")], 1),
    ],
)
def test_count_dispute_rounds(seq: list[tuple[str, str]], expected: int) -> None:
    msgs = [_m(i, a, b) for i, (a, b) in enumerate(seq)]
    assert count_dispute_rounds(ROSTER, msgs) == expected


def _decider_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(r.getMessage().split("decider_decision ", 1)[1])
        for r in caplog.records
        if r.getMessage().startswith("decider_decision ")
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("author", ["Bohr", "Heisenberg", "Einstein"])
async def test_hook_v2_three_roles_logged_with_rule_and_sha(
    author: str, caplog: pytest.LogCaptureFixture
) -> None:
    c = FakeClient(_payload())
    adapter = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: c, rules=RULES)
    msgs = [
        _m(1, "Bohr", "design\n\nNEXT: Einstein"),
        _m(2, "Einstein", "Objection 1\n\nNEXT: Bohr"),
        ThreadMessage("m3", author, "x\nTIER-C: goal\nNEXT: human", "human"),
    ]
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        dr = await run_tierc_hook(
            adapter,
            thread_id="T",
            round_index=3,
            roster=ROSTER,
            messages=msgs,
            stop="human",
            is_forced=False,
            target_role=None,
            spawn_blocked=False,
            naysayer_role=Role.NAYSAYER,
            author_requested_human=True,
        )
    assert dr is not None and dr.outcome is DecisionOutcome.EVALUATED
    (line,) = _decider_lines(caplog)
    assert line["matched_rule"] == "rule_2" and line["matched_rule_source"] == "choice"
    assert line["rules_sha256"] == RULES.sha256
    assert line["questions_version"] == "tierc-v3"
    assert line["verdict"]["scope"] == "in_gate"
    assert line["gate_kind"] is None and line["gate_is_grey_zone"] is False  # labelled ADMIT
    sent_state = json.loads(c.bodies[0]["state"])
    assert sent_state["dispute_rounds"] == 1


@pytest.mark.anyio
async def test_hook_v2_gate_exception_is_still_sent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """msg-4380 Δ2 (overriding msg-4196 DECIDED 1): gate raises → sent with gate_result null."""
    from spirrow_mindwire.decider import hook as hook_mod

    def boom(**kw: Any) -> Any:
        raise RuntimeError("gate down")

    monkeypatch.setattr(hook_mod, "decide_admission", boom)
    c = FakeClient(_payload())
    adapter = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: c, rules=RULES)
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        dr = await run_tierc_hook(
            adapter,
            thread_id="T",
            round_index=1,
            roster=ROSTER,
            messages=[ThreadMessage("m1", "Bohr", "x\nNEXT: human", "human")],
            stop="human",
            is_forced=False,
            target_role=None,
            spawn_blocked=False,
            naysayer_role=Role.NAYSAYER,
            author_requested_human=True,
        )
    assert dr is not None and len(c.bodies) == 1
    assert json.loads(c.bodies[0]["state"])["gate_result"] is None
    (line,) = _decider_lines(caplog)
    assert line["outcome"] == "evaluated" and line["gate_kind"] is None
    assert line["gate_is_grey_zone"] is None


@pytest.mark.anyio
async def test_hook_v2_off_roster_relay_not_entered(caplog: pytest.LogCaptureFixture) -> None:
    """msg-4361: pr-gate-relay's APPROVE → NEXT: human is a notification, not a question."""
    c = FakeClient(_payload())
    adapter = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: c, rules=RULES)
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        dr = await run_tierc_hook(
            adapter,
            thread_id="T",
            round_index=1,
            roster=ROSTER,
            messages=[
                ThreadMessage("m1", "pr-gate-relay", "VERDICT: APPROVE\nNEXT: human", "human")
            ],
            stop="human",
            is_forced=False,
            target_role=None,
            spawn_blocked=False,
            naysayer_role=Role.NAYSAYER,
            author_requested_human=True,
        )
    assert dr is None and c.bodies == [] and _decider_lines(caplog) == []


def test_rules_type_is_frozen() -> None:
    with pytest.raises(AttributeError):
        RULES.sha256 = "x"  # type: ignore[misc]
    assert isinstance(RULES, TierCRules)


# --------------------------------------------------------------------------- replay (v2)


def _load_replay() -> Any:
    import importlib.util
    import sys

    script = Path(__file__).resolve().parent.parent / "scripts" / "decider_replay.py"
    spec = importlib.util.spec_from_file_location("decider_replay_v2_test", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fixture(tmp_path: Path) -> Path:
    rows = [
        {
            "thread_id": "T",
            "round_index": 1,
            "roster": {"Bohr": "proposer"},
            "recent_events": [],
            "parsed_next": "human",
            "gate_result": {"verdict": "admit", "kind": None, "label": "goal"},
            "dispute_rounds": 2,
        },
        {"thread_id": "T", "round_index": 2, "roster": {}, "recent_events": []},
    ]
    f = tmp_path / "f.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return f


def test_replay_v2_dry_run_keeps_gateless_rows_in_gate(tmp_path: Path) -> None:
    replay = _load_replay()
    out = tmp_path / "o.jsonl"
    rc = replay.main(
        [
            "--track", "tierc", "--fixture", str(_fixture(tmp_path)), "--out", str(out),
            "--questions", "tierc-v2", "--rules", str(TEMPLATE),
        ]
    )  # fmt: skip
    assert rc == 0
    recs = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(recs) == 2  # the gate_result-less row is not skipped under v2
    assert {r["scope"] for r in recs} == {"in_gate"}
    assert {r["questions_version"] for r in recs} == {"tierc-v3"}
    assert {r["rules_sha256"] for r in recs} == {RULES.sha256}
    assert recs[0]["state"]["dispute_rounds"] == 2
    assert "dispute_rounds" not in recs[1]["state"]


def test_replay_v2_needs_rules_and_v1_is_unchanged(tmp_path: Path) -> None:
    replay = _load_replay()
    fx = str(_fixture(tmp_path))
    assert replay.main(["--track", "tierc", "--fixture", fx, "--questions", "tierc-v2"]) == 2
    assert replay.main(["--track", "tierc", "--fixture", fx, "--rules", str(TEMPLATE)]) == 2
    out = tmp_path / "v1.jsonl"
    assert replay.main(["--track", "tierc", "--fixture", fx, "--out", str(out)]) == 0
    recs = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(recs) == 1 and recs[0]["questions_version"] == "tierc-v1"
    assert "rules_sha256" not in recs[0]


def test_replay_v2_endpoint_snapshots_rules_and_records_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    replay = _load_replay()
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=_payload(p=0.2, choice="none"))

    real = replay.LexoraClient

    def patched(url: str, **kw: Any) -> Any:
        return real(url, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(replay, "LexoraClient", patched)
    out = tmp_path / "o.jsonl"
    snap = tmp_path / "snap"
    rc = replay.main(
        [
            "--track", "tierc", "--fixture", str(_fixture(tmp_path)), "--out", str(out),
            "--questions", "tierc-v2", "--rules", str(TEMPLATE), "--endpoint", "http://x",
            "--rules-snapshot-dir", str(snap),
        ]
    )  # fmt: skip
    assert rc == 0
    assert len(seen) == 2 and {b["questions_version"] for b in seen} == {"tierc-v3"}
    assert (snap / f"{RULES.sha256}.toml").read_bytes() == TEMPLATE.read_bytes()
    recs = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    for r in recs:
        d = r["decision"]
        assert d["outcome"] == "evaluated" and d["verdict"]["kind"] == "LIKELY_NOT"
        assert d["rules_sha256"] == RULES.sha256 and d["matched_rule"] == "none"
