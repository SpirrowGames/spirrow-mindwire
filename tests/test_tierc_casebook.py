"""T-tierc-overpass-self-contradicting-escalations PR1 — the self-negation detector and the
casebook's decision rule (Bohr msg-6664 §2 / §4, revised by msg-6666)."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.decider.questions import (
    SHOULD_ASK_HUMAN_KEY,
    load_tierc_rules,
    tierc_rules_template_path,
)

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"{name}_module", ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{name}_module"] = mod
    spec.loader.exec_module(mod)
    return mod


sn = _load("tierc_selfneg")
cb = _load("tierc_casebook")


# ---------------------------------------------------------------------------
# detector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ("This escalation is premature.", "en_escalation_is_premature"),
        ("a premature escalation, really", "en_premature_escalation"),
        ("We do not need a human here", "en_not_need_human"),
        ("it does not need Takahito", "en_not_need_human"),
        ("no human judgment is required", "en_no_human_decision_needed"),
        ("no human judgement needed", "en_no_human_decision_needed"),
        ("no human decision is needed", "en_no_human_decision_needed"),
        ("This is not a Tier-C call", "en_not_tier_c"),
        ("Takahitoの判断は不要です", "ja_human_judgement_unneeded"),
        ("人の判断は要らない", "ja_human_judgement_unneeded"),
        ("人の判断はいらない", "ja_human_judgement_unneeded"),
        ("このエスカレーションは時期尚早", "ja_escalation_unneeded"),
        ("エスカレーションは不要", "ja_escalation_unneeded"),
        ("これは Tier-C ではない", "ja_not_tier_c"),
        ("これは Tier-C に当たらない", "ja_not_tier_c"),
    ],
)
def test_each_fixed_pattern_hits(text: str, pattern: str) -> None:
    hits = sn.detect_self_negation(text)
    assert [h.pattern for h in hits] == [pattern]
    assert hits[0].line_no == 1


def test_case_insensitive() -> None:
    assert sn.detect_self_negation("THIS ESCALATION IS PREMATURE")


@pytest.mark.parametrize(
    "text",
    [
        "We do not need a goal change",  # msg-6606's own wording: not a pattern
        "This is a Tier-C decision",
        "Tier-Cではない",  # the pattern has a literal space; not extended (msg-6664 §2)
        "the escalation is not premature" + "",  # no pattern phrase
        "",
    ],
)
def test_non_patterns_do_not_hit(text: str) -> None:
    assert sn.detect_self_negation(text) == []


def test_pattern_list_is_the_fixed_one() -> None:
    """msg-6664 §2: the list is fixed in this PR; a change has to show up in review."""
    assert [p for _, p in sn.SELFNEG_PATTERNS] == [
        r"escalation is premature",
        r"premature escalation",
        r"(do|does) not need (a )?(human|Takahito)",
        r"no human (decision|judg(e)?ment) (is )?(needed|required)",
        r"not (a )?Tier-C",
        r"(人|Takahito)の判断は(不要|要らない|いらない)",
        r"エスカレーションは(不要|時期尚早)",
        r"Tier-C (ではない|に当たらない)",
    ]


@pytest.mark.parametrize(
    "line",
    [
        "NEXT: human — not a Tier-C",
        "TIER-C: goal (not a Tier-C really)",
        "RETRY: escalation is premature",
        "TIER-C-CHECK: none, not a Tier-C",
        "<!-- escalation is premature -->",
        "   <!-- not a Tier-C -->   ",
    ],
)
def test_excluded_lines_are_skipped(line: str) -> None:
    assert sn.detect_self_negation(line) == []


def test_exclusion_is_column_zero_only() -> None:
    """Only line-level rules; an indented handoff-looking line is searched."""
    assert sn.detect_self_negation("  NEXT: escalation is premature")


def test_comment_with_trailing_text_is_searched() -> None:
    assert sn.detect_self_negation("<!-- x --> escalation is premature")


@pytest.mark.parametrize(
    "body",
    [
        "> This escalation is premature.",
        "```\nThis escalation is premature.\n```",
        "~~~\nThis escalation is premature.\n~~~",
        "    This escalation is premature.",
        "`This escalation is premature.`",
    ],
)
def test_markdown_is_not_interpreted(body: str) -> None:
    """msg-6666 Objection 1: quotes and code are NOT removed — a hit there is counted."""
    assert sn.detect_self_negation(body)


def test_line_numbers_and_two_patterns_on_one_line() -> None:
    hits = sn.detect_self_negation("ok\nescalation is premature; not a Tier-C\nNEXT: human")
    assert [(h.line_no, h.pattern) for h in hits] == [
        (2, "en_escalation_is_premature"),
        (2, "en_not_tier_c"),
    ]


def test_looks_is_display_only_guess() -> None:
    lines = ["> quoted", "```", "in fence", "```", "    indented", "plain"]
    assert [sn.looks_quoted_or_fenced(lines, i) for i in range(1, 7)] == [
        "quote",
        "",
        "fence",
        "fence",  # the closing fence line counts as inside
        "indent",
        "",
    ]


# ---------------------------------------------------------------------------
# casebook build helpers
# ---------------------------------------------------------------------------


def _state(head: str, label: str = "goal") -> str:
    return json.dumps({"head_summary": head, "gate_result": {"label": label}})


def test_logged_row_for_requires_exactly_one() -> None:
    rows = [
        {"thread_id": "T", "latest_msg_id": "msg-1", "state_wire": "{}"},
        {"thread_id": "T", "latest_msg_id": "msg-2"},  # no point-in-time input
        {"thread_id": "U", "latest_msg_id": "msg-1", "state_wire": "{}"},
    ]
    assert cb.logged_row_for(rows, "T", "msg-1") is rows[0]
    with pytest.raises(cb.CasebookError):
        cb.logged_row_for(rows, "T", "msg-2")
    with pytest.raises(cb.CasebookError):
        cb.logged_row_for([*rows, rows[0]], "T", "msg-1")


def test_decider_rows_parses_log_lines() -> None:
    lines = [
        'INFO:x:decider_decision {"thread_id": "T"}',
        "INFO:x:something else",
        "INFO:x:decider_decision {broken",
    ]
    assert cb.decider_rows(lines) == [{"thread_id": "T"}]


def _truth(**kw: Any) -> dict[str, Any]:
    base = {
        "project": "p",
        "thread": "T",
        "msg_id": "msg-9",
        "truth": "spurious",
        "failure_type": "self_negation",
        "evidence": ["msg-1"],
    }
    return base | kw


def test_casebook_row_copies_logged_input_and_locates_negation() -> None:
    body = "x" * 600 + " This escalation is premature. more"
    wire = _state(body[:500])
    logged = {
        "state_wire": wire,
        "raw_answers": {SHOULD_ASK_HUMAN_KEY: {"noul": 0.58}},
        "verdict": {"kind": "UNSURE"},
        "decision_id": "d",
    }
    row = cb.casebook_row(
        _truth(negation_sentence="This escalation is premature."),
        logged,
        {"author": "Einstein", "content": body, "timestamp": "t"},
    )
    assert row["jev_input"] == wire  # copied, not rebuilt
    assert row["ask_score"] == 0.58
    assert row["label"] == "goal"
    assert row["truth_source"] == "audit-operator"
    assert row["negation_offset"] == 601
    assert row["negation_in_jev_input"] is False
    assert row["head_is_body_prefix"] is True


def test_casebook_row_negation_inside_head() -> None:
    body = "This escalation is premature. rest"
    row = cb.casebook_row(
        _truth(negation_sentence="This escalation is premature."),
        {"state_wire": _state(body), "raw_answers": {}},
        {"content": body},
    )
    assert row["negation_in_jev_input"] is True


def test_casebook_row_missing_sentence_stops() -> None:
    with pytest.raises(cb.CasebookError):
        cb.casebook_row(
            _truth(negation_sentence="absent"), {"state_wire": _state("x")}, {"content": "x"}
        )


def test_consensus_matches_report_rule() -> None:
    a = [
        {"thread_id": "T", "round_index": 1, "label": "genuine"},
        {"thread_id": "T", "round_index": 2, "label": "genuine", "category": "OTHER"},
        {"thread_id": "T", "round_index": 3, "label": "spurious"},
        {"thread_id": "T", "round_index": 4, "label": "genuine"},
    ]
    b = [
        {"thread_id": "T", "round_index": 1, "label": "genuine"},
        {"thread_id": "T", "round_index": 2, "label": "genuine"},
        {"thread_id": "T", "round_index": 3, "label": "genuine"},
    ]
    assert cb.consensus_of([a, b]) == {
        ("T", 1): "genuine",
        ("T", 2): "ambiguous",
        ("T", 3): "ambiguous",
        ("T", 4): "ambiguous",
    }


def test_is_role_next_human() -> None:
    roster = ["Bohr", "Einstein"]
    assert cb.is_role_next_human("einstein", "x\nNEXT: human", roster)
    assert not cb.is_role_next_human("operator", "x\nNEXT: human", roster)
    assert not cb.is_role_next_human("Bohr", "x\nNEXT: Einstein", roster)
    assert not cb.is_role_next_human("Bohr", "NEXT: human\nNEXT: Einstein", roster)


# ---------------------------------------------------------------------------
# measurement B request
# ---------------------------------------------------------------------------


def _rules() -> Any:
    return load_tierc_rules(tierc_rules_template_path())


def test_v4_candidate_differs_by_the_one_sentence_only() -> None:
    rules = _rules()
    v3 = cb.questions_for(cb.V3, rules)
    v4 = cb.questions_for(cb.V4_CANDIDATE, rules)
    assert v3.keys() == v4.keys()
    for k in v3:
        if k != SHOULD_ASK_HUMAN_KEY:
            assert v3[k] == v4[k]
    a, b = v3[SHOULD_ASK_HUMAN_KEY], v4[SHOULD_ASK_HUMAN_KEY]
    assert b["instructions"] == a["instructions"] + cb.V4_CANDIDATE_SENTENCE
    assert {x: y for x, y in a.items() if x != "instructions"} == {
        x: y for x, y in b.items() if x != "instructions"
    }


def test_request_sends_the_given_state_verbatim() -> None:
    wire = '{"head_summary":"h"}'
    body = cb.request_for(wire, cb.V4_CANDIDATE, _rules())
    assert body["state"] == wire
    assert body["questions_version"] == cb.V4_CANDIDATE
    assert body["policy"] == "mindwire.replay.tierc"


def test_plan_is_seeded_and_complete() -> None:
    p1 = cb.plan(["a", "b"], 3, 7)
    assert p1 == cb.plan(["a", "b"], 3, 7)
    assert len(p1) == 12
    assert {(s.run, s.msg_id, s.variant) for s in p1} == {
        (r, m, v) for r in (1, 2, 3) for m in ("a", "b") for v in cb.VARIANTS
    }
    assert [s.run for s in p1] == sorted(s.run for s in p1)


# ---------------------------------------------------------------------------
# decision rule (msg-6666 §4)
# ---------------------------------------------------------------------------


CASEBOOK: list[dict[str, Any]] = [
    {"msg_id": "g1", "truth": "genuine", "failure_type": "none"},
    {"msg_id": "g2", "truth": "genuine", "failure_type": "none"},
    {
        "msg_id": "s",
        "truth": "spurious",
        "failure_type": "self_negation",
        "negation_in_jev_input": True,
        "negation_offset": 10,
    },
    {"msg_id": "f", "truth": "spurious", "failure_type": "false_premise"},
]


def _jev(
    scores: Mapping[str, Sequence[float | None]], variant: str = "tierc-v4-candidate"
) -> list[Any]:
    return [
        {"msg_id": m, "variant": variant, "run": i + 1, "score": s}
        for m, ss in scores.items()
        for i, s in enumerate(ss)
    ]


def _sn(**hit: bool) -> list[dict[str, Any]]:
    rows = [
        {"set": "i", "msg_id": m, "hits": [{}] if hit.get(m) else []}
        for m in ("g1", "g2", "s", "f")
    ]
    rows.append({"set": "ii", "msg_id": "x", "thread": "T", "hits": [{}] if hit.get("ii") else []})
    rows.append(
        {"set": "iii", "msg_id": "y", "thread": "T", "hits": [{}] if hit.get("iii") else []}
    )
    return rows


B_OK: dict[str, list[float | None]] = {
    "s": [0.2, 0.3, 0.39],
    "g1": [0.4, 0.5, 0.9],
    "g2": [0.6, 0.6, 0.6],
    "f": [0.9, 0.9, 0.9],
}


def test_b_wins_and_discards_a() -> None:
    d = cb.decide(CASEBOOK, _jev(B_OK), _sn(s=True))
    assert (d.outcome, d.b_holds, d.a_holds) == ("B", True, None)


def test_b_reads_only_the_candidate_variant() -> None:
    d = cb.decide(CASEBOOK, _jev(B_OK, variant="tierc-v3"), _sn())
    assert d.outcome == "observe"


@pytest.mark.parametrize(
    "change",
    [
        {"s": [0.2, 0.3, 0.40]},  # 0.40 is not < 0.40
        {"g1": [0.4, 0.39, 0.9]},  # a genuine row drops under
        {"s": [0.2, 0.3]},  # a missing run
        {"g2": [0.6, None, 0.6]},  # an errored run
    ],
)
def test_b_fails_on_any_run(change: dict[str, list[float | None]]) -> None:
    d = cb.decide(CASEBOOK, _jev(B_OK | change), _sn(s=True))
    assert d.outcome == "A" and d.b_holds is False and d.a_holds is True


def test_b_cannot_hold_when_negation_outside_input() -> None:
    book = [dict(r) for r in CASEBOOK]
    book[2]["negation_in_jev_input"] = False
    d = cb.decide(book, _jev(B_OK), _sn(s=True))
    assert d.outcome == "A"
    assert any("input range" in r for r in d.reasons)


def test_a_needs_a_hit_on_the_self_negation_row() -> None:
    assert cb.decide(CASEBOOK, [], _sn()).outcome == "observe"


@pytest.mark.parametrize("veto", ["g1", "ii"])
def test_a_vetoed_by_any_genuine_hit(veto: str) -> None:
    d = cb.decide(CASEBOOK, [], _sn(s=True, **{veto: True}))
    assert d.outcome == "observe" and d.a_holds is False


def test_a_ignores_set_iii_and_false_premise_hits() -> None:
    """Set (iii) is displayed, not a criterion; the false-premise row is not genuine."""
    assert cb.decide(CASEBOOK, [], _sn(s=True, iii=True, f=True)).outcome == "A"


# ---------------------------------------------------------------------------
# redaction (public repository) and original-state recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "redacted"),
    [
        ("route=10.1.2.3:8110 ·", "route=<ipv4>:8110 ·"),
        ("a 192.168.0.1 b", "a <ipv4> b"),
        ("C:\\Users\\someone\\x", "C:\\Users\\<user>\\x"),
        ("C:\\\\Users\\\\someone\\\\x", "C:\\\\Users\\\\<user>\\\\x"),
        ("C:/Users/someone/x", "C:/Users/<user>/x"),
        ("version 1.2.3 and msg-6606", "version 1.2.3 and msg-6606"),
        ("1.2.3.4.5", "1.2.3.4.5"),
    ],
)
def test_redact_infra(raw: str, redacted: str) -> None:
    assert cb.redact_infra(raw) == redacted


def test_casebook_row_redacts_but_keeps_original_hash() -> None:
    body = "route 10.0.0.9:8110"
    wire = _state(body)
    row = cb.casebook_row(_truth(), {"state_wire": wire}, {"content": body})
    assert "10.0.0.9" not in row["jev_input"]
    assert "10.0.0.9" not in row["body"]
    assert row["jev_input_sha256"] == cb.sha256_text(wire)
    assert row["body_sha256"] == cb.sha256_text(body)


def test_original_state_round_trip_and_mismatch() -> None:
    wire = _state("at 10.0.0.9")
    logged = [{"thread_id": "T", "latest_msg_id": "msg-9", "state_wire": wire}]
    row = cb.casebook_row(_truth(), logged[0], {"content": "x"})
    assert cb.original_state(row, logged) == wire
    with pytest.raises(cb.CasebookError):
        cb.original_state(row | {"jev_input_sha256": "0" * 64}, logged)
    with pytest.raises(cb.CasebookError):
        cb.original_state(row | {"jev_input": wire}, logged)
