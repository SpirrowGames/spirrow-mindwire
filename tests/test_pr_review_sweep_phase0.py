"""S0/S1 classification, the measurement/classification split, and the go/no-go.

Every branch of msg-2151 D-6 is reachable from a constructed :class:`ThreadFacts` +
:class:`PrState` pair, so none of this needs a network. The tests are written around
the *decisions* rather than the code paths: what must never be swept, and which way an
ambiguity has to fall.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spirrow_mindwire.github.client import PrRef, PrResolution, PrState
from spirrow_mindwire.pr_review_sweep.phase0 import (
    GO_THRESHOLD,
    MARGIN_LADDER_SECONDS,
    Bucket,
    ThreadFacts,
    Verdict,
    build_report,
    classify,
    measurement_offsets_seconds,
    report_to_json,
    sensitivity_table,
)

_REF = PrRef("o", "r", 1)
_TERMINAL = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
_MARGIN = timedelta(seconds=300)


def _closed(**kw: object) -> PrState:
    return PrState(
        ref=_REF,
        resolution=PrResolution.CLOSED,
        closed_at=kw.pop("closed_at", _TERMINAL),  # type: ignore[arg-type]
        merged=bool(kw.pop("merged", True)),
    )


def _facts(**kw: object) -> ThreadFacts:
    kw.setdefault("thread_id", "T-pr-review-p-1")
    kw.setdefault("status", "active")
    return ThreadFacts(**kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- S0


def test_an_indeterminate_pr_produces_no_verdict_at_all() -> None:
    """D-7 fail-open. A 5xx must not close a thread NOR file it as debt."""
    pr = PrState(ref=_REF, resolution=PrResolution.UNRESOLVABLE)
    result = classify(_facts(), pr, margin=_MARGIN)
    assert result.bucket is Bucket.SKIP
    assert result.reason == "pr-indeterminate"


def test_a_missing_pr_is_a_finding_not_a_skip() -> None:
    """404 is a definite answer, so unlike a 5xx it belongs in the reported set."""
    pr = PrState(ref=_REF, resolution=PrResolution.NOT_FOUND)
    result = classify(_facts(), pr, margin=_MARGIN)
    assert result.bucket is Bucket.AB
    assert result.reason == "pr-unresolvable"


def test_an_open_pr_is_never_swept() -> None:
    """The disjunct dropped in R-3 and restored in D-6. An open PR is live work."""
    pr = PrState(ref=_REF, resolution=PrResolution.OPEN)
    result = classify(_facts(status="active"), pr, margin=_MARGIN)
    assert result.bucket is Bucket.C
    assert result.reason == "pr-open"


def test_an_open_pr_short_circuits_before_any_time_comparison() -> None:
    """``closed_at`` is null while a PR is open; S0 must return before touching it."""
    pr = PrState(ref=_REF, resolution=PrResolution.OPEN, closed_at=None)
    assert classify(_facts(status="resolved"), pr, margin=_MARGIN).bucket is Bucket.C


def test_a_closed_pr_without_closed_at_is_skipped_not_guessed() -> None:
    pr = PrState(ref=_REF, resolution=PrResolution.CLOSED, closed_at=None)
    result = classify(_facts(), pr, margin=_MARGIN)
    assert result.bucket is Bucket.SKIP
    assert result.reason == "closed-without-closed-at"


# --------------------------------------------------------------------------- S1


def test_a_parked_thread_is_never_swept_whatever_else_is_true() -> None:
    """msg-2149 R-1: parking means "waiting on a condition I wrote down", not "dead"."""
    result = classify(_facts(status="parked"), _closed(), margin=_MARGIN)
    assert result.bucket is Bucket.C
    assert result.reason == "thread-parked"


def test_post_terminal_activity_plus_a_named_successor_keeps_a_thread() -> None:
    facts = _facts(
        status="active",
        last_next_participant="Bohr",
        classification_events=(_TERMINAL + timedelta(minutes=30),),
    )
    result = classify(facts, _closed(), margin=_MARGIN)
    assert result.bucket is Bucket.C
    assert result.liveness_limb == "next_participant"


def test_awaiting_reply_satisfies_limb_two_without_a_named_successor() -> None:
    """The limb that actually fires in this deployment — next_participant is usually empty."""
    facts = _facts(
        status="awaiting_reply",
        last_next_participant=None,
        classification_events=(_TERMINAL + timedelta(minutes=1),),
    )
    result = classify(facts, _closed(), margin=_MARGIN)
    assert result.bucket is Bucket.C
    assert result.liveness_limb == "awaiting_reply"


def test_a_closing_remark_with_nobody_named_is_not_liveness() -> None:
    """(i) true, (ii) false: someone wrote a last word and stopped. Not a live thread."""
    facts = _facts(
        status="active",
        last_next_participant="   ",
        classification_events=(_TERMINAL + timedelta(minutes=30),),
    )
    result = classify(facts, _closed(), margin=_MARGIN)
    assert result.bucket is Bucket.AB
    assert result.reason == "terminal-and-quiescent"


def test_silence_since_the_pr_ended_is_not_liveness() -> None:
    """(i) false: the only messages predate the PR's terminal time by more than the margin."""
    facts = _facts(status="awaiting_reply", classification_events=(_TERMINAL - timedelta(hours=3),))
    assert classify(facts, _closed(), margin=_MARGIN).bucket is Bucket.AB


def test_the_margin_shifts_the_cutoff_toward_survival_only() -> None:
    """msg-2164 R-21. A msg one minute BEFORE terminal still counts as post-terminal.

    The asymmetry is the whole point: a false "still alive" costs one more sweep, a
    false "dead" costs an irreversible close in Phase 2.
    """
    just_before = _TERMINAL - timedelta(minutes=1)
    facts = _facts(status="awaiting_reply", classification_events=(just_before,))
    assert classify(facts, _closed(), margin=_MARGIN).bucket is Bucket.C
    # With no margin the same thread is quiescent — so the margin, not the data, is
    # what moved the answer, and that is exactly what the sensitivity table exposes.
    assert classify(facts, _closed(), margin=timedelta(0)).bucket is Bucket.AB


def test_the_cutoff_is_inclusive_matching_the_audit_query() -> None:
    """``since`` is an inclusive lower bound, so an event exactly on it must count."""
    facts = _facts(status="awaiting_reply", classification_events=(_TERMINAL - _MARGIN,))
    assert classify(facts, _closed(), margin=_MARGIN).bucket is Bucket.C


@pytest.mark.parametrize("status", ["resolved", "superseded", ""])
def test_statuses_outside_the_liveness_set_fall_through_to_the_union(status: str) -> None:
    """D-6 verbatim: only ``parked`` / ``active`` / ``awaiting_reply`` can yield C.

    Recorded rather than "fixed": a resolved thread landing in ``a_union_b`` would inflate the
    go/no-go, which is why :func:`build_report` also emits the status breakdown.
    """
    facts = _facts(
        status=status,
        last_next_participant="Bohr",
        classification_events=(_TERMINAL + timedelta(days=1),),
    )
    assert classify(facts, _closed(), margin=_MARGIN).bucket is Bucket.AB


def test_an_unmerged_close_is_still_terminal() -> None:
    """B_unmerged is a real member of the union; merge status does not gate S1."""
    result = classify(_facts(status="active"), _closed(merged=False), margin=_MARGIN)
    assert result.bucket is Bucket.AB


# ------------------------------------------------------- measurement / sensitivity


def test_measurement_is_raw_and_unsummarised() -> None:
    facts = _facts(
        measurement_events=(
            _TERMINAL - timedelta(seconds=90),
            _TERMINAL + timedelta(seconds=30),
        )
    )
    assert measurement_offsets_seconds(facts, _TERMINAL) == [-90.0, 30.0]


def test_classification_events_do_not_leak_into_the_measurement() -> None:
    """msg-2166 R-25: the two queries are separate fields precisely so this holds."""
    facts = _facts(classification_events=(_TERMINAL + timedelta(seconds=5),), measurement_events=())
    assert measurement_offsets_seconds(facts, _TERMINAL) == []


def test_measurement_events_do_not_decide_the_classification() -> None:
    """The converse leak. An event only the unwindowed query saw must not create a C."""
    facts = _facts(
        status="awaiting_reply",
        classification_events=(),
        measurement_events=(_TERMINAL + timedelta(days=2),),
    )
    assert classify(facts, _closed(), margin=_MARGIN).bucket is Bucket.AB


def test_sensitivity_shows_when_the_verdict_depends_on_the_guess() -> None:
    """msg-2166 R-26. A thread whose only msg is 10 minutes pre-terminal flips at 900s."""
    facts = _facts(status="awaiting_reply", measurement_events=(_TERMINAL - timedelta(minutes=10),))
    table = sensitivity_table([(facts, _closed())])
    assert [row["margin_seconds"] for row in table] == list(MARGIN_LADDER_SECONDS)
    counts = {row["margin_seconds"]: row["a_union_b"] for row in table}
    assert counts[0] == 1 and counts[300] == 1  # margin too small: counted as leftover
    assert counts[900] == 0 and counts[86400] == 0  # margin now covers it: alive


def test_sensitivity_is_flat_when_the_margin_is_irrelevant() -> None:
    facts = _facts(status="active", measurement_events=())
    table = sensitivity_table([(facts, _closed())])
    assert {row["a_union_b"] for row in table} == {1}


# --------------------------------------------------------------------------- verdict


def _quiescent(n: int) -> list[tuple[ThreadFacts, PrState]]:
    return [
        (ThreadFacts(thread_id=f"T-pr-review-p-{i}", status="active"), _closed()) for i in range(n)
    ]


def test_the_threshold_is_five_and_it_is_the_union_not_a() -> None:
    assert GO_THRESHOLD == 5
    assert build_report(_quiescent(5), margin_seconds=300).verdict is Verdict.GO
    assert build_report(_quiescent(4), margin_seconds=300).verdict is Verdict.NO_GO


def test_a_no_go_carries_the_whole_short_list() -> None:
    """msg-2172 R-37: Phase 0 cannot compute B_gate, so a human reads the <=4 by hand."""
    report = build_report(_quiescent(4), margin_seconds=300)
    assert len(report.no_go_full_list) == 4
    assert {c.thread_id for c in report.no_go_full_list} == {f"T-pr-review-p-{i}" for i in range(4)}


def test_a_go_does_not_carry_the_list() -> None:
    assert build_report(_quiescent(5), margin_seconds=300).no_go_full_list == []


def test_skips_do_not_count_toward_the_threshold() -> None:
    """Five threads, but the two unreadable ones must not manufacture a GO."""
    pairs = _quiescent(3)
    for i in range(2):
        pairs.append(
            (
                ThreadFacts(thread_id=f"T-skip-{i}", status="active"),
                PrState(ref=_REF, resolution=PrResolution.UNRESOLVABLE),
            )
        )
    report = build_report(pairs, margin_seconds=300)
    assert report.a_union_b == 3
    assert report.verdict is Verdict.NO_GO


def test_the_report_records_the_status_mix_and_the_limb_used() -> None:
    pairs = [
        (ThreadFacts(thread_id="T-a", status="active"), _closed()),
        (ThreadFacts(thread_id="T-b", status="parked"), _closed()),
        (
            ThreadFacts(
                thread_id="T-c",
                status="awaiting_reply",
                classification_events=(_TERMINAL + timedelta(minutes=1),),
            ),
            _closed(),
        ),
    ]
    report = build_report(pairs, margin_seconds=300)
    assert report.status_breakdown == {"active": 1, "parked": 1, "awaiting_reply": 1}
    assert report.liveness_limbs == {"awaiting_reply": 1}


def test_the_serialised_report_declares_that_nothing_was_written() -> None:
    payload = report_to_json(build_report(_quiescent(1), margin_seconds=300))
    assert payload["wrote_anything"] is False
    assert payload["phase"] == 0
    assert payload["margin_seconds"] == 300
    assert payload["go_threshold"] == GO_THRESHOLD
    assert payload["classifications"][0]["terminal_at"] == _TERMINAL.isoformat()
