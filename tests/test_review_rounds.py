"""T-naysayer-exhaustive-first-then-scoped: the reader, counters, prompts and round wiring."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from spirrow_mindwire.adapters.naysayer_lexora import _NAYSAYER_SYSTEM_PROMPT
from spirrow_mindwire.adapters.naysayer_sdk import _NAYSAYER_ROLE_PROMPT
from spirrow_mindwire.conductor.core import Conductor
from spirrow_mindwire.conductor.handoff import build_handoff_protocol_block
from spirrow_mindwire.github.client import GitHubClient, GitHubHTTPError, PrRef, ReviewEvent
from spirrow_mindwire.lexora.client import ChatCompletion
from spirrow_mindwire.naysayer import pr_review
from spirrow_mindwire.naysayer.pr_review import (
    _PR_REVIEW_SYSTEM_PROMPT,
    PrRoundContext,
    _build_messages,
    _is_whole_content_review,
    decide_verdict,
    parse_objections,
)
from spirrow_mindwire.naysayer.review_rounds import (
    OBJECTIONS_SENTINEL,
    REVIEW_ROUND_RULES,
    ObjectionFacts,
    count_round,
    facts_from_element,
    log_design_round,
    read_objection_block,
    render_review_round_line,
)
from spirrow_mindwire.naysayer.round_stats import median_rounds, tally_log_lines, tally_thread
from spirrow_mindwire.thread_context import build_turn_prompt
from spirrow_mindwire.value_objects import (
    ChatroomEvent,
    EventType,
    NewMessagePayload,
    Role,
    ThreadRef,
)

M = OBJECTIONS_SENTINEL
BLOCKING_ITEM: dict[str, Any] = {
    "class": "correctness",
    "where": "D3",
    "evidence": "breaks",
    "fix_min": "a",
    "fix_cut": "none: spec",
    "fix_weight": "small",
}


def _fenced(array: object, *, close: str = "```", summary: str = "objections") -> str:
    return (
        f"prose\n\n<details><summary>{summary}</summary>\n\n{M}\n"
        f"```json\n{json.dumps(array)}\n{close}\n\n</details>\n\nNEXT: Bohr"
    )


# ── msg-7505: the two shapes ──────────────────────────────────────────────────────────────────


def test_summary_bracket_is_not_read_as_the_array() -> None:
    block = read_objection_block(_fenced([BLOCKING_ITEM], summary="objections [v1.2]"))
    assert block.elements == (BLOCKING_ITEM,)


def test_marker_before_details_with_bracket_in_summary_still_reads_the_fence() -> None:
    body = (
        f"{M}\n<details><summary>objections [v1.2]</summary>\n\n"
        f"```json\n{json.dumps([BLOCKING_ITEM])}\n```\n</details>"
    )
    assert read_objection_block(body).elements == (BLOCKING_ITEM,)


def test_raw_array_after_marker_reads_the_real_pr_gate_shape() -> None:
    body = (
        f"critique\n\n{M}\n"
        '[{"class": "correctness", "where": "a.py:1", "evidence": "x"}]\n\n'
        "VERDICT: REQUEST_CHANGES\n\n"
        "<!-- mindwire:verdict head_sha=abc event=REQUEST_CHANGES -->\n\nADR-INDEX: unavailable"
    )
    block = read_objection_block(body)
    assert block.elements is not None and len(block.elements) == 1


def test_raw_form_is_first_even_when_a_fence_appears_later() -> None:
    body = f'{M}\n[]\n\nexample:\n```json\n[{{"class": "correctness"}}]\n```'
    assert read_objection_block(body).elements == ()


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("no block at all", "no-marker"),
        (f"{M}\nno fence, no bracket", "no-block"),
        (f"{M}\n[]\n{M}\n[]", "multi-marker"),
        (f"{M}\n[not json", "bad-json"),
    ],
)
def test_unreadable_is_unparsed_and_never_empty(body: str, reason: str) -> None:
    block = read_objection_block(body)
    assert block.elements is None
    assert block.reason == reason


def test_evidence_holding_comment_end_and_bracket_survives_the_fence() -> None:
    item = {**BLOCKING_ITEM, "evidence": "a --> b ] c"}
    assert read_objection_block(_fenced([item])).elements == (item,)


def test_over_deep_payload_is_unparsed_not_a_crash() -> None:
    assert read_objection_block(f"{M}\n" + "[" * 100_000).elements is None
    assert read_objection_block(f"{M}\n```json\n" + "[" * 100_000 + "\n```").elements is None


# ── msg-7620's three, kept as msg-7732 requires ───────────────────────────────────────────────


def test_evidence_holding_a_python_fence_reads_whole() -> None:
    item = {**BLOCKING_ITEM, "evidence": "fails:\n```python\nfoo()\n```\nhere"}
    assert read_objection_block(_fenced([item])).elements == (item,)


def test_ordinary_form_reads_unchanged() -> None:
    items = [BLOCKING_ITEM, {"class": "structure", "where": "D1", "evidence": "noise"}]
    assert read_objection_block(_fenced(items)).elements == tuple(items)


def test_unclosed_fence_is_unparsed() -> None:
    block = read_objection_block(f"{M}\n```json\n{json.dumps([BLOCKING_ITEM])}\n")
    assert block.elements is None
    assert block.reason == "unclosed-fence"


# ── msg-7721 / msg-7732: the human's condition ────────────────────────────────────────────────


def test_closing_line_with_trailing_text_reads_count_and_content() -> None:
    items = [BLOCKING_ITEM, {"class": "structure", "where": "D1", "evidence": "noise"}]
    body = (
        f"<details><summary>objections</summary>\n{M}\n"
        f"```json\n{json.dumps(items)}\n``` </details>\n\nNEXT: Bohr"
    )
    block = read_objection_block(body)
    assert block.elements is not None
    assert len(block.elements) == 2
    assert block.elements == tuple(items)


def test_closing_fence_may_be_indented() -> None:
    assert read_objection_block(_fenced([BLOCKING_ITEM], close="  ```")).elements == (
        BLOCKING_ITEM,
    )


def test_invalid_json_cut_by_a_raw_newline_fence_is_unparsed_not_zero() -> None:
    # msg-7732: a raw newline inside a string, the next line starting with ```. Cut short, then
    # both parses fail. Silently reading this as no objections is the failure being excluded.
    body = f'{M}\n```json\n[{{"class": "correctness", "evidence": "a\n```python\nb"}}]\n```'
    block = read_objection_block(body)
    assert block.elements is None
    assert block.reason == "bad-json"


def test_fallback_retries_first_to_last_bracket_once() -> None:
    body = f"{M}\n```json\nHere you go:\n{json.dumps([BLOCKING_ITEM])}\nDone.\n```"
    assert read_objection_block(body).elements == (BLOCKING_ITEM,)


# ── D4: counters are measurement ──────────────────────────────────────────────────────────────

VOCABULARY = {"correctness": True, "structure": False}


def test_fix_missing_counts_blocking_only_and_needs_all_three() -> None:
    facts = [
        facts_from_element(BLOCKING_ITEM, VOCABULARY),
        facts_from_element({"class": "correctness", "fix_min": "a"}, VOCABULARY),
        facts_from_element({**BLOCKING_ITEM, "fix_weight": "huge"}, VOCABULARY),
        facts_from_element({"class": "structure"}, VOCABULARY),
    ]
    counts = count_round(facts, round_number=1)
    assert (counts.blocking, counts.advisory, counts.fix_missing) == (3, 1, 2)


def test_late_counters_need_round_two() -> None:
    facts = [ObjectionFacts(blocks=False, late=True), ObjectionFacts(blocks=True, late=True)]
    round_one = count_round(facts, round_number=1)
    assert (round_one.late_advisory, round_one.late) == (0, 0)
    round_two = count_round(facts, round_number=2)
    assert (round_two.late_advisory, round_two.late) == (1, 1)


def test_late_must_be_the_json_true() -> None:
    assert facts_from_element({"class": "correctness", "late": "true"}, VOCABULARY).late is False


def test_unknown_class_and_malformed_element_count_as_blocking() -> None:
    assert facts_from_element({"class": "nope"}, VOCABULARY).blocks is True
    assert facts_from_element("text", VOCABULARY).blocks is True


def test_new_keys_change_neither_the_parse_status_nor_the_verdict() -> None:
    # D4 / Stage 1: the same critique with and without the new keys decides identically.
    from spirrow_mindwire.naysayer.pr_review import _make_diff_view

    bare = {"class": "correctness", "where": "a.py:1", "evidence": "x"}
    extended = {**bare, "late": True, "fix_min": "m"}  # fix_cut, fix_weight absent: fix_missing
    decided = [
        decide_verdict(
            f"c\n\n{M}\n{json.dumps([item])}\n\nVERDICT: APPROVE",
            view=_make_diff_view("d"),
            finish_reason="stop",
        )
        for item in (bare, extended)
    ]
    assert decided[0].objections.status is decided[1].objections.status
    assert decided[0].gate_verdict is decided[1].gate_verdict is ReviewEvent.APPROVE
    assert decided[0].derived_verdict is decided[1].derived_verdict
    parsed = parse_objections(f"{M}\n{json.dumps([extended])}").objections[0]
    assert (parsed.late, parsed.fix_min, parsed.fix_cut) == (True, "m", "")


# ── D5: the log line ──────────────────────────────────────────────────────────────────────────


def _log_design(caplog: pytest.LogCaptureFixture, body: str, round_number: int | None) -> str:
    logger = logging.getLogger("test.rounds")
    with caplog.at_level(logging.INFO, logger="test.rounds"):
        log_design_round(
            logger,
            body=body,
            thread="T-x",
            backend="gemini",
            model="naysayer",
            round_number=round_number,
            out_tokens=12,
        )
    return caplog.messages[-1]


def test_log_line_carries_every_field(caplog: pytest.LogCaptureFixture) -> None:
    line = _log_design(caplog, _fenced([{**BLOCKING_ITEM, "late": True}]), 2)
    assert line.startswith("naysayer.round thread=T-x pr=- backend=gemini model=naysayer round=2")
    for piece in (
        "scope=delta",
        "blocking=1",
        "advisory=0",
        "late=1",
        "late_advisory=0",
        "fix_missing=0",
        "objections_unparsed=0",
        "out_tokens=12",
    ):
        assert piece in line


def test_unparsed_log_line_counts_nothing(caplog: pytest.LogCaptureFixture) -> None:
    line = _log_design(caplog, "a critique with no block", 1)
    assert "scope=full" in line
    assert "objections_unparsed=1" in line
    for field in ("blocking", "advisory", "late", "late_advisory", "fix_missing"):
        assert f" {field}=-" in line


def test_block_that_does_not_parse_logs_unparsed_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A marker with a broken block is the ``elements is None`` path; the log call must survive."""
    line = _log_design(caplog, f"{M}\n```json\n[{{broken\n```\n", 2)
    assert "objections_unparsed=1" in line
    assert " blocking=-" in line


def test_unreadable_vocabulary_logs_unparsed_without_raising(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block parsed but cannot be classified: a posted turn must not be lost to measurement."""
    from spirrow_mindwire.naysayer import principles

    def unreadable() -> None:
        raise principles.PrinciplesError("vocabulary unreadable")

    monkeypatch.setattr(principles, "objection_classes", unreadable)
    line = _log_design(caplog, _fenced([BLOCKING_ITEM]), 2)
    assert "objections_unparsed=1" in line
    assert " blocking=-" in line


def _log_pr(caplog: pytest.LogCaptureFixture, critique: str) -> str:
    """D5 through the PR gate's own parser, so a parser status change reaches this assertion."""
    with caplog.at_level(logging.INFO, logger=pr_review.logger.name):
        pr_review._log_pr_round(
            "o/r#1",
            "naysayer",
            ChatCompletion("", None, "stop", "m", {"completion_tokens": 7}),
            PrRoundContext(round_number=2, scope="delta"),
            parse_objections(critique),
        )
    return caplog.messages[-1]


@pytest.mark.parametrize(
    "payload",
    [
        '[{"class": "correctness", "where": "a", "evidence": "b"}',  # unclosed array
        "[",
        '[{"class": }]',
        '[{"class": "correctness", "evid',  # cut off mid-string
        '{"class": "correctness"}',  # not an array
        "prose first []",
        '[]\n[{"class": "correctness", "where": "a", "evidence": "b"}]',  # decoy before a block
    ],
)
def test_pr_round_block_that_does_not_parse_is_unparsed_not_zero(
    caplog: pytest.LogCaptureFixture, payload: str
) -> None:
    line = _log_pr(caplog, f"{M}\n{payload}\n")
    assert "objections_unparsed=1" in line
    assert " blocking=-" in line


def test_pr_round_without_a_marker_is_unparsed(caplog: pytest.LogCaptureFixture) -> None:
    line = _log_pr(caplog, "a critique with no block")
    assert "objections_unparsed=1" in line
    assert " blocking=-" in line


def test_pr_round_empty_block_is_a_real_zero(caplog: pytest.LogCaptureFixture) -> None:
    """``[]`` read correctly, so it must not be lumped in with failures to read."""
    line = _log_pr(caplog, f"{M}\n[]\n")
    assert "objections_unparsed=0" in line
    assert " blocking=0" in line


@pytest.mark.parametrize(
    ("element", "blocking"),
    [
        ({"class": "no-such-class", "where": "a", "evidence": "b"}, 1),  # status UNKNOWN
        (1, 1),  # malformed element, status UNKNOWN
        ({"class": "correctness", "where": "a"}, 1),  # status NO_EVIDENCE
    ],
)
def test_pr_round_counts_readable_blocks_whose_status_is_not_ok(
    caplog: pytest.LogCaptureFixture, element: object, blocking: int
) -> None:
    """These parsed and block the verdict, so reading only ``OK`` would lose counted blockers."""
    line = _log_pr(caplog, f"{M}\n{json.dumps([element])}\n")
    assert "objections_unparsed=0" in line
    assert f" blocking={blocking}" in line


# ── D1 / D6 / D7: one rule text in every prompt ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "prompt", [_NAYSAYER_ROLE_PROMPT, _NAYSAYER_SYSTEM_PROMPT, _PR_REVIEW_SYSTEM_PROMPT]
)
def test_every_naysayer_prompt_loads_the_one_rule_text(prompt: str) -> None:
    assert REVIEW_ROUND_RULES in prompt
    assert "fix_weight" in prompt


@pytest.mark.parametrize("prompt", [_NAYSAYER_ROLE_PROMPT, _NAYSAYER_SYSTEM_PROMPT])
def test_design_prompts_teach_the_folded_fence_and_it_reads_back(prompt: str) -> None:
    start = prompt.index("<details>")
    example = prompt[start : prompt.index("</details>") + len("</details>")]
    # The prompt's own example must be a shape the reader accepts.
    block = read_objection_block(example)
    assert block.elements is not None and len(block.elements) == 1


def test_rules_carry_d7_independence_and_the_late_rules() -> None:
    assert "as-proposed" in REVIEW_ROUND_RULES
    assert "defects in the proposal itself" in REVIEW_ROUND_RULES
    # The PR-gate shares this text but never reads the PR body, where authors put these marks.
    assert "the diff and not the PR body" in REVIEW_ROUND_RULES
    assert "LATE:" in REVIEW_ROUND_RULES
    assert "Do not raise advisory" in REVIEW_ROUND_RULES


def test_authors_are_told_the_obj_line_and_only_the_implementer_about_merging() -> None:
    proposer = build_handoff_protocol_block(Role.PROPOSER)
    implementer = build_handoff_protocol_block(Role.IMPLEMENTER)
    assert "Obj-<round>-<n>" in proposer
    assert "Obj-<round>-<n>" in implementer
    assert "Gate objections round N" in implementer
    assert "merge commit" in implementer
    assert "merge commit" not in proposer


# ── D2: design-time line ──────────────────────────────────────────────────────────────────────


def _event(notice: str | None) -> ChatroomEvent:
    return ChatroomEvent(
        event_id="T:msg-2",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=ThreadRef(project_id="p", thread_id="T", chatroom_uri="c"),
        occurred_at=datetime(2026, 11, 2, tzinfo=UTC),
        payload=NewMessagePayload(msg_id="msg-2", author="Bohr", body="b", parent_msg_id=None),
        review_round_notice=notice,
    )


def test_review_round_line_leads_the_prompt_and_round_one_has_none() -> None:
    line = render_review_round_line(3, "msg-9")
    assert line == "REVIEW ROUND: 3 / SCOPE: msg-9 より後の全投稿（作者を問わない）"  # noqa: RUF001
    with_line = build_turn_prompt(_event(line), Role.NAYSAYER, "closing")
    assert with_line.index(line) < with_line.index("New message from")
    assert "REVIEW ROUND" not in build_turn_prompt(_event(None), Role.NAYSAYER, "closing")


# ── D2: PR gate ───────────────────────────────────────────────────────────────────────────────

PR = PrRef("o", "r", 7)
A_SHA, C_SHA, D_SHA, E_SHA, F_SHA = ("a" * 40, "c" * 40, "d" * 40, "e" * 40, "f" * 40)


def _review_post(sha: str, event: str = "REQUEST_CHANGES", extra: str = "") -> str:
    return (
        f"{extra}critique of {sha}\n\n{M}\n[]\n\nVERDICT: {event}\n\n"
        f"<!-- mindwire:verdict head_sha={sha} event={event} -->\n\nADR-INDEX: unavailable"
    )


class _Compare:
    def __init__(
        self, status: str = "ahead", delta: str = "+delta", exc: Exception | None = None
    ) -> None:
        self.status, self.delta, self.exc = status, delta, exc
        self.compared: list[tuple[str, str]] = []

    async def fetch_compare_status(self, owner: str, repo: str, base: str, head: str) -> str:
        self.compared.append((base, head))
        if self.exc is not None:
            raise self.exc
        return self.status

    async def fetch_compare_diff(self, owner: str, repo: str, base: str, head: str) -> str:
        return self.delta


def _context(
    posts: list[str] | Exception, compare: _Compare | None = None
) -> Any:  # PrRoundContext
    from spirrow_mindwire.naysayer.pr_review import NaysayerPrReviewDriver

    async def read() -> list[tuple[str, str]]:
        if isinstance(posts, Exception):
            raise posts
        return [("gate", b) for b in posts]

    driver = NaysayerPrReviewDriver.__new__(NaysayerPrReviewDriver)
    driver._github = compare or _Compare()  # type: ignore[assignment]
    return asyncio.run(driver._round_context(PR, "head", read))


def test_first_review_is_round_one_full() -> None:
    context = _context([])
    assert (context.round_number, context.scope) == (1, "full")
    assert context.render() == ""


def test_second_review_scopes_to_the_previous_sha() -> None:
    compare = _Compare()
    context = _context([_review_post(A_SHA)], compare)
    assert (context.round_number, context.scope, context.prev_sha) == (2, "delta", A_SHA)
    assert compare.compared == [(A_SHA, "head")]
    rendered = context.render()
    assert "REVIEW ROUND: 2" in rendered
    assert "+delta" in rendered
    assert f"critique of {A_SHA}" in rendered


def test_non_ancestor_previous_sha_falls_back_to_full() -> None:
    context = _context([_review_post(A_SHA)], _Compare(status="diverged"))
    assert (context.round_number, context.scope) == (2, "full-fallback")
    assert context.render() == ""


@pytest.mark.parametrize(
    "compare",
    [
        _Compare(exc=GitHubHTTPError("down")),
        _Compare(delta="   "),  # same head re-reviewed: an empty delta must not narrow
        _Compare(delta="x" * 500_000),
    ],
)
def test_no_usable_delta_falls_back_to_full(compare: _Compare) -> None:
    assert (_context([_review_post(A_SHA)], compare)).scope == "full-fallback"


def test_unreadable_thread_falls_back_to_full() -> None:
    assert (_context(RuntimeError("down"))).scope == "full-fallback"


def test_posts_that_did_not_judge_the_whole_diff_never_start_a_delta() -> None:
    ci_gate = (
        "CI is failing.\n\nVERDICT: REQUEST_CHANGES\n\n"
        f"<!-- mindwire:verdict head_sha={C_SHA} event=REQUEST_CHANGES -->"
    )
    held = _review_post(D_SHA, "COMMENT")
    truncated = _review_post(E_SHA, extra="<!-- mindwire:note B-diff -->\n")
    cut = _review_post(F_SHA, extra="<!-- mindwire:note B-len -->\n")
    for body in (ci_gate, held, truncated, cut):
        assert _is_whole_content_review(body) is False
    compare = _Compare()
    context = _context([_review_post(A_SHA), ci_gate, held, truncated, cut], compare)
    assert (context.round_number, context.prev_sha) == (2, A_SHA)
    assert (_context([ci_gate, truncated])).scope == "full"


def test_a_replayed_review_is_one_round() -> None:
    post = _review_post(A_SHA)
    assert (_context([post, post])).round_number == 2


def test_round_section_is_in_pass_one_and_absent_for_a_full_review() -> None:
    from spirrow_mindwire.naysayer.pr_review import PrRoundContext

    scoped = PrRoundContext(2, "delta", prev_sha="a" * 40, delta="+d", prior_review="old")
    user = _build_messages("whole", "o/r#7", round_context=scoped)[1].content or ""
    assert user.index("+d") < user.index("```diff\nwhole")
    full = PrRoundContext(1, "full")
    assert (
        _build_messages("whole", "o/r#7", round_context=full)[1].content
        == _build_messages("whole", "o/r#7")[1].content
    )


# ── D5: offline tally ─────────────────────────────────────────────────────────────────────────


def test_tally_reads_arrays_and_author_lines_and_finds_unanswered() -> None:
    round_one = _fenced([BLOCKING_ITEM, {"class": "structure", "where": "D", "evidence": "e"}])
    round_two = (
        _fenced([{**BLOCKING_ITEM, "late": True}])
        + "\n<!-- attest: tier=naysayer · backend=gemini · route=x -->"
    )
    stats = tally_thread(
        "T",
        [
            ("Bohr", "design"),
            ("Einstein", round_one),
            (
                "Bohr",
                "Obj-1-1: 採用=cut — small\nObj-1-2: 採用=as-proposed — ok\nObj-9-9: 採用=min — x",
            ),
            ("Einstein", round_two),
            ("Einstein", "no block"),
        ],
        naysayers=frozenset({"einstein"}),
        vocabulary_blocks=VOCABULARY,
    )
    assert stats.rounds == 3
    assert (stats.blocking, stats.advisory, stats.late) == (2, 1, 1)
    assert stats.objections_unparsed == 1
    assert stats.late_by_backend == {"gemini": 1}
    assert stats.dispositions == {"cut": 1, "as-proposed": 1}
    assert stats.unanswered == 1  # 2-1; the Obj-9-9 line answers nothing
    assert median_rounds([stats]) == 3


def test_tally_reads_author_lines_from_a_pr_body() -> None:
    stats = tally_thread(
        "T",
        [("gate", _fenced([BLOCKING_ITEM]))],
        naysayers=frozenset({"gate"}),
        vocabulary_blocks=VOCABULARY,
        extra_author_text=["## Gate objections round 1\nObj-1-1: 採用=own — mine"],
    )
    assert stats.dispositions == {"own": 1}
    assert stats.unanswered == 0


def test_log_tally_sums_by_target_and_backend_and_skips_dashes() -> None:
    lines = [
        "2026 INFO naysayer.round thread=T pr=- backend=gemini model=m round=1 scope=full "
        "blocking=2 advisory=0 late=0 late_advisory=0 fix_missing=1 objections_unparsed=0 "
        "out_tokens=100",
        "2026 INFO naysayer.round thread=T pr=- backend=gemini model=m round=2 scope=delta "
        "blocking=- advisory=- late=- late_advisory=- fix_missing=- objections_unparsed=1 "
        "out_tokens=50",
        "2026 INFO naysayer.round thread=- pr=o/r#1 backend=codex model=m round=2 "
        "scope=full-fallback blocking=0 advisory=0 late=1 late_advisory=0 fix_missing=0 "
        "objections_unparsed=0 out_tokens=-",
        "unrelated line",
    ]
    tally = tally_log_lines(lines)
    design = tally[("T", "gemini")]
    assert (design["posts"], design["out_tokens"], design["objections_unparsed"]) == (2, 150, 1)
    assert design["fix_missing"] == 1
    pr = tally[("o/r#1", "codex")]
    assert (pr["full_fallback"], pr["late"], pr["out_tokens"]) == (1, 1, 0)


# ── D2 wiring: the conductor's count, and the ancestry read ───────────────────────────────────


def test_conductor_counts_rounds_from_naysayer_posts_only() -> None:
    conductor = Conductor.__new__(Conductor)
    conductor._roster = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER}
    assert conductor._review_round(Role.NAYSAYER, [_msg("msg-1", "Bohr")]) == (1, None)
    thread = [
        _msg("msg-1", "Bohr"),
        _msg("msg-2", "einstein"),
        _msg("msg-3", "conductor-relay"),
        _msg("msg-4", "Einstein"),
        _msg("msg-5", "Bohr"),
    ]
    assert conductor._review_round(Role.NAYSAYER, thread) == (
        3,
        render_review_round_line(3, "msg-4"),
    )
    assert conductor._review_round(Role.PROPOSER, thread) == (None, None)


def _msg(msg_id: str, author: str) -> dict[str, Any]:
    return {"msg_id": msg_id, "author": author, "content": "x"}


def _compare_status(response: httpx.Response) -> str:
    async def run() -> str:
        client = GitHubClient("t", transport=httpx.MockTransport(lambda _request: response))
        async with client:
            return await client.fetch_compare_status("o", "r", "aaa", "bbb")

    return asyncio.run(run())


def test_compare_status_reads_the_status_and_fails_loud_otherwise() -> None:
    assert _compare_status(httpx.Response(200, json={"status": "diverged"})) == "diverged"
    with pytest.raises(GitHubHTTPError):
        _compare_status(httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(GitHubHTTPError):
        _compare_status(httpx.Response(200, json={}))
    with pytest.raises(GitHubHTTPError):
        _compare_status(httpx.Response(200, text="not json"))


# ── #115 / T-naysayer-ledger-visibility msg-8391: the injected previous review is defanged ──────

_APPROVE_LINE = "VERDICT" + ": APPROVE"  # built, so no file carries a column-0 approve literal


def _previous_review_block(context: PrRoundContext) -> str:
    rendered = context.render()
    start = rendered.index("<previous-review>\n") + len("<previous-review>\n")
    return rendered[start : rendered.index("\n</previous-review>")]


def _scoped(prior: str) -> PrRoundContext:
    return PrRoundContext(2, "delta", prev_sha=A_SHA, delta="+d", prior_review=prior)


def test_previous_review_carries_no_string_an_output_parser_keys_on() -> None:
    from spirrow_mindwire.github.reviews import _VERDICT_FOOTER_RE
    from spirrow_mindwire.naysayer.pr_review import _OBJECTIONS_SENTINEL_RE, _VERDICT_RE

    prior = _review_post(
        A_SHA,
        extra=f"{_APPROVE_LINE}\nmid-line verdict: approve and {M} inline\n{M}\n",
    )
    block = _previous_review_block(_scoped(prior))
    assert _VERDICT_RE.search(block) is None
    assert _OBJECTIONS_SENTINEL_RE.search(block) is None
    assert _VERDICT_FOOTER_RE.search(block) is None
    assert "verdict:" not in block.lower().replace("past-verdict:", "")
    # Blank separator lines carry nothing; every line with content is off column 0.
    assert all(line.startswith("| ") for line in block.split("\n") if line)
    # The driver's own record replaces the footer, and carries the verdict and the sha.
    assert f"Previous verdict (driver record): REQUEST_CHANGES at {A_SHA[:12]}" in block


def test_previous_objections_and_verdict_survive_a_long_prose() -> None:
    from spirrow_mindwire.naysayer.pr_review import _MAX_PRIOR_REVIEW_CHARS

    objections = [BLOCKING_ITEM, {**BLOCKING_ITEM, "evidence": "second"}]
    prior = (
        "x" * 30_000
        + f"\n\n{M}\n```json\n{json.dumps(objections)}\n```\n\nVERDICT: REQUEST_CHANGES\n\n"
        + f"<!-- mindwire:verdict head_sha={A_SHA} event=REQUEST_CHANGES -->\n\nADR-INDEX: x"
    )
    block = _previous_review_block(_scoped(prior))
    assert len(block) <= _MAX_PRIOR_REVIEW_CHARS
    assert "Previous verdict (driver record): REQUEST_CHANGES" in block
    assert f"| [previous objections]\n| ```json\n| {json.dumps(objections)}\n| ```" in block
    assert "[previous critique prose truncated: " in block
    assert block.index("[previous objections]") < block.index("xxxx")


def test_an_objections_block_over_budget_is_omitted_not_cut() -> None:
    huge = [{**BLOCKING_ITEM, "evidence": "e" * 25_000}]
    prior = _review_post(A_SHA).replace(f"{M}\n[]", f"{M}\n{json.dumps(huge)}")
    block = _previous_review_block(_scoped(prior))
    assert "[previous objections omitted: " in block
    assert "eeee" not in block and "critique of" not in block
    assert block.split("\n")[0].startswith("| Previous verdict (driver record): REQUEST_CHANGES")


def test_the_cap_holds_when_the_block_leaves_no_room_for_the_prose_label() -> None:
    # PR #538 gate round 1: a block that fits but leaves less than the truncation label's room
    # used to get the label anyway, past the cap. Sweep the block size across that boundary.
    from spirrow_mindwire.naysayer.pr_review import _MAX_PRIOR_REVIEW_CHARS

    seen = {"labelled": 0, "dropped": 0, "omitted": 0}
    for n in range(19_700, 20_000):
        items = [{**BLOCKING_ITEM, "evidence": "e" * n}]
        prior = _review_post(A_SHA, extra="x" * 3_000).replace(
            f"{M}\n[]", f"{M}\n{json.dumps(items)}"
        )
        block = _previous_review_block(_scoped(prior))
        assert len(block) <= _MAX_PRIOR_REVIEW_CHARS, n
        if "[previous objections omitted: " in block:
            seen["omitted"] += 1
            continue
        assert "e" * n in block, n
        if "[previous critique prose truncated: " in block:
            seen["labelled"] += 1
        else:
            assert "critique of" not in block and "xxx" not in block, n
            assert not block.endswith("\n"), n
            seen["dropped"] += 1
    assert all(seen.values()), seen


def test_an_echo_of_the_previous_review_does_not_add_a_verdict_line() -> None:
    from spirrow_mindwire.naysayer.pr_review import ModelVerdict, _parse_model_verdict

    block = _previous_review_block(_scoped(_review_post(A_SHA, event="APPROVE")))
    echo = f"As I said before:\n{block}\n{block.replace('| ', '')}\n\nVERDICT: REQUEST_CHANGES"
    assert _parse_model_verdict(echo) is ModelVerdict.REQUEST_CHANGES


def test_empty_prior_and_non_delta_render_as_before() -> None:
    assert _previous_review_block(_scoped("")) == ""
    for scope in ("full", "full-fallback"):
        assert PrRoundContext(2, scope, prior_review=_review_post(A_SHA)).render() == ""
