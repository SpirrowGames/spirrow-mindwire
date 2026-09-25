"""Decider replay driver — Fermi msg-4066/4067 step 1 分担の一部。

**Scope**: fixture -> DecisionState -> 問い JSONL の dry-run 経路 (step 1) と、
``--endpoint URL`` 指定時に ``/v1/decide`` を実際に叩く経路 (step 2、Bohr
msg-4180 §3)。 ``--endpoint`` 無しは従来どおりの dry-run。 ``--endpoint`` 有りは
live adapter と同じ ``decide_once`` (同じ request builder ``decider.wire``、同じ
応答分類) を policy ``mindwire.replay.tierc`` で呼び、record に ``decision``
(``DecisionResult`` の JSON) を足す。 116 件の一括投入は Jev 切替 (課金判断) 後。

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

``--endpoint`` 指定時は record に ``"decision": {outcome, decision_id, provider,
raw_answers, verdict, policy, questions_version, latency_ms, error}`` が加わる
(live の ``log_decision`` と同じ shape)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from spirrow_mindwire.adapters.decider_lexora import DECIDER_TIMEOUT_SECONDS, decide_once
from spirrow_mindwire.decider.questions import (
    TIERC_QUESTIONS_V1,
    TIERC_QUESTIONS_VERSION,
)
from spirrow_mindwire.decider.result import decision_result_to_dict
from spirrow_mindwire.decider.state import (
    AdmissionGateResult,
    DecisionState,
    DiffStat,
    EventSummary,
    SimpleTurn,
    state_builder,
)
from spirrow_mindwire.decider.verdict import TierCScope
from spirrow_mindwire.decider.wire import POLICY_REPLAY_TIERC, state_to_dict
from spirrow_mindwire.lexora.client import LexoraClient
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
        # Same dict ``decider.wire.state_to_wire`` serialises for /v1/decide (msg-4180 §2-1).
        "state": state_to_dict(state),
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


async def _decide_records(
    states: list[DecisionState], records: list[dict[str, Any]], endpoint: str
) -> None:
    """``--endpoint`` 経路: 各 state を live と同じ ``decide_once`` に流し record に足す。"""
    async with LexoraClient(endpoint, timeout_seconds=DECIDER_TIMEOUT_SECONDS) as client:
        for state, rec in zip(states, records, strict=True):
            dr = await decide_once(state, client=client, policy=POLICY_REPLAY_TIERC)
            rec["decision"] = decision_result_to_dict(dr)


def run_tierc_replay(
    *,
    fixture: Path,
    out: Path | None,
    mode: Literal["dry-run"],
    endpoint: str | None = None,
) -> int:
    """Tier-C fixture を JSONL に吐く。

    ``endpoint`` が ``None`` なら dry-run (step 1 と同一出力)。 指定があれば
    各 record を ``/v1/decide`` に流し ``decision`` を付ける (msg-4180 §3)。
    """

    records: list[dict[str, Any]] = []
    states: list[DecisionState] = []
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
        states.append(state)
        records.append(build_tierc_record(state))

    if endpoint is not None:
        asyncio.run(_decide_records(states, records, endpoint))

    sink = out.open("w", encoding="utf-8") if out is not None else sys.stdout
    try:
        for rec in records:
            sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
    finally:
        if out is not None:
            sink.close()

    print(
        f"decider_replay: emitted {len(records)} tierc records "
        f"(skipped {skipped_no_gate} non-gate turns, mode={mode}, "
        f"endpoint={endpoint or 'none'})",
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
    parser.add_argument(
        "--endpoint",
        default=None,
        help=(
            "Lexora base URL (例 http://localhost:8110)。 指定すると各 turn を "
            "/v1/decide に流し record に decision を付ける (policy=mindwire.replay.tierc)。 "
            "未指定なら dry-run。"
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

    return run_tierc_replay(
        fixture=args.fixture, out=args.out, mode=args.mode, endpoint=args.endpoint
    )


if __name__ == "__main__":
    raise SystemExit(main())
