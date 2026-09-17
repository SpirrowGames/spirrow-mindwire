"""Tests for :mod:`spirrow_mindwire.conductor.gate_records`.

The two facts here are the ONLY history :func:`~spirrow_mindwire.gate_admission.gate_admission`
reads, and both are recovered by parsing text the conductor itself wrote. The failure mode when
a writer and its reader drift is silent — R5 stops firing and an implementer is re-dispatched at
a red head it already failed to fix, with no error anywhere — so the round-trip tests below are
the load-bearing ones, not the parser unit tests.
"""

from __future__ import annotations

import json

from spirrow_mindwire.conductor.gate_records import (
    _CI_ROUTE_CLOSE,
    _CI_ROUTE_OPEN,
    _CI_ROUTE_RE,
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


def test_ci_route_marker_survives_a_check_name_that_closes_the_comment() -> None:
    # ``checks`` holds GitHub check-run names — arbitrary strings chosen by the TARGET repo's
    # workflow, so the writer may not assume they are marker-safe. A name holding ``}`` followed
    # by nothing but spaces and ``-->`` ends _CI_ROUTE_RE's non-greedy capture early; the reader
    # then sees truncated JSON, drops the head, and R5 stops firing for it with no error
    # anywhere — exactly the silent write/read drift this module exists to prevent.
    hostile = "x} --> y"
    marker = render_ci_route_marker(head=_SHA, conclusion="failure", checks=[hostile])
    assert ci_route_heads([marker]) == frozenset({_SHA})

    # The escape costs the reader nothing: json.loads gives the name back verbatim, so the
    # human-facing field is preserved rather than sanitised away.
    assert json.loads(_CI_ROUTE_RE.findall(marker)[0])["checks"] == [hostile]

    # Negative control, deliberately in this same test: the SAME check name in a marker built
    # the way this function built them before the escape existed. It contributes no head, so
    # what rescues the head above is demonstrably the escape and not a property of the input.
    unescaped = (
        _CI_ROUTE_OPEN
        + json.dumps(
            {"head": _SHA, "conclusion": "failure", "checks": [hostile]},
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + _CI_ROUTE_CLOSE
    )
    assert ci_route_heads([unescaped]) == frozenset()


def test_ci_route_marker_never_emits_the_comment_close_in_its_payload() -> None:
    # The test above pins the BEHAVIOUR for one hostile name; this pins the INVARIANT that
    # behaviour rests on, across the family: whatever ``checks`` contains, the only ``-->`` in a
    # rendered marker is the delimiter this module wrote itself. Tab/newline/CR were already
    # safe because ensure_ascii escapes them; a plain space between ``}`` and ``-->`` was not,
    # which is why the trigger is narrower than "the name contains an arrow".
    hostile = ["x} --> y", "x}-->y", "}-->", "a}   -->b", "<!-- -->", "-->"]
    for name in hostile:
        marker = render_ci_route_marker(head=_SHA, conclusion="failure", checks=[name])
        assert marker.startswith(_CI_ROUTE_OPEN)
        assert marker.endswith(_CI_ROUTE_CLOSE)
        payload = marker[len(_CI_ROUTE_OPEN) : -len(_CI_ROUTE_CLOSE)]
        assert "-->" not in payload, f"check name {name!r} closed the marker early: {payload!r}"

    # All of them in one marker, which is the real shape when several checks go red at once.
    together = render_ci_route_marker(head=_SHA, conclusion="failure", checks=hostile)
    assert "-->" not in together[len(_CI_ROUTE_OPEN) : -len(_CI_ROUTE_CLOSE)]
    assert ci_route_heads([together]) == frozenset({_SHA})


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
    # R5 does not fire for that head, so a second red routes an implementer (R4's behaviour --
    # strictly more conservative than the pre-wiring path, which fired the gate regardless of
    # CI) instead of escalating.
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
