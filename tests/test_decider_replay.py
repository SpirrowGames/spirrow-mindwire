"""``scripts/decider_replay.py`` — dry-run driver のテスト。

step 1 の scope: adapter 未着地 ∴ 実 provider 呼び出しは無い。 driver は
fixture → DecisionState → JSONL record の pipeline を dry-run で回すだけ。
検証点:

* fixture (grey zone) → record は ``scope=in_gate``、``questions_version=tierc-v1``。
* fixture (ADMIT with valid label) → record は ``scope=out_of_gate``
  (Fermi DECIDED #3、D18 の invariant)。
* fixture (BOUNCE) → record は ``scope=out_of_gate`` (verdict 合成に流さない)。
* fixture (gate_result null) → skip され、record に載らない。
* ``--track=handoff`` は step 1 未実装 ∴ return code 2 で fail loud。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def _load_replay_module() -> Any:
    """``scripts/decider_replay.py`` を import する — scripts/ は package ではない
    ∴ importlib で直接 load する。"""
    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "decider_replay.py"
    spec = importlib.util.spec_from_file_location("decider_replay_module", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["decider_replay_module"] = mod
    spec.loader.exec_module(mod)
    return mod


def _grey_zone_turn_row() -> dict[str, Any]:
    return {
        "thread_id": "T-decider-conductor-hook",
        "round_index": 7,
        "roster": {"Bohr": "proposer", "Heisenberg": "implementer"},
        "head_summary": "…",
        "recent_events": [
            {
                "msg_id": "msg-4066",
                "author": "Fermi",
                "parsed_next": "Heisenberg",
                "body_head": "…",
            }
        ],
        "parsed_next": "Heisenberg",
        "prev_next": "Bohr",
        "diff_stat": None,
        "gate_result": {
            "verdict": "admit",
            "kind": "ADMIT_UNSURE",
            "label": "unsure:goal?",
            "retry_admit_reason": None,
            "bounce_reason": None,
        },
    }


def _admit_valid_label_turn_row() -> dict[str, Any]:
    row = _grey_zone_turn_row()
    row["round_index"] = 8
    row["gate_result"] = {
        "verdict": "admit",
        "kind": None,  # R1-admit は log を発しない ∴ kind=None
        "label": "goal",
        "retry_admit_reason": None,
        "bounce_reason": None,
    }
    return row


def _bounce_turn_row() -> dict[str, Any]:
    row = _grey_zone_turn_row()
    row["round_index"] = 9
    row["gate_result"] = {
        "verdict": "bounce",
        "kind": "BOUNCED",
        "label": None,
        "retry_admit_reason": None,
        "bounce_reason": "no-label",
    }
    return row


def _no_gate_turn_row() -> dict[str, Any]:
    row = _grey_zone_turn_row()
    row["round_index"] = 10
    row["gate_result"] = None
    return row


def _write_fixture(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "fixture.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


# ---------------------------------------------------------------------------
# fixture → record の scope 判定
# ---------------------------------------------------------------------------


def test_dry_run_grey_zone_emits_in_gate(tmp_path: Path) -> None:
    mod = _load_replay_module()
    fixture = _write_fixture(tmp_path, [_grey_zone_turn_row()])
    out = tmp_path / "out.jsonl"

    code = mod.run_tierc_replay(fixture=fixture, out=out, mode="dry-run")
    assert code == 0

    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["scope"] == "in_gate"
    assert record["questions_version"] == "tierc-v1"
    assert len(record["questions"]) == 6
    # in-memory 契約: state.gate_result が JSON にも載って再現できる
    assert record["state"]["gate_result"]["is_grey_zone"] is True


def test_dry_run_admit_valid_label_emits_out_of_gate(tmp_path: Path) -> None:
    """Fermi DECIDED #3 — ADMIT (valid label) は shadow record として残るが
    scope=out_of_gate で verdict 合成対象外。"""
    mod = _load_replay_module()
    fixture = _write_fixture(tmp_path, [_admit_valid_label_turn_row()])
    out = tmp_path / "out.jsonl"

    mod.run_tierc_replay(fixture=fixture, out=out, mode="dry-run")

    record = json.loads(out.read_text(encoding="utf-8").strip())
    assert record["scope"] == "out_of_gate"


def test_dry_run_bounce_emits_out_of_gate(tmp_path: Path) -> None:
    """BOUNCED は grey zone でない ∴ scope=out_of_gate。"""
    mod = _load_replay_module()
    fixture = _write_fixture(tmp_path, [_bounce_turn_row()])
    out = tmp_path / "out.jsonl"

    mod.run_tierc_replay(fixture=fixture, out=out, mode="dry-run")

    record = json.loads(out.read_text(encoding="utf-8").strip())
    assert record["scope"] == "out_of_gate"


def test_dry_run_skips_turns_with_no_gate_result(tmp_path: Path) -> None:
    """Track B 単独 turn (gate_result null) は step 1 では skip。"""
    mod = _load_replay_module()
    fixture = _write_fixture(tmp_path, [_no_gate_turn_row(), _grey_zone_turn_row()])
    out = tmp_path / "out.jsonl"

    mod.run_tierc_replay(fixture=fixture, out=out, mode="dry-run")

    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["state"]["round_index"] == 7  # grey zone turn だけ残る


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def test_cli_track_handoff_is_not_implemented_in_step_1(tmp_path: Path) -> None:
    """Track B (§4.7) 未実装 — silent no-op ではなく exit code 2 で fail loud。"""
    mod = _load_replay_module()
    dummy = tmp_path / "dummy.jsonl"
    dummy.write_text("", encoding="utf-8")

    rc = mod.main(["--track=handoff", "--fixture", str(dummy)])
    assert rc == 2


def test_cli_missing_fixture_fails_loud(tmp_path: Path) -> None:
    mod = _load_replay_module()
    missing = tmp_path / "nope.jsonl"

    rc = mod.main(["--track=tierc", "--fixture", str(missing)])
    assert rc == 2
