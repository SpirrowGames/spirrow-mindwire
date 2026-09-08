"""Tests for the heartbeat module: accounting rule, state derivation,
freshness/expires_at contract, digest rendering, and the GitHub query pin.

Reference for every assertion below is Bohr msg-2692 (final resolution of both
BLOCKING findings in the T-stalled-pr-has-no-detector design thread) plus
Einstein msg-2693 (advisory: emit an absolute ``expires_at`` so the digest
carries no domain logic).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spirrow_mindwire.stall_ledger.heartbeat import (
    T_HEARTBEAT,
    FetchOutcome,
    HealthState,
    HeartbeatRecord,
    SourceReport,
    build_open_pr_query,
    derive_state,
    is_stale,
    render_digest_lines,
)

# --------------------------------------------------------------------------- #
# Accounting rule — msg-2692 §1: recognized + unrecognized == examined.
#
# This is the load-bearing defence against graceful-empty parsing failures.
# A parser that silently drops rows it does not understand would leave
# recognized + unrecognized < examined. Enforcing the invariant at construction
# means such a state can never enter the state table.
# --------------------------------------------------------------------------- #


class TestSourceReportShape:
    """PR-gate msg-2708 regression pin: ``observed_format_version`` lives on
    ``SourceReport`` itself, not in a parallel dict on ``HeartbeatRecord``.

    The prior draft split the version stamp off into ``HeartbeatRecord.
    observed_format_versions`` and forced ``__post_init__`` to run a
    synchronisation loop checking that every source name had a matching
    dict entry — the dual-management pattern rounds 1-5 kept reducing to.
    This test pins the shape so a future edit that re-splits them reds.
    """

    def test_observed_format_version_is_a_field_on_source_report(self) -> None:
        # dataclasses.fields is the loader-independent way to assert this.
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(SourceReport)}
        assert "observed_format_version" in field_names, (
            "SourceReport must carry observed_format_version directly. "
            "Splitting it off into a parallel dict on HeartbeatRecord "
            "reintroduces the dual-management pattern PR-gate msg-2708 "
            "removed."
        )

    def test_heartbeat_record_does_not_carry_observed_format_versions(self) -> None:
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(HeartbeatRecord)}
        assert "observed_format_versions" not in field_names, (
            "HeartbeatRecord must NOT carry observed_format_versions as a "
            "parallel dict. Per PR-gate msg-2708, the version stamp lives "
            "on SourceReport; re-splitting them here recreates the sync "
            "loop the round-6 refactor eliminated."
        )


class TestAccountingRule:
    def test_matches_examined_is_ok(self) -> None:
        SourceReport(
            name="github_prs",
            fetch_outcome=FetchOutcome.OK,
            examined=3,
            recognized=2,
            unrecognized=1,
            observed_format_version="v1",
        )  # no raise

    def test_undercount_raises(self) -> None:
        """recognized + unrecognized < examined means the parser silently
        dropped rows. This is the exact failure mode the rule exists to catch.
        """

        with pytest.raises(ValueError, match="accounting violation"):
            SourceReport(
                name="github_prs",
                fetch_outcome=FetchOutcome.OK,
                examined=5,
                recognized=2,
                unrecognized=1,  # 3 rows silently disappeared
                observed_format_version="v1",
            )

    def test_overcount_raises(self) -> None:
        """recognized + unrecognized > examined means the parser double-counted
        somewhere; also a bug, also caught here.
        """

        with pytest.raises(ValueError, match="accounting violation"):
            SourceReport(
                name="github_prs",
                fetch_outcome=FetchOutcome.OK,
                examined=3,
                recognized=2,
                unrecognized=2,
                observed_format_version="v1",
            )

    def test_negative_counts_rejected(self) -> None:
        """A negative count would silently make some sums look correct by
        cancellation. Reject before it reaches the state table.
        """

        with pytest.raises(ValueError, match="counts must be non-negative"):
            SourceReport(
                name="github_prs",
                fetch_outcome=FetchOutcome.OK,
                examined=-1,
                recognized=0,
                unrecognized=0,
                observed_format_version="v1",
            )


# --------------------------------------------------------------------------- #
# State derivation — msg-2692 §1 table:
#
#   any source is failing → ingest_failure
#   all sources ok, all examined == 0 → idle
#   otherwise → healthy
#
# Note: examined == 0 with fetch_outcome == ok is IDLE, not INGEST_FAILURE.
# That is Einstein msg-2691's blocking finding: a repository with no open
# PRs must not drive alarm fatigue into the digest.
# --------------------------------------------------------------------------- #


def _make_source(
    name: str = "src",
    outcome: FetchOutcome = FetchOutcome.OK,
    examined: int = 0,
    recognized: int = 0,
    unrecognized: int = 0,
    observed_format_version: str = "v1",
) -> SourceReport:
    return SourceReport(
        name=name,
        fetch_outcome=outcome,
        examined=examined,
        recognized=recognized,
        unrecognized=unrecognized,
        observed_format_version=observed_format_version,
    )


def _make_record(
    *sources: SourceReport,
    input_format_version: str = "v1",
    evaluated_at: datetime = datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
    last_valid_ingest_at: datetime | None = None,
    stalls: tuple[str, ...] = (),
) -> HeartbeatRecord:
    # msg-2708 refactor: observed_format_version lives on SourceReport now,
    # so ``_make_record`` no longer accepts a parallel ``observed`` dict.
    # Tests that want a version mismatch build the source with a mismatched
    # ``observed_format_version=`` directly.
    return HeartbeatRecord(
        evaluated_at=evaluated_at,
        input_format_version=input_format_version,
        sources=tuple(sources),
        last_valid_ingest_at=last_valid_ingest_at,
        stalls=stalls,
    )


class TestStateDerivation:
    def test_healthy_when_examined_and_all_recognized(self) -> None:
        rec = _make_record(_make_source("prs", FetchOutcome.OK, examined=3, recognized=3))
        assert derive_state(rec) == HealthState.HEALTHY

    def test_idle_when_all_sources_examined_zero(self) -> None:
        """Einstein msg-2691 blocking #1: idle repositories must NOT flip
        the state to ingest_failure — that would train the operator to
        ignore the digest.
        """

        rec = _make_record(
            _make_source("prs", FetchOutcome.OK, examined=0),
            _make_source("threads", FetchOutcome.OK, examined=0),
        )
        assert derive_state(rec) == HealthState.IDLE

    def test_ingest_failure_when_fetch_outcome_not_ok(self) -> None:
        rec = _make_record(_make_source("prs", FetchOutcome.HTTP_ERROR, examined=0))
        assert derive_state(rec) == HealthState.INGEST_FAILURE

    def test_ingest_failure_on_timeout(self) -> None:
        rec = _make_record(_make_source("prs", FetchOutcome.TIMEOUT, examined=0))
        assert derive_state(rec) == HealthState.INGEST_FAILURE

    def test_ingest_failure_on_auth(self) -> None:
        rec = _make_record(_make_source("prs", FetchOutcome.AUTH_FAILURE, examined=0))
        assert derive_state(rec) == HealthState.INGEST_FAILURE

    def test_ingest_failure_on_file_missing(self) -> None:
        rec = _make_record(_make_source("q", FetchOutcome.FILE_MISSING, examined=0))
        assert derive_state(rec) == HealthState.INGEST_FAILURE

    def test_ingest_failure_when_unrecognized_positive(self) -> None:
        """Even with fetch_outcome=ok, an unrecognized > 0 says the parser
        saw a row shape it does not understand. That IS drift, and it must
        flip the state loudly rather than be counted as a partial success.
        """

        rec = _make_record(
            _make_source("prs", FetchOutcome.OK, examined=3, recognized=2, unrecognized=1)
        )
        assert derive_state(rec) == HealthState.INGEST_FAILURE

    def test_ingest_failure_on_version_mismatch(self) -> None:
        """A source stamped a version we do not know how to parse. Flag
        rather than trust — even if the shape happens to match by accident.
        """

        rec = _make_record(
            _make_source(
                "prs",
                FetchOutcome.OK,
                examined=1,
                recognized=1,
                observed_format_version="v2",  # emitter says v2, we expect v1
            ),
            input_format_version="v1",
        )
        assert derive_state(rec) == HealthState.INGEST_FAILURE

    def test_failure_wins_over_healthy_in_mixed_sources(self) -> None:
        """A partial outage across multiple sources: two healthy, one 500.
        The 500 must not be masked by the two healthy ones — failure wins.
        """

        rec = _make_record(
            _make_source("prs", FetchOutcome.OK, examined=3, recognized=3),
            _make_source("threads", FetchOutcome.OK, examined=1, recognized=1),
            _make_source("q", FetchOutcome.HTTP_ERROR, examined=0),
        )
        assert derive_state(rec) == HealthState.INGEST_FAILURE

    def test_failure_wins_over_idle_in_mixed_sources(self) -> None:
        """Two sources returned empty; a third returned a failure. Idle
        must NOT swallow the failure just because the other two look quiet.
        """

        rec = _make_record(
            _make_source("prs", FetchOutcome.OK, examined=0),
            _make_source("threads", FetchOutcome.OK, examined=0),
            _make_source("q", FetchOutcome.PARSE_ERROR, examined=0),
        )
        assert derive_state(rec) == HealthState.INGEST_FAILURE


# --------------------------------------------------------------------------- #
# Empty-sources trap — PR-gate msg-2699 (Tier B on #237, round 3):
#
# ``all(...)`` over an empty iterable is vacuously True, so an empty sources
# tuple would silently slip through as ``idle`` if not refused. Round 4
# (msg-2702) retired the "defence in depth" branch and its corruption-based
# tests as YAGNI: the constructor guard is the single source of truth for
# this invariant. Guarding one invariant in two places was precisely the
# dual-management complexity round 2 fought to eliminate.
# --------------------------------------------------------------------------- #


class TestEmptySourcesTrap:
    def test_construction_rejects_empty_sources(self) -> None:
        """PR-gate msg-2699 regression pin: HeartbeatRecord.__post_init__
        MUST refuse to build a record with zero sources. The config bug
        gets surfaced at construction, not laundered into a false ``idle``.
        This is the ONE place the invariant is enforced (round-4 fix).
        """

        with pytest.raises(ValueError, match="sources tuple is empty"):
            HeartbeatRecord(
                evaluated_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
                input_format_version="v1",
                sources=(),
            )

    def test_build_classmethod_also_rejects_empty_sources(self) -> None:
        """The ``HeartbeatRecord.build()`` factory delegates to the raw
        constructor, so the same empty-sources rejection applies. This
        test is on the production entry point rather than on ``__init__``
        directly, so a future refactor of the classmethod that skipped
        the constructor would fail here.
        """

        with pytest.raises(ValueError, match="sources tuple is empty"):
            HeartbeatRecord.build(
                evaluated_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
                input_format_version="v1",
                sources=(),
                previous_last_valid_ingest_at=None,
            )


# --------------------------------------------------------------------------- #
# HeartbeatRecord.build() — msg-2692 §2 + PR-gate msg-2702 (Tier B round 4):
# the one-call factory that closes the circular-dependency seam. Takes the
# previous tick's last_valid_ingest_at (from cross-tick state) and computes
# the new value in the same call as the record's construction, so no caller
# can render a lagging timestamp.
# --------------------------------------------------------------------------- #


def _build_rec(
    *sources: SourceReport,
    previous_last_valid_ingest_at: datetime | None = None,
    evaluated_at: datetime = datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
    input_format_version: str = "v1",
    stalls: tuple[str, ...] = (),
) -> HeartbeatRecord:
    """Test helper: build a record through the production ``HeartbeatRecord.build()``
    factory. Uses the classmethod so tests exercise the same one-call path
    production code must go through.
    """

    return HeartbeatRecord.build(
        evaluated_at=evaluated_at,
        input_format_version=input_format_version,
        sources=tuple(sources),
        previous_last_valid_ingest_at=previous_last_valid_ingest_at,
        stalls=stalls,
    )


class TestBuildFactory:
    def test_healthy_advances_to_this_tick_evaluated_at(self) -> None:
        prev = datetime(2025, 1, 1, tzinfo=UTC)
        rec = _build_rec(
            _make_source("prs", FetchOutcome.OK, examined=1, recognized=1),
            previous_last_valid_ingest_at=prev,
        )
        assert rec.last_valid_ingest_at == rec.evaluated_at

    def test_idle_also_advances(self) -> None:
        """Einstein-blocking-#1: idle advances the heartbeat so an idle
        repository does not accumulate a false 'detector stale' alarm.
        """

        prev = datetime(2025, 1, 1, tzinfo=UTC)
        rec = _build_rec(
            _make_source("prs", FetchOutcome.OK, examined=0),
            previous_last_valid_ingest_at=prev,
        )
        assert rec.last_valid_ingest_at == rec.evaluated_at

    def test_ingest_failure_holds_at_previous(self) -> None:
        prev = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
        rec = _build_rec(
            _make_source("prs", FetchOutcome.TIMEOUT, examined=0),
            previous_last_valid_ingest_at=prev,
        )
        assert rec.last_valid_ingest_at == prev

    def test_ingest_failure_from_none_stays_none(self) -> None:
        rec = _build_rec(
            _make_source("prs", FetchOutcome.HTTP_ERROR, examined=0),
            previous_last_valid_ingest_at=None,
        )
        assert rec.last_valid_ingest_at is None

    def test_healthy_from_none_starts_the_heartbeat(self) -> None:
        rec = _build_rec(
            _make_source("prs", FetchOutcome.OK, examined=1, recognized=1),
            previous_last_valid_ingest_at=None,
        )
        assert rec.last_valid_ingest_at == rec.evaluated_at

    def test_healthy_tick_at_expiry_boundary_is_not_stale(self) -> None:
        """PR-gate msg-2702 BLOCKING regression pin — the exact scenario
        the naysayer named.

        A healthy tick runs 1 minute past the previous tick's expiry
        (previous_last_valid + T_HEARTBEAT). Under the OLD API the caller
        would construct with previous_last_valid, then compute advance,
        then forget to reconstruct — and ``is_stale(now, record)`` would
        return True because the record still carried the lagging
        timestamp. Under ``build()`` the record carries the NEW timestamp
        as of construction, so ``is_stale`` sees a fresh tick.

        The record's own ``expires_at`` idiom (``now > record.expires_at``)
        also sees fresh — a caller cannot get a wrong answer with either
        predicate.
        """

        prev = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
        # Previous expiry = 10:00 + 4h = 14:00. This tick runs at 14:01.
        now = datetime(2026, 9, 8, 14, 1, tzinfo=UTC)
        rec = _build_rec(
            _make_source("prs", FetchOutcome.OK, examined=1, recognized=1),
            previous_last_valid_ingest_at=prev,
            evaluated_at=now,
        )
        # (1) The freshly-built record carries the NEW last_valid = now.
        assert rec.last_valid_ingest_at == now
        # (2) Therefore is_stale returns False.
        assert not is_stale(now=now, record=rec)
        # (3) And the docstring-endorsed idiom now > record.expires_at
        #     also returns False. The property and is_stale agree.
        assert rec.expires_at is not None
        assert not (now > rec.expires_at)


# --------------------------------------------------------------------------- #
# Freshness / expires_at — msg-2693 advisory: digest carries no domain logic;
# it does now > expires_at, where expires_at is derived HERE.
# --------------------------------------------------------------------------- #


class TestExpiresAtAndStaleness:
    def test_expires_at_derives_from_last_valid_ingest_at(self) -> None:
        """PR-gate msg-2696 BLOCKING regression pin: ``expires_at`` MUST be
        computed from ``last_valid_ingest_at``, NOT from ``evaluated_at``.

        The whole point of the split between the two timestamps (msg-2692
        §2) is that ``evaluated_at`` advances every tick regardless of
        outcome, while ``last_valid_ingest_at`` holds during ingest failure.
        If ``expires_at`` reads from ``evaluated_at``, a persistent outage
        keeps pushing the expiry forward and ``now > expires_at`` never
        fires — the exact bug PR-gate #237 caught. The record's expiry is
        one wall-clock time, and its home is the timestamp that CAN stop
        advancing.
        """

        last_valid = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
        rec = _make_record(
            _make_source("prs", FetchOutcome.OK, examined=1, recognized=1),
            last_valid_ingest_at=last_valid,
        )
        assert rec.expires_at == last_valid + T_HEARTBEAT

    def test_expires_at_is_none_when_never_ingested(self) -> None:
        """No ingest has ever succeeded → ``expires_at`` is None, and the
        digest MUST treat that as always stale. Returning ``evaluated_at``
        (the old contract) let a caller mistake "the loop ran" for
        "an ingest succeeded"; ``None`` refuses that misreading.
        """

        rec = _make_record(_make_source("prs", FetchOutcome.OK, examined=0))
        assert rec.expires_at is None

    def test_expires_at_does_not_advance_during_persistent_ingest_failure(self) -> None:
        """PR-gate msg-2696 BLOCKING regression pin (the exact scenario the
        naysayer named): during a persistent outage the emitter still runs
        every tick, so ``evaluated_at`` moves forward; ``last_valid_ingest_at``
        holds; therefore ``expires_at`` holds. A caller doing
        ``now > record.expires_at`` sees the outage go stale on schedule,
        instead of the property forever running away from ``now``.
        """

        last_valid = datetime(2026, 9, 8, 6, 0, tzinfo=UTC)
        # Simulate a failing tick that ran 6 hours after last_valid — evaluated_at
        # has advanced, but the ingest failed so last_valid_ingest_at holds.
        failing_tick = _make_record(
            _make_source("prs", FetchOutcome.HTTP_ERROR, examined=0),
            evaluated_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
            last_valid_ingest_at=last_valid,
        )
        assert failing_tick.expires_at == last_valid + T_HEARTBEAT
        # And the digest predicate agrees: now (well past expiry) is stale.
        now = failing_tick.evaluated_at
        assert is_stale(now=now, record=failing_tick)

    def test_stale_when_last_valid_is_none(self) -> None:
        rec = _make_record(_make_source("prs", FetchOutcome.OK, examined=0))
        assert is_stale(now=rec.evaluated_at, record=rec)

    def test_not_stale_when_within_heartbeat(self) -> None:
        last_valid = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
        rec = _make_record(
            _make_source("prs", FetchOutcome.OK, examined=0),
            last_valid_ingest_at=last_valid,
        )
        # just before expiry
        now = last_valid + T_HEARTBEAT - timedelta(minutes=1)
        assert not is_stale(now=now, record=rec)

    def test_stale_at_expiry_boundary(self) -> None:
        last_valid = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
        rec = _make_record(
            _make_source("prs", FetchOutcome.OK, examined=0),
            last_valid_ingest_at=last_valid,
        )
        now = last_valid + T_HEARTBEAT + timedelta(seconds=1)
        assert is_stale(now=now, record=rec)

    def test_is_stale_reads_last_valid_ingest_at_from_the_record(self) -> None:
        """PR-gate msg-2696 ADVISORY regression pin: there is exactly ONE
        place ``last_valid_ingest_at`` lives — on the record — and every
        downstream predicate reads it from there. The out-of-band parameter
        the earlier draft passed is retired; that signature does not exist
        any more, and calling ``is_stale(now, record)`` is the only way.
        """

        # Same record shape, different `last_valid_ingest_at` values on the
        # record → different is_stale answers. Nothing else needs to change.
        rec_fresh = _make_record(
            _make_source("prs", FetchOutcome.OK, examined=1, recognized=1),
            last_valid_ingest_at=datetime(2026, 9, 8, 11, 0, tzinfo=UTC),
        )
        rec_stale = _make_record(
            _make_source("prs", FetchOutcome.OK, examined=1, recognized=1),
            last_valid_ingest_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
        )
        now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
        assert not is_stale(now=now, record=rec_fresh)
        assert is_stale(now=now, record=rec_stale)


# --------------------------------------------------------------------------- #
# Digest rendering — msg-2692 §4-3: state name is the verdict, examined
# breakdown is subordinate evidence. There must be NO reading of the line
# under which "0 stalls" can claim health while ingest is broken.
# --------------------------------------------------------------------------- #


class TestDigestRendering:
    def _rec_healthy(self, last_valid: datetime | None = None) -> HeartbeatRecord:
        """A HEALTHY record. Callers pass ``last_valid_ingest_at`` if they
        want to exercise a non-``never`` freshness reading; None yields the
        never-yet-succeeded case.
        """

        return _make_record(
            _make_source("prs", FetchOutcome.OK, examined=3, recognized=3),
            _make_source("threads", FetchOutcome.OK, examined=0),
            last_valid_ingest_at=last_valid,
        )

    def test_healthy_state_appears_as_verdict(self) -> None:
        rec = self._rec_healthy(last_valid=datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
        lines = render_digest_lines(record=rec, now=rec.evaluated_at)
        assert lines[0].startswith("detector: healthy")

    def test_idle_state_appears_as_verdict(self) -> None:
        rec = _make_record(
            _make_source("prs", FetchOutcome.OK, examined=0),
            last_valid_ingest_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        )
        lines = render_digest_lines(record=rec, now=rec.evaluated_at)
        assert lines[0].startswith("detector: idle")

    def test_ingest_failure_state_appears_as_verdict(self) -> None:
        # A previous ingest succeeded a moment ago so ``stale`` does not
        # shadow the declared state — the point of the test is that the
        # raw ingest_failure verdict is what surfaces.
        rec = _make_record(
            _make_source("prs", FetchOutcome.TIMEOUT, examined=0),
            last_valid_ingest_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        )
        lines = render_digest_lines(record=rec, now=rec.evaluated_at)
        assert lines[0].startswith("detector: ingest_failure")

    def test_first_ever_failure_renders_stale_over_declared(self) -> None:
        """When last_valid_ingest_at is None the digest MUST show ``stale``
        as the verdict — the record has never once been valid, so wall-clock
        freshness is the load-bearing signal and the declared state (which
        is a per-tick observation) is downgraded to a subordinate note.
        """

        rec = _make_record(_make_source("prs", FetchOutcome.TIMEOUT, examined=0))
        lines = render_digest_lines(record=rec, now=rec.evaluated_at)
        assert lines[0].startswith("detector: stale")
        assert "declared=ingest_failure" in lines[0]

    def test_stale_appears_when_wall_clock_says_so(self) -> None:
        """Digest computes staleness from wall-clock; the record's declared
        state is preserved in the header so an operator can tell "the
        detector died" (declared=healthy, stale=true) from "ingest is
        broken" (declared=ingest_failure).
        """

        last_valid = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
        rec = self._rec_healthy(last_valid=last_valid)
        now = last_valid + T_HEARTBEAT + timedelta(hours=1)
        lines = render_digest_lines(record=rec, now=now)
        assert lines[0].startswith("detector: stale")
        assert "declared=healthy" in lines[0]

    def test_evidence_lines_carry_counts(self) -> None:
        rec = self._rec_healthy(last_valid=datetime(2026, 9, 8, 12, 0, tzinfo=UTC))
        lines = render_digest_lines(record=rec, now=rec.evaluated_at)
        joined = "\n".join(lines)
        assert "prs: fetch=ok examined=3 recognized=3 unrecognized=0" in joined
        assert "threads: fetch=ok examined=0 recognized=0 unrecognized=0" in joined

    def test_verdict_line_does_not_carry_stall_count(self) -> None:
        """Regression pin for msg-2692 §1: the state name is the verdict.
        A prior draft rendered '0 stalls' at the verdict position; that is
        precisely the phrasing that gives the parser 'the right to lie'
        when the ingest is broken. The state must be first, counts must
        not appear on line 0.
        """

        rec = _make_record(
            _make_source("prs", FetchOutcome.OK, examined=0),
            last_valid_ingest_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        )
        lines = render_digest_lines(record=rec, now=rec.evaluated_at)
        assert "stall" not in lines[0].lower(), (
            "the verdict line must not carry a stall count — that grants the "
            "parser the right to print '0 stalls' when ingest is broken. Put "
            "the state name first; counts go on the subordinate evidence lines."
        )

    def test_never_ingested_is_shown_explicitly(self) -> None:
        rec = _make_record(_make_source("prs", FetchOutcome.HTTP_ERROR, examined=0))
        lines = render_digest_lines(record=rec, now=rec.evaluated_at)
        assert "last_valid_ingest_at=never" in lines[0]

    def test_version_drift_is_annotated_on_evidence(self) -> None:
        rec = _make_record(
            _make_source(
                "prs",
                FetchOutcome.OK,
                examined=1,
                recognized=1,
                observed_format_version="v2",  # msg-2708: on the source itself
            ),
            input_format_version="v1",
        )
        lines = render_digest_lines(record=rec, now=rec.evaluated_at)
        assert any("version_drift" in line for line in lines[1:])

    def test_stalls_subordinate_line_rendered_when_non_empty(self) -> None:
        """PR-gate msg-2705 BLOCKING regression pin: when ``stalls`` is
        non-empty the digest MUST render a subordinate evidence line
        naming them, and it must NOT appear on the verdict line (line 0).

        The line-0 exclusion is already pinned by
        ``test_verdict_line_does_not_carry_stall_count`` for the empty case;
        this test pins the non-empty case symmetrically. Without this the
        stalls-rendering branch of ``render_digest_lines`` was entirely
        untested — a rename, a bug in ``str.join``, or a swap of the count
        and the identifiers would go undetected.
        """

        rec = _make_record(
            _make_source("prs", FetchOutcome.OK, examined=2, recognized=2),
            last_valid_ingest_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
            stalls=("pr:spirrow-mindwire#199", "pr:spirrow-mindwire#206"),
        )
        lines = render_digest_lines(record=rec, now=rec.evaluated_at)
        # Verdict line still carries no stall count (msg-2692 §1 boundary).
        assert "stall" not in lines[0].lower()
        # A subordinate line names the stall count AND every stall id, so
        # a rename or a swap between the two would red this test.
        joined = "\n".join(lines[1:])
        assert "stalls: 2 —" in joined, (
            f"expected the subordinate stalls line to appear as "
            f"'stalls: <count> — <ids>'; got lines:\n{lines}"
        )
        assert "pr:spirrow-mindwire#199" in joined
        assert "pr:spirrow-mindwire#206" in joined

    def test_stalls_line_absent_when_empty(self) -> None:
        """Complement to the non-empty test: with no stalls, no subordinate
        stalls line is emitted (the verdict-line exclusion is separately
        pinned by ``test_verdict_line_does_not_carry_stall_count``).
        """

        rec = _make_record(
            _make_source("prs", FetchOutcome.OK, examined=3, recognized=3),
            last_valid_ingest_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
            stalls=(),
        )
        lines = render_digest_lines(record=rec, now=rec.evaluated_at)
        joined = "\n".join(lines)
        assert "stalls:" not in joined, (
            "an empty stalls tuple must not emit a 'stalls: 0 — ' line — "
            "emitting the label with a zero count would recreate the "
            "'right to lie' surface msg-2692 §1 removed."
        )


# --------------------------------------------------------------------------- #
# Query pin — msg-2692 §4-4: pin the exact bytes of our own GitHub PR query
# so a silent change to the retrieval path is caught in CI.
# --------------------------------------------------------------------------- #


class TestOpenPrQueryPin:
    def test_query_shape_is_pinned(self) -> None:
        """PIN: any change to this shape must be discussed in review, and if
        it lands, this snapshot changes in the same commit.

        The rule this test enforces: the heartbeat's live retrieval is
        `GET /repos/{owner}/{repo}/pulls?state=open&per_page=100`. Nothing
        else. `state=all`, an issue-search query, or a switch to graphql
        would each silently change what the live sweep sees vs. what the
        R-2b static fixtures were captured against — precisely the class
        of drift ADV-1's own reasoning (msg-2688) categorises as
        "trip-wire cheaper than fix".
        """

        q = build_open_pr_query(owner="SpirrowGames", repo="spirrow-mindwire")
        assert q == {
            "path": "/repos/SpirrowGames/spirrow-mindwire/pulls",
            "params": {"state": "open", "per_page": 100},
        }

    def test_per_page_is_overridable(self) -> None:
        """Callers can request a different page size (e.g. tests); default
        pinned above is what production uses.
        """

        q = build_open_pr_query(owner="o", repo="r", per_page=10)
        assert q["params"]["per_page"] == 10

    def test_state_is_open_not_all(self) -> None:
        """Anti-regression pin for Einstein msg-2691 blocking #2: the
        R-2c live-canary/replay-probe pattern was withdrawn precisely
        because it would have required this to become ``state=all`` (or a
        historical-only query), which the LIVE sweep never runs. If a
        future edit ever flips this to 'all', we are back to the probe
        that does not exercise the live retrieval path.
        """

        q = build_open_pr_query(owner="o", repo="r")
        assert q["params"]["state"] == "open"
