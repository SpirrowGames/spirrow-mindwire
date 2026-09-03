"""Tests for ``needs_actor`` (§3-4) and ``stalled`` (§3-1).

Both predicates are ORIGIN-BLIND and AUTHOR-BLIND (INV-2). The tests pin that
neither ``origin`` nor ``author`` enters the decision — they take only world state
and a clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spirrow_mindwire.stall_ledger import N_THRESHOLDS, UnitKind, stalled
from spirrow_mindwire.stall_ledger.predicates import (
    PrState,
    QuarantineState,
    ThreadState,
    needs_actor_pr,
    needs_actor_quarantine,
    needs_actor_thread,
)


class TestNeedsActorPr:
    """§3-4 PR row. Every branch is pinned to a scenario named in msg-2354."""

    def _base(self) -> PrState:
        return PrState(
            is_open=True,
            is_draft=False,
            has_verdict=True,
            verdict_is_request_changes=False,
            verdict_recorded_as_indefinite_input=False,
            merge_state_is_executable=True,
            is_approve_awaiting_human_merge=False,
        )

    def test_closed_pr_is_not_needs_actor(self) -> None:
        s = self._base()
        s = PrState(**{**s.__dict__, "is_open": False})
        assert needs_actor_pr(s) is False

    def test_draft_pr_is_not_needs_actor(self) -> None:
        s = self._base()
        s = PrState(**{**s.__dict__, "is_draft": True})
        assert needs_actor_pr(s) is False

    def test_approve_awaiting_human_merge_is_not_needs_actor(self) -> None:
        """§3-4 carve-out: coverage rule (§7) leaves the digest's human-parked
        section to report this row. Overlap would double-count."""
        s = self._base()
        s = PrState(**{**s.__dict__, "is_approve_awaiting_human_merge": True})
        assert needs_actor_pr(s) is False

    def test_no_verdict_is_needs_actor(self) -> None:
        s = self._base()
        s = PrState(**{**s.__dict__, "has_verdict": False})
        assert needs_actor_pr(s) is True

    def test_request_changes_is_needs_actor(self) -> None:
        s = self._base()
        s = PrState(**{**s.__dict__, "verdict_is_request_changes": True})
        assert needs_actor_pr(s) is True

    def test_indefinite_verdict_is_needs_actor(self) -> None:
        """msg-2354 §1 M-4 shape: ``ci=pending`` verdicts should still be
        re-fired. This is where the refireable class picks the row up."""
        s = self._base()
        s = PrState(**{**s.__dict__, "verdict_recorded_as_indefinite_input": True})
        assert needs_actor_pr(s) is True

    def test_conflicting_merge_state_is_needs_actor(self) -> None:
        """M-3 shape: PR #206/#209 with DIRTY merge state. Even a definitive
        verdict does not clear ``needs_actor`` here — the human must rebase."""
        s = self._base()
        s = PrState(**{**s.__dict__, "merge_state_is_executable": False})
        assert needs_actor_pr(s) is True


class TestNeedsActorThread:
    def test_resolved_thread_is_not_needs_actor(self) -> None:
        st = ThreadState(status="resolved", dormant_until_expired=True)
        assert needs_actor_thread(st) is False

    def test_dormant_thread_is_not_needs_actor(self) -> None:
        """A thread that has an active dormant-until pin is intentionally
        parked — that is participant motion, not silent stall."""
        assert needs_actor_thread(ThreadState(status="open", dormant_until_expired=False)) is False

    def test_open_and_not_dormant_is_needs_actor(self) -> None:
        assert needs_actor_thread(ThreadState(status="open", dormant_until_expired=True)) is True


class TestNeedsActorQuarantine:
    def test_present_is_needs_actor(self) -> None:
        assert needs_actor_quarantine(QuarantineState(is_present=True)) is True

    def test_absent_is_not_needs_actor(self) -> None:
        assert needs_actor_quarantine(QuarantineState(is_present=False)) is False


class TestStalledPredicate:
    """§3-1 ``stalled(unit) := needs_actor(unit) and (now - last_participant_motion_at) > N``.

    ``last_participant_motion_at`` is what INV-1' says it must be — advanced only
    by participant-origin events. The predicate itself does not verify that
    invariant (it can't; it's pure); the caller has to hand it a motion timestamp
    that respects INV-1'.
    """

    @pytest.fixture
    def now(self) -> datetime:
        return datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

    def test_not_needs_actor_is_not_stalled(self, now: datetime) -> None:
        """§3-1: ``needs_actor`` gates ``stalled``. A calmly-idle unit is not
        stalled, no matter how old."""
        assert (
            stalled(
                kind=UnitKind.PR,
                last_participant_motion_at=now - timedelta(days=30),
                now=now,
                needs_actor_now=False,
                n=N_THRESHOLDS["pr_with_verdict"],
            )
            is False
        )

    def test_needs_actor_within_n_is_not_stalled(self, now: datetime) -> None:
        assert (
            stalled(
                kind=UnitKind.PR,
                last_participant_motion_at=now
                - N_THRESHOLDS["pr_with_verdict"]
                + timedelta(minutes=1),
                now=now,
                needs_actor_now=True,
                n=N_THRESHOLDS["pr_with_verdict"],
            )
            is False
        )

    def test_needs_actor_past_n_is_stalled(self, now: datetime) -> None:
        assert (
            stalled(
                kind=UnitKind.PR,
                last_participant_motion_at=now
                - N_THRESHOLDS["pr_with_verdict"]
                - timedelta(minutes=1),
                now=now,
                needs_actor_now=True,
                n=N_THRESHOLDS["pr_with_verdict"],
            )
            is True
        )

    def test_quarantine_n_zero_makes_presence_immediately_stalled(self, now: datetime) -> None:
        """§3-5: quarantine's N is 0 — any entry is stalled the moment the scan
        sees it, so a fresh quarantine hits the ladder on the first pass."""
        assert (
            stalled(
                kind=UnitKind.QUARANTINE,
                last_participant_motion_at=now,
                now=now + timedelta(seconds=1),
                needs_actor_now=True,
                n=N_THRESHOLDS["quarantine"],
            )
            is True
        )

    def test_n_threshold_table_shape(self) -> None:
        """§3-5's four rows are all present."""
        assert set(N_THRESHOLDS) == {
            "pr_with_verdict",
            "thread_with_nomination",
            "quarantine",
            "other_thread",
        }
        assert N_THRESHOLDS["quarantine"] == timedelta(0)
        assert N_THRESHOLDS["thread_with_nomination"] < N_THRESHOLDS["pr_with_verdict"]
        assert N_THRESHOLDS["pr_with_verdict"] < N_THRESHOLDS["other_thread"]
