"""Tests for :mod:`spirrow_mindwire.gate_admission`.

Structure follows :mod:`tests.test_routing`'s three-part shape: behaviour
tests for the admission table (R1-R7), signature / structural pins for the
invariants that live in the function's shape rather than its body, and a
small property suite for :func:`ci_clock_start`'s total-function guarantee.

Two invariants this suite pins directly:

1. **INV-CI-2 (改), design §B-2** — :func:`gate_admission` has no ``verdict``
   parameter, so reading the PR-gate verdict content is structurally out of
   reach. A dedicated signature test fires the moment anyone adds a
   ``verdict``-shaped parameter (or a positional slot that could carry one).
   The signature is the enforcement; the test is the early warning.

2. **Total-function :func:`ci_clock_start`, design §A-4 property #1** — every
   combination of ``started_at`` / ``created_at`` nulls (including an empty
   rollup) must return a :class:`ClockStart` without raising. The property
   test enumerates the 3^2 combinations (plus the empty rollup) for a
   single-row rollup, so the shape is checked, not sampled.

What this suite deliberately does NOT try to pin: the wiring of the
predicate into the conductor's ``NEXT: pr-review <ref>`` handler. That
wiring depends on a rollup-fetching client method (per-check timestamps +
status) that this repository does not yet expose from
:class:`~spirrow_mindwire.github.client.GitHubClient`. The pure predicate
is the port both the current conductor and the future operator will call
against; the adapter that populates :class:`CheckRow` is out of scope for
this PR (T-operator-board msg-2568 §A — landing the primitive first).
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from itertools import product

import pytest

from spirrow_mindwire.gate_admission import (
    CAP_CHECK,
    CAP_NOCLOCK,
    COMPLETED,
    RED_CONCLUSIONS,
    AdmissionResult,
    CheckRow,
    Clock,
    ClockStart,
    GateAdmission,
    ci_clock_start,
    gate_admission,
)

# --------------------------------------------------------------------------- #
# Fixtures / small builders.
# --------------------------------------------------------------------------- #

# A reference "now" used by every behaviour test that needs to talk about
# durations. All rollup timestamps are expressed relative to this value so
# the assertions read the way the design table does ("6 h ago", "8 h ago").
NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
# The commit's committed date, used only as the COMMIT-clock fallback. Chosen
# well before NOW so the fallback path is testable without accidentally
# straying into the CAP boundary of the CHECK path.
HEAD_COMMITTED = NOW - timedelta(hours=1)
HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def _running_check(
    *,
    name: str = "gate",
    started_at: datetime | None = None,
    created_at: datetime | None = None,
) -> CheckRow:
    """A rollup row that has not yet concluded (``status='in_progress'``).

    ``started_at`` / ``created_at`` default to ``None`` so a test that only
    cares about the completion shape (concluded / red) doesn't have to pass
    unused timestamps.
    """
    return CheckRow(
        name=name,
        status="in_progress",
        conclusion=None,
        started_at=started_at,
        created_at=created_at,
    )


def _queued_check(name: str = "gate") -> CheckRow:
    """A rollup row that has been queued but not started (both timestamps null).

    Reproduces the GitHub Actions shape that would have crashed the v0.3
    ``min(startedAt)`` reference implementation (design §A-1 BLOCKING).
    """
    return CheckRow(name=name, status="queued", conclusion=None, started_at=None, created_at=None)


def _completed_check(
    *,
    name: str = "gate",
    conclusion: str | None = "success",
    started_at: datetime | None = None,
    created_at: datetime | None = None,
) -> CheckRow:
    return CheckRow(
        name=name,
        status=COMPLETED,
        conclusion=conclusion,
        started_at=started_at,
        created_at=created_at,
    )


def _admit(
    *,
    rollup: list[CheckRow],
    head: str = HEAD,
    now: datetime = NOW,
    nomination_is_self: bool = True,
    verdict_heads: frozenset[str] = frozenset(),
    ci_red_routed_heads: frozenset[str] = frozenset(),
    head_committed: datetime = HEAD_COMMITTED,
) -> AdmissionResult:
    """Call :func:`gate_admission` with named defaults so tests read at the site."""
    return gate_admission(
        rollup=rollup,
        head_sha=head,
        head_committed_date=head_committed,
        now=now,
        nomination_is_self=nomination_is_self,
        verdict_heads=verdict_heads,
        ci_red_routed_heads=ci_red_routed_heads,
    )


# --------------------------------------------------------------------------- #
# R1 — empty rollup ⇒ INVOKE (no CI configured for this PR).
# --------------------------------------------------------------------------- #


def test_r1_empty_rollup_invokes() -> None:
    result = _admit(rollup=[])
    assert result.admission is GateAdmission.INVOKE
    assert result.rule == "R1"


# --------------------------------------------------------------------------- #
# R2 — CI incomplete under the CAP ⇒ DEFER (no model invocation).
# --------------------------------------------------------------------------- #


def test_r2_defers_within_check_cap() -> None:
    # 1 h into a 6 h budget: clearly DEFER.
    started = NOW - timedelta(hours=1)
    result = _admit(rollup=[_running_check(started_at=started)])
    assert result.admission is GateAdmission.DEFER
    assert result.rule == "R2"
    assert "clock=check" in result.reason


def test_r2_defers_at_check_cap_boundary() -> None:
    # Exactly at the CAP: the design table uses ``≤`` so the boundary DEFERs.
    # Off-by-one guard: the human-terminal escalation should not fire early.
    started = NOW - CAP_CHECK
    result = _admit(rollup=[_running_check(started_at=started)])
    assert result.admission is GateAdmission.DEFER
    assert result.rule == "R2"


def test_r2_defers_with_all_null_timestamps_using_commit_clock() -> None:
    # The exact rollup shape the v0.3 crash-case cited (design §A-4 property
    # test #2): every check is queued with null timestamps, and the wait
    # budget must come from the head commit's committedDate — no
    # ``min(None, None)`` anywhere in the code path.
    result = _admit(rollup=[_queued_check(), _queued_check("other")])
    assert result.admission is GateAdmission.DEFER
    assert result.rule == "R2"
    # The reason names the commit clock so operators can tell "fallback path"
    # from "check-clock path" at the log line.
    assert "clock=commit" in result.reason


# --------------------------------------------------------------------------- #
# R3 — CI incomplete past the CAP ⇒ ROUTE_HUMAN (real escalation).
# --------------------------------------------------------------------------- #


def test_r3_escalates_past_check_cap() -> None:
    # Just past the 6 h cap: this is the case where "we waited a working
    # day for CI and it never finished" fires. The escalation is the
    # right answer here.
    started = NOW - (CAP_CHECK + timedelta(seconds=1))
    result = _admit(rollup=[_running_check(started_at=started)])
    assert result.admission is GateAdmission.ROUTE_HUMAN
    assert result.rule == "R3"
    # The reason must name the check(s) the operator will inspect, and which
    # clock decided the escalation. Design §A-4 property test #4.
    assert "clock=check" in result.reason
    assert "gate(in_progress)" in result.reason


def test_r3_escalates_past_commit_cap_with_clock_commit_in_reason() -> None:
    # Design §A-4 property test #4: when the commit clock is used, the
    # reason string names ``clock=commit`` explicitly so an operator can
    # distinguish a real stall from a false-early on an old commit.
    old_committed = NOW - (CAP_NOCLOCK + timedelta(hours=1))
    result = _admit(rollup=[_queued_check()], head_committed=old_committed)
    assert result.admission is GateAdmission.ROUTE_HUMAN
    assert result.rule == "R3"
    assert "clock=commit" in result.reason
    assert "gate(queued)" in result.reason


def test_r3_check_cap_much_shorter_than_commit_cap() -> None:
    # If we're 8 h in with a check timestamp available, CHECK's 6 h cap fires.
    # A commit clock in the same window would still DEFER (12 h cap). The two
    # caps must not blur.
    started = NOW - timedelta(hours=8)
    result = _admit(rollup=[_running_check(started_at=started)])
    assert result.admission is GateAdmission.ROUTE_HUMAN
    assert result.rule == "R3"


def test_r2_commit_cap_defers_where_check_cap_would_escalate() -> None:
    # Symmetric to the above: an 8 h wait on the commit clock is still under
    # CAP_NOCLOCK. This locks the cap-per-clock split in place.
    old_committed = NOW - timedelta(hours=8)
    result = _admit(rollup=[_queued_check()], head_committed=old_committed)
    assert result.admission is GateAdmission.DEFER
    assert result.rule == "R2"


# --------------------------------------------------------------------------- #
# R4 / R5 — red rollup routing.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("conclusion", sorted(RED_CONCLUSIONS))
def test_r4_routes_to_implementer_on_any_red_conclusion(conclusion: str) -> None:
    # Every conclusion in RED_CONCLUSIONS must trigger R4 uniformly — the
    # naysayer's own gate policy already treats these as failures, and R4
    # matches it. This is the "we agree with the gate about what red means"
    # cross-reference.
    result = _admit(rollup=[_completed_check(conclusion=conclusion)])
    assert result.admission is GateAdmission.ROUTE_IMPLEMENTER
    assert result.rule == "R4"


def test_r5_escalates_to_human_on_repeated_red_same_head() -> None:
    # The loop-safety escalation: this head has already been routed to the
    # implementer for a CI-red fix, but the CI came back red again with no
    # new push. Dispatching the implementer a second time is exactly the
    # loop the marker was added to stop.
    result = _admit(
        rollup=[_completed_check(conclusion="failure")],
        ci_red_routed_heads=frozenset({HEAD}),
    )
    assert result.admission is GateAdmission.ROUTE_HUMAN
    assert result.rule == "R5"


def test_r4_fires_when_red_head_marker_is_on_a_different_head() -> None:
    # A ci-route marker from a previous head must NOT bleed forward: only
    # the current head_sha counts. Otherwise a fixed-and-force-pushed PR
    # would jump straight to R5 on its first red.
    result = _admit(
        rollup=[_completed_check(conclusion="failure")],
        ci_red_routed_heads=frozenset({OTHER_HEAD}),
    )
    assert result.admission is GateAdmission.ROUTE_IMPLEMENTER
    assert result.rule == "R4"


# --------------------------------------------------------------------------- #
# R6 / R7 — green rollup routing.
# --------------------------------------------------------------------------- #


def test_r6_dedupes_a_self_nomination_on_an_already_reviewed_head() -> None:
    # The DEFER-wake-up path: our own pr-gate-relay author posted the
    # ``NEXT: pr-review`` and the gate has already produced a verdict on
    # this head. INV-CI-1 forbids re-invoking the model.
    result = _admit(
        rollup=[_completed_check(conclusion="success")],
        nomination_is_self=True,
        verdict_heads=frozenset({HEAD}),
    )
    assert result.admission is GateAdmission.ALREADY_REVIEWED
    assert result.rule == "R6"


def test_r7_manual_operator_override_bypasses_r6_dedup() -> None:
    # An operator's hand-typed ``NEXT: pr-review`` (``nomination_is_self=False``)
    # is the documented override: a re-invocation on the same reviewed head
    # is a legitimate user demand and must NOT be deduped. Preserves the
    # msg-2550 / msg-2556 / msg-2562 manual-fire escape hatch after wiring.
    result = _admit(
        rollup=[_completed_check(conclusion="success")],
        nomination_is_self=False,
        verdict_heads=frozenset({HEAD}),
    )
    assert result.admission is GateAdmission.INVOKE
    assert result.rule == "R7"


def test_r7_invokes_when_head_not_in_verdict_heads() -> None:
    # First green fetch on this head, self-nominated or not: gate fires.
    result = _admit(
        rollup=[_completed_check(conclusion="success")], verdict_heads=frozenset({OTHER_HEAD})
    )
    assert result.admission is GateAdmission.INVOKE
    assert result.rule == "R7"


def test_r7_treats_neutral_and_skipped_as_green() -> None:
    # These conclusions are green in the naysayer's own gate policy
    # (_CI_OK_CONCLUSIONS); admission must not diverge from that or the
    # gate would fire on a rollup the naysayer will then judge as green
    # and vice versa.
    for conclusion in ("neutral", "skipped"):
        result = _admit(rollup=[_completed_check(conclusion=conclusion)])
        assert result.admission is GateAdmission.INVOKE, conclusion
        assert result.rule == "R7"


def test_r7_conclusion_none_on_a_completed_row_is_not_red() -> None:
    # A completed row with a null conclusion is a race shape that must not
    # be silently treated as red — the naysayer's gate returns UNKNOWN in
    # this case; here we prefer INVOKE and let the naysayer's fail-closed
    # CI-gate short-circuit make the actual call. Locking this behaviour
    # so a future "None means failure" refactor is visible.
    result = _admit(rollup=[_completed_check(conclusion=None)])
    assert result.admission is GateAdmission.INVOKE
    assert result.rule == "R7"


# --------------------------------------------------------------------------- #
# Design §A-4 property test #3: completion / colour judgements never read
# a timestamp. Setting every clock field to null must not flip either judgement.
# --------------------------------------------------------------------------- #


def test_a4_property_3_null_timestamps_do_not_change_completion_or_colour() -> None:
    started = NOW - timedelta(hours=2)
    # A green concluded rollup — should stay INVOKE regardless of timestamps.
    with_ts = [_completed_check(conclusion="success", started_at=started)]
    without_ts = [_completed_check(conclusion="success", started_at=None, created_at=None)]
    assert _admit(rollup=with_ts).rule == "R7"
    assert _admit(rollup=without_ts).rule == "R7"
    # A red concluded rollup — should stay R4 regardless of timestamps.
    red_with = [_completed_check(conclusion="failure", started_at=started)]
    red_without = [_completed_check(conclusion="failure", started_at=None, created_at=None)]
    assert _admit(rollup=red_with).rule == "R4"
    assert _admit(rollup=red_without).rule == "R4"


# --------------------------------------------------------------------------- #
# Design §A-4 property test #1 — :func:`ci_clock_start` is total across every
# combination of null timestamps (including the empty rollup). The 3² grid
# for a single-row rollup + the empty case = 10 shapes, all must return a
# ClockStart without raising.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("started", "created"),
    list(product([None, NOW - timedelta(hours=2), NOW - timedelta(hours=3)], repeat=2)),
)
def test_a4_property_1_ci_clock_start_is_total_for_single_row(
    started: datetime | None, created: datetime | None
) -> None:
    row = _running_check(started_at=started, created_at=created)
    result = ci_clock_start([row], HEAD_COMMITTED)
    assert isinstance(result, ClockStart)
    # The row contributes iff at least one of its timestamps is non-null;
    # otherwise we must be on the commit clock (never a raise, never a
    # sentinel like ``None``).
    if started is not None or created is not None:
        assert result.source is Clock.CHECK
        assert result.at == (started if started is not None else created)
    else:
        assert result.source is Clock.COMMIT
        assert result.at == HEAD_COMMITTED


def test_a4_property_1_ci_clock_start_is_total_for_empty_rollup() -> None:
    # The empty-rollup case is not otherwise reached by :func:`gate_admission`
    # (R1 handles it), but :func:`ci_clock_start` is exposed to callers and
    # must be total there too.
    result = ci_clock_start([], HEAD_COMMITTED)
    assert result.source is Clock.COMMIT
    assert result.at == HEAD_COMMITTED


def test_ci_clock_start_prefers_started_at_over_created_at() -> None:
    # Documented precedence in :func:`ci_clock_start` — ``started_at`` wins
    # when both are present; ``created_at`` is only the fallback per row.
    started = NOW - timedelta(hours=1)
    created = NOW - timedelta(hours=5)
    rollup = [_running_check(started_at=started, created_at=created)]
    result = ci_clock_start(rollup, HEAD_COMMITTED)
    assert result.source is Clock.CHECK
    assert result.at == started


def test_ci_clock_start_takes_min_of_available_stamps() -> None:
    # Locks the ``min`` selection: as more checks join, the clock can only
    # move *earlier*, so the CAP can never mis-fire *late* on a rollup
    # that's silently growing.
    earlier = NOW - timedelta(hours=3)
    later = NOW - timedelta(hours=1)
    rollup = [
        _running_check(name="a", started_at=later),
        _running_check(name="b", started_at=earlier),
    ]
    result = ci_clock_start(rollup, HEAD_COMMITTED)
    assert result.at == earlier


def test_ci_clock_start_ignores_all_null_rows_but_uses_others() -> None:
    # Mixed shape: one row has a stamp, another is fully null. The null row
    # contributes nothing and the CHECK clock still fires on the row that
    # DID have a stamp — the fallback triggers only when *no* row contributes.
    started = NOW - timedelta(hours=2)
    rollup = [
        _running_check(name="observed", started_at=started),
        _queued_check(name="null-row"),
    ]
    result = ci_clock_start(rollup, HEAD_COMMITTED)
    assert result.source is Clock.CHECK
    assert result.at == started


# --------------------------------------------------------------------------- #
# INV-CI-2 (改) — signature-level enforcement of "no verdict input".
#
# The invariant lives in the function's parameter list, not in its body:
# reading the PR-gate verdict CONTENT is structurally impossible here
# because there is no verdict to read. This test fires the moment anyone
# adds a ``verdict``-shaped parameter — the earliest, cheapest surface
# for the drift the design says must not silently return.
# --------------------------------------------------------------------------- #


def test_inv_ci_2_gate_admission_signature_has_no_verdict_input() -> None:
    sig = inspect.signature(gate_admission)
    names = set(sig.parameters)
    # An exact-name check plus a substring guard: "verdict" alone, and any
    # parameter whose name embeds "verdict" (e.g. ``verdict_content``,
    # ``pr_verdict``) — the latter would still be a re-expression of
    # carve-out ②'s input.
    assert "verdict" not in names, (
        "gate_admission must not take a `verdict` parameter (INV-CI-2 改, "
        "design §B-2): carve-out ② is decided BEFORE the gate is invoked, "
        "and this function's admission never depends on the verdict content"
    )
    verdictish = {n for n in names if "verdict" in n and n != "verdict_heads"}
    assert verdictish == set(), (
        f"gate_admission must not take any verdict-content-shaped parameter; "
        f"got {sorted(verdictish)}. `verdict_heads` (the SET of heads a "
        f"verdict already exists for) is allowed — it names the existence, "
        f"not the content."
    )
    # Positional-only signature would be a way to smuggle a verdict in past
    # the name check; the design keeps every parameter keyword-only for
    # readability at the call site AND to keep this signature check honest.
    for param in sig.parameters.values():
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"gate_admission parameter {param.name!r} must be keyword-only "
            f"(kind={param.kind!r}); a positional parameter could carry a "
            f"verdict payload past the name-based signature test above"
        )


def test_inv_ci_2_gate_admission_module_does_not_import_verdict_types() -> None:
    # A determined verdict-smuggler could stuff a ``ReviewEvent`` or
    # ``PrReviewOutcome`` into a closure or a module-level constant. The
    # cleanest structural warning is: this module doesn't import them.
    # If a future refactor genuinely needs one, it should land as a spec
    # amendment that also revisits INV-CI-2 — not slip in under the radar.
    import spirrow_mindwire.gate_admission as ga

    source = inspect.getsource(ga)
    forbidden_symbols = ("ReviewEvent", "PrReviewOutcome", "ModelVerdict")
    for symbol in forbidden_symbols:
        assert symbol not in source, (
            f"gate_admission must not reference {symbol!r} — it is a "
            f"verdict-content type, and INV-CI-2 keeps verdict content "
            f"structurally out of this module"
        )
