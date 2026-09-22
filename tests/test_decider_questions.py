"""Decider 問いセット v1 のテスト (Fermi msg-4066 テスト要件「questions の version 固定」)。

step 1 で確定させたい invariant:

* ``TIERC_QUESTIONS_VERSION == "tierc-v1"`` — Track B 追加時にこの値が
  動かないことを保証する (Fermi DECIDED #2 の set 単位 version)。
* Tier-C の問い数は 3 + 3 = 6 (§4.1 / §4.2、D8)。
* genuine / spurious の分類が §4.1 / §4.2 と一致する。
* key はすべて互いに一意 (dict / dataclass 上の key 衝突を防ぐ)。
"""

from __future__ import annotations

from spirrow_mindwire.decider.questions import (
    TIERC_QUESTIONS_V1,
    TIERC_QUESTIONS_VERSION,
    TierCQuestionKind,
)


def test_tierc_version_is_pinned() -> None:
    """version string は set 単位で固定 (Fermi DECIDED #2)。"""
    assert TIERC_QUESTIONS_VERSION == "tierc-v1"


def test_tierc_v1_has_three_genuine_and_three_spurious() -> None:
    """D8 の 3+3+0 shape。"""
    genuine = [q for q in TIERC_QUESTIONS_V1 if q.kind is TierCQuestionKind.GENUINE]
    spurious = [q for q in TIERC_QUESTIONS_V1 if q.kind is TierCQuestionKind.SPURIOUS]
    assert len(genuine) == 3
    assert len(spurious) == 3
    assert len(TIERC_QUESTIONS_V1) == 6


def test_tierc_v1_genuine_keys_match_spec() -> None:
    """§4.1 の 3 問と key が一致する。"""
    genuine_keys = {q.key for q in TIERC_QUESTIONS_V1 if q.kind is TierCQuestionKind.GENUINE}
    assert genuine_keys == {"changes_goal_or_spec", "incurs_cost", "irreversible"}


def test_tierc_v1_spurious_keys_match_spec() -> None:
    """§4.2 の 3 問と key が一致する。"""
    spurious_keys = {q.key for q in TIERC_QUESTIONS_V1 if q.kind is TierCQuestionKind.SPURIOUS}
    assert spurious_keys == {
        "answerable_from_thread",
        "is_permission_seeking",
        "is_review_disposition",
    }


def test_tierc_v1_keys_are_unique() -> None:
    """key 衝突は log join key を壊す (D6)。"""
    keys = [q.key for q in TIERC_QUESTIONS_V1]
    assert len(keys) == len(set(keys))


def test_tierc_v1_prompts_are_nonempty() -> None:
    """prompt 空文字は fixture の malformed record と区別できない ∴ ban。"""
    for q in TIERC_QUESTIONS_V1:
        assert q.prompt.strip(), f"question {q.key!r} has empty prompt"
