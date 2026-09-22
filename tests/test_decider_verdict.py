"""Decider verdict 合成規則の境界値テスト (Fermi msg-4066 テスト要件)。

**Scope**: §4.4 の合成規則を純関数 ``evaluate_tierc`` について正確に検証する。
scope=out_of_gate の record (``build_out_of_gate_verdict``) は verdict
合成に影響しない invariant も確認する。

* genuine = sum, spurious = max
* CONFIRMED: genuine_score >= genuine_min
* LIKELY_NOT: spurious_score >= spurious_min かつ genuine_score < genuine_max
* UNSURE: それ以外
* fired_reason: LIKELY_NOT のときのみ populated (arg-max spurious key)
* scope: evaluate_tierc → IN_GATE 固定、build_out_of_gate_verdict → OUT_OF_GATE
"""

from __future__ import annotations

import pytest

from spirrow_mindwire.decider.verdict import (
    TIER_C_GENUINE_KEYS,
    TIER_C_SPURIOUS_KEYS,
    TierCScope,
    TierCThresholds,
    TierCVerdictKind,
    build_out_of_gate_verdict,
    evaluate_tierc,
)


def _all_answers(
    *,
    changes_goal_or_spec: float = 0.0,
    incurs_cost: float = 0.0,
    irreversible: float = 0.0,
    answerable_from_thread: float = 0.0,
    is_permission_seeking: float = 0.0,
    is_review_disposition: float = 0.0,
) -> dict[str, float]:
    """テスト向け helper — 6 問すべてに default 値を与える。"""
    return {
        "changes_goal_or_spec": changes_goal_or_spec,
        "incurs_cost": incurs_cost,
        "irreversible": irreversible,
        "answerable_from_thread": answerable_from_thread,
        "is_permission_seeking": is_permission_seeking,
        "is_review_disposition": is_review_disposition,
    }


# ---------------------------------------------------------------------------
# key 集合の shape
# ---------------------------------------------------------------------------


def test_genuine_and_spurious_keys_partition_the_six_questions() -> None:
    """genuine / spurious 集合は空でなく、互いに重ならず、和で 6 問。"""
    assert TIER_C_GENUINE_KEYS
    assert TIER_C_SPURIOUS_KEYS
    assert TIER_C_GENUINE_KEYS.isdisjoint(TIER_C_SPURIOUS_KEYS)
    assert len(TIER_C_GENUINE_KEYS) + len(TIER_C_SPURIOUS_KEYS) == 6


# ---------------------------------------------------------------------------
# CONFIRMED
# ---------------------------------------------------------------------------


def test_confirmed_when_genuine_sum_reaches_min() -> None:
    """genuine 3 問の和 = 0.6 == genuine_min → CONFIRMED。"""
    answers = _all_answers(changes_goal_or_spec=0.2, incurs_cost=0.2, irreversible=0.2)
    verdict = evaluate_tierc(answers)  # default thresholds: genuine_min=0.60
    assert verdict.kind is TierCVerdictKind.CONFIRMED
    assert verdict.fired_reason is None
    assert verdict.genuine_score == pytest.approx(0.6)
    assert verdict.scope is TierCScope.IN_GATE


def test_confirmed_takes_precedence_over_likely_not() -> None:
    """genuine >= genuine_min が最優先、 spurious 側は無関係。"""
    answers = _all_answers(
        changes_goal_or_spec=0.5,
        incurs_cost=0.5,
        irreversible=0.5,
        answerable_from_thread=1.0,  # spurious も MAX だが CONFIRMED が勝つ
    )
    verdict = evaluate_tierc(answers)
    assert verdict.kind is TierCVerdictKind.CONFIRMED
    assert verdict.fired_reason is None


def test_confirmed_boundary_genuine_min_minus_epsilon_falls_through() -> None:
    """境界値: genuine_min 直前ならUNSURE / LIKELY_NOT 側 (>= の等号は CONFIRMED)。"""
    # genuine_sum = 0.59 < genuine_min = 0.60 → CONFIRMED 発火せず
    answers = _all_answers(changes_goal_or_spec=0.20, incurs_cost=0.20, irreversible=0.19)
    verdict = evaluate_tierc(answers)
    assert verdict.kind is not TierCVerdictKind.CONFIRMED


# ---------------------------------------------------------------------------
# LIKELY_NOT
# ---------------------------------------------------------------------------


def test_likely_not_when_spurious_max_meets_min_and_genuine_below_genuine_max() -> None:
    """spurious max = 0.7 >= 0.60 かつ genuine_sum = 0.0 < 0.40 → LIKELY_NOT。"""
    answers = _all_answers(answerable_from_thread=0.7)
    verdict = evaluate_tierc(answers)
    assert verdict.kind is TierCVerdictKind.LIKELY_NOT
    assert verdict.fired_reason == "answerable_from_thread"
    assert verdict.spurious_score == pytest.approx(0.7)


def test_likely_not_fired_reason_is_arg_max_spurious() -> None:
    """spurious 3 問のうち max を出した key が fired_reason に載る。"""
    answers = _all_answers(
        answerable_from_thread=0.6,
        is_permission_seeking=0.85,  # ← max
        is_review_disposition=0.7,
    )
    verdict = evaluate_tierc(answers)
    assert verdict.kind is TierCVerdictKind.LIKELY_NOT
    assert verdict.fired_reason == "is_permission_seeking"


def test_likely_not_blocked_when_genuine_meets_genuine_max() -> None:
    """genuine_sum >= genuine_max だが < genuine_min → UNSURE (LIKELY_NOT で無い)。"""
    # genuine_max = 0.40 (default) — 到達しても LIKELY_NOT に落ちない
    answers = _all_answers(
        changes_goal_or_spec=0.4,  # genuine_sum = 0.4 == genuine_max
        answerable_from_thread=0.9,  # spurious は閾値超え
    )
    verdict = evaluate_tierc(answers)
    assert verdict.kind is TierCVerdictKind.UNSURE
    assert verdict.fired_reason is None


def test_likely_not_boundary_spurious_min_minus_epsilon() -> None:
    """spurious max = 0.59 < 0.60 → LIKELY_NOT でない (UNSURE)。"""
    answers = _all_answers(answerable_from_thread=0.59)
    verdict = evaluate_tierc(answers)
    assert verdict.kind is TierCVerdictKind.UNSURE


# ---------------------------------------------------------------------------
# UNSURE
# ---------------------------------------------------------------------------


def test_unsure_when_all_answers_zero() -> None:
    """全 0 → CONFIRMED でも LIKELY_NOT でもない → UNSURE。"""
    verdict = evaluate_tierc(_all_answers())
    assert verdict.kind is TierCVerdictKind.UNSURE
    assert verdict.fired_reason is None


# ---------------------------------------------------------------------------
# 閾値の上書き
# ---------------------------------------------------------------------------


def test_custom_thresholds_shift_boundary() -> None:
    """caller が Thresholds を差し込むと境界が動く (config → thresholds の分離)。"""
    strict = TierCThresholds(genuine_min=0.9, genuine_max=0.1, spurious_min=0.9)
    # genuine_sum = 0.6, spurious max = 0.5 → strict thresholds 下では UNSURE
    verdict = evaluate_tierc(
        _all_answers(
            changes_goal_or_spec=0.2,
            incurs_cost=0.2,
            irreversible=0.2,
            answerable_from_thread=0.5,
        ),
        thresholds=strict,
    )
    assert verdict.kind is TierCVerdictKind.UNSURE


# ---------------------------------------------------------------------------
# 入力検証
# ---------------------------------------------------------------------------


def test_missing_answer_raises_key_error() -> None:
    """silent drop 禁止 — 未回答 key は KeyError で早期 fail。"""
    partial = _all_answers()
    del partial["irreversible"]
    with pytest.raises(KeyError, match="irreversible"):
        evaluate_tierc(partial)


def test_out_of_range_answer_raises_value_error() -> None:
    """noul answer は [0, 1] — 範囲外は ValueError。"""
    bad = _all_answers(incurs_cost=1.5)
    with pytest.raises(ValueError, match=r"out of range"):
        evaluate_tierc(bad)


def test_negative_answer_raises_value_error() -> None:
    """負値も範囲外。"""
    bad = _all_answers(is_permission_seeking=-0.1)
    with pytest.raises(ValueError, match=r"out of range"):
        evaluate_tierc(bad)


def test_thresholds_out_of_range_raise() -> None:
    """genuine 系は [0, 3]、spurious 系は [0, 1]。 3.0 超えは即 fail。"""
    with pytest.raises(ValueError):
        TierCThresholds(genuine_min=3.1)


# ---------------------------------------------------------------------------
# scope=out_of_gate helper (D18 invariant)
# ---------------------------------------------------------------------------


def test_out_of_gate_verdict_never_fires_annotate_or_bounce() -> None:
    """shadow-only の ADMIT ターン record は kind=UNSURE / fired_reason=None を保つ。

    たとえ答えが CONFIRMED / LIKELY_NOT を出す形でも、 helper は verdict
    合成を行わず、 scope=out_of_gate として annotate / bounce 対象外にする
    (Fermi DECIDED #3、 D18 の invariant)。
    """
    answers = _all_answers(
        changes_goal_or_spec=1.0,
        incurs_cost=1.0,
        irreversible=1.0,
        answerable_from_thread=1.0,
    )
    verdict = build_out_of_gate_verdict(answers)
    assert verdict.kind is TierCVerdictKind.UNSURE
    assert verdict.fired_reason is None
    assert verdict.scope is TierCScope.OUT_OF_GATE
    # scores は集計用に埋まる (§6.3 の evaluation で使える)。
    assert verdict.genuine_score == pytest.approx(3.0)
    assert verdict.spurious_score == pytest.approx(1.0)
