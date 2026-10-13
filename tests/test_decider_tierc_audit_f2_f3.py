"""T-jev-tierc-audit-2026-10-12 F2'' / F3 (Bohr msg-6871 / msg-6873 / msg-6875 / msg-6877, endorsed
by Einstein).

* F2'' — the role writes ``ASK: <one line>`` directly above ``TIER-C:`` (the handoff guidance);
  the decider moves the **last** such line to the front of ``head_summary``, 500 chars in total;
  without one the summary is ``content[:500]`` as before. ``recent_events`` are unchanged.
* F3 — the ``should_ask_human`` frame carries the merge-request exclusion after the fix-now one,
  and nothing else (the permission sentence was dropped, msg-6871 §3); the version is
  ``tierc-v4``; ``scripts/tierc_casebook.py`` keeps asking the pinned v3 frame.

F1 (``count_dispute_rounds``) is tested next to the old cases in ``test_decider_tierc_v2.py``;
the ``ask_line`` log column in ``test_decider_tierc_v2.py`` / ``test_d4_guardrails.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.conductor.handoff import (
    _tier_c_label_above_last_next,
    build_handoff_protocol_block,
)
from spirrow_mindwire.decider.hook import (
    ASK_LINE_MAX,
    BODY_HEAD_M,
    ThreadMessage,
    compute_gate_result,
    extract_ask,
    head_summary_for,
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
from spirrow_mindwire.tier_c_admission_gate import AdmissionVerdict
from spirrow_mindwire.value_objects import Role

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = tierc_rules_template_path()
RULES = load_tierc_rules(TEMPLATE)
ROSTER = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER, "Heisenberg": Role.IMPLEMENTER}

LONG = "I explicitly endorse the approach. " * 30  # ~1050 chars before the ask
TRAILER = "\nTIER-C: goal\nNEXT: human"

MERGE_EXCLUSION = (
    "main への merge や gate の再発火を依頼するだけのハンドオフは、"
    "それだけではどの条にも当たらない(merge される中身が仕様を変える場合は、その中身で判断する)。"
)
FIX_NOW_EXCLUSION = (
    "ただし、指摘を今の PR で直すか follow-up PR で直すかの"
    "順序・タイミングだけを問うハンドオフは、"
    "どの条にも当たらない(修正自体が仕様の追加・削除・変更を伴う場合は、その中身で判断する)。"
)


# --- F2'': the ASK: line ------------------------------------------------------------------------


def test_ask_line_near_the_end_of_a_long_body_leads_the_summary() -> None:
    """msg-6873 row 1 / msg-6875: red without ``re.MULTILINE`` (the line is not the body start)."""
    content = f"{LONG}\n\nASK: B と C のどちらにするか{TRAILER}"
    summary, ask_line = head_summary_for(content)
    prefix = "ASK: B と C のどちらにするか\n"
    assert ask_line is True
    assert summary == prefix + content[: BODY_HEAD_M - len(prefix)]
    assert len(summary) == BODY_HEAD_M


def test_the_last_ask_line_wins() -> None:
    content = f"ASK: first\n{LONG}\nASK: second{TRAILER}"
    assert extract_ask(content) == "second"
    assert head_summary_for(content)[0].startswith("ASK: second\nASK: first\n")


def test_an_overlong_ask_line_is_capped_and_the_total_stays_500() -> None:
    content = f"{LONG}\nASK: {'x' * 450}{TRAILER}"
    ask = extract_ask(content)
    assert ask == "x" * ASK_LINE_MAX
    summary, _ = head_summary_for(content)
    assert len(summary) <= BODY_HEAD_M
    assert summary.startswith(f"ASK: {'x' * ASK_LINE_MAX}\n")


def test_no_ask_line_is_the_old_input_and_never_raises() -> None:
    """msg-6873 row 4 / msg-6877 (Einstein msg-6876: ``findall(...)[-1]`` on ``[]``)."""
    content = f"{LONG}{TRAILER}"
    assert extract_ask(content) is None
    assert head_summary_for(content) == (content[:BODY_HEAD_M], False)
    assert head_summary_for("") == ("", False)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("ASK: B と C のどちらにするか", "B と C のどちらにするか"),
        ("ASK:　B と C のどちらにするか", "B と C のどちらにするか"),  # U+3000 (msg-6877)
        ("ASK:B と C", "B と C"),  # no space
        ("ASK:  B と C  \t", "B と C"),  # trailing whitespace dropped
        ("ASK:   ", None),  # empty (msg-6875)
        ("ASK:", None),
        ("ASK\uff1aB と C", None),  # U+FF1A full-width colon — not the fixed form (msg-6877)
        ("> ASK: B と C", None),  # quoted (msg-6875)
        ("**ASK:** B と C", None),  # emphasised (msg-6875)
        ("the reply mentions `ASK:` inline", None),  # not at line start (msg-6873)
        ("  ASK: indented", None),
    ],
)
def test_only_the_fixed_form_line_matches(line: str, expected: str | None) -> None:
    content = f"{LONG}\n{line}{TRAILER}"
    assert extract_ask(content) == expected
    assert head_summary_for(content)[1] is (expected is not None)


def test_an_empty_ask_line_does_not_swallow_the_next_line() -> None:
    """``[^\\S\\n]`` excludes the newline, so an empty ``ASK:`` never reads ``TIER-C: goal``."""
    assert extract_ask(f"{LONG}\nASK:\nTIER-C: goal\nNEXT: human") is None


def test_turn_uses_the_ask_for_head_summary_only() -> None:
    content = f"{LONG}\n\nASK: B か C か{TRAILER}"
    msgs = [
        ThreadMessage("m1", "Bohr", "design", "Einstein"),
        ThreadMessage("m2", "Einstein", content, "human"),
    ]
    turn = turn_from_messages(thread_id="T", round_index=2, roster=ROSTER, messages=msgs)
    assert turn.head_summary == head_summary_for(content)[0]
    assert turn.recent_events[0].body_head == content[:BODY_HEAD_M]  # unchanged
    assert turn.recent_events[1].body_head == "design"


# --- F2'': the writer side ----------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.PROPOSER, Role.IMPLEMENTER, Role.NAYSAYER])
def test_every_escalating_role_is_told_to_write_the_ask_line(role: Role) -> None:
    block = build_handoff_protocol_block(role)
    assert "write `ASK: <what you need the human to decide, in one line>`" in block
    assert "directly above the `TIER-C:` line" in block


def test_a_reply_written_as_instructed_keeps_its_label_and_passes_the_gate() -> None:
    """The ``ASK:`` line sits above ``TIER-C:``, so the label reader (line above the last
    ``NEXT:``) and the admission gate see the same body as before."""
    body = f"{LONG}\n\nASK: B か C か{TRAILER}"
    assert _tier_c_label_above_last_next(body) == "goal"
    gate = compute_gate_result(body=body, author="Einstein", now=datetime(2026, 10, 13, tzinfo=UTC))
    assert gate is not None and gate.verdict is AdmissionVerdict.ADMIT
    assert extract_ask(body) == "B か C か"


# --- F3: frame and version ----------------------------------------------------------------------


def test_v4_frame_is_v3_plus_the_merge_exclusion_only() -> None:
    instructions = tierc_v2_questions(RULES)[SHOULD_ASK_HUMAN_KEY]["instructions"]
    assert instructions.endswith(FIX_NOW_EXCLUSION + MERGE_EXCLUSION)
    assert "権限" not in instructions and "guard" not in instructions  # dropped (msg-6871 §3)
    assert MERGE_EXCLUSION not in TEMPLATE.read_text(encoding="utf-8")
    proceed = tierc_v2_questions(RULES, proceed=True)[SHOULD_ASK_HUMAN_KEY]["instructions"]
    assert "merge" not in proceed
    assert TIERC_ESCALATION_QUESTIONS_VERSION == "tierc-v4"
    assert TIERC_V2_PROCEED_QUESTIONS_VERSION == "tierc-v2-proceed"


def _casebook() -> Any:
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "tierc_casebook_f3", ROOT / "scripts" / "tierc_casebook.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_casebook_keeps_asking_the_pinned_v3_frame() -> None:
    """msg-6871 §4: the casebook's v3 no longer follows the live frame."""
    cb = _casebook()
    assert cb.V3 == "tierc-v3"
    v3 = cb.questions_for(cb.V3, RULES)[SHOULD_ASK_HUMAN_KEY]["instructions"]
    assert v3.endswith(FIX_NOW_EXCLUSION)
    assert MERGE_EXCLUSION not in v3
    live = tierc_v2_questions(RULES)[SHOULD_ASK_HUMAN_KEY]["instructions"]
    assert live == v3 + MERGE_EXCLUSION  # v4 differs from v3 by that sentence only
