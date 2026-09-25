"""Decider state — ``DecisionState`` と ``state_builder``(§3.2 / §3.5)。

**Scope**: Fermi msg-4066/4067 の step 1 分担。adapter (`decider_lexora.py`) と
Conductor フック配線は step 2 扱い ∴ ここには入らない。本 module は Turn 形
の in-memory input から Decider に渡す純データを組み立てるだけ。

**Design pointer**:

* §3.2 の ``DecisionState`` フィールド一覧
* §3.5 の in-memory ``turn.gate_result`` 契約 — JSONL からの live join 禁止
  (dual-management / distribution shift の 2 重回避、D17)
* D14 — ``TIER_C_LABELS`` は ``tier_c_admission_gate.ADMIT_LABELS`` を
  そのまま再 export し文字列二重管理を避ける

**Einstein msg-4067 advisory への disposition**: naysayer は step 1 の直前で
「``state_builder`` に ``gate_result`` を copy させると LLM 側 payload を
汚す」との architectural advisory を出した。同 msg で **non-blocking** と
明記されている ∴ 本 PR では設計 v3.4 §3.2 (`DecisionState.gate_result`)
と Fermi msg-4066 の DECIDED (「state_builder が `gate_result` を turn から
コピー」がテスト要件) をそのまま採る。 step 1 時点ではここに「送信 payload
から `gate_result` を除外する責務は step 2 の adapter に置く」と書いていたが、
step 2 の Bohr msg-4180 §2-1 は replay の serialize (``gate_result`` を含む) を
そのまま ``decider/wire.py`` に移すと決めた (設計 v3.4 §6.4 も ``gate_result``
を feature として扱う)。 ∴ 現行の wire は ``gate_result`` を含む — 詳細と
変更点は ``decider/wire.py`` の docstring を参照。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from spirrow_mindwire.tier_c_admission_gate import (
    ADMIT_LABELS,
    AdmissionVerdict,
    BounceReason,
    LogKind,
    RetryAdmitReason,
)
from spirrow_mindwire.value_objects import Role

# D14 — 二重管理禁止。``TIER_C_LABELS`` を新たに定義せず、
# ``tier_c_admission_gate.ADMIT_LABELS`` をそのまま再 export する。
TIER_C_LABELS = ADMIT_LABELS


@dataclass(frozen=True)
class EventSummary:
    """直近ターンの 1 event 分の要約(§3.2)。

    author / ``NEXT:`` パース結果 / 本文先頭 M 文字。M / N は §10 open
    question として v3.4 で暫定値 (N=5, M=500 chars)。本 module は値を
    受け取るだけで cap しない — 呼び出し側 (Conductor / replay driver)
    が方針に従って cap する。
    """

    msg_id: str
    author: str
    parsed_next: str | None
    body_head: str


@dataclass(frozen=True)
class DiffStat:
    """implementer / PR ターンの diff 統計(§3.2)。

    Track B の ``made_progress`` 判定入力。Tier-C 単独の step 1 では
    使わないが shape は将来のために持たせる。
    """

    files_changed: int
    insertions: int
    deletions: int


@dataclass(frozen=True)
class AdmissionGateResult:
    """admission-gate → Decider の in-memory 受け渡し contract (§3.5, D17)。

    ``tier_c_admission_gate.decide_admission`` の返り値から必要な要素だけ
    を保持する compact な dataclass。JSONL からの live join は禁止 ∴
    live Conductor は ``decide_admission`` の直後にこの型を組み立てて
    ``turn.gate_result`` に載せる。 replay driver も同じ shape を fixture
    として作って ``turn`` に載せる。 Decider から見た state は live / replay
    で同一 shape に揃う (distribution shift 回避)。

    * ``kind`` — ``LogKind`` の集合のうち Decider が grey-zone gating に
      使う値: ``ADMIT_UNSURE`` / ``BOUNCED`` / ``RETRY_ADMIT`` に加え、
      通常の ADMIT (LABEL_MIGRATION / 単純 admit) を表す ``None`` 相当
      として、admission-gate が log を発しない R1-admit の場合は
      ``kind = None`` かつ ``label`` が有効ラベルを持つ形にする。
    * ``verdict`` — ``admit`` / ``bounce``。 grey-zone gating (§3.3.b、D18)
      は ``verdict is ADMIT`` を前提にする。
    * ``label`` — post-legacy-normalisation の正規化済みラベル
      (``ADMIT_LABELS`` の値 / ``unsure:goal?`` / ``None``)。
    * ``retry_admit_reason`` — ``kind == RETRY_ADMIT`` のときのみ有効。
      ``second_time_force_admit`` は grey zone の一員 (D18)。
    * ``bounce_reason`` — ``verdict == BOUNCE`` のときの reason。

    ``is_admit`` は grey-zone gating の caller-side ヘルパ。 ``is_grey_zone``
    は §3.3.b の入場条件を集約する。
    """

    verdict: AdmissionVerdict
    kind: LogKind | None
    label: str | None = None
    retry_admit_reason: RetryAdmitReason | None = None
    bounce_reason: BounceReason | None = None

    @property
    def is_admit(self) -> bool:
        """admission-gate が human へ通した場合に真。 grey-zone gating の
        entry-guard で使う (§3.3.b, D18)。"""
        return self.verdict is AdmissionVerdict.ADMIT

    @property
    def is_grey_zone(self) -> bool:
        """§3.3.b の grey-zone gating 入場条件 (D18)。

        ``ADMIT_UNSURE`` (unsure:goal? admit) と ``second_time_force_admit``
        RETRY admit だけを grey zone と見なす。 有効な Tier-C ラベルで
        admit された ADMIT は Decider に問い掛けしない (D9 で ``merge-protected``
        などを Decider の genuine 語彙から外している ∴ 再評価すると誤 bounce)。
        """
        if not self.is_admit:
            return False
        if self.kind is LogKind.ADMIT_UNSURE:
            return True
        return (
            self.kind is LogKind.RETRY_ADMIT
            and self.retry_admit_reason is RetryAdmitReason.SECOND_TIME_FORCE_ADMIT
        )


@runtime_checkable
class Turn(Protocol):
    """Conductor turn の Decider 用 minimum surface。

    step 1 では Conductor 側の実 Turn 型は未実装 ∴ Protocol として書き、
    replay driver / tests は最小の attribute を持った代用値を構築できる。
    step 2 で Conductor 側が同 attribute を露出することを保証する。

    ``gate_result`` は §3.5 の in-memory 契約 — admission-gate が
    Conductor loop 側で turn に載せる。 Decider は turn から読むだけで、
    JSONL は触らない。
    """

    thread_id: str
    round_index: int
    roster: Mapping[str, Role]
    head_summary: str
    recent_events: tuple[EventSummary, ...]
    parsed_next: str | None
    prev_next: str | None
    diff_stat: DiffStat | None
    gate_result: AdmissionGateResult | None


@dataclass
class SimpleTurn:
    """``Turn`` protocol の具体実装。 replay driver / tests から使う。

    Conductor 側で live に turn を組み立てるときは、 conductor 側の実 Turn
    型がこの protocol を満たしていればよく、この dataclass を必ずしも
    使う必要は無い。 step 2 の Conductor 配線側で決める。

    非 frozen — Protocol Turn の attribute annotations は settable と解釈
    される ∴ Protocol を満たすために mutable dataclass にする (テスト時
    に fixture を組み立てて後で ``gate_result`` を差し替えられる利便性
    もあり、frozen である必要が無い)。
    """

    thread_id: str
    round_index: int
    roster: Mapping[str, Role]
    head_summary: str
    recent_events: tuple[EventSummary, ...]
    parsed_next: str | None = None
    prev_next: str | None = None
    diff_stat: DiffStat | None = None
    gate_result: AdmissionGateResult | None = None


@dataclass(frozen=True)
class DecisionState:
    """Decider に渡す事実 payload の全体 (§3.2)。

    §3.2 のフィールド一覧をそのまま実装。``gate_result`` は in-memory
    契約 (§3.5) — ``state_builder`` が ``turn.gate_result`` を copy する
    だけで、 JSONL からの live join は禁止 (D17)。

    ``recent_events`` は ``EventSummary`` の並び (最新 → 過去)。N と
    ``head_summary`` の M は §10 の open question ∴ ここでは cap せず
    ``state_builder`` の caller に委ねる。
    """

    thread_id: str
    round_index: int
    roster: Mapping[str, Role]
    head_summary: str
    recent_events: tuple[EventSummary, ...]
    parsed_next: str | None
    prev_next: str | None
    diff_stat: DiffStat | None
    gate_result: AdmissionGateResult | None


def state_builder(turn: Turn) -> DecisionState:
    """Turn から ``DecisionState`` を組み立てる (§3.2 / §3.5 / D17)。

    * ``turn.gate_result`` を ``DecisionState.gate_result`` にそのまま
      コピー — JSONL からの live join は禁止 (§3.5 in-memory 契約、
      Principle 2 二重管理禁止)。
    * live と replay で入力 shape を同一に揃える ∴ replay driver は
      同じ ``Turn`` 形の fixture を構築し ``state_builder`` に渡す。
    * 本関数は純関数 — I/O を持たない、 clock を読まない、モジュール
      状態を持たない。単に protocol の attribute を取り出して frozen
      dataclass に詰め直すだけ。

    Fermi msg-4066 の DECIDED 「``state_builder`` が ``gate_result`` を
    turn からコピーし JSONL を読まないこと」に対応するテストは
    ``tests/test_decider_state.py`` に置いた。
    """

    # 明示的な field copy: attribute の抜けを型検査で捕まえるため。
    return DecisionState(
        thread_id=turn.thread_id,
        round_index=turn.round_index,
        roster=dict(turn.roster),
        head_summary=turn.head_summary,
        recent_events=tuple(turn.recent_events),
        parsed_next=turn.parsed_next,
        prev_next=turn.prev_next,
        diff_stat=turn.diff_stat,
        gate_result=turn.gate_result,
    )


__all__ = [
    "TIER_C_LABELS",
    "AdmissionGateResult",
    "DecisionState",
    "DiffStat",
    "EventSummary",
    "SimpleTurn",
    "Turn",
    "state_builder",
]

# ``field`` is imported for use by downstream extensions; silence unused warning.
_ = field
