"""Backtest: feed the operator-lane incident fixtures into the classifier and
assert the predicate identifies the stall (or that the fixture is honestly
recorded as not-representable).

This suite is the mechanical side of the monotonic-fixture obligation in
`spec/process/obligations.yaml` → OBL-STALL-DETECTOR-MONOTONIC-FIXTURES.
Every operator-observed incident is a row in `tests/data/stall_ledger_incidents/`;
this file drives all of them.

Why backtest against static fixtures rather than a live probe:

    * Fixtures capture the state operator lane MEASURED at the time (§4-5 of
      msg-2692). A live probe (R-2c) was withdrawn because a historical
      query is not the same path as the live sweep's `state=open` retrieval
      (Einstein msg-2691 blocking #2).
    * A fixture that the current parser cannot even represent is preserved
      with `expected_verdict == "not-representable"` and a reason. That is
      the finding msg-2354 §3-1 asked for — "which of M-1..M-4 can the
      single result-side predicate actually see?" — recorded as data instead
      of as prose that decays.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.stall_ledger.model import UnitKind
from spirrow_mindwire.stall_ledger.predicates import (
    PrState,
    QuarantineState,
    ThreadState,
    needs_actor_pr,
    needs_actor_quarantine,
    needs_actor_thread,
    stalled,
)

_FIXTURES_DIR = Path(__file__).parent / "data" / "stall_ledger_incidents"


def _load_fixtures() -> list[dict[str, Any]]:
    """Return every incident fixture as a dict. Skips the README."""

    out: list[dict[str, Any]] = []
    for path in sorted(_FIXTURES_DIR.glob("*.json")):
        with path.open(encoding="utf-8") as f:
            out.append(json.load(f))
    return out


_FIXTURES = _load_fixtures()


def test_fixtures_directory_is_non_empty() -> None:
    """Regression pin: `tests/data/stall_ledger_incidents/` must always
    carry at least the four seed incidents from msg-2354 §1. The monotonic
    obligation says the set grows; it must never shrink to zero.
    """

    assert len(_FIXTURES) >= 4, (
        f"expected ≥4 seed fixtures from msg-2354 §1 (M-1..M-4), found "
        f"{len(_FIXTURES)}. If a fixture was retired, it must be replaced by "
        "a superseding one — see OBL-STALL-DETECTOR-MONOTONIC-FIXTURES."
    )


def test_all_seed_incidents_present() -> None:
    ids = {fx["id"] for fx in _FIXTURES}
    for expected in ("M-1", "M-2", "M-3", "M-4"):
        assert expected in ids, (
            f"seed fixture {expected!r} from msg-2354 §1 is missing from "
            f"{_FIXTURES_DIR}. Do not delete seed fixtures — they are the "
            "ground-truth backtest corpus."
        )


@pytest.mark.parametrize(
    "fixture",
    _FIXTURES,
    ids=[fx["id"] for fx in _FIXTURES],
)
def test_fixture_has_required_provenance_fields(fixture: dict[str, Any]) -> None:
    """Every fixture MUST record how it was captured — otherwise the corpus
    silently drifts into "assembled from memory" (which msg-2354 §5 flagged
    as the failure mode this whole thread was created to eliminate).
    """

    required = (
        "id",
        "observed_by",
        "observed_at",
        "spec_msg_ids",
        "provenance",
        "expected_verdict",
    )
    for field in required:
        assert field in fixture, (
            f"fixture {fixture.get('id', '?')!r} missing required field {field!r}"
        )
    for field in ("source", "captured_at", "captured_by"):
        assert field in fixture["provenance"], (
            f"fixture {fixture['id']!r} provenance missing {field!r} — the corpus "
            "must record its own capture chain (msg-2692 §4-5)."
        )
    assert fixture["expected_verdict"] in ("stall", "not-stall", "not-representable"), (
        f"fixture {fixture['id']!r} expected_verdict must be one of the three "
        "documented values; a fourth value here means the backtest cannot decide."
    )
    if fixture["expected_verdict"] == "not-representable":
        assert fixture.get("not_representable_reason"), (
            f"fixture {fixture['id']!r} claims not-representable but does not "
            "give a reason — reason is required so the residual is documented."
        )


@pytest.mark.parametrize(
    "fixture",
    _FIXTURES,
    ids=[fx["id"] for fx in _FIXTURES],
)
def test_backtest_predicate(fixture: dict[str, Any]) -> None:
    """The ledger's needs_actor + threshold predicate returns the expected
    verdict for this fixture.

    A "not-representable" fixture skips this assertion — the fixture exists
    to DOCUMENT that the current schema cannot express the incident, not
    to demand a green from a predicate that has no input for it.
    """

    if fixture["expected_verdict"] == "not-representable":
        pytest.skip(
            f"{fixture['id']}: intentionally not representable in v1 schema — "
            f"{fixture['not_representable_reason']}"
        )

    inp = fixture["predicate_input"]
    kind = inp["kind"]

    # PR-gate msg-2702 BLOCKING regression pin: materialise the datetime
    # pair and call the REAL ``stalled()`` function. The earlier draft
    # reimplemented the ``>`` comparison inline, which meant any drift in
    # the production predicate (inclusive vs. exclusive boundary, grace
    # period, whatever) would silently pass this backtest. The whole
    # purpose of the incident corpus (OBL-STALL-DETECTOR-MONOTONIC-FIXTURES)
    # is defeated by a test that shadows the production function.
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    hours_since = inp["hours_since_last_participant_motion"]
    last_participant_motion_at = now - timedelta(hours=hours_since)
    n = timedelta(hours=inp["n_threshold_hours"])

    if kind == "pr":
        pr_state = PrState(**inp["pr_state"])
        actor_needed = needs_actor_pr(pr_state)
        unit_kind = UnitKind.PR
    elif kind == "quarantine":
        q_state = QuarantineState(**inp["quarantine_state"])
        actor_needed = needs_actor_quarantine(q_state)
        unit_kind = UnitKind.QUARANTINE
    elif kind == "thread":
        t_state = ThreadState(**inp["thread_state"])
        actor_needed = needs_actor_thread(t_state)
        unit_kind = UnitKind.THREAD
    else:
        pytest.fail(f"fixture {fixture['id']!r}: unknown kind {kind!r}")

    observed_stall = stalled(
        kind=unit_kind,
        last_participant_motion_at=last_participant_motion_at,
        now=now,
        needs_actor_now=actor_needed,
        n=n,
    )

    expected = fixture["expected_verdict"] == "stall"
    assert observed_stall is expected, (
        f"{fixture['id']}: expected verdict={fixture['expected_verdict']!r}, "
        f"but needs_actor={actor_needed} stalled()={observed_stall} "
        f"(hours_since={hours_since}, n_hours={inp['n_threshold_hours']}). "
        "If the incident's shape has genuinely changed, update the fixture; "
        "if the predicate's meaning has shifted, that is a regression the "
        "fixture is preventing."
    )


def test_representability_summary() -> None:
    """A summary check that at least ONE fixture in each observed kind is
    representable. The monotonic-fixture obligation exists so this remains
    true — if every incident of a kind becomes not-representable, the
    detector has lost coverage of that kind and the residual must be
    addressed in the design thread, not silently absorbed.
    """

    representable_kinds: set[str] = set()
    for fx in _FIXTURES:
        if fx["expected_verdict"] in ("stall", "not-stall"):
            representable_kinds.add(fx["predicate_input"]["kind"])
    # At least PR and quarantine coverage is present in the seed corpus.
    assert "pr" in representable_kinds, (
        "no PR fixture is representable — the predicate lost PR coverage."
    )
    assert "quarantine" in representable_kinds, (
        "no quarantine fixture is representable — the predicate lost quarantine coverage."
    )


def test_unit_kinds_are_consistent_with_registry() -> None:
    """Sanity: any 'kind' string in a fixture matches a UnitKind value.

    A fixture that names a kind the ledger does not know is a schema drift
    warning — the predicate for it does not exist yet.
    """

    known = {k.value for k in UnitKind}
    for fx in _FIXTURES:
        if fx["expected_verdict"] == "not-representable":
            continue
        kind = fx["predicate_input"]["kind"]
        assert kind in known, (
            f"fixture {fx['id']!r} names unit kind {kind!r} which is not in "
            f"UnitKind: {known}. Either add the kind to the model or mark the "
            "fixture not-representable with a reason."
        )
