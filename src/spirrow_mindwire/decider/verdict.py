"""Decider verdict 合成 — Tier-C 3+3 の純関数 (§4.4)。

**Scope**: Fermi msg-4066/4067 DECIDED による step 1 分担。 Track B の verdict
は本 module には入らない — Track B 追加 PR で ``evaluate_trackb`` を並置する。

**Design pointer**:

* §4.4 合成規則:

  * genuine=和: ``sum(genuine) >= genuine_min → CONFIRMED``
  * spurious=max: ``max(spurious) >= spurious_min かつ genuine < genuine_max
    → LIKELY_NOT``
  * それ以外は UNSURE。

* §3.4 ``[decider.thresholds]``: Tier-C の 3 閾値
  (``genuine_min`` / ``genuine_max`` / ``spurious_min``) が config で調整
  可能。 純関数 API では ``Thresholds`` dataclass として arg で受け取り、
  config との紐付けは caller (adapter / replay script) の責務にする。

* **scope 分離** (Fermi DECIDED #3): shadow モードでも ADMIT
  (有効ラベル付き) ターンの問いログを残す。 verdict 合成 (annotate / bounce
  判断) には流さない ∴ ``TierCVerdict.scope = "out_of_gate"`` を付けて
  ``evaluate_tierc`` を呼ばずに record only を返す helper (``build_out_of_gate_verdict``)
  を用意する。 caller (adapter / replay driver) は
  ``AdmissionGateResult.is_grey_zone`` を見て ``evaluate_tierc``
  (``in_gate``) と ``build_out_of_gate_verdict`` (``out_of_gate``) を
  使い分ける。 §6.3 の集計は ``scope`` で split する。

* D18 — grey-zone gating。 valid label 付きの ADMIT に対して verdict 合成
  を回さないのは caller 側で ``is_grey_zone`` を判定する責任。 本
  module は「与えられた 6 個の答え」を規則に従って合成するだけの純関数
  で、 gating の判定は行わない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from spirrow_mindwire.decider.questions import (
    TIERC_QUESTIONS_V1,
    TIERC_QUESTIONS_VERSION,
    TierCQuestionKind,
)

# §3.4 [decider.thresholds] の Tier-C 3 値の暫定 default。 config と揃える。
DEFAULT_GENUINE_MIN: Final[float] = 0.60
"""genuine 和がこの値以上で ``CONFIRMED`` (Tier-C 判定成立)。 v3.4 暫定値。"""

DEFAULT_GENUINE_MAX: Final[float] = 0.40
"""spurious 側が発火するためには genuine 和がこの値未満である必要がある。
LIKELY_NOT の入場ガード ∴ genuine 側とオーバーラップしないよう
``GENUINE_MAX < GENUINE_MIN`` を推奨。"""

DEFAULT_SPURIOUS_MIN: Final[float] = 0.60
"""spurious の max がこの値以上で ``LIKELY_NOT`` (Decider の bounce / annotate
入場条件)。 v3.4 暫定値。"""


# 問いセットから自動導出した genuine / spurious key の並び。
# caller は評価点を dict で渡し、本 module がこの並びで分類する。
#
# **tuple である理由 (Fermi msg-4087 DECIDED)**: frozenset[str] を iterate
# すると、Python の string hash randomization (PYTHONHASHSEED) が iteration
# 順に染み込み、``_extract_scores`` が組み立てる dict の insertion 順が
# process 依存でランダムになる。 spurious の tie-break (§4.4 の
# ``max(...)``) はその dict の走査順で決まるため、``fired_reason`` が
# 同じ入力に対して process ごとに変わる — §4.5 で ``fired_reason ==
# "answerable_from_thread"`` を bounce の入場条件に使っている以上、この
# 非決定性は replay と live で bounce が割れる致命的な穴だった (PR #337
# pr-gate BLOCKING correctness)。 tuple は宣言順で iterate される ∴
# hash 依存が構造的に入り込む余地が無い。
TIER_C_GENUINE_KEYS: tuple[str, ...] = tuple(
    q.key for q in TIERC_QUESTIONS_V1 if q.kind is TierCQuestionKind.GENUINE
)
TIER_C_SPURIOUS_KEYS: tuple[str, ...] = tuple(
    q.key for q in TIERC_QUESTIONS_V1 if q.kind is TierCQuestionKind.SPURIOUS
)

# spurious の tie-break で「``answerable_from_thread`` を負ける側に倒す」
# ためのラベル (Fermi msg-4087 DECIDED #2)。 §4.5 の bounce 入場条件で
# 特別扱いされる key で、同点時にこれを ``fired_reason`` にしてしまうと
# 「同点で bounce 側 (人に届かない側)」が発火しうる。 D2 単調性の精神で
# 「迷ったら人に届く側」に倒すため、この key は tie で負ける。
_TIER_C_BOUNCE_TRIGGER_SPURIOUS_KEY: Final[str] = "answerable_from_thread"


class TierCVerdictKind(StrEnum):
    """Tier-C 合成規則の 3 値 (§4.4)。

    * ``CONFIRMED`` — genuine 和が閾値以上 ∴ Tier-C 該当は本物と扱う。
    * ``LIKELY_NOT`` — spurious max が閾値以上かつ genuine が genuine_max
      未満 ∴ Tier-C 該当ではない可能性が高い (annotate / bounce の対象)。
    * ``UNSURE`` — いずれの条件も満たさない ∴ Decider は判断を保留し、
      admission-gate / caller にそのまま人へ届ける。
    """

    CONFIRMED = "CONFIRMED"
    LIKELY_NOT = "LIKELY_NOT"
    UNSURE = "UNSURE"


class TierCScope(StrEnum):
    """verdict record の scope tag (Fermi msg-4066 DECIDED #3)。

    * ``IN_GATE`` — grey zone (ADMIT_UNSURE / second_time_force_admit) で
      得られた verdict。 annotate / bounce の合成 (§3.3.b) に流れる。
    * ``OUT_OF_GATE`` — ADMIT (有効ラベル付き) や BOUNCED など grey zone
      外で shadow-only に記録された問いログ。 verdict 合成には一切流さず
      ∴ D18 の invariant (「grey zone 外は Decider に判断させない」) を保つ。
      §6.3 の evaluation では in_gate / out_of_gate を絶対に合算しない。
    """

    IN_GATE = "in_gate"
    OUT_OF_GATE = "out_of_gate"


@dataclass(frozen=True)
class TierCThresholds:
    """§3.4 [decider.thresholds] の Tier-C 3 値 (純関数の arg)。

    caller (adapter / replay script) が config から値を取り出してこの
    dataclass を組み立て、``evaluate_tierc`` に渡す。 config と純関数を
    分離することでテスト側は境界値をここで直接指定できる (Fermi
    DECIDED のテスト要件「verdict の合成規則の境界値」)。
    """

    genuine_min: float = DEFAULT_GENUINE_MIN
    genuine_max: float = DEFAULT_GENUINE_MAX
    spurious_min: float = DEFAULT_SPURIOUS_MIN

    def __post_init__(self) -> None:
        # genuine は 3 問の和 (§4.4) ∴ 論理上限は 3.0。
        # spurious は 3 問の max (§4.4) ∴ 論理上限は 1.0 —
        # ここを 3.0 で緩めると spurious_min > 1.0 が silently 受理され
        # LIKELY_NOT が到達不能になる (PR #337 pr-gate BLOCKING correctness)。
        for name, value in (
            ("genuine_min", self.genuine_min),
            ("genuine_max", self.genuine_max),
        ):
            if not 0.0 <= value <= 3.0:
                raise ValueError(
                    f"{name}={value} must be within [0.0, 3.0] "
                    "(genuine=sum of 3 noul values each in [0, 1])"
                )
        if not 0.0 <= self.spurious_min <= 1.0:
            raise ValueError(
                f"spurious_min={self.spurious_min} must be within [0.0, 1.0] "
                "(spurious=max of 3 noul values each in [0, 1]; "
                "a value > 1.0 makes LIKELY_NOT structurally unreachable)"
            )


@dataclass(frozen=True)
class TierCVerdict:
    """Tier-C verdict の合成結果 + Decider record 用の付帯情報。

    * ``kind`` — 3 値の合成結果。
    * ``genuine_score`` / ``spurious_score`` — 合成値 (debug / calibration
      / 集計用)。 caller は evaluation で使う。
    * ``fired_reason`` — LIKELY_NOT の根拠となった spurious key。
      §4.5 bounce 定義で ``answerable_from_thread`` かどうかを見るのに
      使う。 CONFIRMED / UNSURE では ``None``。
    * ``scope`` — in_gate / out_of_gate (§6.3 集計の split key)。
      ``build_out_of_gate_verdict`` で作った record は ``OUT_OF_GATE``。
    * ``questions_version`` — 問いセット version ("tierc-v1")。 log join
      key の一部 (D6)。
    """

    kind: TierCVerdictKind
    genuine_score: float
    spurious_score: float
    fired_reason: str | None
    scope: TierCScope
    questions_version: str = TIERC_QUESTIONS_VERSION


def _extract_scores(answers: Mapping[str, float], keys: tuple[str, ...]) -> dict[str, float]:
    """``answers`` から ``keys`` に含まれる key を選び、[0, 1] 範囲を検証する。

    未回答 key は KeyError で早期に fail (silent drop 禁止)。 範囲外は
    ValueError。 noul answer は「その命題が真である確からしさ」で
    [0, 1] に載る想定 (choice 系は本 module では扱わない)。

    ``keys`` は tuple (宣言順) で受け取る ∴ 返す dict の insertion 順も
    宣言順で安定する。 spurious の tie-break はこの順序に依存するため、
    ここに frozenset / set を再導入すると PR #337 pr-gate BLOCKING が
    再発する。
    """

    picked: dict[str, float] = {}
    for k in keys:
        if k not in answers:
            raise KeyError(
                f"answers missing required Tier-C question key: {k!r} "
                f"(expected all of: {list(keys)})"
            )
        v = answers[k]
        if not 0.0 <= v <= 1.0:
            raise ValueError(
                f"answers[{k!r}] = {v!r} out of range [0.0, 1.0] "
                "(noul answers represent probability the proposition holds)"
            )
        picked[k] = v
    return picked


def _spurious_argmax(spurious_answers: dict[str, float]) -> tuple[str, float]:
    """spurious の arg-max + score を **決定的な** tie-break で返す。

    tie-break の優先順位 (max() の tuple key の順、大きいほうが勝つ):

    1. ``score`` (高いほうが勝つ) — §4.4 の合成規則そのもの。
    2. ``key != "answerable_from_thread"`` (True が勝つ) — §4.5 で
       ``answerable_from_thread`` は bounce の入場条件 ∴ 同点でこれを
       ``fired_reason`` にすると同点で bounce 側 (人に届かない側) が
       発火しうる。 D2 単調性の精神で「迷ったら人に届く側」に倒す
       (Fermi msg-4087 DECIDED #2)。
    3. ``-declaration_index`` (小さい index が勝つ) — 非
       ``answerable_from_thread`` 同士の同点は宣言順で先の key を選ぶ。
       宣言順は ``TIERC_QUESTIONS_V1`` = ``TIER_C_SPURIOUS_KEYS`` で
       固定 ∴ ``PYTHONHASHSEED`` に依存せず replay / live が一致する
       (PR #337 pr-gate BLOCKING correctness の是正の中核)。

    """

    def sort_key(item: tuple[str, float]) -> tuple[float, bool, int]:
        key, score = item
        return (
            score,
            key != _TIER_C_BOUNCE_TRIGGER_SPURIOUS_KEY,
            -TIER_C_SPURIOUS_KEYS.index(key),
        )

    fired_key, spurious_score = max(spurious_answers.items(), key=sort_key)
    return fired_key, spurious_score


def evaluate_tierc(
    answers: Mapping[str, float],
    thresholds: TierCThresholds | None = None,
) -> TierCVerdict:
    """Tier-C 3+3 の答えを規則で合成 (§4.4)。

    Args:
        answers: 6 個の noul 回答 (``changes_goal_or_spec`` / ``incurs_cost`` /
            ``irreversible`` / ``answerable_from_thread`` / ``is_permission_seeking`` /
            ``is_review_disposition``)。 各値は [0, 1]。
        thresholds: §3.4 [decider.thresholds] の Tier-C 3 値。 None なら
            v3.4 暫定 default を使う。

    Returns:
        ``TierCVerdict`` (``kind ∈ CONFIRMED / LIKELY_NOT / UNSURE``、
        scope=``IN_GATE``、questions_version=``"tierc-v1"``)。

    合成規則:
        * ``genuine_score = sum(genuine 3 値)`` (§4.4)
        * ``spurious_score = max(spurious 3 値)`` (§4.4 — correlated な
          外延のため sum / 平均は避ける)
        * ``genuine_score >= genuine_min`` → ``CONFIRMED``
        * それ以外で ``spurious_score >= spurious_min`` かつ
          ``genuine_score < genuine_max`` → ``LIKELY_NOT`` (``fired_reason``
          は arg-max の spurious key)
        * それ以外 → ``UNSURE``

    純関数: I/O 無し、clock 無し、モジュール状態を持たない。 config /
    転送先の adapter は caller 側の責務。 本 API は step 2 で
    ``decider_lexora`` から呼ばれ、step 1 では replay driver の dry-run
    でも使える (問いへの回答 fixture を与えれば境界値検証ができる)。

    D18 との関係: 本関数を呼ぶかどうかの gating (grey zone or not) は
    caller の責務で、本関数は「渡された 6 個の答えから verdict を出す」
    だけの純関数。 shadow モードで ADMIT (有効ラベル付き) ターンの
    問いログを残したい場合は ``build_out_of_gate_verdict`` を使い、
    本関数は grey zone に限って呼ぶ。
    """

    th = thresholds if thresholds is not None else TierCThresholds()

    genuine_answers = _extract_scores(answers, TIER_C_GENUINE_KEYS)
    spurious_answers = _extract_scores(answers, TIER_C_SPURIOUS_KEYS)

    genuine_score = sum(genuine_answers.values())
    # spurious_answers は空にならない (TIER_C_SPURIOUS_KEYS が空でないことを
    # ``TIERC_QUESTIONS_V1`` の shape 不変条件でテストする)。 tie-break は
    # ``_spurious_argmax`` の docstring 参照 — hash 依存を持ち込まないため
    # 素の ``max(..., key=lambda kv: kv[1])`` は使わない。
    fired_key, spurious_score = _spurious_argmax(spurious_answers)

    if genuine_score >= th.genuine_min:
        return TierCVerdict(
            kind=TierCVerdictKind.CONFIRMED,
            genuine_score=genuine_score,
            spurious_score=spurious_score,
            fired_reason=None,
            scope=TierCScope.IN_GATE,
        )

    if spurious_score >= th.spurious_min and genuine_score < th.genuine_max:
        return TierCVerdict(
            kind=TierCVerdictKind.LIKELY_NOT,
            genuine_score=genuine_score,
            spurious_score=spurious_score,
            fired_reason=fired_key,
            scope=TierCScope.IN_GATE,
        )

    return TierCVerdict(
        kind=TierCVerdictKind.UNSURE,
        genuine_score=genuine_score,
        spurious_score=spurious_score,
        fired_reason=None,
        scope=TierCScope.IN_GATE,
    )


def build_out_of_gate_verdict(
    answers: Mapping[str, float],
) -> TierCVerdict:
    """shadow-only の ADMIT ターン問いログを作る (Fermi DECIDED #3)。

    grey zone 外 (ADMIT with valid label など、``is_grey_zone == False``
    な ADMIT ターン) の記録用。 verdict 合成は行わず、単に scores を
    埋めた ``TierCVerdict(scope=OUT_OF_GATE, kind=UNSURE)`` を返す。

    D18 の invariant を維持するため:

    * ``kind = UNSURE`` (annotate / bounce の対象にならない)
    * ``fired_reason = None`` (bounce の入場条件 ``answerable_from_thread``
      が発火しない)
    * ``scope = OUT_OF_GATE`` (§6.3 集計で in_gate と絶対に合算しない
      ためのタグ)

    この record が §3.3.b の Conductor フックで annotate / bounce に
    流れる経路は無い — caller (adapter / replay) が gating で
    ``evaluate_tierc`` を呼ばずに本 helper を呼ぶ運用にする。

    Rationale (Fermi msg-4066 DECIDED #3): A-post の n 不足 (§6.2) への
    対処。 ADMIT ターンで Jev が LIKELY_NOT を出す率 ≈ 誤 bounce リスク
    の実測値そのもの ∴ bounce 投入判断の材料として shadow でだけ記録
    しておく価値が高い。 shadow のみ ∴ 誤 bounce リスクは発生しない。
    """

    genuine_answers = _extract_scores(answers, TIER_C_GENUINE_KEYS)
    spurious_answers = _extract_scores(answers, TIER_C_SPURIOUS_KEYS)

    genuine_score = sum(genuine_answers.values())
    # 同じ ``_spurious_argmax`` を使い、tuple 定数の一本化 (Fermi msg-4087
    # DECIDED #4) と合わせて集計側 (§6.3) の score が非決定的に揺れないようにする。
    # scope=OUT_OF_GATE の record は fired_reason を捨てて None 固定にする
    # (D18 の invariant — grey zone 外は annotate / bounce の判断材料にしない) ため
    # ここで得た key は使わない。
    _, spurious_score = _spurious_argmax(spurious_answers)

    return TierCVerdict(
        kind=TierCVerdictKind.UNSURE,
        genuine_score=genuine_score,
        spurious_score=spurious_score,
        fired_reason=None,
        scope=TierCScope.OUT_OF_GATE,
    )


__all__ = [
    "DEFAULT_GENUINE_MAX",
    "DEFAULT_GENUINE_MIN",
    "DEFAULT_SPURIOUS_MIN",
    "TIER_C_GENUINE_KEYS",
    "TIER_C_SPURIOUS_KEYS",
    "TierCScope",
    "TierCThresholds",
    "TierCVerdict",
    "TierCVerdictKind",
    "build_out_of_gate_verdict",
    "evaluate_tierc",
]
