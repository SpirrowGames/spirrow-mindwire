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
raw_answers, verdict, policy, questions_version, latency_ms, error, matched_rule,
matched_rule_source, matched_rule_error, rules_sha256}`` が加わる (live の ``log_decision`` と
同じ shape)。

**tierc-v2** (T-decider-tierc-v2-all-escalations msg-4380 / 4382 / 4384): ``--questions tierc-v2
--rules PATH`` で五ヶ条の問いセットを使う。 v1 との違い:

* ``gate_result`` が ``null`` の turn も skip しない (D18 撤廃 + msg-4380 Δ2: 全件 Jev に通す)。
  ``scope`` は全件 ``in_gate``。
* record に ``rules_sha256`` が付く。 ``--endpoint`` 時は使ったルールファイルを
  ``--rules-snapshot-dir`` (既定 ``eval/tierc/rules/``) に ``<rules_sha256>.toml`` として保存し、
  各 record の ``rules_sha256`` から引けるようにする (msg-4382 評価の再現性)。
* fixture 行の ``dispute_rounds`` (任意、rule_5 の feature) を state に載せる。

``--questions`` の既定は ``tierc-v1`` (既存の replay / fixture の挙動を変えない)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import IO, Any, Literal

from spirrow_mindwire.adapters.decider_lexora import DECIDER_TIMEOUT_SECONDS, decide_once
from spirrow_mindwire.decider.questions import (
    TIERC_ESCALATION_QUESTIONS_VERSION,
    TIERC_QUESTIONS_V1,
    TIERC_QUESTIONS_VERSION,
    TierCRules,
    load_tierc_rules,
    tierc_v2_questions,
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
        dispute_rounds=_parse_dispute_rounds(row.get("dispute_rounds")),
    )


def _parse_dispute_rounds(raw: Any) -> int | None:
    """Optional tierc-v2 rule_5 feature (msg-4380 Δ5); absent / null → not computed."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"dispute_rounds must be a non-negative int, got {raw!r}")
    return raw


# ---------------------------------------------------------------------------
# DecisionState / TierC record の JSON 化
# ---------------------------------------------------------------------------


def _questions_to_json() -> list[dict[str, Any]]:
    return [{"key": q.key, "kind": q.kind.value, "prompt": q.prompt} for q in TIERC_QUESTIONS_V1]


def build_tierc_v2_record(state: DecisionState, rules: TierCRules) -> dict[str, Any]:
    """tierc-v2 の dry-run record。 scope は常に ``in_gate`` (D18 撤廃、msg-4360)。"""
    return {
        "thread_id": state.thread_id,
        "round_index": state.round_index,
        "questions_version": TIERC_ESCALATION_QUESTIONS_VERSION,
        "rules_sha256": rules.sha256,
        "scope": TierCScope.IN_GATE.value,
        "questions": tierc_v2_questions(rules),
        "state": state_to_dict(state),
    }


def snapshot_rules(rules: TierCRules, snapshot_dir: Path) -> Path:
    """Copy the rules file to ``<snapshot_dir>/<rules_sha256>.toml`` (msg-4382). An existing
    snapshot with that name has the same bytes by construction, so it is left alone."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    dest = snapshot_dir / f"{rules.sha256}.toml"
    if not dest.exists():
        shutil.copyfile(rules.source, dest)
    return dest


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


def record_key(rec: dict[str, Any]) -> tuple[str, int]:
    """The replay-record join key ``(thread_id, round_index)`` (msg-4219 §2)."""
    return (str(rec["thread_id"]), int(rec["round_index"]))


def load_done_keys(path: Path) -> set[tuple[str, int]]:
    """Keys already written to ``path`` by an earlier ``--resume`` run (msg-4219 §2).

    Only records that carry a ``decision`` count as done: a record from a dry-run has no
    decision and must not stop a later live run from sending it.
    """
    done: set[tuple[str, int]] = set()
    if not path.is_file():
        return done
    for rec in iter_fixture(path):
        if isinstance(rec.get("decision"), dict):
            done.add(record_key(rec))
    return done


async def _decide_records(
    states: list[DecisionState],
    records: list[dict[str, Any]],
    endpoint: str,
    *,
    sink: IO[str],
    done: set[tuple[str, int]],
    max_calls: int | None,
    rules: TierCRules | None = None,
) -> tuple[int, int, int]:
    """``--endpoint`` 経路: 各 state を live と同じ ``decide_once`` に流し record に足す。

    Records are written as each call returns (not buffered), so a crashed or capped run
    leaves every paid-for answer on disk for ``--resume`` to skip. ``max_calls`` stops the
    run **before** the call that would exceed it (msg-4219 §2 billing brake): that record and
    every later one are neither sent nor written. Returns ``(called, skipped_done,
    not_sent_cap)``.
    """
    called = skipped_done = not_sent = 0
    async with LexoraClient(endpoint, timeout_seconds=DECIDER_TIMEOUT_SECONDS) as client:
        for state, rec in zip(states, records, strict=True):
            if record_key(rec) in done:
                skipped_done += 1
                continue
            if max_calls is not None and called >= max_calls:
                not_sent += 1
                continue
            if rules is None:
                dr = await decide_once(state, client=client, policy=POLICY_REPLAY_TIERC)
            else:
                dr = await decide_once(
                    state, client=client, policy=POLICY_REPLAY_TIERC, rules=rules
                )
            called += 1
            rec["decision"] = decision_result_to_dict(dr)
            sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
            sink.flush()
    return called, skipped_done, not_sent


def run_tierc_replay(
    *,
    fixture: Path,
    out: Path | None,
    mode: Literal["dry-run"],
    endpoint: str | None = None,
    max_calls: int | None = None,
    resume: bool = False,
    rules: TierCRules | None = None,
    rules_snapshot_dir: Path | None = None,
) -> int:
    """Tier-C fixture を JSONL に吐く。

    ``rules`` given = tierc-v2 (every turn, gate or not; ``in_gate``); ``None`` = tierc-v1.

    ``endpoint`` が ``None`` なら dry-run (step 1 と同一出力)。 指定があれば
    各 record を ``/v1/decide`` に流し ``decision`` を付ける (msg-4180 §3)。

    ``max_calls`` / ``resume`` (T-decider-tierc-replay-eval msg-4219 §2) are live-path only:
    ``max_calls`` caps how many ``/v1/decide`` calls this run may make; ``resume`` appends to
    ``out`` and skips every key already written there with a decision.
    """

    if endpoint is None and (max_calls is not None or resume):
        print("decider_replay: --max-calls / --resume need --endpoint", file=sys.stderr)
        return 2
    if resume and out is None:
        print("decider_replay: --resume needs --out", file=sys.stderr)
        return 2
    if max_calls is not None and max_calls < 0:
        print("decider_replay: --max-calls must be >= 0", file=sys.stderr)
        return 2

    records: list[dict[str, Any]] = []
    states: list[DecisionState] = []
    skipped_no_gate = 0
    for row in iter_fixture(fixture):
        try:
            turn = parse_turn(row)
        except (KeyError, ValueError) as exc:
            print(f"decider_replay: skip malformed turn: {exc}", file=sys.stderr)
            continue

        if turn.gate_result is None and rules is None:
            # v1: Tier-C 対象外 (Track B 単独)。 step 1 では Track B 未実装 ∴
            # skip して count だけ残す。 v2 は skip しない (msg-4380 Δ2)。
            skipped_no_gate += 1
            continue

        state = state_builder(turn)
        states.append(state)
        records.append(
            build_tierc_record(state) if rules is None else build_tierc_v2_record(state, rules)
        )

    keys = [record_key(r) for r in records]
    if len(set(keys)) != len(keys):
        # --resume skips by key, so a duplicate key would silently drop a turn.
        print("decider_replay: duplicate (thread_id, round_index) in fixture", file=sys.stderr)
        return 2

    if endpoint is not None:
        if rules is not None:
            snap = snapshot_rules(
                rules, rules_snapshot_dir if rules_snapshot_dir is not None else _SNAPSHOT_DIR
            )
            print(f"decider_replay: rules snapshot {snap}", file=sys.stderr)
        done = load_done_keys(out) if (resume and out is not None) else set()
        sink = (
            out.open("a" if resume else "w", encoding="utf-8", newline="\n")
            if out is not None
            else sys.stdout
        )
        try:
            called, skipped_done, not_sent = asyncio.run(
                _decide_records(
                    states,
                    records,
                    endpoint,
                    sink=sink,
                    done=done,
                    max_calls=max_calls,
                    rules=rules,
                )
            )
        finally:
            if out is not None:
                sink.close()
        print(
            f"decider_replay: called {called} (skipped {skipped_done} already done, "
            f"{not_sent} not sent: --max-calls={max_calls}; skipped {skipped_no_gate} "
            f"non-gate turns, endpoint={endpoint})",
            file=sys.stderr,
        )
        return 0

    sink = out.open("w", encoding="utf-8", newline="\n") if out is not None else sys.stdout
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


_SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "eval" / "tierc" / "rules"
"""Default ``--rules-snapshot-dir``: ``eval/tierc/rules/`` in this repository (msg-4382)."""


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
    parser.add_argument(
        "--max-calls",
        type=int,
        default=None,
        help=(
            "--endpoint 時の /v1/decide 呼び出し上限 (課金の歯止め、msg-4219 §2)。 上限に"
            "達したら以降の turn は送らず書かない (--resume で続きを流せる)。"
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="--out に追記し、decision 付きで出力済みの (thread_id, round_index) を飛ばす。",
    )
    parser.add_argument(
        "--questions",
        choices=("tierc-v1", "tierc-v2"),
        default="tierc-v1",
        help="問いセット。 tierc-v2 (五ヶ条) は --rules が必要。 既定 tierc-v1。",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        default=None,
        help="tierc-v2 のルールファイル (tierc_rules.toml)。",
    )
    parser.add_argument(
        "--rules-snapshot-dir",
        type=Path,
        default=None,
        help="--endpoint 時にルールファイルを <sha256>.toml で保存する先 (既定 eval/tierc/rules)。",
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

    rules: TierCRules | None = None
    if args.questions == "tierc-v2":
        if args.rules is None:
            print("decider_replay: --questions tierc-v2 needs --rules", file=sys.stderr)
            return 2
        try:
            rules = load_tierc_rules(args.rules)
        except ValueError as exc:
            print(f"decider_replay: {exc}", file=sys.stderr)
            return 2
    elif args.rules is not None:
        print("decider_replay: --rules is only for --questions tierc-v2", file=sys.stderr)
        return 2

    return run_tierc_replay(
        fixture=args.fixture,
        out=args.out,
        mode=args.mode,
        endpoint=args.endpoint,
        max_calls=args.max_calls,
        resume=args.resume,
        rules=rules,
        rules_snapshot_dir=args.rules_snapshot_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
