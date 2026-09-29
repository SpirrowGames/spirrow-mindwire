"""T-decider-conductor-hook step 2d — ``tierc_eval_report.py`` v2 path.

Spec: Bohr msg-4639 DECIDED 2d-4 (INVALID on any ``NO_VERDICT``; ``MALFORMED`` counted) and
DECIDED 2d-5 (recompute by ``questions_version``; v2 rows never ``NO_VERDICT``; the 0.60 / 0.40
boundaries; the v1 ``report.md`` unchanged byte for byte), msg-4641 (``SWEEP_V2`` a constant table
holding the primary hypothesis; the ``following_n < 3`` table).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.decider.verdict import (
    DEFAULT_V2_ASK_MIN,
    DEFAULT_V2_NOT_ASK_MAX,
    TierCV2Thresholds,
)

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval" / "tierc"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"{name}_module", ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{name}_module"] = mod
    spec.loader.exec_module(mod)
    return mod


report = _load("tierc_eval_report")


def _rec(key: tuple[str, int], ask: float | None, outcome: str = "evaluated") -> dict[str, Any]:
    answers: dict[str, dict[str, Any]] | None = (
        None if ask is None else {"should_ask_human": {"noul": ask}}
    )
    if outcome == "no_verdict_malformed":
        answers = {"should_ask_human": {"noul": "bad"}}
    return {
        "thread_id": key[0],
        "round_index": key[1],
        "questions_version": "tierc-v2",
        "state": {"head_summary": "h", "gate_result": None},
        "decision": {"outcome": outcome, "raw_answers": answers, "latency_ms": 4, "verdict": None},
    }


def _fx(key: tuple[str, int], following_n: int = 3) -> dict[str, Any]:
    return {
        "thread_id": key[0],
        "round_index": key[1],
        "msg_id": f"m-{key[0]}-{key[1]}",
        "author": "Bohr",
        "eval_set": "shadow",
        "live_entry": True,
        "roster_source": "logged",
        "following_n": following_n,
    }


def _lab(label: str) -> Any:
    return report.Label(label=label, category=None, rationale="")


def _render(recs: list[dict[str, Any]], fx: list[dict[str, Any]], truth: dict[Any, str]) -> str:
    rows = report.join(recs, fx)
    labs = {"a": {k: _lab(v) for k, v in truth.items()}}
    return str(report.render(rows, labs, report.TierCThresholds()))


# --------------------------------------------------------------------------- v1 regression


def test_v1_report_md_is_unchanged_byte_for_byte() -> None:
    """2d-5: the committed replay report is regenerated from its own inputs, identically."""
    fixture = report.read_jsonl(EVAL / "fixture.jsonl")
    sha = report.file_sha256(EVAL / "fixture.jsonl")
    corr = report.load_corrections(
        EVAL / "corrections" / "2026-09-28-roster-selection.json", fixture, sha
    )
    # report.md was produced on Windows; the path string it embeds is that spelling.
    corr = dataclasses.replace(
        corr, path="eval\\tierc\\corrections\\2026-09-28-roster-selection.json"
    )
    labellers = {
        who: report.load_labels(report.read_jsonl(EVAL / f"labels.{who}.jsonl"))
        for who in ("naysayer-tier", "frontier-tier")
    }
    rows = report.join(report.read_jsonl(EVAL / "replay.jsonl"), fixture)
    replayed = len(rows)
    rows, labellers = report.apply_corrections(rows, labellers, corr)
    assert {r.questions_version for r in rows} == {"tierc-v1"}
    text = report.render(
        rows,
        labellers,
        report.TierCThresholds(),
        report.read_jsonl(EVAL / "population.jsonl"),
        corr,
        replayed,
        th_v2=TierCV2Thresholds(),
    )
    committed = (EVAL / "report.md").read_bytes().decode("utf-8")
    generated = text + "\n"
    # Line by line first, so a failure names the first differing line.
    gen_lines = generated.splitlines()
    assert committed.splitlines()[: len(gen_lines)] == gen_lines
    assert committed.startswith(generated)
    assert committed[len(generated) :].startswith("\n---\n")


# --------------------------------------------------------------------------- v2 verdicts


def test_v2_rows_are_recomputed_not_no_verdict() -> None:
    rows = report.join([_rec(("T", 1), 0.9), _rec(("T", 2), 0.1)], [_fx(("T", 1)), _fx(("T", 2))])
    th = TierCV2Thresholds()
    assert [r.ask_score for r in rows] == [0.9, 0.1]
    assert [report.recompute_v2(r.ask_score, th) for r in rows] == ["CONFIRMED", "LIKELY_NOT"]
    # The v1 path would have called these NO_VERDICT — the failure 2d-5 exists to remove.
    assert [report.recompute(r.scores, report.TierCThresholds())[0] for r in rows] == [
        "NO_VERDICT",
        "NO_VERDICT",
    ]


@pytest.mark.parametrize(
    ("ask", "kind"),
    [
        (0.60, "CONFIRMED"),
        (0.5999, "UNSURE"),
        (0.40, "UNSURE"),
        (0.3999, "LIKELY_NOT"),
        (0.0, "LIKELY_NOT"),
        (1.0, "CONFIRMED"),
    ],
)
def test_v2_boundaries(ask: float, kind: str) -> None:
    """2d-5: ``p >= 0.60`` CONFIRMED, ``p < 0.40`` LIKELY_NOT, otherwise UNSURE."""
    assert report.recompute_v2(ask, TierCV2Thresholds()) == kind


def test_primary_hypothesis_is_the_code_default_and_in_the_sweep() -> None:
    """2d-4 / msg-4641: ``(0.60, 0.40)`` is the pre-registered pair and sits in ``SWEEP_V2``."""
    assert (DEFAULT_V2_ASK_MIN, DEFAULT_V2_NOT_ASK_MAX) == (0.60, 0.40)
    assert (0.60, 0.40) in report.SWEEP_V2
    assert report.SWEEP_V2 == ((0.50, 0.33), (0.60, 0.40), (0.70, 0.47), (0.80, 0.53))
    for ask_min, not_ask_max in report.SWEEP_V2:
        TierCV2Thresholds(ask_min=ask_min, not_ask_max=not_ask_max)  # each pair is valid


def test_v2_report_valid_headline_sweep_and_auc() -> None:
    recs = [_rec(("G", 1), 0.9), _rec(("S", 1), 0.1), _rec(("S", 2), 0.5)]
    fx = [_fx(("G", 1)), _fx(("S", 1), following_n=1), _fx(("S", 2))]
    text = _render(recs, fx, {("G", 1): "genuine", ("S", 1): "spurious", ("S", 2): "spurious"})
    assert "# Tier-C evaluation — Jev, tierc-v2" in text
    assert "**valid**" in text and "INVALID" not in text
    assert "- reduction (spurious → LIKELY_NOT): 1/2 = 50.0%" in text
    assert "- recall genuine (reaches human): 1/1 = 100.0%" in text
    assert "AUC of should_ask_human (genuine* vs spurious): 1.000 (1 vs 2)" in text
    assert "| 0.70 | 0.47 |" in text and "0.4666" not in text
    assert "- rows: 1/3 = 33.3%" in text  # following_n < 3
    assert "- consensus truth: {'spurious': 1}" in text
    assert "# Tier-C replay evaluation" not in text  # no v1 section for a pure-v2 input


def test_one_no_verdict_row_makes_the_v2_set_invalid() -> None:
    """2d-4: a single ``NO_VERDICT`` → INVALID, and no headline (recall would pass vacuously)."""
    recs = [_rec(("G", 1), 0.9), _rec(("X", 1), None, outcome="transport_error")]
    text = _render(recs, [_fx(("G", 1)), _fx(("X", 1))], {("G", 1): "genuine"})
    assert "**INVALID**" in text
    assert "NO_VERDICT rows: 1" in text
    assert "m-X-1" in text
    assert "recall genuine" not in text and "Threshold sweep" not in text


def test_malformed_is_counted_and_invalidates() -> None:
    recs = [_rec(("G", 1), 0.9), _rec(("M", 1), None, outcome="no_verdict_malformed")]
    text = _render(recs, [_fx(("G", 1)), _fx(("M", 1))], {})
    assert "MALFORMED (outcome `no_verdict_malformed`): 1/2 = 50.0%" in text
    assert "**INVALID**" in text


# ------------------------------------------------------------------ 2d-11 (msg-4648 / 4650)

exporter = _load("export_shadow_eval_set")


def _v1_rec(key: tuple[str, int]) -> dict[str, Any]:
    return {
        "thread_id": key[0],
        "round_index": key[1],
        "questions_version": "tierc-v1",
        "state": {"head_summary": "h", "gate_result": None},
        "decision": {"outcome": "evaluated", "raw_answers": None},
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _v2_export(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    """A v2 export written by the exporter's own ``write_outputs``: (out_dir, replay, fixture,
    export.json, labels)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "RUBRIC-v2.md").write_text("rubric v2\n", encoding="utf-8")
    (src / "label_prompt-v2.md").write_text("prompt v2\n", encoding="utf-8")
    out = tmp_path / "lab"
    replay = tmp_path / "jev" / "replay.jsonl"
    keys = [("G", 1), ("S", 1)]
    exporter.write_outputs(
        out,
        replay,
        [{"thread_id": t, "round_index": r, "body": "b"} for t, r in keys],
        [_fx(k) for k in keys],
        [_rec(("G", 1), 0.9), _rec(("S", 1), 0.1)],
        {"as_of": "2026-10-15T00:00:00+00:00", "counted": 2},
        sources={"rubric": src / "RUBRIC-v2.md", "label_prompt": src / "label_prompt-v2.md"},
    )
    labels = tmp_path / "labels.jsonl"
    _write_jsonl(
        labels,
        [
            {"thread_id": "G", "round_index": 1, "label": "genuine"},
            {"thread_id": "S", "round_index": 1, "label": "spurious"},
        ],
    )
    return out, replay, out / "fixture.jsonl", out / "export.json", labels


def _cli(replay: Path, fixture: Path, labels: Path, *extra: str) -> int:
    return int(
        report.main(
            ["--replay", str(replay), "--fixture", str(fixture), "--labels", f"a={labels}", *extra]
        )
    )


def test_v1_run_without_manifest_still_runs(tmp_path: Path) -> None:
    """23a, CLI half (bytes: ``test_v1_report_md_is_unchanged_byte_for_byte``)."""
    rp, fp, lp = tmp_path / "r.jsonl", tmp_path / "f.jsonl", tmp_path / "l.jsonl"
    _write_jsonl(rp, [_v1_rec(("V", 1))])
    _write_jsonl(fp, [{**_fx(("V", 1)), "eval_set": "body"}])
    _write_jsonl(lp, [])
    assert _cli(rp, fp, lp, "--out", str(tmp_path / "o.md")) == 0
    assert (
        (tmp_path / "o.md")
        .read_text(encoding="utf-8")
        .startswith("# Tier-C replay evaluation — Jev")
    )


def test_v2_run_without_manifest_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """23b: no ``--export-manifest`` → refused; deleting ``export.json`` opens nothing."""
    _, replay, fixture, manifest, labels = _v2_export(tmp_path)
    manifest.unlink()
    assert _cli(replay, fixture, labels) == 2
    assert "requires --export-manifest" in capsys.readouterr().err


@pytest.mark.parametrize("which", ["replay", "fixture", "materials"])
def test_one_changed_byte_is_refused(
    tmp_path: Path, which: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """23c: a byte changed in any of the three locked files → refused, naming the file."""
    out, replay, fixture, manifest, labels = _v2_export(tmp_path)
    target = {"replay": replay, "fixture": fixture, "materials": out / "materials.jsonl"}[which]
    target.write_bytes(target.read_bytes() + b" ")
    assert _cli(replay, fixture, labels, "--export-manifest", str(manifest)) == 2
    err = capsys.readouterr().err
    assert which in err and "changed after it was written" in err


def test_v1_run_with_manifest_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """23d: nothing would be checked, so the argument is refused."""
    _, _, _, manifest, labels = _v2_export(tmp_path)
    rp, fp = tmp_path / "r1.jsonl", tmp_path / "f1.jsonl"
    _write_jsonl(rp, [_v1_rec(("V", 1))])
    _write_jsonl(fp, [{**_fx(("V", 1)), "eval_set": "body"}])
    assert _cli(rp, fp, labels, "--export-manifest", str(manifest)) == 2
    assert "v1 run" in capsys.readouterr().err


def test_valid_v2_run_prints_the_export_lock(tmp_path: Path) -> None:
    """23e: accepted; ``as_of`` and the four sha256 open the v2 section."""
    _, replay, fixture, manifest, labels = _v2_export(tmp_path)
    out = tmp_path / "o.md"
    assert _cli(replay, fixture, labels, "--export-manifest", str(manifest), "--out", str(out)) == 0
    text = out.read_text(encoding="utf-8")
    lock = json.loads(manifest.read_text(encoding="utf-8"))
    head = text.split("## Validity", 1)[0]
    assert "- as_of: 2026-10-15T00:00:00+00:00" in head
    assert report.file_sha256(manifest) in head
    for k in ("materials", "fixture", "replay"):
        assert lock["files"][k]["sha256"] in head
    assert "**valid**" in text and "recall genuine (reaches human): 1/1 = 100.0%" in text


@pytest.mark.parametrize("with_manifest", [False, True])
def test_mixed_versions_are_refused_as_mixed(
    tmp_path: Path, with_manifest: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """23f: refused for the version mix, with or without a manifest — not as a hash mismatch."""
    _, replay, fixture, manifest, labels = _v2_export(tmp_path)
    mixed = tmp_path / "mixed.jsonl"
    _write_jsonl(mixed, [*report.read_jsonl(replay), _v1_rec(("V", 1))])
    extra = ["--export-manifest", str(manifest)] if with_manifest else []
    assert _cli(mixed, fixture, labels, *extra) == 2
    err = capsys.readouterr().err
    assert "v1 only or v2 only" in err and "changed after" not in err


def test_render_refuses_mixed_rows() -> None:
    rows = report.join([_v1_rec(("V", 1)), _rec(("W", 1), 0.9)], [_fx(("V", 1)), _fx(("W", 1))])
    with pytest.raises(report.InputError, match="v1 only or v2 only"):
        report.render(rows, {}, report.TierCThresholds())
