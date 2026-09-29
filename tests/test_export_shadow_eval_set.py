"""T-decider-conductor-hook step 2d — ``scripts/export_shadow_eval_set.py``.

Spec: Bohr msg-4634 DECIDED 2d-2 (no Jev column in the labellers' files — checked column by
column; out-of-scope rows excluded), msg-4636 DECIDED 2d-3 (tests 19 / 21: no state rebuild, rows
without the point-in-time columns excluded), msg-4639 DECIDED 2d-4 / 2d-6 (``tierc-v2`` only;
``following`` = 3, constants from ``build_tierc_eval_fixture``), msg-4641 DECIDED 2d-7 (tests 20,
22a-22d: terminated / quiet threads, the 72-hour boundary, determinism) and msg-4643 DECIDED 2d-8
(tests 22e-22g: the ``--as-of`` cut comes first). Test 18 lives in ``test_decider_step2.py``.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
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
builder = _load("build_tierc_eval_fixture")

SHA = "a" * 64
ROSTER = {"Bohr": "proposer", "Einstein": "naysayer", "Heisenberg": "implementer"}
T0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
AS_OF = T0 + timedelta(days=10)


def _at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def _msg(i: int, author: str = "Bohr", body: str | None = None, at: str | None = None) -> Any:
    return ex.Message(
        msg_id=f"msg-{i}",
        author=author,
        content=body if body is not None else f"body {i}\n\nNEXT: Einstein",
        timestamp=at if at is not None else _at(i),
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
    version: str = "tierc-v2",
    logged_at: str | None = None,
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
        "questions_version": version,
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
        r["logged_at"] = logged_at if logged_at is not None else _at(6.5)
        r["state_wire"] = json.dumps(
            {"thread_id": thread_id, "round_index": round_index, "gate_result": None},
            sort_keys=True,
            separators=(",", ":"),
        )
    return r


def _build(rows: list[dict[str, Any]], threads: Mapping[str, Any], as_of: datetime = AS_OF) -> Any:
    return ex.build_outputs(rows, SHA, threads, as_of)


def _bytes(out: Any) -> str:
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


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


# --------------------------------------------------------------------------- test 19 / 2d-6


def test_exporter_never_imports_the_state_builder() -> None:
    """Test 19 (msg-4636): no ``state_builder`` / ``turn_from_messages`` and no direct import
    from ``spirrow_mindwire.decider`` — the state comes from the log, never rebuilt."""
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


def test_material_constants_come_from_the_replay_builder() -> None:
    """msg-4639 DECIDED 2d-6: one definition, imported — not a copy."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    from_builder = {
        a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "build_tierc_eval_fixture"
        for a in n.names
    }
    assert from_builder == {
        "MATERIAL_BODY_MAX",
        "MATERIAL_FOLLOWING_N",
        "MATERIAL_HEAD_M",
        "MATERIAL_PRIOR_N",
    }
    assert ex.MATERIAL_FOLLOWING_N == builder.MATERIAL_FOLLOWING_N == 3


# --------------------------------------------------------------------------- test 20 / 22a-c


def test_output_is_fixed_once_three_following_exist() -> None:
    """Test 20 (msg-4641): messages after i+3 change nothing."""
    before = _build([_row()], {"T-a": ("p", _thread(10))})
    after = _build([_row()], {"T-a": ("p", _thread(15))})
    assert _bytes(before) == _bytes(after)
    (m,) = before[0]
    assert m["msg_id"] == "msg-6" and m["following_n"] == 3
    assert [f["msg_id"] for f in m["following"]] == ["msg-7", "msg-8", "msg-9"]
    assert [p["msg_id"] for p in m["prior"]] == [f"msg-{i}" for i in range(1, 6)]


def test_terminated_thread_row_is_counted_with_one_following() -> None:
    """22a: the thread ended with ``NEXT: none`` after 1 message → counted, ``following_n=1``."""
    thread = [*_thread(7), _msg(7, "human", "decided.\n\nNEXT: none")]
    materials, fixture, _, summary = _build(
        [_row()], {"T-a": ("p", thread)}, T0 + timedelta(hours=1)
    )
    assert summary["counted"] == 1 and summary["held"] == 0
    assert materials[0]["following_n"] == 1 == fixture[0]["following_n"]
    assert summary["following_n_lt_3"] == 1


def test_quiet_thread_row_is_counted_with_no_following() -> None:
    """22b: last message ≥ 72 h before ``--as-of`` → counted with ``following_n=0``."""
    materials, _, _, summary = _build([_row()], {"T-a": ("p", _thread(7))}, AS_OF)
    assert summary["counted"] == 1
    assert materials[0]["following"] == [] and materials[0]["following_n"] == 0


def test_seventy_two_hour_boundary() -> None:
    """22c: exactly 72 h after the last message counts; one second less is held."""
    last = T0 + timedelta(minutes=6)
    threads = {"T-a": ("p", _thread(7))}
    at = _build([_row()], threads, last + timedelta(hours=72))[3]
    before = _build([_row()], threads, last + timedelta(hours=72) - timedelta(seconds=1))[3]
    assert (at["counted"], at["held"]) == (1, 0)
    assert (before["counted"], before["held"]) == (0, 1)


def test_held_row_is_counted_by_number_only_then_exported_once_ready() -> None:
    """msg-4641: an active thread with < 3 following is held; once 3 more messages exist (by a
    later ``--as-of``) the row is exported."""
    as_of = T0 + timedelta(hours=1)
    short = _build([_row()], {"T-a": ("p", _thread(8))}, as_of)
    assert short[0] == [] and short[1] == [] and short[2] == []
    assert short[3]["held"] == 1 and short[3]["by_bucket"] == {ex.HELD: 1}
    ready = _build([_row()], {"T-a": ("p", _thread(10))}, as_of)
    assert ready[3]["counted"] == 1 and ready[0][0]["following_n"] == 3


# --------------------------------------------------------------------------- 22d-g (--as-of)


def test_data_after_as_of_changes_no_byte() -> None:
    """22d + 22e (msg-4643): a thread quiet for 72 h at ``--as-of`` then resumes, and new log
    rows arrive — materials / fixture / replay and the held count are byte-identical."""
    as_of = T0 + timedelta(hours=80)
    rows = [_row(), _row(thread_id="T-b", latest="msg-1", logged_at=_at(1.5))]
    t_b = [*_thread(3), _msg(3, at=(as_of - timedelta(hours=1)).isoformat())]
    threads = {"T-a": ("p", _thread(7)), "T-b": ("q", t_b)}
    first = _build(rows, threads, as_of)
    later = [
        _msg(100 + k, "human", at=(as_of + timedelta(minutes=k + 1)).isoformat()) for k in range(4)
    ]
    grown_threads = {"T-a": ("p", [*_thread(7), *later]), "T-b": ("q", [*t_b, *later])}
    late_row = _row(latest="msg-100", logged_at=(as_of + timedelta(minutes=2)).isoformat())
    second = _build([*rows, late_row], grown_threads, as_of)
    assert _bytes(first) == _bytes(second)
    # T-a was quiet 80 h → counted with 0 following; T-b has 2 following and moved 1 h ago → held.
    assert first[3]["counted"] == 1 and first[3]["held"] == 1


def test_rows_logged_after_as_of_are_in_no_bucket() -> None:
    """22f: a row with ``logged_at > as_of`` is neither counted nor held nor bucketed."""
    as_of = T0 + timedelta(hours=100)
    late = _row(logged_at=(as_of + timedelta(seconds=1)).isoformat())
    out = _build([late], {"T-a": ("p", _thread(7))}, as_of)
    assert out[0] == [] and out[3]["by_bucket"] == {} and out[3]["rows_up_to_as_of"] == 0
    on_time = _row(logged_at=as_of.isoformat())
    assert _build([on_time], {"T-a": ("p", _thread(7))}, as_of)[3]["counted"] == 1


def test_message_at_exactly_as_of_is_kept() -> None:
    """22g: ``timestamp == as_of`` stays."""
    as_of = T0 + timedelta(minutes=9)
    materials, *_ = _build([_row()], {"T-a": ("p", _thread(10))}, as_of)
    assert [f["msg_id"] for f in materials[0]["following"]] == ["msg-7", "msg-8", "msg-9"]
    cut = _build([_row()], {"T-a": ("p", _thread(10))}, as_of - timedelta(seconds=1))
    assert cut[3]["held"] == 1


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("2026-10-05T00:00:00", "no timezone"),
        ("2026-10-05", "no timezone"),
        ("yesterday", "not ISO 8601"),
        ("2026-10-12T00:00:01+00:00", "in the future"),
    ],
)
def test_as_of_must_be_aware_and_not_future(raw: str, match: str) -> None:
    """22g: naive or future ``--as-of`` is an error."""
    with pytest.raises(ex.ExportError, match=match):
        ex.parse_as_of(raw, datetime(2026, 10, 12, tzinfo=UTC))
    assert ex.parse_as_of("2026-10-12T09:00:00+09:00", datetime(2026, 10, 12, tzinfo=UTC))


def test_main_refuses_bad_as_of(tmp_path: Path) -> None:
    log = tmp_path / "c.log"
    log.write_text("", encoding="utf-8")
    base = ["--log", str(log), "--rules-sha256", SHA, "--project", "p"]
    base += ["--out-dir", str(tmp_path / "lab"), "--replay-out", str(tmp_path / "r.jsonl")]
    now = datetime(2026, 10, 12, tzinfo=UTC)
    assert ex.main([*base, "--as-of", "2026-10-11T00:00:00"], now=now) == 2
    assert ex.main([*base, "--as-of", "2026-10-13T00:00:00+00:00"], now=now) == 2
    assert not (tmp_path / "lab").exists()


# --------------------------------------------------------------------------- test 21 / scope


def test_rows_without_point_in_time_columns_are_not_counted() -> None:
    """Test 21 (msg-4636): a row written before the columns existed is counted by bucket only."""
    rows = [_row(wire=False), _row(thread_id="T-b", latest="msg-3")]
    materials, fixture, replay, summary = _build(rows, {"T-b": ("p", _thread(7))})
    assert [m["thread_id"] for m in materials] == ["T-b"]
    assert len(fixture) == len(replay) == 1
    assert summary["by_bucket"] == {ex.COUNTED: 1, ex.NO_POINT_IN_TIME: 1}


@pytest.mark.parametrize(
    ("row", "bucket"),
    [
        (_row(wire=False), "no_point_in_time_columns"),
        (_row(latest=None), "no_point_in_time_columns"),
        ({**_row(), "logged_at": None}, "no_point_in_time_columns"),
        (_row(routed="forced_naysayer"), "routed:forced_naysayer"),
        (_row(routed="spawn_blocked"), "routed:spawn_blocked"),
        (_row(version="tierc-v1"), "questions_version_not_tierc_v2"),
        (_row(sha="b" * 64), "rules_sha256_mismatch"),
        (_row(sha=None), "rules_sha256_mismatch"),
        (_row(), "candidate"),
    ],
)
def test_classify(row: dict[str, Any], bucket: str) -> None:
    """2d-1 / 2d-3 / 2d-4: the scope, and the separate forced / spawn table."""
    assert ex.classify(row, SHA) == bucket


def test_out_of_scope_rows_need_no_thread_fetch() -> None:
    rows = [_row(routed="forced_naysayer"), _row(routed="spawn_blocked"), _row(sha="c" * 64)]
    materials, _, _, summary = _build(rows, {})
    assert materials == []
    assert summary["counted"] == 0 and summary["rows_up_to_as_of"] == 3


def test_duplicate_latest_msg_id_stops_the_export() -> None:
    with pytest.raises(ex.ExportError, match="msg-6"):
        _build([_row(), _row(round_index=4)], {"T-a": ("p", _thread(10))})


def test_unfetched_thread_or_missing_message_stops_the_export() -> None:
    with pytest.raises(ex.ExportError, match="not fetched"):
        _build([_row()], {})
    with pytest.raises(ex.ExportError, match="not in the fetched thread"):
        _build([_row()], {"T-a": ("p", _thread(3))})


# --------------------------------------------------------------------------- double blind


def test_labeller_files_carry_no_jev_column() -> None:
    """msg-4634 DECIDED 2d-2 negative test: column by column, at every depth."""
    materials, fixture, replay, _ = _build([_row()], {"T-a": ("p", _thread(10))})
    for col in sorted(ex.JEV_COLUMNS):
        for m in materials:
            assert col not in _keys_deep(m), col
        for f in fixture:
            assert col not in _keys_deep(f), col
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
            "--as-of",
            "2026-10-01T00:00:00+00:00",
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
    """``--replay`` / ``--fixture`` shapes, keyed the same, ``msg_id`` = ``latest_msg_id``."""
    rows = [_row(), _row(thread_id="T-b", round_index=1, latest="msg-2", logged_at=_at(2.5))]
    materials, fixture, replay, _ = _build(
        rows, {"T-a": ("p", _thread(12)), "T-b": ("q", _thread(6))}
    )
    joined = report.join(replay, fixture)
    assert [(r.key, r.msg_id, r.author, r.project) for r in joined] == [
        (("T-a", 6), "msg-6", "Bohr", "p"),
        (("T-b", 2), "msg-2", "Bohr", "q"),
    ]
    assert all(r.live_entry and r.roster_source == "logged" for r in joined)
    assert all(r.questions_version == "tierc-v2" and r.ask_score == 0.3 for r in joined)
    assert [r.following_n for r in joined] == [3, 3]
    assert {(m["thread_id"], m["round_index"]) for m in materials} == {r.key for r in joined}
    assert [f["conductor_round_index"] for f in fixture] == [1, 1]


def test_parse_log_lines_reads_only_decider_rows() -> None:
    rec = _row()
    lines = [
        "INFO:spirrow_mindwire.conductor:something else",
        f"INFO:spirrow_mindwire.decider.hook:decider_decision {json.dumps(rec)}",
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
    """A real hook row → exporter: the Jev-side ``state`` equals what the adapter sent, the
    row's ``logged_at`` is the hook's ``now`` and the material is keyed at the logged head."""
    rules = load_tierc_rules(tierc_rules_template_path())
    c = _Client()
    adapter = DeciderLexoraAdapter(tierc_mode="shadow", client_factory=lambda: c, rules=rules)
    roster = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER}
    thread = [
        ThreadMessage("msg-0", "Bohr", "plan\n\nNEXT: Einstein", "Einstein"),
        ThreadMessage("msg-1", "Einstein", "critique\n\nNEXT: Bohr", "Bohr"),
        ThreadMessage("msg-2", "Bohr", "need a call\n\nNEXT: human", "human"),
    ]
    decided = T0 + timedelta(minutes=3)
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
            now=decided,
        )
    rows = ex.parse_log_lines(r.getMessage() for r in caplog.records)
    (row,) = rows
    assert row["logged_at"] == decided.isoformat()
    msgs = [ex.Message(m.msg_id, m.author, m.content, _at(k)) for k, m in enumerate(thread)]
    msgs.append(ex.Message("msg-3", "human", "ok\n\nNEXT: none", _at(4)))
    materials, fixture, replay, summary = ex.build_outputs(
        rows, str(row["rules_sha256"]), {"T-e2e": ("p", msgs)}, T0 + timedelta(hours=1)
    )
    assert summary["counted"] == 1
    assert replay[0]["state"] == json.loads(c.bodies[0]["state"])
    assert replay[0]["decision"]["decision_id"] == "d-e2e"
    assert materials[0]["msg_id"] == "msg-2" and materials[0]["round_index"] == 2
    assert materials[0]["following_n"] == 1
    assert fixture[0]["roster"] == {"Bohr": "proposer", "Einstein": "naysayer"}
