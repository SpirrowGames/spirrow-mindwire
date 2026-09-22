"""Decider replay driver — Fermi msg-4066/4067 step 1 分担の一部。

**Scope**: adapter (``decider_lexora.py``) が着地するまでの間、fixture ->
DecisionState -> 問い JSONL の dry-run 経路だけを提供する。 実際に
``/v1/decide`` を叩く経路 (--track=tierc の実呼び出し) は step 2 で
adapter 着地後に足す。

**Design pointer**:

* §6.1 replay script — ``--track=tierc / --track=handoff``。 step 1 では
  Track B 系の ``--track=handoff`` は未実装 (問いセット / evaluate 側が無い
  ため)。 CLI parser で明示的に「未実装」を返す。
* §3.5 の in-memory ``AdmissionGateResult`` 契約 — replay driver は
  過去 JSONL を fixture 構築の入力にしてよいが、Decider へ渡す時点では
  in-memory ``AdmissionGateResult`` に正規化する (JSONL からの live join
  禁止、D17)。 driver は本方針の実運用モデルを最小の形で提示する。
* Fermi msg-4066 DECIDED #3 「ADMIT ターンも shadow では問いログを残す
  (scope=out_of_gate)」 — dry-run 経路が verdict 合成を回すか否かは
  fixture の gate_result.is_grey_zone で切り分ける。 grey zone のとき
  ``TIERC_QUESTIONS_V1`` を JSONL に emit、そうでないときは scope=out_of_gate
  の record として emit する。

**Fixture 形式** (JSONL — 1 行 = 1 turn):

.. code-block:: json

    {
      "thread_id": "T-decider-conductor-hook",
      "round_index": 12,
      "roster": {"Bohr": "proposer", "Heisenberg": "implementer"},
      "head_summary": "…",
      "recent_events": [
        {"msg_id": "msg-4066", "author": "Fermi", "parsed_next": "Heisenberg", "body_head": "…"}
      ],
      "parsed_next": "Heisenberg",
      "prev_next": "Bohr",
      "diff_stat": null,
      "gate_result": {
        "verdict": "admit", "kind": "ADMIT_UNSURE",
        "label": "unsure:goal?", "retry_admit_reason": null, "bounce_reason": null
      }
    }

``gate_result`` が ``null`` の turn は §3.3.b 対象外 (Track B 単独) ∴
step 1 の replay では skip (WARN)。 driver は Track B 未実装なので
Tier-C fixture のみを想定する。

**出力** (JSONL — 1 行 = 1 record):

.. code-block:: json

    {
      "thread_id": "…", "round_index": 12,
      "questions_version": "tierc-v1", "scope": "in_gate",
      "questions": [
        {"key": "changes_goal_or_spec", "kind": "genuine", "prompt": "…"},
        …
      ],
      "state": { … DecisionState を JSON 化した payload … }
    }

adapter 着地後 (step 2) は本 record に ``answers`` / ``verdict`` を後付け
した joined record に拡張する。 現在は dry-run: 問いと state だけを吐く。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from spirrow_mindwire.decider.questions import (
    TIERC_QUESTIONS_V1,
    TIERC_QUESTIONS_VERSION,
)
from spirrow_mindwire.decider.state import (
    AdmissionGateResult,
    DecisionState,
    DiffStat,
    EventSummary,
    SimpleTurn,
    state_builder,
)
from spirrow_mindwire.decider.verdict import TierCScope
from spirrow_mindwire.tier_c_admission_gate import (
    AdmissionVerdict,
    BounceReason,
    LogKind,
    RetryAdmitReason,
)
from spirrow_mindwire.value_objects import Role

# stderr は fixture の path / message を吐きうる ∴ Windows cp932 で raise
# しないよう backslashreplace を設定 (`scripts/thread_heads.py` と同じ規約)。
_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(errors="backslashreplace")


# ---------------------------------------------------------------------------
# fixture -> Turn の parse
# ---------------------------------------------------------------------------


def _parse_gate_result(raw: dict[str, Any]) -> AdmissionGateResult:
    """JSONL の ``gate_result`` object を ``AdmissionGateResult`` に整形。

    live / replay の入力を同一 shape (§3.5) に揃えるための helper。 JSONL の
    値は全て文字列 enum 名 (``"admit"`` / ``"ADMIT_UNSURE"`` / ...) 想定 —
    admission-gate 側の enum と 1:1 対応する。
    """

    verdict_raw = str(raw["verdict"])
    verdict = AdmissionVerdict(verdict_raw)

    kind_raw = raw.get("kind")
    kind: LogKind | None = LogKind(str(kind_raw)) if kind_raw else None

    label = raw.get("label")
    label_str: str | None = str(label) if label is not None else None

    retry_reason_raw = raw.get("retry_admit_reason")
    retry_reason: RetryAdmitReason | None = (
        RetryAdmitReason(str(retry_reason_raw)) if retry_reason_raw else None
    )

    bounce_reason_raw = raw.get("bounce_reason")
    bounce_reason: BounceReason | None = (
        BounceReason(str(bounce_reason_raw)) if bounce_reason_raw else None
    )

    return AdmissionGateResult(
        verdict=verdict,
        kind=kind,
        label=label_str,
        retry_admit_reason=retry_reason,
        bounce_reason=bounce_reason,
    )


def _parse_roster(raw: dict[str, Any]) -> dict[str, Role]:
    return {str(k): Role(str(v)) for k, v in raw.items()}


def _parse_events(raw: list[Any]) -> tuple[EventSummary, ...]:
    events: list[EventSummary] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        events.append(
            EventSummary(
                msg_id=str(item.get("msg_id") or ""),
                author=str(item.get("author") or ""),
                parsed_next=(None if item.get("parsed_next") is None else str(item["parsed_next"])),
                body_head=str(item.get("body_head") or ""),
            )
        )
    return tuple(events)


def _parse_diff_stat(raw: dict[str, Any] | None) -> DiffStat | None:
    if raw is None:
        return None
    return DiffStat(
        files_changed=int(raw.get("files_changed", 0)),
        insertions=int(raw.get("insertions", 0)),
        deletions=int(raw.get("deletions", 0)),
    )


def parse_turn(row: dict[str, Any]) -> SimpleTurn:
    """1 行 JSONL fixture を ``SimpleTurn`` に整形する。

    ``gate_result`` は §3.5 の in-memory 契約に従い ``AdmissionGateResult``
    dataclass に normalise される — Decider が本 turn を state_builder に
    通した瞬間、live と同一 shape になる。 JSONL からの live join は
    起きない (fixture 構築の入力として JSONL を使うだけ)。
    """

    gate_raw = row.get("gate_result")
    gate_result = _parse_gate_result(gate_raw) if isinstance(gate_raw, dict) else None

    roster_raw = row.get("roster") or {}
    if not isinstance(roster_raw, dict):
        raise ValueError(f"roster must be an object, got {type(roster_raw).__name__}")

    events_raw = row.get("recent_events") or []
    if not isinstance(events_raw, list):
        raise ValueError(f"recent_events must be a list, got {type(events_raw).__name__}")

    diff_raw = row.get("diff_stat")
    if diff_raw is not None and not isinstance(diff_raw, dict):
        raise ValueError(f"diff_stat must be null or object, got {type(diff_raw).__name__}")

    return SimpleTurn(
        thread_id=str(row["thread_id"]),
        round_index=int(row["round_index"]),
        roster=_parse_roster(roster_raw),
        head_summary=str(row.get("head_summary") or ""),
        recent_events=_parse_events(events_raw),
        parsed_next=(None if row.get("parsed_next") is None else str(row["parsed_next"])),
        prev_next=None if row.get("prev_next") is None else str(row["prev_next"]),
        diff_stat=_parse_diff_stat(diff_raw),
        gate_result=gate_result,
    )


# ---------------------------------------------------------------------------
# DecisionState / TierC record の JSON 化
# ---------------------------------------------------------------------------


def _gate_result_to_json(gate: AdmissionGateResult | None) -> dict[str, Any] | None:
    if gate is None:
        return None
    return {
        "verdict": gate.verdict.value,
        "kind": gate.kind.value if gate.kind is not None else None,
        "label": gate.label,
        "retry_admit_reason": (gate.retry_admit_reason.value if gate.retry_admit_reason else None),
        "bounce_reason": gate.bounce_reason.value if gate.bounce_reason else None,
        "is_grey_zone": gate.is_grey_zone,
    }


def _state_to_json(state: DecisionState) -> dict[str, Any]:
    return {
        "thread_id": state.thread_id,
        "round_index": state.round_index,
        "roster": {k: v.value for k, v in state.roster.items()},
        "head_summary": state.head_summary,
        "recent_events": [asdict(e) for e in state.recent_events],
        "parsed_next": state.parsed_next,
        "prev_next": state.prev_next,
        "diff_stat": asdict(state.diff_stat) if state.diff_stat else None,
        "gate_result": _gate_result_to_json(state.gate_result),
    }


def _questions_to_json() -> list[dict[str, Any]]:
    return [{"key": q.key, "kind": q.kind.value, "prompt": q.prompt} for q in TIERC_QUESTIONS_V1]


def build_tierc_record(state: DecisionState) -> dict[str, Any]:
    """1 turn 分の Tier-C 問い + state を dry-run record として組み立てる。

    ``scope`` (§6.3 集計の split key、Fermi DECIDED #3):

    * grey zone (``state.gate_result.is_grey_zone == True``) → ``in_gate``。
      §3.3.b で ``evaluate_tierc`` が実際に呼ばれる対象。
    * それ以外の ADMIT ターン → ``out_of_gate``。 shadow-only の問いログ、
      verdict 合成には流さない (D18 invariant)。
    * ``gate_result is None`` (Tier-C 対象外) → 呼び出し元で skip する
      想定 (本関数は呼ばれない)。 fallback として ``out_of_gate`` を返す。
    """

    gr = state.gate_result
    if gr is None:
        scope = TierCScope.OUT_OF_GATE
    else:
        scope = TierCScope.IN_GATE if gr.is_grey_zone else TierCScope.OUT_OF_GATE

    return {
        "thread_id": state.thread_id,
        "round_index": state.round_index,
        "questions_version": TIERC_QUESTIONS_VERSION,
        "scope": scope.value,
        "questions": _questions_to_json(),
        "state": _state_to_json(state),
    }


# ---------------------------------------------------------------------------
# fixture 読み込み / driver
# ---------------------------------------------------------------------------


def iter_fixture(path: Path) -> Iterable[dict[str, Any]]:
    """JSONL fixture を 1 行 1 dict で返す iterator。

    空行 / '#' で始まる行はコメントとして読み飛ばす (人手で fixture を
    編集する運用を想定)。
    """

    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                yield json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON — {exc}") from exc


def run_tierc_replay(
    *,
    fixture: Path,
    out: Path | None,
    mode: Literal["dry-run"],
) -> int:
    """Tier-C fixture を dry-run で JSONL に吐く (step 1 の cap)。

    ``mode="dry-run"`` のみ実装 — adapter 未着地 ∴ 実 provider 呼び出しは
    step 2 の PR で足す。 CLI から他の mode を指定すると parser で拒否
    される (Literal 型で強制)。
    """

    records: list[dict[str, Any]] = []
    skipped_no_gate = 0
    for row in iter_fixture(fixture):
        try:
            turn = parse_turn(row)
        except (KeyError, ValueError) as exc:
            print(f"decider_replay: skip malformed turn: {exc}", file=sys.stderr)
            continue

        if turn.gate_result is None:
            # Tier-C 対象外 (Track B 単独)。 step 1 では Track B 未実装 ∴
            # skip して count だけ残す。
            skipped_no_gate += 1
            continue

        state = state_builder(turn)
        records.append(build_tierc_record(state))

    sink = out.open("w", encoding="utf-8") if out is not None else sys.stdout
    try:
        for rec in records:
            sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
    finally:
        if out is not None:
            sink.close()

    print(
        f"decider_replay: emitted {len(records)} tierc records "
        f"(skipped {skipped_no_gate} non-gate turns, mode={mode})",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--track",
        choices=("tierc", "handoff"),
        required=True,
        help=(
            "問いセットの種類。 'tierc' のみ step 1 で実装。 'handoff' は "
            "Track B (§4.7) — step 5 (Fermi msg-4066/4067) で adapter 着地後に足す。"
        ),
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help="1 行 1 turn の JSONL fixture (parse_turn の shape に従う)。",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="出力先 JSONL パス。 未指定なら stdout。",
    )
    parser.add_argument(
        "--mode",
        choices=("dry-run",),
        default="dry-run",
        help=(
            "step 1 は 'dry-run' 固定 (adapter 未着地 ∴ 実呼び出し不可)。 "
            "step 2 で 'live' / 'shadow' を足す。"
        ),
    )
    args = parser.parse_args(argv)

    if args.track != "tierc":
        # Track B 未実装 — fail loud (silent no-op を避ける)。
        print(
            f"decider_replay: --track={args.track} is not implemented in step 1 "
            "(only --track=tierc). Track B (§4.7) lands in a later PR.",
            file=sys.stderr,
        )
        return 2

    if not args.fixture.is_file():
        print(
            f"decider_replay: fixture not found: {args.fixture}",
            file=sys.stderr,
        )
        return 2

    return run_tierc_replay(fixture=args.fixture, out=args.out, mode=args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
