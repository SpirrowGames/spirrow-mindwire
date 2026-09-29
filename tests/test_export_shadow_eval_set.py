"""T-decider-conductor-hook step 2d — ``scripts/export_shadow_eval_set.py``.

Spec: Bohr msg-4634 DECIDED 2d-2 (Jev's output never in the labellers' file — negative test
column by column; out-of-scope rows excluded) and msg-4636 DECIDED 2d-3 (tests 19-21: no state
rebuild, material fixed at ``latest_msg_id``, rows without the point-in-time columns excluded).
Test 18 (the logged ``state_wire`` is the bytes sent) lives in ``test_decider_step2.py``; the
end-to-end test here feeds a real hook log line through the exporter.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.adapters.decider_lexora import DeciderLexoraAdapter
from spirrow_mindwire.decider.hook import ThreadMessage, run_tierc_hook
from spirrow_mindwire.decider.questions import load_tierc_rules, tierc_rules_template_path
from spirrow_mindwire.value_objects import Role

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "export_shadow_eval_set.py"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"{name}_module", ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{name}_module"] = mod
    spec.loader.exec_module(mod)
    return mod


ex = _load("export_shadow_eval_set")
report = _load("tierc_eval_report")

SHA = "a" * 64
ROSTER = {"Bohr": "proposer", "Einstein": "naysayer", "Heisenberg": "implementer"}


def _msg(i: int, author: str = "Bohr", body: str | None = None) -> Any:
    return ex.Message(
        msg_id=f"msg-{i}",
        author=author,
        content=body if body is not None else f"body {i}\n\nNEXT: human",
        timestamp=f"2026-10-01T00:00:{i:02d}+00:00",
    )


def _thread(n: int) -> list[Any]:
    return [_msg(i, "Einstein" if i % 2 else "Bohr") for i in range(n)]


def _row(
    *,
    thread_id: str = "T-a",
    round_index: int = 1,
    latest: str | None = "msg-6",
    routed: str = "stop",
    sha: str | None = SHA,
    wire: bool = True,
) -> dict[str, Any]:
    r: dict[str, Any] = {
        "thread_id": thread_id,
        "round_index": round_index,
        "stop": "human" if routed != "forced_naysayer" else None,
        "routed": routed,
        "gate_kind": None,
        "gate_is_grey_zone": False,
        "outcome": "evaluated",
        "decision_id": f"d-{thread_id}-{latest}",
        "provider": "jev",
        "raw_answers": {"should_ask_human": {"noul": 0.3}},
        "verdict": {"kind": "UNSURE", "scope": "in_gate", "ask_score": 0.3, "fired_reason": None},
        "policy": "live",
        "questions_version": "tierc-v2",
        "latency_ms": 5,
        "error": None,
        "matched_rule": "none",
        "matched_rule_source": "choice",
        "matched_rule_error": None,
        "rules_sha256": sha,
    }
    if wire:
        r["latest_msg_id"] = latest
        r["roster"] = dict(ROSTER)
        r["state_wire"] = json.dumps(
            {"thread_id": thread_id, "round_index": round_index, "gate_result": None},
            sort_keys=True,
            separators=(",", ":"),
        )
    return r


def _keys_deep(obj: Any) -> set[str]:
    if isinstance(obj, Mapping):
        out = set(obj)
        for v in obj.values():
            out |= _keys_deep(v)
        return out
    if isinstance(obj, list):
        out = set()
        for v in obj:
            out |= _keys_deep(v)
        return out
    return set()


# --------------------------------------------------------------------------- test 19


def test_exporter_never_imports_the_state_builder() -> None:
    """Test 19 (msg-4636): no ``state_builder`` / ``turn_from_messages``, and nothing from
    ``spirrow_mindwire.decider`` at all — the state comes from the log, never rebuilt."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
    assert "state_builder" not in imported
    assert "turn_from_messages" not in imported
    assert not [m for m in imported if m.startswith("spirrow_mindwire.decider")]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "state_builder" not in names and "turn_from_messages" not in names


# --------------------------------------------------------------------------- test 20


def test_material_is_fixed_at_latest_msg_id_when_the_thread_grows() -> None:
    """Test 20 (msg-4636): messages added after ``latest_msg_id`` change nothing."""
    rows = [_row()]
    before = ex.build_outputs(rows, SHA, {"T-a": ("p", _thread(7))})
    after = ex.build_outputs(rows, SHA, {"T-a": ("p", _thread(12))})
    assert before == after
    (m,) = before[0]
    assert m["msg_id"] == "msg-6" and m["following"] == []
    assert [p["msg_id"] for p in m["prior"]] == [f"msg-{i}" for i in range(1, 6)]


def test_nothing_after_latest_msg_id_reaches_any_output() -> None:
    thread = [*_thread(7), _msg(7, "human", "FUTURE-OUTCOME the human decided")]
    out = ex.build_outputs([_row()], SHA, {"T-a": ("p", thread)})
    assert "FUTURE-OUTCOME" not in json.dumps(out, ensure_ascii=False)
    assert "msg-7" not in json.dumps(out, ensure_ascii=False)


# --------------------------------------------------------------------------- test 21 / scope


def test_rows_without_point_in_time_columns_are_not_counted() -> None:
    """Test 21 (msg-4636): a row written before the columns existed is counted by reason only."""
    rows = [_row(wire=False), _row(thread_id="T-b", latest="msg-3")]
    materials, fixture, replay, summary = ex.build_outputs(rows, SHA, {"T-b": ("p", _thread(4))})
    assert [m["thread_id"] for m in materials] == ["T-b"]
    assert len(fixture) == len(replay) == 1
    assert summary["by_reason"] == {ex.COUNTED: 1, ex.NO_POINT_IN_TIME: 1}


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (_row(wire=False), "no_point_in_time_columns"),
        (_row(latest=None), "no_point_in_time_columns"),
        (_row(routed="forced_naysayer"), "routed:forced_naysayer"),
        (_row(routed="spawn_blocked"), "routed:spawn_blocked"),
        (_row(sha="b" * 64), "rules_sha256_mismatch"),
        (_row(sha=None), "rules_sha256_mismatch"),
        (_row(), "counted"),
    ],
)
def test_classify(row: dict[str, Any], reason: str) -> None:
    """msg-4634 2d-1 / msg-4636 2d-3: the counted scope, and the separate forced / spawn table."""
    assert ex.classify(row, SHA) == reason


def test_out_of_scope_rows_need_no_thread_fetch() -> None:
    rows = [_row(routed="forced_naysayer"), _row(routed="spawn_blocked"), _row(sha="c" * 64)]
    materials, _, _, summary = ex.build_outputs(rows, SHA, {})
    assert materials == []
    assert summary["counted"] == 0 and summary["rows_read"] == 3


def test_duplicate_latest_msg_id_stops_the_export() -> None:
    with pytest.raises(ex.ExportError, match="msg-6"):
        ex.build_outputs([_row(), _row(round_index=4)], SHA, {"T-a": ("p", _thread(7))})


def test_unfetched_thread_or_missing_message_stops_the_export() -> None:
    with pytest.raises(ex.ExportError, match="not fetched"):
        ex.build_outputs([_row()], SHA, {})
    with pytest.raises(ex.ExportError, match="not in the fetched thread"):
        ex.build_outputs([_row()], SHA, {"T-a": ("p", _thread(3))})


# --------------------------------------------------------------------------- double blind


def test_labeller_files_carry_no_jev_column() -> None:
    """msg-4634 DECIDED 2d-2 negative test: column by column, at every depth."""
    materials, fixture, replay, _ = ex.build_outputs([_row()], SHA, {"T-a": ("p", _thread(7))})
    for col in sorted(ex.JEV_COLUMNS):
        for m in materials:
            assert col not in _keys_deep(m), col
        for f in fixture:
            assert col not in _keys_deep(f), col
    # The Jev file is where they are.
    assert {"state", "decision"} <= set(replay[0])
    assert replay[0]["decision"]["raw_answers"] == {"should_ask_human": {"noul": 0.3}}


def test_jev_columns_cover_every_decision_result_key() -> None:
    from spirrow_mindwire.decider.result import (
        DecisionOutcome,
        DecisionResult,
        decision_result_to_dict,
    )

    dr = DecisionResult(
        outcome=DecisionOutcome.TRANSPORT_ERROR,
        decision_id=None,
        provider=None,
        raw_answers=None,
        verdict=None,
        policy="p",
        questions_version="v",
        latency_ms=None,
        error="x",
    )
    assert set(decision_result_to_dict(dr)) <= ex.JEV_COLUMNS
    assert set(ex._DECISION_KEYS) == set(decision_result_to_dict(dr))


def test_replay_out_inside_out_dir_is_refused(tmp_path: Path) -> None:
    log = tmp_path / "c.log"
    log.write_text("", encoding="utf-8")
    rc = ex.main(
        [
            "--log",
            str(log),
            "--rules-sha256",
            SHA,
            "--project",
            "p",
            "--out-dir",
            str(tmp_path / "lab"),
            "--replay-out",
            str(tmp_path / "lab" / "replay.jsonl"),
        ]
    )
    assert rc == 2
    assert not (tmp_path / "lab").exists()


# --------------------------------------------------------------------------- report formats


def test_outputs_join_in_tierc_eval_report() -> None:
    """The msg-4635 advisory as corrected by msg-4636: ``--replay`` / ``--fixture`` shapes, keyed
    the same, ``msg_id`` = ``latest_msg_id`` on both sides."""
    rows = [_row(), _row(thread_id="T-b", round_index=1, latest="msg-2")]
    materials, fixture, replay, _ = ex.build_outputs(
        rows, SHA, {"T-a": ("p", _thread(9)), "T-b": ("q", _thread(3))}
    )
    joined = report.join(replay, fixture)
    assert [(r.key, r.msg_id, r.author, r.project) for r in joined] == [
        (("T-a", 6), "msg-6", "Bohr", "p"),
        (("T-b", 2), "msg-2", "Bohr", "q"),
    ]
    assert all(r.live_entry and r.roster_source == "logged" for r in joined)
    assert {(m["thread_id"], m["round_index"]) for m in materials} == {r.key for r in joined}
    # Same conductor round in two threads, and the per-thread position is the key.
    assert [f["conductor_round_index"] for f in fixture] == [1, 1]


def test_parse_log_lines_reads_only_decider_rows() -> None:
    rec = _row()
    lines = [
        "2026-10-01 INFO spirrow_mindwire.conductor something else",
        f"2026-10-01 INFO spirrow_mindwire.decider.hook decider_decision {json.dumps(rec)}",
    ]
    assert ex.parse_log_lines(lines) == [rec]
    with pytest.raises(ex.ExportError):
        ex.parse_log_lines(["decider_decision {not json"])


# --------------------------------------------------------------------------- end to end


class _Client:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    async def decide(self, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        return {
            "answers": {
                "should_ask_human": {"noul": 0.2},
                "matched_rule": {"choice": "none", "probabilities": {"none": 1.0}},
            },
            "provider": "jev",
            "decision_id": "d-e2e",
            "latency_ms": 3,
        }

    async def aclose(self) -> None:
        return None


@pytest.mark.anyio
async def test_hook_log_line_exports_the_state_jev_was_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real hook row → exporter: the Jev-side ``state`` equals what the adapter sent, and the
    labeller material ends at the head the hook logged."""
    rules = load_tierc_rules(tierc_rules_template_path())
    c = _Client()
    adapter = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: c, rules=rules)
    roster = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER}
    thread = [
        ThreadMessage("msg-0", "Bohr", "plan\n\nNEXT: Einstein", "Einstein"),
        ThreadMessage("msg-1", "Einstein", "critique\n\nNEXT: Bohr", "Bohr"),
        ThreadMessage("msg-2", "Bohr", "need a call\n\nNEXT: human", "human"),
    ]
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.decider.hook"):
        await run_tierc_hook(
            adapter,
            thread_id="T-e2e",
            round_index=0,
            roster=roster,
            messages=thread,
            stop="human",
            is_forced=False,
            target_role=None,
            spawn_blocked=False,
            naysayer_role=Role.NAYSAYER,
            author_wrote_next_human=True,
        )
    rows = ex.parse_log_lines(r.getMessage() for r in caplog.records)
    (row,) = rows
    grown = [ex.Message(m.msg_id, m.author, m.content, "t") for m in thread]
    grown.append(ex.Message("msg-3", "human", "later", "t"))
    materials, fixture, replay, summary = ex.build_outputs(
        rows, str(row["rules_sha256"]), {"T-e2e": ("p", grown)}
    )
    assert summary["counted"] == 1
    assert replay[0]["state"] == json.loads(c.bodies[0]["state"])
    assert replay[0]["decision"]["decision_id"] == "d-e2e"
    assert materials[0]["msg_id"] == "msg-2" and materials[0]["round_index"] == 2
    assert fixture[0]["roster"] == {"Bohr": "proposer", "Einstein": "naysayer"}
