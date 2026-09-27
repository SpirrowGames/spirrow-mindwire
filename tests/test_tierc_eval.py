"""T-decider-tierc-replay-eval: fixture builder, replay billing brake / resume, report maths.

Pins the conditions msg-4224 / msg-4226 attached to the design (Einstein approval msg-4227):

* seal (a) — nothing after the escalation reaches the state;
* seal (b) — roster = authors up to the escalation, ``roster_source`` recorded;
* seal (c) — ``now`` is mandatory in the builder;
* the builder's gate result is exactly the live ``compute_gate_result`` (retry_lookup=never);
* neither new script imports ``spirrow_mindwire.decision_request`` and both run with the
  dashboard env var unset (ADR-2026-09-18-22 fail-fast stays out of the path);
* ``--max-calls`` stops before exceeding the cap; ``--resume`` skips keys already decided.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.decider.hook import compute_gate_result, never_retry
from spirrow_mindwire.decider.questions import TIERC_QUESTIONS_V1
from spirrow_mindwire.decider.result import DecisionOutcome, DecisionResult
from spirrow_mindwire.decider.verdict import build_out_of_gate_verdict
from spirrow_mindwire.decider.wire import gate_result_to_dict
from spirrow_mindwire.value_objects import Role

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"{name}_module", ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{name}_module"] = mod
    spec.loader.exec_module(mod)
    return mod


builder = _load("build_tierc_eval_fixture")
report = _load("tierc_eval_report")
replay = _load("decider_replay")

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _msg(i: int, author: str, content: str, role: str | None = None) -> Any:
    return builder.RawMessage(
        msg_id=f"msg-{i}",
        author=author,
        content=content,
        timestamp=T0 + timedelta(minutes=i),
        role=role,
        next_participant=None,
    )


def _thread() -> list[Any]:
    return [
        _msg(1, "Fermi", "goal\n\nNEXT: Bohr"),
        _msg(2, "Bohr", "design\n\nNEXT: Einstein", role="proposer"),
        _msg(3, "Einstein", "ok\n\nNEXT: Bohr", role="naysayer"),
        _msg(4, "Bohr", "please decide\n\nTIER-C: scope\nNEXT: human", role="proposer"),
        _msg(5, "Heisenberg", "FUTURE-ONLY-TEXT\n\nNEXT: Bohr", role="implementer"),
        _msg(6, "Latecomer", "FUTURE-ONLY-TEXT-2\n\nNEXT: human", role="proposer"),
    ]


def _row(current: dict[str, Role] | None = None) -> dict[str, Any]:
    msgs = _thread()
    return dict(
        builder.build_eval_row(
            project="spirrow-mindwire",
            thread_id="T-x",
            messages=msgs,
            head_index=3,
            current_roster=current or {},
            now=msgs[3].timestamp,
            set_name="eval",
        )
    )


def test_seal_a_nothing_after_the_escalation_reaches_the_state() -> None:
    row = _row()
    blob = json.dumps(row, ensure_ascii=False)
    assert "FUTURE-ONLY-TEXT" not in blob
    assert "msg-5" not in blob and "msg-6" not in blob
    assert row["recent_events"][0]["msg_id"] == "msg-4"
    assert row["head_summary"].startswith("please decide")
    assert row["round_index"] == 3
    assert row["parsed_next"] == "human"


def test_seal_b_roster_is_authors_so_far_with_source() -> None:
    row = _row()
    assert row["roster"] == {"Bohr": "proposer", "Einstein": "naysayer"}
    assert "Heisenberg" not in row["roster"] and "Latecomer" not in row["roster"]
    assert row["roster_source"] == "historical"
    assert row["live_entry"] is True

    fallback = _row(current={"Fermi": Role.PROPOSER})
    assert fallback["roster"]["Fermi"] == "proposer"
    assert fallback["roster_source"] == "current_fallback"


def test_seal_c_now_is_mandatory() -> None:
    msgs = _thread()
    with pytest.raises(ValueError, match="now is required"):
        builder.build_eval_row(
            project="p",
            thread_id="T-x",
            messages=msgs,
            head_index=3,
            current_roster={},
            now=None,
            set_name="eval",
        )


def test_gate_is_the_live_compute_gate_result() -> None:
    msgs = _thread()
    expected = compute_gate_result(
        body=msgs[3].content, author="Bohr", now=msgs[3].timestamp, retry_lookup=never_retry
    )
    assert _row()["gate_result"] == gate_result_to_dict(expected)


def test_escalation_selection_and_sets() -> None:
    msgs = _thread()
    rows = builder.rows_for_thread(project="p", thread_id="T-x", messages=msgs, current_roster={})
    assert [r["msg_id"] for r, _ in rows] == ["msg-4", "msg-6"]
    assert rows[0][0]["tier_c_label"] == "scope"
    old = builder.RawMessage(
        msg_id="m",
        author="Bohr",
        content="x\nTIER-C: scope\nNEXT: human",
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        role=None,
        next_participant=None,
    )
    assert builder.classify_set(old, "scope") == "retro_candidate"
    assert builder.classify_set(old, None) is None
    assert builder.classify_set(msgs[3], None) == "eval"


def test_label_template_is_blind() -> None:
    fixture, labels, manifest = builder.build_outputs([("p", "T-x", _thread())], {})
    assert manifest["rows"] == 2
    for lab in labels:
        assert lab["label"] is None and lab["category"] is None
        assert "decision" not in lab and "gate_result" not in lab
    # the fixture carries no label field at all (msg-4219 §2)
    assert all("label" not in r for r in fixture)


def test_duplicate_key_across_projects_raises() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        builder.build_outputs([("p1", "T-x", _thread()), ("p2", "T-x", _thread())], {})


@pytest.mark.parametrize("script", ["build_tierc_eval_fixture", "tierc_eval_report"])
def test_scripts_do_not_import_decision_request(script: str) -> None:
    env = {k: v for k, v in os.environ.items() if "DASHBOARD" not in k}
    path = ROOT / "scripts" / f"{script}.py"
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('m', r'{path}')\n"
        "m = importlib.util.module_from_spec(spec); sys.modules['m'] = m\n"
        "spec.loader.exec_module(m)\n"
        "bad = [k for k in sys.modules if k.startswith('spirrow_mindwire.decision_request')]\n"
        "print(bad); sys.exit(1 if bad else 0)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ---------------------------------------------------------------------------
# replay: --max-calls / --resume
# ---------------------------------------------------------------------------


def _answers(g: float, s: float) -> dict[str, float]:
    return {q.key: (g if q.kind.value == "genuine" else s) for q in TIERC_QUESTIONS_V1}


class _FakeClient:
    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None


def _fake_replay(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    async def fake_decide_once(state: Any, *, client: Any, policy: str) -> DecisionResult:
        calls.append(state.round_index)
        a = _answers(0.1, 0.9)
        return DecisionResult(
            outcome=DecisionOutcome.EVALUATED,
            decision_id=f"d{state.round_index}",
            provider="jev",
            raw_answers={k: {"type": "noul", "noul": v} for k, v in a.items()},
            verdict=build_out_of_gate_verdict(a),
            policy=policy,
        )

    monkeypatch.setattr(replay, "decide_once", fake_decide_once)
    monkeypatch.setattr(replay, "LexoraClient", _FakeClient)
    return calls


def _fixture(tmp_path: Path, n: int) -> Path:
    p = tmp_path / "fx.jsonl"
    rows = []
    for i in range(n):
        rows.append(
            {
                "thread_id": "T-x",
                "round_index": i,
                "roster": {"Bohr": "proposer"},
                "head_summary": "h",
                "recent_events": [],
                "parsed_next": "human",
                "prev_next": None,
                "diff_stat": None,
                "gate_result": {
                    "verdict": "bounce",
                    "kind": "BOUNCED",
                    "label": None,
                    "retry_admit_reason": None,
                    "bounce_reason": "no-label",
                },
            }
        )
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def test_max_calls_stops_before_exceeding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _fake_replay(monkeypatch)
    out = tmp_path / "out.jsonl"
    rc = replay.run_tierc_replay(
        fixture=_fixture(tmp_path, 5), out=out, mode="dry-run", endpoint="http://x", max_calls=2
    )
    assert rc == 0
    assert calls == [0, 1]
    assert [json.loads(line)["round_index"] for line in out.read_text().splitlines()] == [0, 1]


def test_resume_skips_done_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _fake_replay(monkeypatch)
    out = tmp_path / "out.jsonl"
    fx = _fixture(tmp_path, 5)
    replay.run_tierc_replay(fixture=fx, out=out, mode="dry-run", endpoint="http://x", max_calls=2)
    calls.clear()
    replay.run_tierc_replay(fixture=fx, out=out, mode="dry-run", endpoint="http://x", resume=True)
    assert calls == [2, 3, 4]
    keys = [json.loads(line)["round_index"] for line in out.read_text().splitlines()]
    assert keys == [0, 1, 2, 3, 4]


def test_brake_flags_need_endpoint(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, 1)
    assert replay.run_tierc_replay(fixture=fx, out=None, mode="dry-run", max_calls=1) == 2
    assert (
        replay.run_tierc_replay(
            fixture=fx, out=None, mode="dry-run", endpoint="http://x", resume=True
        )
        == 2
    )


# ---------------------------------------------------------------------------
# report maths
# ---------------------------------------------------------------------------


def _lab(label: str, rationale: str = "r") -> Any:
    return report.Label(label=label, category=None, rationale=rationale)


def test_consensus_disagreement_is_ambiguous() -> None:
    a = {("T", 1): _lab("genuine"), ("T", 2): _lab("spurious"), ("T", 3): _lab("spurious")}
    b = {("T", 1): _lab("genuine"), ("T", 2): _lab("genuine")}
    c = report.consensus({"Bohr": a, "Einstein": b})
    assert c == {("T", 1): "genuine", ("T", 2): "ambiguous", ("T", 3): "ambiguous"}


def test_unknown_label_raises() -> None:
    with pytest.raises(ValueError, match="unknown label"):
        report.load_labels([{"thread_id": "T", "round_index": 1, "label": "genuin"}])


def test_kappa() -> None:
    a = {("T", i): x for i, x in enumerate(["g", "g", "s", "s"])}
    k, n = report.cohen_kappa(a, dict(a))
    assert n == 4 and k == pytest.approx(1.0)
    b = {("T", i): x for i, x in enumerate(["g", "s", "g", "s"])}
    k2, _ = report.cohen_kappa(a, b)
    assert k2 == pytest.approx(0.0)


def _row_obj(i: int, g: float, s: float) -> Any:
    return report.Row(
        key=("T", i),
        msg_id=f"msg-{i}",
        author="Bohr",
        project="p",
        gate_bucket="x",
        grey_zone=False,
        live_entry=True,
        roster_source="historical",
        body_head="b",
        scores=_answers(g, s),
        server_verdict="UNSURE",
        outcome="evaluated",
        latency_ms=10,
    )


def test_headline_and_misses_recompute_from_raw_answers() -> None:
    rows = [_row_obj(1, 0.05, 0.9), _row_obj(2, 0.05, 0.9), _row_obj(3, 0.5, 0.1)]
    labs = {
        "Bohr": {
            ("T", 1): _lab("spurious"),
            ("T", 2): _lab("genuine", "goal change"),
            ("T", 3): _lab("genuine"),
        }
    }
    truth = report.consensus(labs)
    th = report.TierCThresholds()
    h = report.headline(rows, truth, th)
    assert h["reduction (spurious → LIKELY_NOT)"].startswith("1/1")
    assert h["recall genuine (reaches human)"].startswith("1/2")
    m = report.misses(rows, truth, labs, th)
    assert len(m) == 1 and "msg-2" in m[0] and "goal change" in m[0]


def test_auc() -> None:
    assert report.auc([0.9, 0.8], [0.1, 0.2]) == pytest.approx(1.0)
    assert report.auc([0.5], [0.5]) == pytest.approx(0.5)
    assert report.auc([], [0.1]) is None


def test_render_smoke_reports_grey_zone_first() -> None:
    rows = [_row_obj(1, 0.05, 0.9)]
    text = report.render(rows, {"Bohr": {("T", 1): _lab("spurious")}}, report.TierCThresholds())
    head = text.split("## (A) Jev")[0]
    assert "(B) grey zone" in head and "0/1" in head
