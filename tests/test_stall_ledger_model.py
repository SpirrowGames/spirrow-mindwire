"""Model-shape tests for the stall_ledger — the invariants that must hold on the
record type ALONE, before any predicate or classifier is called.

These are the constants and shape-tests that would silently drift if the module were
refactored without them. Every constant name that the spec messages call out by name
(``T_INFLIGHT`` / ``T_UNCERTAIN`` / ``T_SKEW``) is pinned here; the invariant
``T_INFLIGHT == T_UNCERTAIN`` from msg-2474 §2 (single physical delay, two clocks) is
pinned as a separate assertion so a future edit that splits them for local reasons is
caught by a failing test rather than by silent drift.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spirrow_mindwire.stall_ledger import (
    T_INFLIGHT,
    T_SKEW,
    T_UNCERTAIN,
    EmittedEventIds,
    RemedyAttempt,
    RemedyState,
    StallRecord,
    Unit,
    UnitKind,
)


class TestConstants:
    def test_t_inflight_equals_t_uncertain(self) -> None:
        """msg-2474 §2: 'same physical delay ⇒ same value'. Splitting them silently
        would let a caller widen only one and re-open E-104."""
        assert T_INFLIGHT == T_UNCERTAIN

    def test_t_inflight_is_thirty_minutes(self) -> None:
        """msg-2474 §2 pins 30 minutes as the initial value."""
        assert timedelta(minutes=30) == T_INFLIGHT

    def test_t_skew_is_five_minutes(self) -> None:
        """msg-2476 §1 pins 5 minutes."""
        assert timedelta(minutes=5) == T_SKEW

    def test_t_skew_smaller_than_t_uncertain(self) -> None:
        """A skew larger than the uncertain window would let the lower bound reach
        events the upper bound would then miss, breaking the window's semantic."""
        assert T_SKEW < T_UNCERTAIN


class TestUnitKey:
    """§3-2: unit keys are ``<kind>:<identifier>`` — required by the coverage rule
    (§7) which enumerates records by key."""

    def test_pr_key(self) -> None:
        u = Unit(kind=UnitKind.PR, identifier="spirrow-mindwire#206")
        assert u.key == "pr:spirrow-mindwire#206"

    def test_thread_key(self) -> None:
        u = Unit(kind=UnitKind.THREAD, identifier="spirrow-mindwire/T-foo")
        assert u.key == "thread:spirrow-mindwire/T-foo"

    def test_quarantine_key(self) -> None:
        u = Unit(kind=UnitKind.QUARANTINE, identifier="spirrow-voxelworld/T-bar")
        assert u.key == "quar:spirrow-voxelworld/T-bar" or u.key.startswith(
            f"{UnitKind.QUARANTINE.value}:"
        )


class TestEmittedEventIdsMembership:
    """msg-2472 §2-1: ``event.id in union(remedy_attempts[*].emitted_event_ids)`` is
    the ORIGIN predicate's first clause. Membership has to work across both fields,
    or the origin check misses whichever surface it does not look at."""

    def test_matches_github_review_id(self) -> None:
        e = EmittedEventIds(github_review_ids=("PRR_kwXXXX",), chatroom_msg_ids=())
        assert "PRR_kwXXXX" in e

    def test_matches_chatroom_msg_id(self) -> None:
        e = EmittedEventIds(github_review_ids=(), chatroom_msg_ids=("msg-2410",))
        assert "msg-2410" in e

    def test_membership_is_disjunctive(self) -> None:
        """Both surfaces at once — one remedy tick emits BOTH (msg-2472 §2-1)."""
        e = EmittedEventIds(github_review_ids=("PRR_1",), chatroom_msg_ids=("msg-1",))
        assert "PRR_1" in e
        assert "msg-1" in e
        assert "PRR_2" not in e

    def test_empty_ids_are_empty(self) -> None:
        assert EmittedEventIds().is_empty()
        assert not EmittedEventIds(github_review_ids=("x",)).is_empty()


class TestRemedyAttemptWindow:
    """msg-2474 §2 + msg-2476 §1: an in-flight or id-lost attempt keeps its origin-
    uncertain window open UP TO ``a.at + T_UNCERTAIN``. A completed attempt has its
    ids known, so the window is closed and only clause 1 (id membership) matters."""

    @pytest.fixture
    def now(self) -> datetime:
        return datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

    def _attempt(self, at: datetime, state: RemedyState) -> RemedyAttempt:
        return RemedyAttempt(at=at, kind="gate-refire", head_sha="abc123", state=state)

    def test_in_flight_open_at_boundary(self, now: datetime) -> None:
        a = self._attempt(now - T_UNCERTAIN, RemedyState.IN_FLIGHT)
        assert a.is_open_window(now) is True

    def test_in_flight_closed_past_boundary(self, now: datetime) -> None:
        a = self._attempt(now - T_UNCERTAIN - timedelta(seconds=1), RemedyState.IN_FLIGHT)
        assert a.is_open_window(now) is False

    def test_id_lost_still_uses_window(self, now: datetime) -> None:
        """msg-2474 §2 is explicit: ``id-lost`` keeps the window open — that is
        precisely what makes ``id-lost`` safe."""
        a = self._attempt(now - timedelta(minutes=10), RemedyState.ID_LOST)
        assert a.is_open_window(now) is True

    def test_completed_never_uses_window(self, now: datetime) -> None:
        """Completed attempts have their ids captured, so clause 1 answers alone."""
        a = self._attempt(now, RemedyState.COMPLETED)
        assert a.is_open_window(now) is False


class TestStallRecordIdentity:
    """msg-2470 §2-2: identity is ``(unit, stall_epoch_start)``. Neither ``klass``
    nor any remedy attempt is part of identity, so class changes and remedy retries
    do NOT change the record id."""

    def test_record_id_uses_epoch_start(self) -> None:
        epoch = datetime(2026, 8, 30, 16, 51, tzinfo=UTC)
        r = StallRecord(
            unit=Unit(kind=UnitKind.PR, identifier="spirrow-mindwire#206"),
            stall_epoch_start=epoch,
            condition_first_seen_at=epoch,
            klass="refireable",
        )
        assert epoch.isoformat() in r.record_id
        assert "pr:spirrow-mindwire#206" in r.record_id

    def test_age_does_not_shrink_when_remedy_appended(self) -> None:
        """INV-3: adding a remedy attempt does NOT rejuvenate the record. Age is
        computed from ``stall_epoch_start``, which is fixed at construction."""
        epoch = datetime(2026, 8, 30, 16, 51, tzinfo=UTC)
        r = StallRecord(
            unit=Unit(kind=UnitKind.PR, identifier="spirrow-mindwire#206"),
            stall_epoch_start=epoch,
            condition_first_seen_at=epoch,
            klass="refireable",
        )
        now = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
        age_before = r.age(now)

        r.remedy_attempts.append(RemedyAttempt(at=now, kind="gate-refire", head_sha="deadbeef"))
        assert r.age(now) == age_before, "INV-3: remedy append must not shrink age"
