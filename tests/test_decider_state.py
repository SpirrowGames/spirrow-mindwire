"""``state_builder`` の in-memory 契約テスト (Fermi msg-4066 テスト要件)。

要件 (msg-4066): 「``state_builder`` が ``gate_result`` を turn からコピーし
JSONL を読まないこと」。

そのためのテスト:

* ``state_builder`` は与えられた ``Turn`` から ``DecisionState`` を組み立て、
  ``gate_result`` を identity で持ち回す (§3.5 in-memory 契約、D17)。
* 本モジュールは JSONL に一切触らない — pathlib / open を import しない、
  file system を触らない (dogfood テストとして importlib 上で検証する)。
* Track B (``gate_result is None``) turn も許容する — Tier-C 対象外 turn
  で fallback せずに ``DecisionState.gate_result = None`` を保つ。
* ``TIER_C_LABELS`` は ``tier_c_admission_gate.ADMIT_LABELS`` と同一 object
  (D14 — 文字列二重管理禁止)。
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from spirrow_mindwire.decider.state import (
    TIER_C_LABELS,
    AdmissionGateResult,
    DecisionState,
    DiffStat,
    EventSummary,
    SimpleTurn,
    state_builder,
)
from spirrow_mindwire.tier_c_admission_gate import (
    ADMIT_LABELS,
    AdmissionVerdict,
    LogKind,
    RetryAdmitReason,
)
from spirrow_mindwire.value_objects import Role


def _minimal_turn(*, gate_result: AdmissionGateResult | None = None) -> SimpleTurn:
    return SimpleTurn(
        thread_id="T-decider-conductor-hook",
        round_index=7,
        roster={"Bohr": Role.PROPOSER, "Heisenberg": Role.IMPLEMENTER},
        head_summary="head summary",
        recent_events=(
            EventSummary(
                msg_id="msg-4066",
                author="Fermi",
                parsed_next="Heisenberg",
                body_head="…",
            ),
        ),
        parsed_next="Heisenberg",
        prev_next="Bohr",
        diff_stat=DiffStat(files_changed=2, insertions=10, deletions=1),
        gate_result=gate_result,
    )


# ---------------------------------------------------------------------------
# D14 — TIER_C_LABELS は再定義せず ADMIT_LABELS を再 export する
# ---------------------------------------------------------------------------


def test_tier_c_labels_is_identical_to_admit_labels() -> None:
    """D14: ``TIER_C_LABELS`` は ``ADMIT_LABELS`` と同一 object。

    文字列を二重管理すると片方の update が他方を追い越し、admission-gate
    と Decider の判定語彙が silently ずれる。 identity 比較で二重定義を
    disable する。
    """
    assert TIER_C_LABELS is ADMIT_LABELS


# ---------------------------------------------------------------------------
# state_builder の in-memory 契約 (Fermi DECIDED テスト要件)
# ---------------------------------------------------------------------------


def test_state_builder_copies_gate_result_from_turn_by_identity() -> None:
    """§3.5 in-memory 契約 — ``turn.gate_result`` の値を identity で持ち回す。"""
    gate = AdmissionGateResult(
        verdict=AdmissionVerdict.ADMIT,
        kind=LogKind.ADMIT_UNSURE,
        label="unsure:goal?",
    )
    turn = _minimal_turn(gate_result=gate)

    state = state_builder(turn)

    assert isinstance(state, DecisionState)
    assert state.gate_result is gate  # identity! (frozen dataclass の値識別性)


def test_state_builder_preserves_none_gate_result() -> None:
    """Tier-C 対象外 turn (Track B / gate off) は ``gate_result = None`` を保つ。

    fallback の暗黙 default に置き換えると Decider が Track B ターンでも
    Tier-C フックへ回ってしまう ∴ None は None のまま。
    """
    turn = _minimal_turn(gate_result=None)

    state = state_builder(turn)

    assert state.gate_result is None


def test_state_builder_copies_all_turn_fields() -> None:
    """§3.2 の全 field を漏れなくコピー。"""
    gate = AdmissionGateResult(
        verdict=AdmissionVerdict.ADMIT,
        kind=LogKind.RETRY_ADMIT,
        retry_admit_reason=RetryAdmitReason.SECOND_TIME_FORCE_ADMIT,
    )
    turn = _minimal_turn(gate_result=gate)

    state = state_builder(turn)

    assert state.thread_id == turn.thread_id
    assert state.round_index == turn.round_index
    assert dict(state.roster) == dict(turn.roster)
    assert state.head_summary == turn.head_summary
    assert state.recent_events == turn.recent_events
    assert state.parsed_next == turn.parsed_next
    assert state.prev_next == turn.prev_next
    assert state.diff_stat == turn.diff_stat


# ---------------------------------------------------------------------------
# state.py は JSONL を読まない
# ---------------------------------------------------------------------------


def test_state_module_does_not_import_file_io() -> None:
    """JSONL への live join を禁止する D17 の invariant を code-level に固定。

    ``state.py`` の source から ``open(`` / ``json.load`` / ``pathlib.Path``
    のような file 系 API が呼ばれていないことをテストで検証する。
    static analyser の linter 相当を pytest から掛ける薄いガード。
    """
    module = importlib.import_module("spirrow_mindwire.decider.state")
    source = inspect.getsource(module)

    forbidden_needles = [
        "open(",
        "json.loads",
        "json.load",
        "Path(",
        "pathlib",
        # future: JSONL append 系
        "jsonl",
    ]
    for needle in forbidden_needles:
        assert needle not in source, (
            f"decider/state.py must not touch file I/O (D17) — found {needle!r}"
        )


# ---------------------------------------------------------------------------
# AdmissionGateResult.is_grey_zone helper — D18 gating の shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "retry_reason", "expected"),
    [
        (LogKind.ADMIT_UNSURE, None, True),
        (
            LogKind.RETRY_ADMIT,
            RetryAdmitReason.SECOND_TIME_FORCE_ADMIT,
            True,
        ),
        (LogKind.RETRY_ADMIT, RetryAdmitReason.LABEL_CORRECTED, False),
        (LogKind.RETRY_ADMIT, RetryAdmitReason.UNSURE_AFTER_RETRY, False),
        (LogKind.LABEL_MIGRATION, None, False),
        (None, None, False),
    ],
)
def test_admission_gate_result_grey_zone_helper(
    kind: LogKind | None,
    retry_reason: RetryAdmitReason | None,
    expected: bool,
) -> None:
    """§3.3.b の入場条件 (D18) を helper で明示的にテストする。"""
    gr = AdmissionGateResult(
        verdict=AdmissionVerdict.ADMIT,
        kind=kind,
        retry_admit_reason=retry_reason,
    )
    assert gr.is_grey_zone is expected


def test_bounced_verdict_is_not_grey_zone() -> None:
    """BOUNCE は admission-gate が既に人へ送らないと判断した状態 ∴ grey
    zone ではない (Decider に問い掛けしない)。"""
    gr = AdmissionGateResult(
        verdict=AdmissionVerdict.BOUNCE,
        kind=LogKind.BOUNCED,
    )
    assert gr.is_admit is False
    assert gr.is_grey_zone is False
