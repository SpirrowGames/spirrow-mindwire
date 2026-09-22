"""Decider 問いセット — Tier-C v1 のみ (Fermi msg-4066/4067 DECIDED #2)。

**Scope**: step 1 は Tier-C 3+3 問いだけを実装する。 Track B の 2 問
(``handoff_valid`` / ``made_progress``, §4.7) は後の PR で ``TRACKB_QUESTIONS_V1``
として別 set で追加する — Tier-C の set version は Track B の追加で動かない
(Fermi DECIDED #2 の拡張性担保)。

**Design pointer**:

* §4.1 — Tier-C genuine 3 問 (``changes_goal_or_spec`` / ``incurs_cost`` /
  ``irreversible``)
* §4.2 — Tier-C spurious 3 問 (``answerable_from_thread`` / ``is_permission_seeking`` /
  ``is_review_disposition``)
* D5 — 問いはコードで持ちバージョン付与しログに残す
* D8 — 3+3+0 の合成規則: genuine=和 / spurious=max (実装は
  ``spirrow_mindwire.decider.verdict``)

**Version 文字列は set 単位** (Fermi DECIDED #2)。 ``/v1/decide`` の
``questions_version`` field には ``"tierc-v1"`` をそのまま渡す。 Track B
の set 追加時は ``"trackb-v1"`` が別 set 単位の version として並ぶ ∴
Tier-C の version を bump しない。 T-decide-endpoint 側の schema にも
「1 リクエスト = 1 set」で対応する。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TierCQuestionKind(StrEnum):
    """§4 の genuine / spurious 二分類 (D8 の合成規則で意味を持つ)。

    * ``GENUINE`` — Tier-C 該当を積み上げる側 (和で合成)。
    * ``SPURIOUS`` — Tier-C ではない証拠を集める側 (max で合成、
      correlated な外延を持つため sum / 平均は避ける — §4.4)。
    """

    GENUINE = "genuine"
    SPURIOUS = "spurious"


@dataclass(frozen=True)
class TierCQuestion:
    """1 問分の定義。

    * ``key`` — /v1/decide の payload 上の question 識別子。安定した
      機械可読 ID (snake_case)。
    * ``kind`` — genuine / spurious。
    * ``prompt`` — LLM / Jev に渡す自然言語の問い (日本語)。 §4.1 / §4.2
      の説明文をそのまま採る。
    """

    key: str
    kind: TierCQuestionKind
    prompt: str


# ---------------------------------------------------------------------------
# Tier-C 問いセット v1 — §4.1 / §4.2 の逐語 (v3.4 設計書より)。
# ---------------------------------------------------------------------------

TIERC_QUESTIONS_VERSION = "tierc-v1"
"""Tier-C 問いセットの set 単位 version 文字列 (Fermi DECIDED #2)。

/v1/decide への request body で ``questions_version`` として送る。
Track B 問い ``TRACKB_QUESTIONS_V1`` が後で足されても本 version は
動かない — 別 set の version は独立に管理する。
"""


TIERC_QUESTIONS_V1: tuple[TierCQuestion, ...] = (
    # ---------------- genuine 3 問 (§4.1) ----------------
    TierCQuestion(
        key="changes_goal_or_spec",
        kind=TierCQuestionKind.GENUINE,
        prompt=("このハンドオフが承認を要する変更(goal / spec の書き換え)を含むか"),
    ),
    TierCQuestion(
        key="incurs_cost",
        kind=TierCQuestionKind.GENUINE,
        prompt="このハンドオフが cost を発生させる決定を含むか",
    ),
    TierCQuestion(
        key="irreversible",
        kind=TierCQuestionKind.GENUINE,
        prompt=("このハンドオフが取り消せない操作(データ削除・公開リリース)を含むか"),
    ),
    # ---------------- spurious 3 問 (§4.2) ----------------
    TierCQuestion(
        key="answerable_from_thread",
        kind=TierCQuestionKind.SPURIOUS,
        prompt="スレッド内の既存情報から答えが導けるか",
    ),
    TierCQuestion(
        key="is_permission_seeking",
        kind=TierCQuestionKind.SPURIOUS,
        prompt=("権限を求めているだけで、判断そのものは著者ができる状態か"),
    ),
    TierCQuestion(
        key="is_review_disposition",
        kind=TierCQuestionKind.SPURIOUS,
        prompt=(
            "レビュー結果への disposition (「異論なし、進めてよいか」型) で、実質は自律進行が正解か"
        ),
    ),
)


__all__ = [
    "TIERC_QUESTIONS_V1",
    "TIERC_QUESTIONS_VERSION",
    "TierCQuestion",
    "TierCQuestionKind",
]
