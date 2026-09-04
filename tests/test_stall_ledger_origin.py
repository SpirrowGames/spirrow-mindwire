"""Origin-predicate tests — the surface each of E-101..E-106 landed on.

Every objection in the design thread is pinned as a named test class here. The
tests are what makes the amendments *actually stick*: if a future edit removes the
skew margin or reverts the id-membership clause, the class named for the objection
fails, and the failure names the objection number in its docstring so the diff
reviewer sees the history at once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from spirrow_mindwire.stall_ledger import (
    T_SKEW,
    T_UNCERTAIN,
    EmittedEventIds,
    Event,
    OriginKind,
    RemedyAttempt,
    RemedyState,
    StallRecord,
    Unit,
    UnitKind,
    origin,
)


def _now() -> datetime:
    return datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _record() -> StallRecord:
    now = _now()
    return StallRecord(
        unit=Unit(kind=UnitKind.PR, identifier="spirrow-mindwire#206"),
        stall_epoch_start=now - timedelta(hours=1),
        condition_first_seen_at=now - timedelta(hours=1),
        klass="refireable",
    )


class TestClauseOneIdMembership:
    """msg-2472 §2-1 clause 1 — the id-based origin check. This is the surface
    E-103 fixed."""

    def test_matching_github_review_id_is_loop(self) -> None:
        r = _record()
        r.remedy_attempts.append(
            RemedyAttempt(
                at=_now() - timedelta(minutes=10),
                kind="gate-refire",
                head_sha="abc",
                state=RemedyState.COMPLETED,
                emitted_event_ids=EmittedEventIds(github_review_ids=("PRR_kwXXXX",)),
            )
        )
        e = Event(id="PRR_kwXXXX", at=_now())
        assert origin(e, r) is OriginKind.LOOP

    def test_matching_chatroom_msg_id_is_loop(self) -> None:
        r = _record()
        r.remedy_attempts.append(
            RemedyAttempt(
                at=_now() - timedelta(minutes=10),
                kind="gate-refire",
                head_sha="abc",
                state=RemedyState.COMPLETED,
                emitted_event_ids=EmittedEventIds(chatroom_msg_ids=("msg-2410",)),
            )
        )
        e = Event(id="msg-2410", at=_now())
        assert origin(e, r) is OriginKind.LOOP


class TestClauseTwoOriginUncertainWindow:
    """msg-2474 §2 clause 2 — the origin-uncertain window. This is the surface
    E-104 fixed by giving ``id-lost`` a window instead of falling through to the
    default."""

    def test_id_lost_event_within_window_is_loop(self) -> None:
        """E-104 regression: an ``id-lost`` remedy whose emitted events would fall
        through to ``participant`` under the DEFAULT rule must be caught by the
        window instead — otherwise clock advance re-opens age laundering."""
        r = _record()
        a_at = _now() - timedelta(minutes=10)
        r.remedy_attempts.append(
            RemedyAttempt(
                at=a_at,
                kind="gate-refire",
                head_sha="abc",
                state=RemedyState.ID_LOST,
            )
        )
        # An event emitted 5 minutes after ``a.at`` — inside T_UNCERTAIN.
        e = Event(id="PRR_unrecorded", at=a_at + timedelta(minutes=5))
        assert origin(e, r) is OriginKind.LOOP

    def test_in_flight_event_within_window_is_loop(self) -> None:
        r = _record()
        a_at = _now() - timedelta(minutes=5)
        r.remedy_attempts.append(
            RemedyAttempt(at=a_at, kind="gate-refire", head_sha="abc", state=RemedyState.IN_FLIGHT)
        )
        e = Event(id="PRR_side_effect", at=a_at + timedelta(minutes=2))
        assert origin(e, r) is OriginKind.LOOP

    def test_completed_attempt_does_not_open_a_window(self) -> None:
        """msg-2474 §2 mandates that ``COMPLETED`` closes the window. If the window
        stayed open past ``completed`` we would silence real participant motion."""
        r = _record()
        a_at = _now() - timedelta(minutes=5)
        r.remedy_attempts.append(
            RemedyAttempt(
                at=a_at,
                kind="gate-refire",
                head_sha="abc",
                state=RemedyState.COMPLETED,
                emitted_event_ids=EmittedEventIds(github_review_ids=("PRR_known",)),
            )
        )
        e = Event(id="PRR_unrelated_participant_review", at=a_at + timedelta(minutes=1))
        assert origin(e, r) is OriginKind.PARTICIPANT


class TestClockSkewOnLowerBound:
    """msg-2476 §1 — E-106's fix. The lower bound must absorb clock skew so a
    remote event whose ``event.at`` slightly precedes our local ``a.at`` (because
    our local clock is ahead) is still caught by the window."""

    def test_lower_bound_absorbs_skew(self) -> None:
        r = _record()
        a_at = _now()
        r.remedy_attempts.append(
            RemedyAttempt(at=a_at, kind="gate-refire", head_sha="abc", state=RemedyState.IN_FLIGHT)
        )
        # Remote event's clock is behind ours by ``T_SKEW - 1s``. This is INSIDE
        # the skew margin.
        e = Event(id="PRR_side_effect", at=a_at - T_SKEW + timedelta(seconds=1))
        assert origin(e, r) is OriginKind.LOOP

    def test_beyond_skew_lower_bound_falls_through(self) -> None:
        """The skew margin is a bound, not a hint — outside it the default rule
        (participant) applies. Otherwise the window would silently swallow every
        preceding participant event."""
        r = _record()
        a_at = _now()
        r.remedy_attempts.append(
            RemedyAttempt(at=a_at, kind="gate-refire", head_sha="abc", state=RemedyState.IN_FLIGHT)
        )
        e = Event(id="PRR_earlier", at=a_at - T_SKEW - timedelta(seconds=1))
        assert origin(e, r) is OriginKind.PARTICIPANT

    def test_upper_bound_does_not_widen_with_skew(self) -> None:
        """msg-2476 §1 is explicit that the skew margin applies only to the
        LOWER bound. Widening the upper bound would catch participant motion after
        the remedy's physical window is over — the quiet failure direction."""
        r = _record()
        a_at = _now()
        r.remedy_attempts.append(
            RemedyAttempt(at=a_at, kind="gate-refire", head_sha="abc", state=RemedyState.IN_FLIGHT)
        )
        e = Event(id="PRR_late_participant", at=a_at + T_UNCERTAIN + timedelta(seconds=1))
        assert origin(e, r) is OriginKind.PARTICIPANT


class TestDefaultParticipant:
    """msg-2472 §2-3: the default is ``participant``. This is the failure-direction
    reversal E-103 gave us: the writer of ``emitted_event_ids`` and the reader of
    it are the same process, so completeness is self-guaranteed, and an event we
    do not recognise IS a participant event."""

    def test_no_remedy_no_window_no_match_is_participant(self) -> None:
        r = _record()
        e = Event(id="PRR_first_review_ever", at=_now())
        assert origin(e, r) is OriginKind.PARTICIPANT

    def test_same_author_different_events_are_separated_by_id(self) -> None:
        """msg-2472 §2-5's table row #1 vs. row #3: the same identity's design
        review (participant) and the gate-refire's review (loop) must be
        distinguished. Under the id-based rule they are."""
        r = _record()
        a_at = _now() - timedelta(minutes=15)
        r.remedy_attempts.append(
            RemedyAttempt(
                at=a_at,
                kind="gate-refire",
                head_sha="abc",
                state=RemedyState.COMPLETED,
                emitted_event_ids=EmittedEventIds(github_review_ids=("PRR_refire",)),
            )
        )
        # Same identity (in real life), different id: the design review the
        # participant just posted.
        design = Event(id="PRR_design_review", at=_now())
        refire = Event(id="PRR_refire", at=a_at + timedelta(seconds=30))
        assert origin(design, r) is OriginKind.PARTICIPANT
        assert origin(refire, r) is OriginKind.LOOP


class TestPerUnitScoping:
    """§3-3's origin function takes a record because the loop-vs-participant
    judgement is scoped to a unit. The SAME event should classify differently
    against different records — one that emitted it, and one that did not."""

    def test_event_is_loop_for_record_that_emitted_it_participant_for_others(
        self,
    ) -> None:
        r_own = _record()
        r_own.remedy_attempts.append(
            RemedyAttempt(
                at=_now() - timedelta(minutes=1),
                kind="gate-refire",
                head_sha="abc",
                state=RemedyState.COMPLETED,
                emitted_event_ids=EmittedEventIds(github_review_ids=("PRR_x",)),
            )
        )
        r_other = _record()
        e = Event(id="PRR_x", at=_now())
        assert origin(e, r_own) is OriginKind.LOOP
        assert origin(e, r_other) is OriginKind.PARTICIPANT
