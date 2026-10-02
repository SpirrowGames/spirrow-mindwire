"""Phase 1 of the PR-review sweep: the five-class table of msg-5808 section 2."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from spirrow_mindwire.github.client import PrRef, PrResolution, PrState
from spirrow_mindwire.pr_review_sweep.phase0 import OUT_OF_SCOPE_FINISHED, Excluded
from spirrow_mindwire.pr_review_sweep.phase1 import (
    CLOSE_CANDIDATE_CLASSES,
    REASON_CLOSED_WITHOUT_CLOSED_AT,
    REASON_ID_UNPARSEABLE,
    REASON_PR_CLOSED_UNMERGED,
    REASON_PR_INDETERMINATE,
    REASON_PR_MERGED,
    REASON_PR_NOT_FOUND,
    REASON_PR_OPEN,
    Phase1Class,
    build_report,
    classify_ledger,
    report_to_json,
    unparseable_row,
)

_REF = PrRef("SpirrowGames", "spirrow-mindwire", 42)
_CLOSED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_TID = "T-pr-review-spirrow-mindwire-42"


def _pr(resolution: PrResolution, *, merged: bool = False, closed: bool = True) -> PrState:
    return PrState(
        ref=_REF,
        resolution=resolution,
        closed_at=_CLOSED_AT if closed and resolution is PrResolution.CLOSED else None,
        merged=merged,
    )


@pytest.mark.parametrize(
    ("pr", "cls", "reason"),
    [
        (_pr(PrResolution.CLOSED, merged=True), Phase1Class.TERMINAL_MERGED, REASON_PR_MERGED),
        (_pr(PrResolution.CLOSED), Phase1Class.TERMINAL_CLOSED, REASON_PR_CLOSED_UNMERGED),
        (_pr(PrResolution.OPEN), Phase1Class.OPEN, REASON_PR_OPEN),
        (_pr(PrResolution.UNRESOLVABLE), Phase1Class.SKIP, REASON_PR_INDETERMINATE),
        (
            _pr(PrResolution.CLOSED, merged=True, closed=False),
            Phase1Class.SKIP,
            REASON_CLOSED_WITHOUT_CLOSED_AT,
        ),
        (_pr(PrResolution.NOT_FOUND), Phase1Class.UNPARSEABLE, REASON_PR_NOT_FOUND),
    ],
)
def test_table_rows(pr: PrState, cls: Phase1Class, reason: str) -> None:
    row = classify_ledger(_TID, "spirrow-mindwire", "active", pr)
    assert (row.cls, row.reason) == (cls, reason)
    assert row.pr == "SpirrowGames/spirrow-mindwire#42"


def test_every_resolution_is_classified() -> None:
    """Totality: every PrResolution value lands in some class."""
    for resolution in PrResolution:
        row = classify_ledger(_TID, "p", "active", _pr(resolution))
        assert row.cls in set(Phase1Class)


def test_only_terminal_classes_are_close_candidates() -> None:
    assert {Phase1Class.TERMINAL_MERGED, Phase1Class.TERMINAL_CLOSED} == CLOSE_CANDIDATE_CLASSES
    assert not classify_ledger(_TID, "p", "active", _pr(PrResolution.OPEN)).is_close_candidate
    assert not unparseable_row("T-pr-review-173", "p", "active").is_close_candidate


def test_post_terminal_activity_is_a_column_not_a_gate() -> None:
    """msg-5808: S1 is not a gate. A busy ledger on a merged PR is still a candidate."""
    for status in ("active", "awaiting_reply", "parked"):
        row = classify_ledger(
            _TID, "p", status, _pr(PrResolution.CLOSED, merged=True), post_terminal_messages=9
        )
        assert row.cls is Phase1Class.TERMINAL_MERGED
        assert row.post_terminal_messages == 9
        assert row.closed_at == _CLOSED_AT


def test_non_terminal_rows_carry_no_post_terminal_count() -> None:
    row = classify_ledger(_TID, "p", "active", _pr(PrResolution.OPEN), post_terminal_messages=3)
    assert row.post_terminal_messages is None
    assert row.closed_at is None


def test_report_json_shape() -> None:
    rows = [
        classify_ledger(_TID, "p", "active", _pr(PrResolution.CLOSED, merged=True)),
        classify_ledger("T-pr-review-p-43", "p", "active", _pr(PrResolution.CLOSED)),
        classify_ledger("T-pr-review-p-44", "p", "active", _pr(PrResolution.OPEN)),
        unparseable_row("T-pr-review-173", "p", "active"),
    ]
    excluded = [Excluded("T-pr-review-p-1", "resolved", OUT_OF_SCOPE_FINISHED)]
    payload = report_to_json(build_report(rows, out_of_scope=excluded))

    assert payload["phase"] == 1
    assert payload["wrote_anything"] is False
    assert payload["counts"] == {
        "terminal_merged": 1,
        "terminal_closed": 1,
        "open": 1,
        "skip": 0,
        "unparseable": 1,
    }
    assert payload["close_candidates"] == [_TID, "T-pr-review-p-43"]
    assert payload["close_candidate_count"] == 2
    assert [r["thread_id"] for r in payload["human_list"]] == ["T-pr-review-173"]
    assert payload["human_list"][0]["reason"] == REASON_ID_UNPARSEABLE
    assert payload["out_of_scope_count"] == 1
    assert sum(payload["counts"].values()) == len(payload["rows"])
