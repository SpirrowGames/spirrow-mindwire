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


def test_outputs_split_and_following_only_in_materials() -> None:
    population, fixture, materials, manifest = builder.build_outputs([("p", "T-x", _thread())], {})
    assert manifest["rows"] == 2
    # msg-4 (Bohr) and msg-6 (Latecomer) are both proposer-authored eval-window rows.
    assert [r["eval_set"] for r in population] == ["body", "body"]
    assert len(fixture) == len(materials) == 2
    # the fixture carries no label and none of the later messages (seal (a) holds per row)
    assert all("label" not in r for r in fixture)
    assert "FUTURE-ONLY-TEXT" not in json.dumps(fixture[0], ensure_ascii=False)
    m0 = materials[0]
    assert [x["msg_id"] for x in m0["following"]] == ["msg-5", "msg-6"]
    assert [x["msg_id"] for x in m0["prior"]] == ["msg-1", "msg-2", "msg-3"]
    assert m0["body"].startswith("please decide") and m0["body_truncated"] is False
    assert "gate_result" not in m0 and "decision" not in m0


def test_eval_set_rules() -> None:
    base = {"set": "eval", "live_entry": True, "gate_result": {"verdict": "bounce"}}
    assert builder.eval_set_of(base) == "body"
    assert builder.eval_set_of({**base, "live_entry": False}) == "gate_only"
    admit = {**base, "live_entry": False, "gate_result": {"verdict": "admit"}}
    assert builder.eval_set_of(admit) == "supplement"
    assert builder.eval_set_of({**base, "set": "retro_candidate"}) == "supplement"


def test_material_body_cap() -> None:
    msgs = _thread()
    long = builder.RawMessage(
        msg_id="msg-9",
        author="Bohr",
        content="x" * 9000 + "\nNEXT: human",
        timestamp=T0,
        role="proposer",
        next_participant=None,
    )
    row = {
        "thread_id": "T",
        "round_index": 0,
        "project": "p",
        "msg_id": "msg-9",
        "author": "Bohr",
        "posted_at": "t",
        "eval_set": "body",
    }
    m = builder.material_row(row, [*msgs[:2], long], 2)
    assert len(m["body"]) == 8000 and m["body_truncated"] is True and m["body_chars"] > 9000


def test_duplicate_key_across_projects_raises() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        builder.build_outputs([("p1", "T-x", _thread()), ("p2", "T-x", _thread())], {})


@pytest.mark.parametrize(
    "script", ["build_tierc_eval_fixture", "tierc_eval_report", "label_eval_set"]
)
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
        eval_set="body",
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


def test_category_other_counts_as_ambiguous() -> None:
    labs = report.load_labels(
        [{"thread_id": "T", "round_index": 1, "label": "spurious", "category": "OTHER"}]
    )
    assert labs[("T", 1)].label == "ambiguous"


def test_supplement_is_not_in_headline() -> None:
    body = _row_obj(1, 0.05, 0.9)
    sup = report.Row(**{**body.__dict__, "key": ("T", 2), "eval_set": "supplement"})
    labs = {"Bohr": {("T", 1): _lab("spurious"), ("T", 2): _lab("genuine")}}
    text = report.render([body, sup], labs, report.TierCThresholds())
    head = text.split("## Supplement set")[0]
    assert "recall genuine (reaches human): n/a (0)" in head
    assert "179 rows cannot be reproduced" in text


# ---------------------------------------------------------------------------
# labeller (msg-4231 / msg-4233)
# ---------------------------------------------------------------------------

labeller = _load("label_eval_set")


def _materials(n: int) -> list[dict[str, Any]]:
    return [
        {
            "thread_id": "T",
            "round_index": i,
            "project": "p",
            "author": "Bohr",
            "posted_at": "t",
            "eval_set": "body",
            "body": f"body {i}",
            "body_truncated": False,
            "body_chars": 6,
            "prior": [],
            "following": [],
        }
        for i in range(n)
    ]


def _answer(keys: list[tuple[str, int]], label: str = "spurious") -> str:
    lines = [
        json.dumps(
            {"thread_id": t, "round_index": r, "label": label, "category": "IMPL", "rationale": "x"}
        )
        for t, r in keys
    ]
    return "```jsonl\n" + "\n".join(lines) + "\n```"


def _items_sent(messages: list[Any]) -> list[dict[str, Any]]:
    return list(json.loads(messages[1].content.split("\n\n", 1)[1]))


class _FakeLexora:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.sent: list[list[Any]] = []

    async def chat_completion(self, *, model: str, messages: list[Any], max_tokens: int) -> Any:
        from spirrow_mindwire.lexora.client import ChatCompletion

        self.sent.append(messages)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        if callable(r):
            r = r(messages)
        return ChatCompletion(
            content=r,
            reasoning_content=None,
            finish_reason="stop",
            model="m-" + model,
            usage={"prompt_tokens": 5},
        )

    async def stats_costs_recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [{"model": "m-naysayer", "tokens_input": 5, "backend": "gemini", "cost_usd": 0.1}]


def _echo(messages: list[Any]) -> str:
    return _answer([(i["thread_id"], i["round_index"]) for i in _items_sent(messages)])


async def _nosleep(_: float) -> None:
    return None


def _run(client: Any, tmp_path: Path, n: int, batch: int = 15, who: str = "naysayer-tier") -> Any:
    import asyncio

    return asyncio.run(
        labeller.label_all(
            client=client,
            labeller=who,
            materials=_materials(n),
            system="SYS",
            out_dir=tmp_path,
            batch_size=batch,
            sleep=_nosleep,
        )
    )


def _label_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_parse_answer_rejects_wrong_keys_and_values() -> None:
    keys = [("T", 0), ("T", 1)]
    assert len(labeller.parse_answer(_answer(keys), keys)) == 2
    with pytest.raises(ValueError, match="keys differ"):
        labeller.parse_answer(_answer(keys[:1]), keys)
    with pytest.raises(ValueError, match="label"):
        labeller.parse_answer(_answer(keys, label="maybe"), keys)
    with pytest.raises(ValueError, match="block"):
        labeller.parse_answer("no block", keys)


def test_labels_written_with_provenance(tmp_path: Path) -> None:
    stats = _run(_FakeLexora([_echo, _echo]), tmp_path, 20)
    rows = _label_lines(tmp_path / "labels.naysayer-tier.jsonl")
    assert len(rows) == 20 and stats["batches"] == 2
    assert rows[0]["backend"] == "gemini" and rows[0]["batch"] == 0 and rows[-1]["batch"] == 1
    assert rows[0]["prompt_sha256"] == labeller.sha256_text("SYS")


def test_format_failure_retries_once_then_stops_and_writes_nothing(tmp_path: Path) -> None:
    fake = _FakeLexora([_echo, "garbage", "garbage again"])
    with pytest.raises(labeller.LabelStopError, match="format failure"):
        _run(fake, tmp_path, 20)
    assert len(_label_lines(tmp_path / "labels.naysayer-tier.jsonl")) == 15  # batch 1 not written
    assert len(list((tmp_path / "failures").iterdir())) == 1
    assert len(fake.sent) == 3


def test_format_failure_then_success_on_retry(tmp_path: Path) -> None:
    _run(_FakeLexora(["garbage", _echo]), tmp_path, 3)
    assert len(_label_lines(tmp_path / "labels.naysayer-tier.jsonl")) == 3


def test_transport_retries_three_times_then_stops(tmp_path: Path) -> None:
    from spirrow_mindwire.lexora.client import LexoraHTTPError, LexoraTimeoutError

    ok = _FakeLexora(
        [
            LexoraTimeoutError("t"),
            LexoraHTTPError("5", status_code=502),
            LexoraHTTPError("conn"),
            _echo,
        ]
    )
    _run(ok, tmp_path, 2)  # 3 transport failures are not a format failure
    with pytest.raises(labeller.LabelStopError, match="retries exhausted"):
        _run(_FakeLexora([LexoraTimeoutError("t")] * 4), tmp_path / "b", 2)
    with pytest.raises(labeller.LabelStopError, match="non-retryable"):
        _run(_FakeLexora([LexoraHTTPError("bad", status_code=400)]), tmp_path / "c", 2)


def test_resume_skips_labelled_keys(tmp_path: Path) -> None:
    _run(_FakeLexora([_echo]), tmp_path, 3)
    fake = _FakeLexora([_echo])
    _run(fake, tmp_path, 5)
    assert [i["round_index"] for i in _items_sent(fake.sent[0])] == [3, 4]


def test_no_naysayer_preamble_and_no_other_outputs_are_sent(tmp_path: Path) -> None:
    import asyncio

    from spirrow_mindwire.naysayer.principles import build_preamble

    prompt = (ROOT / "eval" / "tierc" / "label_prompt.md").read_text(encoding="utf-8")
    rubric = (ROOT / "eval" / "tierc" / "RUBRIC.md").read_text(encoding="utf-8")
    system = labeller.system_prompt(prompt, rubric)
    fake = _FakeLexora([_echo])
    asyncio.run(
        labeller.label_all(
            client=fake,
            labeller="frontier-tier",
            materials=_materials(2),
            system=system,
            out_dir=tmp_path,
            sleep=_nosleep,
        )
    )
    sent = "\n".join(m.content for m in fake.sent[0])
    preamble = build_preamble()
    assert preamble not in sent
    assert preamble.splitlines()[0] not in sent
    item = _items_sent(fake.sent[0])[0]
    assert "decision" not in item and "label" not in item and "gate_result" not in item
    assert rubric in sent


def test_max_batches_bounds_a_run_and_resume_finishes_it(tmp_path: Path) -> None:
    import asyncio

    def go(fake: _FakeLexora, max_batches: int | None) -> Any:
        return asyncio.run(
            labeller.label_all(
                client=fake,
                labeller="naysayer-tier",
                materials=_materials(12),
                system="SYS",
                out_dir=tmp_path,
                batch_size=5,
                sleep=_nosleep,
                max_batches=max_batches,
            )
        )

    first = _FakeLexora([_echo, _echo, _echo])
    stats = go(first, 1)
    assert len(first.sent) == 1 and stats["batches"] == 1 and stats["remaining"] == 7
    rest = _FakeLexora([_echo, _echo])
    stats = go(rest, None)
    assert stats["batches"] == 2 and stats["remaining"] == 0
    rows = _label_lines(tmp_path / "labels.naysayer-tier.jsonl")
    assert len(rows) == 12 and sorted({r["batch"] for r in rows}) == [0, 1, 2]
    with pytest.raises(labeller.LabelStopError, match="max_batches"):
        go(_FakeLexora([]), 0)


def test_cli_refuses_existing_label_file_without_resume(tmp_path: Path) -> None:
    for name in ("label_prompt.md", "RUBRIC.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "materials.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "labels.frontier-tier.jsonl").write_text(
        json.dumps({"thread_id": "T", "round_index": 0}) + "\n", encoding="utf-8"
    )
    rc = labeller.main(["--labeller", "frontier-tier", "--dir", str(tmp_path)])
    assert rc == 1
    assert not (tmp_path / "label_runs.jsonl").exists()  # nothing was sent, nothing logged


def test_labeller_names_follow_msg_4245() -> None:
    assert labeller.LABELLERS == {"naysayer-tier": "naysayer", "frontier-tier": "frontier"}
    assert "labels.frontier-tier.jsonl" in labeller.LOCKED_FILES


def _lock_dir(tmp_path: Path, frontier_sha: str | None = None) -> Path:
    for name in ("label_prompt.md", "RUBRIC.md", "fixture.jsonl", "label_runs.jsonl"):
        (tmp_path / name).write_text("x\n" if name.endswith(".md") else "", encoding="utf-8")
    mats = [{"thread_id": "T", "round_index": i} for i in range(3)]
    (tmp_path / "materials.jsonl").write_text(
        "".join(json.dumps(m) + "\n" for m in mats), encoding="utf-8"
    )
    sha = labeller.sha256_text(labeller.system_prompt("x\n", "x\n"))
    for who, (backend, model) in {
        "naysayer-tier": ("gemini", "g-1"),
        "frontier-tier": ("frontier", "claude-fable-5-1"),
    }.items():
        use = frontier_sha if (who == "frontier-tier" and frontier_sha) else sha
        rows = [{**m, "backend": backend, "model": model, "prompt_sha256": use} for m in mats]
        (tmp_path / f"labels.{who}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
    return tmp_path


def test_lock_records_row_provenance(tmp_path: Path) -> None:
    manifest = labeller.write_lock(_lock_dir(tmp_path))
    fr = manifest["labellers"]["frontier-tier"]
    assert fr == {
        "tier": "frontier",
        "rows": 3,
        "backends": ["frontier"],
        "models": ["claude-fable-5-1"],
    }
    assert set(manifest["sha256"]) == set(labeller.LOCKED_FILES)


def test_lock_refuses_rows_labelled_under_another_prompt(tmp_path: Path) -> None:
    with pytest.raises(labeller.LabelStopError, match="prompt sha"):
        labeller.write_lock(_lock_dir(tmp_path, frontier_sha="other"))


def test_committed_eval_jsonl_files_are_strict_jsonl() -> None:
    """Every committed ``eval/tierc/**/*.jsonl`` line must be a JSON value (PR #347 gate).

    ``decider_replay.iter_fixture`` tolerates ``#`` comments, but ``jq`` and a plain
    ``json.loads(line)`` do not; a ``.jsonl`` file in the repo must parse with either.
    """
    files = sorted((ROOT / "eval" / "tierc").rglob("*.jsonl"))
    tracked = subprocess.run(
        ["git", "ls-files", "--", "eval/tierc"], cwd=ROOT, capture_output=True, text=True
    )
    if tracked.returncode == 0 and tracked.stdout.strip():
        names = set(tracked.stdout.split())
        files = [p for p in files if p.relative_to(ROOT).as_posix() in names]
    assert files, "no eval/tierc jsonl files found"
    for path in files:
        with path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                assert line.strip(), f"{path}:{lineno}: blank line in JSONL"
                json.loads(line)
