"""Refire predicate + budget + CON-1 lifecycle tests (Deliverable 4).

The head-sha budget is load-bearing (D-5'): a refire consumes it whether it
succeeded, failed, or lost its emitted ids. The tests pin all three so a future
edit that "skips id-lost when checking the budget" (making the SDK path retry
silently) fails loudly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from spirrow_mindwire.stall_ledger import (
    T_INFLIGHT,
    Class,
    RemedyState,
    StallRecord,
    Unit,
    UnitKind,
    should_refire,
)
from spirrow_mindwire.stall_ledger.refire import (
    budget_for_head_sha,
    expire_in_flight,
    finalize_attempt,
    prepare_attempt,
)


def _now() -> datetime:
    return datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _record(klass: str = Class.REFIREABLE.value) -> StallRecord:
    now = _now()
    return StallRecord(
        unit=Unit(kind=UnitKind.PR, identifier="spirrow-mindwire#206"),
        stall_epoch_start=now - timedelta(hours=1),
        condition_first_seen_at=now - timedelta(hours=1),
        klass=klass,
    )


class TestShouldRefirePolicy:
    def test_refireable_and_fresh_head_is_allowed(self) -> None:
        r = _record()
        v = should_refire(r, head_sha="deadbeef")
        assert v.allowed is True

    def test_wrong_class_is_not_allowed(self) -> None:
        r = _record(klass=Class.EXTERNALLY_BLOCKED.value)
        v = should_refire(r, head_sha="deadbeef")
        assert v.allowed is False
        assert "not 'refireable'" in v.reason

    def test_same_head_after_completed_attempt_is_denied(self) -> None:
        r = _record()
        prepare_attempt(r, _now() - timedelta(minutes=20), head_sha="deadbeef")
        finalize_attempt(
            r.remedy_attempts[-1],
            github_review_ids=("PRR_x",),
            outcome="no-state-change",
        )
        v = should_refire(r, head_sha="deadbeef")
        assert v.allowed is False

    def test_same_head_after_id_lost_attempt_is_still_denied(self) -> None:
        """msg-2474 §3: id-lost MUST NOT unlock the budget, otherwise the head-
        sha cap is defeated by silent side effects."""
        r = _record()
        prepare_attempt(r, _now() - timedelta(minutes=20), head_sha="deadbeef")
        finalize_attempt(r.remedy_attempts[-1])  # no ids captured → id-lost
        assert r.remedy_attempts[-1].state == RemedyState.ID_LOST
        v = should_refire(r, head_sha="deadbeef")
        assert v.allowed is False

    def test_different_head_reopens_budget(self) -> None:
        r = _record()
        prepare_attempt(r, _now() - timedelta(minutes=20), head_sha="oldsha")
        finalize_attempt(r.remedy_attempts[-1], github_review_ids=("PRR_x",))
        v = should_refire(r, head_sha="newsha")
        assert v.allowed is True

    def test_budget_check_scans_all_prior_attempts(self) -> None:
        """A record with multiple head-sha attempts must not have one head's
        budget accidentally released by a later head's success. Explicit test."""
        r = _record()
        for sha in ("a", "b", "c"):
            prepare_attempt(r, _now(), head_sha=sha)
            finalize_attempt(r.remedy_attempts[-1], github_review_ids=(f"PRR_{sha}",))
        assert budget_for_head_sha(r, "a").allowed is False
        assert budget_for_head_sha(r, "b").allowed is False
        assert budget_for_head_sha(r, "c").allowed is False
        assert budget_for_head_sha(r, "d").allowed is True


class TestPrepareAndFinalizeAttempt:
    def test_prepare_appends_in_flight(self) -> None:
        """CON-1: the attempt exists on disk BEFORE the remedy runs."""
        r = _record()
        a = prepare_attempt(r, _now(), head_sha="abc")
        assert a.state == RemedyState.IN_FLIGHT
        assert r.remedy_attempts[-1] is a
        assert a.emitted_event_ids.is_empty()

    def test_finalize_with_ids_transitions_to_completed(self) -> None:
        r = _record()
        a = prepare_attempt(r, _now(), head_sha="abc")
        finalize_attempt(a, github_review_ids=("PRR_x",), chatroom_msg_ids=("msg-1",))
        assert a.state == RemedyState.COMPLETED
        assert "PRR_x" in a.emitted_event_ids
        assert "msg-1" in a.emitted_event_ids

    def test_finalize_without_ids_transitions_to_id_lost(self) -> None:
        r = _record()
        a = prepare_attempt(r, _now(), head_sha="abc")
        finalize_attempt(a)
        assert a.state == RemedyState.ID_LOST


class TestInflightExpiry:
    """INV-4 + msg-2474 §3: a long-in-flight attempt must automatically fall to
    ``id-lost`` at ``a.at + T_INFLIGHT``, otherwise M-2's latch shape returns."""

    def test_within_window_does_not_expire(self) -> None:
        r = _record()
        a = prepare_attempt(r, _now(), head_sha="abc")
        assert expire_in_flight(a, _now() + T_INFLIGHT) is False
        assert a.state == RemedyState.IN_FLIGHT

    def test_past_window_transitions_to_id_lost(self) -> None:
        r = _record()
        a = prepare_attempt(r, _now(), head_sha="abc")
        assert expire_in_flight(a, _now() + T_INFLIGHT + timedelta(seconds=1)) is True
        assert a.state == RemedyState.ID_LOST
        assert "inflight-expired" in a.flags

    def test_completed_attempt_is_not_re_expired(self) -> None:
        """Idempotence: calling ``expire_in_flight`` on a completed attempt is a
        no-op — otherwise a scan storm would keep rewriting the flag."""
        r = _record()
        a = prepare_attempt(r, _now(), head_sha="abc")
        finalize_attempt(a, github_review_ids=("PRR_x",))
        assert expire_in_flight(a, _now() + T_INFLIGHT * 10) is False
        assert a.state == RemedyState.COMPLETED
