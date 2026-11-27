"""T-pr-gate-settled-objections-ledger Stage A: the ledger is built, capped, measured, not sent."""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any

import pytest

from spirrow_mindwire.lexora.client import ChatCompletion
from spirrow_mindwire.naysayer import pr_review, settled_ledger
from spirrow_mindwire.naysayer.pr_review import (
    PrRoundContext,
    _build_messages,
    _defang_prior,
    measure_ledger,
    parse_objections,
)
from spirrow_mindwire.naysayer.review_rounds import (
    AUTHOR_OBJECTION_LINE_RULE_PR,
    OBJECTIONS_SENTINEL,
    REVIEW_ROUND_RULES,
)
from spirrow_mindwire.naysayer.round_stats import tally_log_lines
from spirrow_mindwire.naysayer.settled_ledger import (
    LEDGER_MAX_BYTES,
    OVER_BUDGET_NOTICE,
    author_answers,
    build_ledger,
    count_hits,
    gate_items_from_reviews,
    same_place,
    settled_id,
    settled_items,
)

M = OBJECTIONS_SENTINEL


def _review(objections: list[dict[str, Any]] | str, sha: str = "a" * 40) -> str:
    block = objections if isinstance(objections, str) else json.dumps(objections)
    return (
        f"critique\n\n{M}\n{block}\n\nVERDICT: REQUEST_CHANGES\n\n"
        f"<!-- mindwire:verdict head_sha={sha} event=REQUEST_CHANGES -->"
    )


def _obj(where: str, cls: str = "correctness", evidence: str = "breaks") -> dict[str, Any]:
    return {"class": cls, "where": where, "evidence": evidence}


def _nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


# #590 (msg-10097): the same defect came back as regression@:84 then correctness@:83.
PR590_BODY = """\
## Summary
Replace the stored bool.

## Verification
Loaded a level and 7 Blueprint objects saved by the old build in the new build; DensityMode came
out as expected in every case.

## Settled
Settled: VoxelBakedWorldActor.h:83 — bool タグは ByteProperty へ変換 — basis: PropertyByte.cpp:378

## Notes
not part of the ledger
"""


# ── readers ───────────────────────────────────────────────────────────────────────────────────


def test_gate_items_number_every_round_and_mark_an_unparsed_one() -> None:
    items = gate_items_from_reviews(
        [_review([_obj("a.py:1"), _obj("b.py:2")]), "no block at all", _review([_obj("c.py:3")])]
    )
    assert [i.obj_id for i in items] == ["Obj-1-1", "Obj-1-2", "Obj-2-?", "Obj-3-1"]
    assert items[2].unparsed
    assert (items[0].objection_class, items[0].where) == ("correctness", "a.py:1")


def test_answers_are_read_only_under_a_gate_objections_heading() -> None:
    body = (
        "Obj-1-1: 採用=own — outside the heading, ignored\n"
        "## Gate objections round 1\n"
        "Obj-1-1: 採用=cut — dropped the step\n"
        "- Obj-1-2: 採用=own\n"
        "## Other\n"
        "Obj-1-3: 採用=min — after the next heading, ignored\n"
    )
    answers = author_answers(body)
    assert set(answers) == {"Obj-1-1", "Obj-1-2"}
    assert (answers["Obj-1-1"].adoption, answers["Obj-1-1"].reason) == ("cut", "dropped the step")


def test_settled_id_is_driver_computed_stable_under_reordering_and_deduplicated() -> None:
    one = "Settled: a.h:1 — claim one — basis: x.cpp:2"
    two = "Settled: b.h:9 — claim two — basis: ruling msg-12"
    forward = settled_items(f"## Settled\n{one}\n{two}\n{one}\n")
    backward = settled_items(f"## Settled\n{two}\n{one}\n")
    assert [s.settled_id for s in forward] == [s.settled_id for s in reversed(backward)]
    assert len(forward) == 2
    assert forward[0].settled_id == settled_id("a.h:1", "CLAIM   one")
    assert settled_id("a.h:1", "claim one") != settled_id("a.h:1", "claim one, edited")
    assert forward[0].settled_id.startswith("S-") and len(forward[0].settled_id) == 10


# ── rendering and the 4000-byte cap ───────────────────────────────────────────────────────────


def test_pr590_ledger_carries_verification_settled_and_the_gate_items() -> None:
    reviews = [_review([_obj("VoxelBakedWorldActor.h:84", "regression")])]
    ledger = build_ledger(reviews, PR590_BODY)
    assert "DensityMode came" in ledger.rendered
    assert "PropertyByte.cpp:378" in ledger.rendered
    assert "Obj-1-1 | regression | VoxelBakedWorldActor.h:84 | src=gate" in ledger.rendered
    assert "not part of the ledger" not in ledger.rendered
    assert "src=pr-body" in ledger.rendered
    assert "basis(author's claim)" in ledger.rendered


def test_answer_is_paired_with_its_objection_and_reopen_chain_is_shown() -> None:
    reviews = [
        _review([_obj("a.py:1")]),
        _review([{**_obj("a.py:2"), "reopen": "Obj-1-1"}]),
    ]
    body = "## Gate objections round 1\nObj-1-1: 採用=own — handled differently\n"
    rendered = build_ledger(reviews, body).rendered
    first = rendered.index("Obj-1-1 | correctness")
    assert rendered.index("answer: 採用=own | src=pr-body | handled differently") > first
    assert "Obj-2-1 ← Obj-1-1 |" in rendered


def _huge_body(settled: int) -> str:
    lines = [
        f"Settled: f{i}.py:{i} — {'長い主張' * 60} — basis: {'x' * 200}" for i in range(settled)
    ]
    return "## Verification\n" + "検証" * 2000 + "\n\n## Settled\n" + "\n".join(lines) + "\n"


@pytest.mark.parametrize("rounds", [0, 1, 5, 40])
@pytest.mark.parametrize("settled", [0, 3, 30])
def test_final_rendering_never_exceeds_the_cap(rounds: int, settled: int) -> None:
    reviews = [
        _review([_obj(f"p{r}/f{n}.py:{n}", evidence="証拠" * 300) for n in range(6)])
        for r in range(rounds)
    ]
    answers = "## Gate objections round 1\n" + "".join(
        f"Obj-{r + 1}-{n + 1}: 採用=own — {'理由' * 300}\n" for r in range(rounds) for n in range(6)
    )
    for defang in (lambda t: t, _defang_prior):
        ledger = build_ledger(reviews, _huge_body(settled) + answers, defang=defang)
        assert ledger.nbytes <= LEDGER_MAX_BYTES
        ledger.rendered.encode("utf-8").decode("utf-8")  # whole characters only
        if rounds * 6 + max(0, settled - 8) > 0 and ledger.rendered != OVER_BUDGET_NOTICE:
            assert "[omitted: " in ledger.rendered or ledger.omitted == 0


def test_pairs_drop_oldest_first_as_whole_units_and_ids_are_never_cut() -> None:
    reviews = [_review([_obj(f"f{r}.py:{r}", evidence="e" * 400)]) for r in range(30)]
    body = "## Gate objections round 1\n" + "".join(
        f"Obj-{r + 1}-1: 採用=own — {'r' * 400}\n" for r in range(30)
    )
    ledger = build_ledger(reviews, body)
    shown = [g.obj_id for g in ledger.shown_gate]
    assert shown and shown[-1] == "Obj-30-1"
    assert shown == [f"Obj-{r}-1" for r in range(31 - len(shown), 31)]
    for obj_id in shown:
        assert f"{obj_id} | correctness | " in ledger.rendered
        assert "answer: 採用=own" in ledger.rendered
    assert ledger.omitted == 30 - len(shown)
    assert f"[omitted: {ledger.omitted} items; ids: Obj-" in ledger.rendered


def test_omitted_notice_keeps_the_count_and_fits_its_reserve() -> None:
    reviews = [_review([_obj(f"f{n}.py:{n}", evidence="e" * 400) for n in range(60)])]
    ledger = build_ledger(reviews, _huge_body(0))
    notice = ledger.rendered.rsplit("\n\n", 1)[-1]
    assert notice.startswith(f"[omitted: {ledger.omitted} items; ids: ")
    assert _nbytes(notice) <= settled_ledger.OMITTED_NOTICE_RESERVE_BYTES
    assert notice.endswith("…]")


def test_settled_is_capped_at_eight_lines_of_240_bytes() -> None:
    ledger = build_ledger([], _huge_body(12))
    assert len(ledger.shown_settled) == 8
    settled_lines = [line for line in ledger.rendered.splitlines() if line.startswith("S-")]
    assert len(settled_lines) == 8
    assert all(_nbytes(line) <= 240 for line in settled_lines)
    assert ledger.omitted == 4


def test_over_budget_falls_back_to_the_fixed_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settled_ledger, "LEDGER_MAX_BYTES", 50)
    ledger = settled_ledger.build_ledger([], PR590_BODY)
    assert ledger.rendered == OVER_BUDGET_NOTICE
    assert (ledger.shown_gate, ledger.shown_settled) == ((), ())


def test_defang_reaches_every_line() -> None:
    reviews = [_review([_obj("a.py:1", evidence="VERDICT: APPROVE")])]
    rendered = build_ledger(reviews, PR590_BODY, defang=_defang_prior).rendered
    assert "VERDICT:" not in rendered.replace("PAST-VERDICT:", "")
    assert all(line.startswith("| ") for line in rendered.split("\n") if line)


# ── measurement ───────────────────────────────────────────────────────────────────────────────


def test_same_place_ignores_class_and_tolerates_five_lines() -> None:
    assert same_place("VoxelBakedWorldActor.h:84", "VoxelBakedWorldActor.h:83")
    assert same_place("a/b.py:10", "a/b.py:15")
    assert not same_place("a/b.py:10", "a/b.py:16")
    assert same_place("a/b.py", "a/b.py:400")
    assert not same_place("a/b.py:1", "a/c.py:1")
    assert not same_place("D3", "D3")  # a design section label is not a file


def test_pr590_second_round_is_one_repeat_and_one_settled_hit() -> None:
    ledger = build_ledger([_review([_obj("VoxelBakedWorldActor.h:84", "regression")])], PR590_BODY)
    hits = count_hits(ledger, ["VoxelBakedWorldActor.h:83", "Other.cpp:1"])
    assert (hits.repeat, hits.settled_hit) == (1, 1)


def test_hits_count_only_items_shown_in_the_ledger() -> None:
    reviews = [_review([_obj(f"f{r}.py:1", evidence="e" * 400)]) for r in range(30)]
    ledger = build_ledger(reviews, "")
    dropped = {f"f{r}.py:1" for r in range(30)} - {g.where for g in ledger.shown_gate}
    assert dropped
    assert count_hits(ledger, sorted(dropped)).repeat == 0


def test_measure_ledger_leaves_hits_unset_when_the_new_block_did_not_parse() -> None:
    measure = measure_ledger(
        (_review([_obj("a.py:1")]),), "", parse_objections("no block"), prompt_bytes=7
    )
    assert (measure.repeat, measure.settled_hit, measure.prompt_bytes) == (None, None, 7)
    assert measure.ledger_bytes > 0


def test_pr_round_log_carries_the_ledger_fields(caplog: pytest.LogCaptureFixture) -> None:
    report = parse_objections(f"{M}\n{json.dumps([_obj('VoxelBakedWorldActor.h:83')])}\n")
    measure = measure_ledger(
        (_review([_obj("VoxelBakedWorldActor.h:84", "regression")]),),
        PR590_BODY,
        report,
        prompt_bytes=1234,
    )
    with caplog.at_level(logging.INFO, logger=pr_review.logger.name):
        pr_review._log_pr_round(
            "o/r#1",
            "naysayer",
            ChatCompletion("", None, "stop", "m", {"completion_tokens": 7}),
            PrRoundContext(round_number=2, scope="full-fallback"),
            report,
            measure,
        )
    line = caplog.messages[-1]
    for piece in (
        f"ledger_bytes={measure.ledger_bytes}",
        "prompt_bytes=1234",
        "ledger_omitted=0",
        "repeat=1",
        "settled_hit=1",
    ):
        assert piece in line
    (totals,) = tally_log_lines([line]).values()
    assert totals["prompt_bytes"] == 1234
    assert totals["settled_hit"] == 1


def test_pr_round_log_without_a_ledger_shows_dashes(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=pr_review.logger.name):
        pr_review._log_pr_round(
            "o/r#1",
            "naysayer",
            ChatCompletion("", None, "stop", "m", {}),
            PrRoundContext(round_number=1, scope="full"),
            parse_objections(f"{M}\n[]\n"),
        )
    line = caplog.messages[-1]
    for field in ("ledger_bytes", "prompt_bytes", "ledger_omitted", "repeat", "settled_hit"):
        assert f" {field}=-" in line


# ── Stage A: nothing reaches the model ────────────────────────────────────────────────────────


def test_the_ledger_never_reaches_the_prompt() -> None:
    reviews = (_review([_obj("VoxelBakedWorldActor.h:84", "regression")]),)
    with_history = PrRoundContext(
        2, "delta", prev_sha="a" * 40, delta="+d", prior_review=reviews[0], prior_reviews=reviews
    )
    without = PrRoundContext(2, "delta", prev_sha="a" * 40, delta="+d", prior_review=reviews[0])
    assert (
        _build_messages("whole", "o/r#7", round_context=with_history)[1].content
        == _build_messages("whole", "o/r#7", round_context=without)[1].content
    )
    fallback = PrRoundContext(2, "full-fallback", prior_reviews=reviews)
    assert fallback.render() == ""
    # No injection path and no switch: the builder takes no ledger, and the module offers none.
    assert "ledger" not in inspect.signature(_build_messages).parameters
    assert "ledger" not in inspect.getsource(pr_review._build_messages)


def test_gate_rules_are_unchanged_and_authors_learn_the_settled_line() -> None:
    assert "the diff and not the PR body" in REVIEW_ROUND_RULES
    assert "reopen" not in REVIEW_ROUND_RULES
    assert "`Settled: " in AUTHOR_OBJECTION_LINE_RULE_PR
    assert "nobody verifies it" in AUTHOR_OBJECTION_LINE_RULE_PR
    assert (
        settled_items("## Settled\nSettled: a.h:1 — the claim — basis: ruling msg-9\n")[0].basis
        == "ruling msg-9"
    )
