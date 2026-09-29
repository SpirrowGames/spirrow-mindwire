"""Decider 問いセット — Tier-C v1 (Fermi msg-4066/4067 DECIDED #2) と v2 (五ヶ条、末尾の節)。

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

import hashlib
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


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


# ---------------------------------------------------------------------------
# Tier-C 問いセット v2 — 五ヶ条 (T-decider-tierc-v2-all-escalations msg-4360 / 4361 / 4380-4384)
# ---------------------------------------------------------------------------
#
# v1 (上) は replay 比較用に残す。 v2 は問いが 2 本:
#
# * ``should_ask_human`` (noul) — instructions = 枠の文 + 五ヶ条の箇条書き + 問い。 verdict は
#   この確率 1 本だけで合成する (``verdict.evaluate_tierc_v2``)。
# * ``matched_rule`` (choice) — 当たったルール 1 つ (``rule_1``..``rule_N``) か ``none``。
#   通知 / ログの理由表示用で、verdict 合成には使わない (msg-4360)。
#   Lexora ``/v1/decide`` は ``type: "choice"`` を受け、選択肢は ``criteria`` の
#   ``[{name, description}]`` から読む (spirrow-lexora main 344d467 の ``decide/contract.py``
#   ``QuestionSpec`` / ``decide/providers.py`` ``_extract_choice_options``)
#   ∴ msg-4382 の経路 (a)。 noul 5 問への代替 (経路 (b)) は作らない。
#
# **何がどこにあるか (msg-4382 / msg-4384).** ルールの文言は Takahito が編集する 1 ファイル
# (``<data_dir>/config/tierc_rules.toml``、雛形は同梱の ``tierc_rules.default.toml``) にあり、その
# sha256 が ``rules_sha256`` として記録される。問いの枠の文はここ (コード) にあり、
# ``TIERC_V2_QUESTIONS_VERSION`` に含まれる。枠の文を変えたら version を上げる。

TIERC_V2_QUESTIONS_VERSION = "tierc-v2"
"""v2 問いセットの set 単位 version。ルール文言の編集では動かない (``rules_sha256`` が区別する)。"""

SHOULD_ASK_HUMAN_KEY = "should_ask_human"
MATCHED_RULE_KEY = "matched_rule"
MATCHED_RULE_NONE = "none"
"""``matched_rule`` の「どのルールにも当たらない」選択肢。"""

TIERC_V2_SHOULD_ASK_CRITERIA: dict[str, str] = {
    "true": "五ヶ条のいずれかに当たる",
    "false": "五ヶ条のどれにも当たらない",
}
"""msg-4361 の criteria (逐語)。"""

_V2_RULES_HEADER = "次の五ヶ条は、AI エージェントが人間(Takahito)に問うべきものを定めている。"
_V2_SHOULD_ASK_QUESTION = (
    "このハンドオフ(NEXT: human)は、五ヶ条のいずれかに当たり、人間に問うべきものか。"
)
_V2_MATCHED_RULE_INSTRUCTIONS = (
    "このハンドオフ(NEXT: human)が当たる五ヶ条のルールを 1 つ選べ。"
    "複数に当たるなら最もよく当たるもの、どれにも当たらなければ none。"
)
_V2_NONE_DESCRIPTION = "五ヶ条のどれにも当たらない"

_RULE_ID_RE = re.compile(r"^rule_[1-9][0-9]*$")

TIERC_RULES_TEMPLATE_NAME = "tierc_rules.default.toml"
"""同梱の雛形 (パッケージデータ)。実行時のフォールバックには使わない
(正本の二重化を防ぐ、msg-4382)。"""


class TierCRulesError(ValueError):
    """ルールファイルが無い / パースできない / 形が不正。
    ``build_decider`` が起動を拒否する理由。"""


@dataclass(frozen=True)
class TierCRule:
    """五ヶ条の 1 条。 ``note`` は任意の補足 (rule_5 の往復数の目安など)。"""

    id: str
    text: str
    note: str | None = None


@dataclass(frozen=True)
class TierCRules:
    """読み込んだルールファイル。
    ``sha256`` はファイルのバイト列の hex digest (``rules_sha256``)。"""

    rules: tuple[TierCRule, ...]
    sha256: str
    source: str

    @property
    def matched_rule_options(self) -> tuple[str, ...]:
        """``matched_rule`` の選択肢: 宣言順の rule id と ``none``。"""
        return (*(r.id for r in self.rules), MATCHED_RULE_NONE)


def parse_tierc_rules(data: bytes, *, source: str) -> TierCRules:
    """ルールファイルのバイト列 → :class:`TierCRules`。形が不正なら :class:`TierCRulesError`。

    検証: ``[[rule]]`` が 1 件以上、各 ``id`` は ``rule_<正の整数>`` で一意、``text`` は空でない
    文字列、``note`` は省略か空でない文字列、未知のキーは拒否 (typo を黙って捨てない)。
    """
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise TierCRulesError(f"{source}: cannot parse tierc rules: {exc}") from exc
    extra = set(doc) - {"rule"}
    if extra:
        raise TierCRulesError(f"{source}: unknown top-level keys {sorted(extra)}")
    raw_rules: Any = doc.get("rule")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise TierCRulesError(f"{source}: needs at least one [[rule]] table")
    rules: list[TierCRule] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            raise TierCRulesError(f"{source}: rule #{i + 1} is not a table")
        unknown = set(raw) - {"id", "text", "note"}
        if unknown:
            raise TierCRulesError(f"{source}: rule #{i + 1} has unknown keys {sorted(unknown)}")
        rid, text, note = raw.get("id"), raw.get("text"), raw.get("note")
        if not isinstance(rid, str) or not _RULE_ID_RE.match(rid):
            raise TierCRulesError(f"{source}: rule #{i + 1} id {rid!r} is not rule_<number>")
        if rid in seen:
            raise TierCRulesError(f"{source}: duplicate rule id {rid!r}")
        seen.add(rid)
        if not isinstance(text, str) or not text.strip():
            raise TierCRulesError(f"{source}: rule {rid} needs a non-empty text")
        if note is not None and (not isinstance(note, str) or not note.strip()):
            raise TierCRulesError(f"{source}: rule {rid} note must be a non-empty string")
        rules.append(TierCRule(id=rid, text=text.strip(), note=note.strip() if note else None))
    return TierCRules(rules=tuple(rules), sha256=hashlib.sha256(data).hexdigest(), source=source)


def load_tierc_rules(path: Path) -> TierCRules:
    """``path`` を 1 回読んで :class:`TierCRules` に。
    無い / 読めない / 不正は :class:`TierCRulesError`。

    起動時に 1 回だけ呼ぶ (msg-4384: hot-reload はしない)。文言を直したら conductor を再起動する。
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TierCRulesError(
            f"tierc rules file {path} is not readable ({exc}); "
            "place it with `mindwire init-config tierc-rules`"
        ) from exc
    return parse_tierc_rules(data, source=str(path))


def tierc_rules_template_path() -> Path:
    """同梱の雛形のパス (``init-config`` がコピー元に使う)。"""
    return Path(__file__).with_name(TIERC_RULES_TEMPLATE_NAME)


def render_rules_bullets(rules: TierCRules) -> str:
    """五ヶ条の箇条書き (``- rule_k: text`` + 任意の ``note`` 行)。"""
    lines: list[str] = []
    for r in rules.rules:
        lines.append(f"- {r.id}: {r.text}")
        if r.note is not None:
            lines.append(f"  - {r.note}")
    return "\n".join(lines)


def tierc_v2_questions(rules: TierCRules) -> dict[str, dict[str, Any]]:
    """v2 の ``questions`` (``/v1/decide`` の wire 形、宣言順)。

    ``matched_rule`` の instructions には五ヶ条の全文を繰り返さない — 各条の文言は選択肢の
    ``description`` が持つ (Einstein の最終レビュー advisory 1 の趣旨:
    同じ全文を問いごとに重ねない)。
    """
    should_ask = f"{_V2_RULES_HEADER}\n{render_rules_bullets(rules)}\n\n{_V2_SHOULD_ASK_QUESTION}"
    options = [{"name": r.id, "description": r.text} for r in rules.rules]
    options.append({"name": MATCHED_RULE_NONE, "description": _V2_NONE_DESCRIPTION})
    return {
        SHOULD_ASK_HUMAN_KEY: {
            "type": "noul",
            "instructions": should_ask,
            "criteria": dict(TIERC_V2_SHOULD_ASK_CRITERIA),
        },
        MATCHED_RULE_KEY: {
            "type": "choice",
            "instructions": _V2_MATCHED_RULE_INSTRUCTIONS,
            "criteria": options,
        },
    }


__all__ = [
    "MATCHED_RULE_KEY",
    "MATCHED_RULE_NONE",
    "SHOULD_ASK_HUMAN_KEY",
    "TIERC_QUESTIONS_V1",
    "TIERC_QUESTIONS_VERSION",
    "TIERC_RULES_TEMPLATE_NAME",
    "TIERC_V2_QUESTIONS_VERSION",
    "TIERC_V2_SHOULD_ASK_CRITERIA",
    "TierCQuestion",
    "TierCQuestionKind",
    "TierCRule",
    "TierCRules",
    "TierCRulesError",
    "load_tierc_rules",
    "parse_tierc_rules",
    "render_rules_bullets",
    "tierc_rules_template_path",
    "tierc_v2_questions",
]
