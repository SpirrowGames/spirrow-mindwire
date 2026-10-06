"""T-decider-conductor-hook DECIDED 2e-2 — Jev's line on a ``NEXT: human`` (display only).

Spec: Bohr msg-5141 §2e-2 (endorsed Einstein msg-5142 / msg-5144), carried by msg-6626 §3:

- ``build_decider`` accepts ``annotate``;
- the digest and the decision page show ``Jev: should_ask_human=p (未検証、AUC 未確立) —
  <該当ルール>`` under a ``NEXT: human`` that passed the gate, lower ``p`` listed lower (this
  repository carries the digest; the decision page is rendered by magickit);
- delivery, stopping and routing are unchanged (D20).

The digest half is pinned in ``tests/Test-SweepDigest.ps1``; this file pins the write side (the
Conductor), the shared wording, the file format and the join in ``scripts/parked_humans.py``.
Scenario shape follows ``test_tierc_jev_bounce``: Bohr design → Einstein critique → Bohr's head.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import _ROSTER as ROSTER
from test_conductor_core import _attested, _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref
from test_tierc_jev_bounce import LABELLED, _bounces, _Decider, _gate, _result

from spirrow_mindwire.adapters.decider_lexora import build_decider
from spirrow_mindwire.conductor import core as core_mod
from spirrow_mindwire.conductor.core import Conductor, StopReason
from spirrow_mindwire.conductor.tierc_gate import TierCGate
from spirrow_mindwire.decider import annotation as ann_mod
from spirrow_mindwire.decider.annotation import (
    NO_RULE_TEXT,
    TierCAnnotation,
    annotation_from_result,
    annotation_line,
    append_annotation,
    compact_annotations,
    read_annotations,
)
from spirrow_mindwire.decider.result import DecisionOutcome, DecisionResult
from spirrow_mindwire.decider.verdict import TierCScope, TierCVerdict, TierCVerdictKind
from spirrow_mindwire.value_objects import Role

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
PROJECT = _thread_ref().project_id
THREAD = _thread_ref().thread_id
UNLABELLED = "revised\n\nNEXT: human"


def _ann_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "tierc_annotations.jsonl"


def _ann_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _run(
    tmp_path: Path,
    decider: _Decider | None,
    *,
    gate: TierCGate | None = None,
    head: str = LABELLED,
    proposer_replies: list[str] | None = None,
    with_path: bool = True,
) -> tuple[Any, _ScriptedDispatcher, _FakeChatroomMcp]:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    mcp.seed(author="Einstein", content=_attested("critique\n\nNEXT: Bohr"))
    mcp.seed(author="Bohr", content=head)
    disp = _ScriptedDispatcher(mcp, {Role.PROPOSER: list(proposer_replies or [])})
    conductor = Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=ROSTER,
        naysayer_identity="Einstein",
        max_rounds=12,
        decider=decider,
        tierc_gate=gate,
        tierc_annotations_path=_ann_path(tmp_path) if with_path else None,
    )
    return await conductor.run(), disp, mcp


def _with_rule(dr: DecisionResult, rule: str | None) -> DecisionResult:
    return DecisionResult(
        outcome=dr.outcome,
        decision_id=dr.decision_id,
        provider=dr.provider,
        raw_answers=dr.raw_answers,
        verdict=dr.verdict,
        policy=dr.policy,
        matched_rule=rule,
    )


# --------------------------------------------------------------------------- wording


def test_annotation_line_is_the_msg_5141_wording_verbatim() -> None:
    ann = TierCAnnotation(PROJECT, THREAD, "m3", 0.123, "R3", "tierc-v3", "d", NOW.isoformat())
    assert annotation_line(ann) == "Jev: should_ask_human=0.12（未検証、AUC 未確立）— R3"  # noqa: RUF001


def test_annotation_line_without_a_rule_says_so() -> None:
    ann = TierCAnnotation(PROJECT, THREAD, "m3", 0.9, None, "tierc-v3", None, "")
    assert annotation_line(ann).endswith(f"— {NO_RULE_TEXT}")


# --------------------------------------------------------------------------- from a result


@pytest.mark.parametrize("kind", list(TierCVerdictKind))
def test_every_actionable_v2_kind_is_annotated_with_its_p(kind: TierCVerdictKind) -> None:
    ann = annotation_from_result(
        _with_rule(_result(kind, 0.42), "R2"),
        project=PROJECT,
        thread_id=THREAD,
        msg_id="m3",
        now=NOW,
    )
    assert ann is not None
    assert (ann.ask_score, ann.matched_rule, ann.msg_id, ann.decision_id) == (0.42, "R2", "m3", "d")
    assert ann.questions_version == "tierc-v3"


def _null_result() -> DecisionResult:
    return DecisionResult(
        outcome=DecisionOutcome.NO_VERDICT_NULL,
        decision_id="d-null",
        provider="null",
        raw_answers={},
        verdict=None,
        policy="p",
    )


def _v1_result() -> DecisionResult:
    return DecisionResult(
        outcome=DecisionOutcome.EVALUATED,
        decision_id="d-v1",
        provider="jev",
        raw_answers={},
        verdict=TierCVerdict(
            kind=TierCVerdictKind.LIKELY_NOT,
            genuine_score=0.1,
            spurious_score=0.9,
            scope=TierCScope.IN_GATE,
            fired_reason=None,
        ),
        policy="p",
    )


@pytest.mark.parametrize("dr", [None, _null_result(), _v1_result()])
def test_no_actionable_v2_verdict_means_no_annotation(dr: DecisionResult | None) -> None:
    assert (
        annotation_from_result(dr, project=PROJECT, thread_id=THREAD, msg_id="m", now=NOW) is None
    )


# --------------------------------------------------------------------------- file format


def test_append_then_read_round_trips_and_last_row_per_key_wins(tmp_path: Path) -> None:
    path = _ann_path(tmp_path)
    first = TierCAnnotation(PROJECT, THREAD, "m3", 0.7, "R1", "tierc-v3", "d1", "t1")
    second = TierCAnnotation(PROJECT, THREAD, "m3", 0.2, "R4", "tierc-v3", "d2", "t2")
    other = TierCAnnotation(PROJECT, "T-other", "m3", 0.5, None, "tierc-v3", None, "t3")
    for a in (first, other, second):
        append_annotation(path, a)
    assert b"\r\n" not in path.read_bytes()
    found, skipped = read_annotations(path)
    assert skipped == 0
    assert found == {first.key(): second, other.key(): other}


def _ann(i: int, score: float = 0.5) -> TierCAnnotation:
    return TierCAnnotation(PROJECT, THREAD, f"m{i}", score, "R1", "tierc-v3", f"d{i}", "t")


def test_append_compacts_to_the_newest_lines_once_over_the_size_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#465 PR-gate advisory (224d068): the file is bounded. Over the cap, an append keeps only
    the newest KEEP_LINES lines; the newest row (and last-row-per-key-wins) survive."""
    monkeypatch.setattr(ann_mod, "COMPACT_AT_BYTES", 2000)
    monkeypatch.setattr(ann_mod, "KEEP_LINES", 5)
    path = _ann_path(tmp_path)
    for i in range(40):
        append_annotation(path, _ann(i))
    append_annotation(path, _ann(39, score=0.1))
    rows = _ann_rows(path)
    assert path.stat().st_size <= 2000 + 400  # one append past the cap at most
    assert len(rows) <= 5 + 2000 // 100
    assert rows[-1]["msg_id"] == "m39" and rows[-1]["ask_score"] == 0.1
    assert all(r["msg_id"] != "m0" for r in rows)
    found, skipped = read_annotations(path)
    assert skipped == 0
    assert found[(PROJECT, THREAD, "m39")].ask_score == 0.1
    assert b"\r\n" not in path.read_bytes()
    assert not path.with_name(path.name + ".compact.tmp").exists()


def test_compact_keeps_exactly_the_suffix(tmp_path: Path) -> None:
    path = _ann_path(tmp_path)
    for i in range(10):
        append_annotation(path, _ann(i))
    compact_annotations(path, keep_lines=3)
    assert [r["msg_id"] for r in _ann_rows(path)] == ["m7", "m8", "m9"]
    compact_annotations(path, keep_lines=3)  # at or under the limit: unchanged
    assert [r["msg_id"] for r in _ann_rows(path)] == ["m7", "m8", "m9"]


def test_a_failed_compaction_is_a_warning_and_keeps_the_appended_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ann_mod, "COMPACT_AT_BYTES", 0)

    def boom(_path: Path, keep_lines: int = 0) -> None:
        raise PermissionError("file in use")

    monkeypatch.setattr(ann_mod, "compact_annotations", boom)
    path = _ann_path(tmp_path)
    with caplog.at_level("WARNING", logger=ann_mod.__name__):
        append_annotation(path, _ann(1))
    assert [r["msg_id"] for r in _ann_rows(path)] == ["m1"]
    assert "compaction" in caplog.text


def test_missing_file_is_nothing_annotated(tmp_path: Path) -> None:
    assert read_annotations(tmp_path / "absent.jsonl") == ({}, 0)


def test_malformed_lines_are_skipped_and_counted(tmp_path: Path) -> None:
    path = _ann_path(tmp_path)
    good = TierCAnnotation(PROJECT, THREAD, "m3", 0.3, "R2", "tierc-v3", "d", "t")
    append_annotation(path, good)
    base = {
        "project": PROJECT,
        "thread_id": THREAD,
        "msg_id": "m9",
        "questions_version": "tierc-v3",
        "ask_score": 0.5,
    }
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write("not json\n")
        f.write("[]\n")
        f.write(json.dumps({**base, "ask_score": 1.5}) + "\n")
        f.write(json.dumps({**base, "ask_score": True}) + "\n")
        f.write(json.dumps({**base, "msg_id": ""}) + "\n")
        f.write(json.dumps({k: v for k, v in base.items() if k != "project"}) + "\n")
        f.write("\n")
    found, skipped = read_annotations(path)
    assert found == {good.key(): good}
    assert skipped == 6


# --------------------------------------------------------------------------- build_decider


def test_build_decider_accepts_annotate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MINDWIRE_DECIDER_BACKEND", raising=False)
    monkeypatch.setenv("MINDWIRE_LEXORA_URL", "http://lexora.test")
    built = build_decider(config_backend="lexora", tierc_mode="annotate", questions="tierc-v1")
    assert built is not None and built.tierc_mode == "annotate"


def test_build_decider_still_refuses_an_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDWIRE_DECIDER_BACKEND", raising=False)
    monkeypatch.setenv("MINDWIRE_LEXORA_URL", "http://lexora.test")
    with pytest.raises(ValueError, match="unknown"):
        build_decider(config_backend="lexora", tierc_mode="annotat")


# --------------------------------------------------------------------------- the Conductor


@pytest.mark.anyio
async def test_annotate_writes_the_head_that_reaches_the_human(tmp_path: Path) -> None:
    dr = _with_rule(_result(TierCVerdictKind.CONFIRMED, 0.9), "R1")
    outcome, _, _ = await _run(tmp_path, _Decider(dr, mode="annotate"))
    assert outcome.stop_reason is StopReason.HUMAN
    (row,) = _ann_rows(_ann_path(tmp_path))
    assert (row["project"], row["thread_id"], row["msg_id"]) == (PROJECT, THREAD, "m3")
    assert (row["ask_score"], row["matched_rule"], row["decision_id"]) == (0.9, "R1", "d")


@pytest.mark.anyio
async def test_annotate_never_bounces_even_a_likely_not(tmp_path: Path) -> None:
    """``annotate`` is display only: with the gate enforced a LIKELY_NOT still reaches the human."""
    gate = _gate(tmp_path)
    outcome, _, mcp = await _run(
        tmp_path, _Decider(_result(TierCVerdictKind.LIKELY_NOT, 0.2), mode="annotate"), gate=gate
    )
    assert outcome.stop_reason is StopReason.HUMAN
    assert _bounces(mcp) == []
    (row,) = _ann_rows(_ann_path(tmp_path))
    assert (row["msg_id"], row["ask_score"]) == ("m3", 0.2)


@pytest.mark.anyio
async def test_annotate_changes_no_routing_compared_with_shadow(tmp_path: Path) -> None:
    """D20: the same thread under shadow and annotate ends the same way with the same posts."""
    dr = _result(TierCVerdictKind.LIKELY_NOT, 0.2)
    shadow_out, shadow_disp, shadow_mcp = await _run(
        tmp_path / "s", _Decider(dr, mode="shadow"), gate=_gate(tmp_path / "s")
    )
    ann_out, ann_disp, ann_mcp = await _run(
        tmp_path / "a", _Decider(dr, mode="annotate"), gate=_gate(tmp_path / "a")
    )
    assert ann_out.stop_reason is shadow_out.stop_reason
    assert ann_out.last_msg_id == shadow_out.last_msg_id
    assert ann_mcp.posts == shadow_mcp.posts
    assert ann_disp.dispatches == shadow_disp.dispatches
    assert _ann_rows(_ann_path(tmp_path / "s")) == []
    assert len(_ann_rows(_ann_path(tmp_path / "a"))) == 1


@pytest.mark.anyio
async def test_bounce_mode_annotates_only_what_reaches_the_human(tmp_path: Path) -> None:
    """A Jev-bounced head never reached the human, so it has no line; its RETRY reached the human
    and gets one."""
    gate = _gate(tmp_path)
    retry = "still needed\n\nRETRY: u-2\nTIER-C: goal\nNEXT: human"
    outcome, _, mcp = await _run(
        tmp_path,
        _Decider(_result(TierCVerdictKind.LIKELY_NOT, 0.2)),
        gate=gate,
        proposer_replies=[retry],
    )
    assert outcome.stop_reason is StopReason.HUMAN
    assert len(_bounces(mcp)) == 1
    rows = _ann_rows(_ann_path(tmp_path))
    assert [r["msg_id"] for r in rows] == [outcome.last_msg_id]
    assert "m3" not in [r["msg_id"] for r in rows]


@pytest.mark.anyio
async def test_a_label_gate_bounce_is_not_annotated(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    await _run(
        tmp_path,
        _Decider(_result(TierCVerdictKind.CONFIRMED, 0.9), mode="annotate"),
        gate=gate,
        head=UNLABELLED,
    )
    assert _ann_rows(_ann_path(tmp_path)) == []


@pytest.mark.anyio
async def test_shadow_writes_nothing(tmp_path: Path) -> None:
    await _run(tmp_path, _Decider(_result(TierCVerdictKind.CONFIRMED, 0.9), mode="shadow"))
    assert not _ann_path(tmp_path).exists()


@pytest.mark.anyio
async def test_no_path_writes_nothing(tmp_path: Path) -> None:
    await _run(
        tmp_path,
        _Decider(_result(TierCVerdictKind.CONFIRMED, 0.9), mode="annotate"),
        with_path=False,
    )
    assert not _ann_path(tmp_path).exists()


@pytest.mark.anyio
@pytest.mark.parametrize("dr", [None, _null_result()])
async def test_no_actionable_result_writes_nothing(
    tmp_path: Path, dr: DecisionResult | None
) -> None:
    outcome, _, _ = await _run(tmp_path, _Decider(dr, mode="annotate"))
    assert outcome.stop_reason is StopReason.HUMAN
    assert not _ann_path(tmp_path).exists()


@pytest.mark.anyio
async def test_a_write_failure_leaves_the_escalation_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(core_mod, "append_annotation", boom)
    outcome, _, _ = await _run(
        tmp_path, _Decider(_result(TierCVerdictKind.CONFIRMED, 0.9), mode="annotate")
    )
    assert outcome.stop_reason is StopReason.HUMAN
    assert any("tierc annotation" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- parked_humans join

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "parked_humans.py"


def _load_parked_humans() -> Any:
    spec = importlib.util.spec_from_file_location("_parked_humans_annotation_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_PH = _load_parked_humans()


class _OneThreadMcp:
    def __init__(self, bodies: dict[str, tuple[str, str]]) -> None:
        self.bodies = bodies

    async def call_tool(self, name: str, params: dict[str, object]) -> object:
        msg_id, body = self.bodies[str(params["thread_id"])]
        return {
            "messages": [
                {"msg_id": msg_id, "content": body, "timestamp": "2026-10-06T00:00:00+00:00"}
            ]
        }


def test_parked_rows_carry_the_annotation_for_their_exact_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _ann_path(tmp_path)
    append_annotation(path, TierCAnnotation("p", "T-a", "msg-a", 0.15, "R2", "tierc-v3", "d", "t"))
    # Same thread, an older head: must not be shown on the new head.
    append_annotation(path, TierCAnnotation("p", "T-b", "msg-old", 0.9, "R1", "tierc-v3", "d", "t"))
    # Same thread and head, another project: must not leak across projects.
    append_annotation(path, TierCAnnotation("q", "T-c", "msg-c", 0.5, None, "tierc-v3", "d", "t"))
    fake = _OneThreadMcp(
        {
            "T-a": ("msg-a", "x\n\nNEXT: human"),
            "T-b": ("msg-new", "x\n\nNEXT: human"),
            "T-c": ("msg-c", "x\n\nNEXT: human"),
        }
    )
    monkeypatch.setattr(_PH, "StreamableHttpChatroomMcp", lambda *_a, **_k: fake)
    cands = [{"thread_id": t, "head_msg_id": ""} for t in ("T-a", "T-b", "T-c")]
    result = asyncio.run(_PH._poll("p", cands, None, _PH._load_annotations(path)))
    by = {r["thread_id"]: r for r in result["parked"]}
    assert by["T-a"]["jev_ask_score"] == 0.15
    assert by["T-a"]["jev_line"] == "Jev: should_ask_human=0.15（未検証、AUC 未確立）— R2"  # noqa: RUF001
    assert (by["T-b"]["jev_ask_score"], by["T-b"]["jev_line"]) == (None, "")
    assert (by["T-c"]["jev_ask_score"], by["T-c"]["jev_line"]) == (None, "")
    assert result["errors"] == []


def test_an_unreadable_annotation_file_shows_rows_without_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _ann_path(tmp_path)
    path.mkdir(parents=True)  # a directory where the file should be → OSError on open
    assert _PH._load_annotations(path) == {}
    assert "annotations unreadable" in capsys.readouterr().err


def test_no_annotations_flag_means_no_join() -> None:
    assert _PH._load_annotations(None) == {}


# --------------------------------------------------------------------------- composition root


@pytest.mark.parametrize(
    ("mode", "wired"), [("shadow", False), ("annotate", True), ("bounce", True)]
)
def test_build_conductor_wires_the_annotation_path_under_annotate_and_bounce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, wired: bool
) -> None:
    from test_loop_runner import _conductor_settings
    from test_tierc_gate_enforce import _build

    from spirrow_mindwire.config import DeciderConfig, DeciderTierCConfig, PathsConfig

    monkeypatch.delenv("MINDWIRE_DECIDER_BACKEND", raising=False)
    monkeypatch.setenv("MINDWIRE_LEXORA_URL", "http://lexora.test")
    s = _conductor_settings().model_copy(
        update={
            "paths": PathsConfig(data_dir=tmp_path),
            "decider": DeciderConfig(
                backend="lexora",
                tierc=DeciderTierCConfig(mode=mode, questions="tierc-v1"),  # type: ignore[arg-type]
            ),
        }
    )
    cond = _build(s).conductor
    expected = tmp_path / "state" / "tierc_annotations.jsonl" if wired else None
    assert cond._tierc_annotations_path == expected
