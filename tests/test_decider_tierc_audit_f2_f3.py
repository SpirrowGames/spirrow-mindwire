"""T-jev-tierc-audit-2026-10-12 F2 / F3 (Bohr msg-6860, endorsed by Einstein msg-6861 onward).

* F2 — ``head_summary`` is the head message's ask window (``tail-v1``): ``M`` stays 500, the
  closing block is dropped and the tail is taken on a paragraph boundary; ``recent_events``
  keep the first ``M`` chars; the log rows carry ``head_window``.
* F3 — the ``should_ask_human`` frame carries the two standing-policy exclusions after the
  fix-now one, the rules template does not, the proceed frame is untouched, and the version is
  ``tierc-v4``.

F1 (``count_dispute_rounds``) is tested next to the old cases in ``test_decider_tierc_v2.py``.
"""

from __future__ import annotations

from spirrow_mindwire.decider.hook import (
    BODY_HEAD_M,
    ThreadMessage,
    head_window,
    turn_from_messages,
)
from spirrow_mindwire.decider.questions import (
    SHOULD_ASK_HUMAN_KEY,
    TIERC_ESCALATION_QUESTIONS_VERSION,
    TIERC_V2_PROCEED_QUESTIONS_VERSION,
    load_tierc_rules,
    tierc_rules_template_path,
    tierc_v2_questions,
)
from spirrow_mindwire.value_objects import Role

TEMPLATE = tierc_rules_template_path()
RULES = load_tierc_rules(TEMPLATE)
ROSTER = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER, "Heisenberg": Role.IMPLEMENTER}

MERGE_EXCLUSION = (
    "main への merge や gate の再発火を依頼するだけのハンドオフは、"
    "それだけではどの条にも当たらない(merge される中身が仕様を変える場合は、その中身で判断する)。"
)
CAPABILITY_EXCLUSION = (
    "role の権限や道具が足りないこと(指名できない、コマンドが無い、guard に止められた等)を"
    "理由に人を呼ぶハンドオフは、どの条にも当たらない。"
)
FIX_NOW_EXCLUSION = (
    "ただし、指摘を今の PR で直すか follow-up PR で直すかの"
    "順序・タイミングだけを問うハンドオフは、"
    "どの条にも当たらない(修正自体が仕様の追加・削除・変更を伴う場合は、その中身で判断する)。"
)

ENDORSE = "I explicitly endorse the approach. " * 30  # ~1050 chars, one paragraph
ASK = "## 裁定のお願い\n\nceiling を戻すか、ruleset で作り直すかを決めてください。A / B。"
TRAILER = (
    "TIER-C: goal\nNEXT: human\n\n"
    "<!-- source: tools=3 · mcp=0 · setting_sources=empty · route=unset · tier=x -->\n"
    "<!-- attest: tier=naysayer · backend=gemini · expected=gemini · route=r · probe=p -->"
)


# --- F2: head_window ---------------------------------------------------------------------------


def test_short_body_is_passed_whole_as_before() -> None:
    body = "short ask\nTIER-C: cost\nNEXT: human"
    assert head_window(body) == body


def test_ask_after_a_long_endorsement_reaches_the_window() -> None:
    """msg-6286 shape: endorsement first, then the ask — the first 500 chars held no ask."""
    content = f"{ENDORSE}\n\n{ASK}\n{TRAILER}"
    assert ASK not in content[:BODY_HEAD_M]
    got = head_window(content)
    assert len(got) <= BODY_HEAD_M
    assert got.startswith("## 裁定のお願い")  # whole trailing paragraphs, boundary-aligned
    assert "A / B" in got
    assert "TIER-C: goal" in got  # TIER-C: is part of the ask
    assert "NEXT:" not in got and "<!--" not in got


def test_closing_block_is_dropped_and_the_rest_passed_whole_when_it_fits() -> None:
    body = "x" * 300
    content = body + "\n\nNEXT: human\nSTOP: done\nOPERATOR-TASK: t\nTIER-C-CHECK: none\n\n"
    content += "<!-- source: a -->\n" * 20
    assert len(content) > BODY_HEAD_M
    assert head_window(content) == body


def test_longest_run_of_whole_trailing_paragraphs() -> None:
    paras = [f"p{i} " + "y" * 150 for i in range(6)]  # ~155 chars each
    content = "\n\n".join(paras) + "\n\nNEXT: human"
    got = head_window(content)
    assert got == "\n\n".join(paras[-3:])  # 3 * 155 + 4 <= 500 < 4 * 155 + 6


def test_one_overlong_last_paragraph_gives_its_last_m_chars() -> None:
    last = "z" * 400 + "THE ASK"
    content = "a" * 200 + "\n\n" + "b" * 100 + last + "\nNEXT: human"
    got = head_window(content)
    assert len(got) == BODY_HEAD_M and got.endswith("THE ASK")


def test_a_next_line_mid_body_is_not_the_closing_block() -> None:
    content = "NEXT: earlier quote\n" + "w" * 600
    assert head_window(content) == content[-BODY_HEAD_M:]


def test_turn_uses_the_window_for_head_summary_only() -> None:
    content = f"{ENDORSE}\n\n{ASK}\n{TRAILER}"
    msgs = [
        ThreadMessage("m1", "Bohr", "design", "Einstein"),
        ThreadMessage("m2", "Bohr", content, "human"),
    ]
    turn = turn_from_messages(thread_id="T", round_index=2, roster=ROSTER, messages=msgs)
    assert turn.head_summary == head_window(content)
    # recent_events stay the first M chars (context only, msg-6860 F2)
    assert turn.recent_events[0].body_head == content[:BODY_HEAD_M]
    assert turn.recent_events[1].body_head == "design"


# --- F3: frame sentences and version -------------------------------------------------------------


def test_v4_frame_carries_both_standing_policy_exclusions_after_fix_now() -> None:
    instructions = tierc_v2_questions(RULES)[SHOULD_ASK_HUMAN_KEY]["instructions"]
    assert instructions.endswith(FIX_NOW_EXCLUSION + MERGE_EXCLUSION + CAPABILITY_EXCLUSION)
    template = TEMPLATE.read_text(encoding="utf-8")
    assert MERGE_EXCLUSION not in template and CAPABILITY_EXCLUSION not in template
    proceed = tierc_v2_questions(RULES, proceed=True)[SHOULD_ASK_HUMAN_KEY]["instructions"]
    assert "merge" not in proceed and "guard" not in proceed
    assert TIERC_ESCALATION_QUESTIONS_VERSION == "tierc-v4"
    assert TIERC_V2_PROCEED_QUESTIONS_VERSION == "tierc-v2-proceed"
