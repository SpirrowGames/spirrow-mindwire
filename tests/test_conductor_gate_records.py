"""Tests for :mod:`spirrow_mindwire.conductor.gate_records`.

The two facts here are the ONLY history :func:`~spirrow_mindwire.gate_admission.gate_admission`
reads, and both are recovered by parsing text the conductor itself wrote. The failure mode when
a writer and its reader drift is silent — R5 stops firing and an implementer is re-dispatched at
a red head it already failed to fix, with no error anywhere — so the round-trip tests below are
the load-bearing ones, not the parser unit tests.
"""

from __future__ import annotations

from spirrow_mindwire.conductor.gate_records import (
    ci_route_heads,
    normalize_sha,
    render_ci_route_marker,
    render_relay_heading,
    verdict_heads,
)

_SHA = "703b836737f29fe0f4139d86d2d02077c662ce5f"


# ---------- ci-route marker (design v0.3.1 §5.2A.5) ---------------------- #


def test_ci_route_marker_matches_the_design_byte_form() -> None:
    # The design specifies the marker literally. Pinning the exact bytes (not just
    # "round-trips") is what stops a future edit from inventing a second dialect that this
    # module's own parser would happily accept while any other reader of the thread would not.
    marker = render_ci_route_marker(head="ABC123DEF456", conclusion="failure", checks=["gate"])
    assert marker == (
        '<!-- mindwire:ci-route v1 {"head":"abc123def456","conclusion":"failure",'
        '"checks":["gate"]} -->'
    )


def test_ci_route_marker_round_trips_through_a_relay_body() -> None:
    # The real shape: the marker is the last line of a longer post, not a standalone string.
    body = "PR-gate admission — acme/widgets#7\n\nCI is red.\n\nNEXT: Heisenberg\n\n" + (
        render_ci_route_marker(head=_SHA, conclusion="failure", checks=["CI", "lint"])
    )
    assert ci_route_heads([body]) == frozenset({_SHA})


def test_ci_route_heads_collects_every_head_across_messages() -> None:
    other = "0" * 40
    bodies = [
        render_ci_route_marker(head=_SHA, conclusion="failure", checks=[]),
        "unrelated message with no marker",
        render_ci_route_marker(head=other, conclusion="failure", checks=["x"]),
    ]
    assert ci_route_heads(bodies) == frozenset({_SHA, other})


def test_ci_route_heads_ignores_a_malformed_marker_without_raising() -> None:
    # A scheduled loop reads a thread it does not control. One unparseable comment must cost
    # exactly one head, not the tick. The cost of the drop is bounded and named in the module:
    # R5 does not fire for that head, so a second red routes an implementer (R4's behaviour,
    # i.e. what happened before this wiring existed) instead of escalating.
    bodies = [
        "<!-- mindwire:ci-route v1 {not json} -->",
        '<!-- mindwire:ci-route v1 ["a","list","not","an","object"] -->',
        '<!-- mindwire:ci-route v1 {"conclusion":"failure"} -->',  # no head key
        render_ci_route_marker(head=_SHA, conclusion="failure", checks=[]),
    ]
    assert ci_route_heads(bodies) == frozenset({_SHA})


def test_ci_route_heads_is_empty_for_a_thread_that_never_routed() -> None:
    assert ci_route_heads(["design proposal\n\nNEXT: Einstein", ""]) == frozenset()


# ---------- verdict relay heading (gate_admission's verdict_heads) -------- #


def test_relay_heading_names_the_head_and_reads_back() -> None:
    heading = render_relay_heading("acme/widgets#7", _SHA)
    body = f"{heading}\n\nVERDICT: approve (ci=success)\n\ncritique\n\nNEXT: human"
    assert verdict_heads([body]) == frozenset({_SHA})


def test_relay_heading_without_a_head_records_nothing() -> None:
    # ``PrReviewOutcome.head_sha`` is optional — a CI read that failed closed still produces a
    # verdict. The heading then reads exactly as it did before this wiring and contributes no
    # entry, which is correct: R6 may only dedupe a re-review when it can name the head the
    # first verdict was about. Silently recording *some* head here would suppress a real review.
    heading = render_relay_heading("acme/widgets#7", None)
    assert heading == "PR-gate (Tier B independent naysayer) — acme/widgets#7"
    assert verdict_heads([f"{heading}\n\nVERDICT: comment (ci=unknown)\n\nNEXT: human"]) == (
        frozenset()
    )


def test_verdict_heads_reads_only_the_first_line() -> None:
    # A critique that QUOTES a heading (this arc's own review turns quote markers verbatim)
    # must not register as a verdict on that head — that would suppress a genuine gate firing.
    # Same restriction, same reason, as ``Conductor._attested``'s last-line-only rule.
    body = (
        "PR-gate (Tier B independent naysayer) — acme/widgets#7\n\n"
        "VERDICT: request_changes (ci=success)\n\n"
        f"> quoting an older relay: PR-gate ... — acme/widgets#7 @ {_SHA}\n\n"
        "NEXT: Heisenberg"
    )
    assert verdict_heads([body]) == frozenset()


def test_verdict_heads_tolerates_an_empty_body() -> None:
    assert verdict_heads(["", "   \n\n  "]) == frozenset()


# ---------- normalisation ------------------------------------------------ #


def test_normalisation_makes_a_hand_written_head_compare_equal() -> None:
    # gate_admission tests membership with ``in`` against a frozenset — exact string equality.
    # GitHub answers lower-case, but a marker travels through human hands (a quoted body, a
    # hand-written correction), so both sides normalise at every boundary.
    upper = _SHA.upper()
    assert normalize_sha(f"  {upper}  ") == _SHA
    upper_marker = render_ci_route_marker(head=upper, conclusion="failure", checks=[])
    assert ci_route_heads([upper_marker]) == frozenset({_SHA})
    assert verdict_heads([render_relay_heading("acme/widgets#7", upper)]) == frozenset({_SHA})
