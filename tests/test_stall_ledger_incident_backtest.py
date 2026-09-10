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

import dataclasses
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


# ─── Per-incident structural residual (Bohr msg-2853 D-14 item 2) ────────────────────
#
# Einstein E-11 (msg-2749) named the first shape this mechanism fixes: the original
# summary check built ``representable_kinds`` from stall / not-stall fixtures only, so
# a kind appearing exclusively in ``not-representable`` fixtures registered as absent.
# A corpus carrying incidents of such a kind was "silently absorbed" — the docstring's
# own name for the failure mode.
#
# Until e1de998 the declaration lived in a module-level ``_KNOWN_UNCOVERED_KINDS`` dict
# keyed by KIND. PR-gate msg-2851 objection 2 showed that key is too coarse, and Bohr
# msg-2853 §2 reproduced it end to end: M-5 and M-6 are BOTH ``"kind": "thread"`` yet
# document different structural gaps. Make M-5 representable and the per-kind loop
# failed with an imperative — "Coverage arrived — delete the entry" — whose only
# resolution was deleting the single entry that also covered M-6. Obeying the suite's
# own instruction measured 2187 passed / 2 skipped, leaving M-6 in the corpus as a
# not-representable fixture with no entry, no receipt thread and no missing layers:
# the exact absorption this mechanism exists to prevent, reached by FOLLOWING the
# mechanism's instructions rather than by carelessness. Both that reproduction and the
# 2187 figure were re-measured first-hand at e1de998 before this replacement was written.
#
# The per-kind form also MIS-ROUTED, live at e1de998 and conditioned on nothing (Bohr
# msg-2853 §2): the single "thread" entry named ``T-next-line-carries-who-not-why`` and
# carried M-5's three layers only, so M-6 — a gate-relay incident whose fix lands on
# ``T-pr-gate-relay-belongs-to-the-conductor-not-the-gate`` — was pointed at the wrong
# design thread. ``receipt_thread`` is the field the design calls "the mechanical
# routing dependency", so a wrong value there is worse than a missing one.
#
# The record is therefore keyed PER INCIDENT and lives in the fixture itself, as a
# ``residual`` object carrying ``receipt_thread`` and ``missing_layers``. Two
# consequences, both deliberate:
#
#   * It SUBSUMES Einstein E-11 rather than dropping it. A kind appearing only in
#     not-representable fixtures is still declared — now once per incident, which is
#     strictly finer than once per kind.
#   * It removes the duplication PR-gate msg-2851 objection 3 filed as advisory. The
#     residual text was carried in BOTH the JSON ``not_representable_reason`` and the
#     Python dict, measured at 100% / 100% / 94% content-token overlap (Bohr msg-2853
#     §3), and the per-incident fix would otherwise have TRIPLED it. Which copy is the
#     duplicate is decided by OBL-STALL-DETECTOR-MONOTONIC-FIXTURES, which makes the
#     FIXTURE side compulsory ("a not-representable fixture MUST carry a reason") — so
#     the Python side is the copy that goes.
#
# What belongs in a residual: ``receipt_thread`` is the mechanical routing dependency
# (a chatroom thread a schema fix would arrive on) and ``missing_layers`` is the
# machine-readable list of what the schema does not carry today. What does NOT belong:
# project-management state (e.g. "no thread owns this yet"). Ownership state goes stale
# while the code has not changed — Einstein msg-2832 ADVISORY / Bohr msg-2833 §2.


def _assert_residual_is_exactly_the_not_representable_set(
    fixtures: list[dict[str, Any]],
) -> None:
    """Hold the biconditional: a fixture carries ``residual`` IFF it is not-representable.

    Two named directions, which together ARE that biconditional:

    (a) forward — every ``not-representable`` fixture carries a well-formed
        ``residual``, i.e. non-empty ``receipt_thread`` and ``missing_layers``;
    (b) stale-on-coverage — no representable fixture keeps a ``residual``. This is the
        direction that makes the deletion MECHANICAL: the day a schema change lets a
        fixture flip to stall / not-stall, its residual record must go, and that
        removal is the proof coverage arrived.

    The ORPHAN direction that ``_assert_allowlist_is_exactly_the_residual`` needed — a
    declared residual naming something NO fixture mentions — is gone, and the test that
    pinned it (``test_allowlist_invariant_rejects_orphan_entry_no_fixture_mentions``,
    endorsed by name in PR-gate msg-2851) is deleted with it. That is not a dropped
    invariant: a ``residual`` now lives inside the fixture it describes and cannot be
    constructed detached from one, so the defect class was removed BY CONSTRUCTION
    rather than left untested. Reading that deletion as a regression is precisely the
    misreading this paragraph exists to prevent (Bohr msg-2853 D-14 item 2.3).

    Module-level rather than inline test body so the repair is itself pinned:
    ``test_residual_invariant_*`` below drive it with in-memory dicts. A fix that is
    not under test can regress as silently as the hole it closed.
    """

    for fx in fixtures:
        fid = fx.get("id", "?")
        residual = fx.get("residual")

        if fx["expected_verdict"] != "not-representable":
            assert residual is None, (
                f"fixture {fid!r} is representable (expected_verdict="
                f"{fx['expected_verdict']!r}) but STILL carries a residual record. "
                "Coverage arrived — delete the residual. That deletion is the "
                "mechanical proof the residual closed; leaving it turns the record "
                "into an account of what was once missing rather than what is "
                "missing now."
            )
            continue

        assert residual, (
            f"fixture {fid!r} is not-representable but carries NO residual. Either "
            "the predicate silently lost coverage (representable fixtures got "
            "retired — forbidden by the monotonic obligation), or a not-representable "
            "fixture was added without declaring its residual. Both are the shape the "
            "monotonic-fixture obligation exists to prevent, and the per-kind "
            "allowlist this replaced reported exactly this state as 2187 passed."
        )
        assert residual.get("receipt_thread"), (
            f"fixture {fid!r} residual must name receipt_thread (the design thread a "
            "schema fix would arrive on). Without it the residual has no mechanical "
            "routing target and is indistinguishable from prose."
        )
        assert residual.get("missing_layers"), (
            f"fixture {fid!r} residual must list missing_layers so the residual is "
            "machine-readable rather than prose. Each layer is a one-line description "
            "of what the schema does not carry."
        )


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
        # ``not_representable_reason`` is the OBLIGATION side: prose for a human,
        # compulsory per OBL-STALL-DETECTOR-MONOTONIC-FIXTURES ("a not-representable
        # fixture MUST carry a reason"). The ``residual`` object checked by
        # ``_assert_residual_is_exactly_the_not_representable_set`` is the MACHINE
        # side: a receipt thread to route the fix to, and the layers as a list. They
        # are deliberately separate fields with different consumers, not a duplicate
        # pair — the duplication PR-gate msg-2851 objection 3 filed was between this
        # JSON reason and a Python dict, and that dict is what msg-2853 item 2 deleted.
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
    """Coverage check across ALL observed kinds, plus the per-incident residual.

    Old form (retired at 1747db7): built ``representable_kinds`` from stall /
    not-stall fixtures only and hard-coded ``pr`` / ``quarantine`` asserts. A
    fixture whose kind appeared exclusively in ``not-representable`` verdicts —
    exactly what M-5 / M-6 / M-7 introduce for ``thread`` and ``hold`` —
    registered as absent, so the docstring's own "silently absorbed" state
    became reachable while the test stayed green (Einstein msg-2749 E-11).

    Second form (retired at e1de998): a per-KIND allowlist. It closed E-11 but
    was too coarse to hold, because M-5 and M-6 share ``"kind": "thread"``. See
    the module comment above for the end-to-end reproduction — the short version
    is that making M-5 representable forced the deletion of the one entry that
    also covered M-6, and the suite went green at 2187 passed with M-6 untracked.

    Current form (Bohr msg-2853 D-14 item 2): the residual is declared PER
    INCIDENT, inside the fixture, and the biconditional "carries a residual IFF
    not-representable" is held by
    ``_assert_residual_is_exactly_the_not_representable_set``. The kind-level
    coverage asserts below survive unchanged and are now what guards kind-level
    coverage LOSS; the residual helper guards the per-incident declaration.

    Deliberately NOT done here (Einstein msg-2749 E-11 "採ってはならない修正"):
    a bare ``assert "thread" in representable_kinds``. That would hold the corpus
    red until the schema shipped, and the corpus is monotonic — the residual path
    is the shape that lets a gap be RECORDED without blocking further fixtures.
    """

    all_kinds: set[str] = set()
    representable_kinds: set[str] = set()
    for fx in _FIXTURES:
        kind = fx["predicate_input"]["kind"]
        all_kinds.add(kind)
        if fx["expected_verdict"] in ("stall", "not-stall"):
            representable_kinds.add(kind)

    # The seed corpus (M-1..M-4) guarantees PR and quarantine coverage. Assert
    # them explicitly so a retire of the seeds is caught here — the monotonic
    # obligation forbids that retire, and this line is its runtime witness.
    # Kept verbatim across the per-kind → per-incident move (Bohr msg-2853 item
    # 2.5): after that move these two lines, together with
    # ``test_all_seed_incidents_present``, are the whole of the kind-level
    # coverage-loss guard.
    assert "pr" in representable_kinds, (
        "no PR fixture is representable — the predicate lost PR coverage."
    )
    assert "quarantine" in representable_kinds, (
        "no quarantine fixture is representable — the predicate lost quarantine coverage."
    )

    _assert_residual_is_exactly_the_not_representable_set(_FIXTURES)


# Self-contained inputs for the residual-invariant pins below. Deliberately NOT the
# live fixtures: those residuals are supposed to be DELETED the day their schema gap
# closes, and a pin that read them would break on that deletion — exactly when the
# invariant matters most.
_SYNTHETIC_RESIDUAL: dict[str, Any] = {
    "receipt_thread": "T-synthetic-pin-target-not-a-real-thread",
    "missing_layers": ("synthetic: exists only inside the residual-invariant pins",),
}


def test_residual_invariant_rejects_stale_record_once_coverage_arrives() -> None:
    """Pin for the stale-on-coverage direction — PR-gate msg-2844 objection 2.

    The direction that makes the deletion mechanical: a fixture that has become
    representable must not keep the record of what used to be missing. At 1747db7
    the per-kind ancestor of this check passed on the equivalent input (the loop hit
    a bare ``continue`` and the entry survived); Bohr msg-2845 §2 reproduced that by
    dropping a scratch fixture into the corpus and observing PASSED.

    Pinned in memory because Bohr msg-2845 §3.4 forbids pinning it the way it was
    reproduced: a probe file written into ``tests/data/stall_ledger_incidents/``
    would pollute the corpus, which is monotonic and cannot take the file back out.
    """

    with pytest.raises(AssertionError, match="Coverage arrived"):
        _assert_residual_is_exactly_the_not_representable_set(
            [
                {
                    "id": "SYNTH-covered",
                    "expected_verdict": "stall",
                    "residual": _SYNTHETIC_RESIDUAL,
                }
            ]
        )


def test_residual_invariant_rejects_not_representable_fixture_with_no_residual() -> None:
    """Pin for the forward direction — the M-6 ABSORPTION, PR-gate msg-2851 objection 2.

    This input is the state the retired per-kind allowlist could not see, and it is
    not hypothetical: at e1de998, flipping M-5 to ``stall`` and then obeying the
    suite's own imperative to delete the now-stale ``"thread"`` entry left M-6 as a
    not-representable fixture with no entry, no receipt thread and no missing layers
    — and the whole suite reported **2187 passed, 2 skipped, 6 deselected**. That
    figure was measured first-hand at e1de998, not quoted.

    Keyed per incident, the same state is unrepresentable-by-omission: M-6 cannot
    lose its declaration when M-5 gains coverage, because the two records are
    separate objects living in separate fixtures.
    """

    with pytest.raises(AssertionError, match="carries NO residual"):
        _assert_residual_is_exactly_the_not_representable_set(
            [{"id": "SYNTH-M6-like", "expected_verdict": "not-representable"}]
        )


def test_unit_kinds_are_consistent_with_registry() -> None:
    """Sanity: any 'kind' string in a fixture matches a UnitKind value.

    A fixture that names a kind the ledger does not know is a schema drift
    warning — the predicate for it does not exist yet.
    """

    # Reads the registry to validate FIXTURES against it. Its sibling, the M-7
    # collision pin, holds the registry itself to an exact set — the two are
    # complementary: this one reds when a fixture names an unknown kind, that one
    # reds when the registry changes under the fixtures.
    known = {k.value for k in UnitKind}
    for fx in _FIXTURES:
        # Load-bearing: M-7 is the fixture that takes this branch — its "kind": "hold"
        # is deliberately absent from UnitKind (see the M-7 collision pin below).
        if fx["expected_verdict"] == "not-representable":
            continue
        kind = fx["predicate_input"]["kind"]
        assert kind in known, (
            f"fixture {fx['id']!r} names unit kind {kind!r} which is not in "
            f"UnitKind: {known}. Either add the kind to the model or mark the "
            "fixture not-representable with a reason."
        )


# ─── Collision pins for M-5 / M-6 / M-7 (msg-2749 E-12, msg-2833 §1 D-12″) ─────────────
#
# Each not-representable fixture is paired with a RUNNING assertion that materialises
# the incident and its valid-wait counterexample and shows the v1 schema cannot
# distinguish them (or, for M-7, that the schema has no kind for the shape at all).
# The pin serves TWO roles that share one implementation (Einstein msg-2832 Q-4):
#
#     (1) Local supersede guard: when a later reviewer wants to flip a not-
#         representable fixture to stall / not-stall, the pin either passes
#         (equivalence still holds — flipping the verdict would silently
#         contradict a still-running assertion) or fails (equivalence broke —
#         the fixture may need a fresh capture).
#
#     (2) Monotonic-corpus invariant guard (Bohr msg-2401 §3-4): the pin reds
#         the day the schema grows a way to distinguish incident from valid
#         wait. That red is the mechanical trigger for retiring the allowlist
#         entry and superseding the fixture — which is what turns the
#         allowlist from "indefinite parking" into "waiting bounded by a
#         machine trigger".
#
# The docstring of each pin says explicitly WHAT MUST CHANGE FOR IT TO RED — Bohr
# msg-2833 §1 D-12″: "各 pin の docstring に「何が変われば red になるか」を書く".


_THREAD_STATE_FIELDS_AT_E1DE998 = frozenset({"status", "dormant_until_expired"})


def _assert_thread_state_schema_unchanged(observed_field_names: set[str]) -> None:
    """Red when ``ThreadState``'s FIELD SET changes — the real M-5 / M-6 supersede trigger.

    Why this exists, and why the byte-equivalence assertions below are not enough.
    Both collision pins claimed to red when ``ThreadState`` "gains any field carrying
    the completion time of the awaited act". PR-gate msg-2851 objection 1 said that
    claim was false for OPTIONAL fields, and Bohr msg-2853 §1 settled it by adding the
    exact field the M-5 docstring names — ``completion_time_of_awaited_act: datetime |
    None = None`` — and running everything. Re-measured first-hand at e1de998 before
    this helper was written:

        optional field (``= None``) → 2186 passed, 3 skipped, 6 deselected,
                                      BYTE-IDENTICAL to the untouched baseline;
        required field (no default) → both collision pins FAIL with ``TypeError``.

    So the trigger did not merely miss its own case in its own pin: with the schema
    change the fixtures are waiting for actually applied, NOTHING in the repository
    went red. The cause is structural — both sides are built from the same two
    hardcoded kwargs, so ``incident_state == valid_wait_state`` is a tautology over any
    field the constructor defaults, and the assertion can only fail by the constructor
    RAISING. Optional-with-default is also the likely shape: a frozen dataclass grows
    backward-compatible fields far more often than required ones.

    The remedy is the device PR-gate msg-2851 endorsed in the M-7 pin, applied here:
    inspect the SCHEMA, not two instances. The field set is taken as a PARAMETER rather
    than read inside, so ``test_thread_state_schema_pin_reds_on_field_addition`` can
    drive it without mutating the real dataclass (and without this suite ever editing
    ``predicates.py``, which msg-2853 forbids: the pins must red WHEN the schema
    changes, not BY changing it).

    Accepted cost, stated rather than hidden: this reds on ANY ``ThreadState`` field
    addition, including ones with nothing to do with M-5 or M-6. That is intended. One
    unnecessary revisit of two fixtures is cheaper than a trigger that is silently
    gone, and the measured alternative is 2186 green.
    """

    assert observed_field_names == set(_THREAD_STATE_FIELDS_AT_E1DE998), (
        f"ThreadState's field set changed: expected "
        f"{sorted(_THREAD_STATE_FIELDS_AT_E1DE998)}, observed "
        f"{sorted(observed_field_names)}. The M-5 / M-6 collision pins assume the v1 "
        "schema cannot distinguish an incident from a valid wait; a schema change is "
        "the mechanical trigger to revisit BOTH fixtures' not-representable verdicts "
        "and their residual records. If the new field is unrelated to act-fulfilment, "
        "widen the expected set here and say so — do not delete this assertion, which "
        "is the only thing standing between an optional field and a silent 2186 green."
    )


def test_thread_state_schema_pin_reds_on_field_addition() -> None:
    """Pin the schema helper itself — a fix that is not under test can regress silently.

    Drives a three-element set through the helper in memory. This is the shape Bohr
    msg-2853 §1 produced against the live dataclass
    (``['completion_time_of_awaited_act', 'dormant_until_expired', 'status']``) and
    measured as green under the equality device and red under this one.
    """

    with pytest.raises(AssertionError, match="field set changed"):
        _assert_thread_state_schema_unchanged(
            {"status", "dormant_until_expired", "completion_time_of_awaited_act"}
        )


def test_m5_collision_pin_incident_equivalent_to_valid_wait() -> None:
    """Collision pin for M-5 (stale NEXT: human after PR #237 merged).

    Materialises the incident and a valid-wait counterexample as
    ``ThreadState`` values and asserts the v1 predicate cannot distinguish
    them. This is the machine-readable form of Einstein msg-2749 §2 —
    "incident と反例が等価な `ThreadState` に落ちる".

    Reds when — every clause below is now backed by a NAMED assertion in this
    function. At e1de998 the first clause was backed by nothing: it claimed a
    trigger that measured 2186 green (PR-gate msg-2851 objection 1, reproduced in
    Bohr msg-2853 §1 and again here before this rewrite).

        * ``ThreadState`` gains ANY field, including an optional one with a
          default — held by ``_assert_thread_state_schema_unchanged``, the FIRST
          assertion below. This is the schema layer (Einstein E-9), plus the
          ledger-side half of the granularity layer (E-10): a request-level
          fulfilment field that lands ON ``ThreadState`` changes the field set.
        * OR the byte-equivalence of the two states below stops holding for any
          reason — a signal the fixture's capture drifted. These are the original
          assertions, kept: they document the collision the fixture asserts, they
          are simply not the trigger.

    NOT covered, stated plainly because the e1de998 docstring implied otherwise:
    request-level fulfilment that lands on a SEPARATE object rather than on
    ``ThreadState`` (the rest of granularity layer E-10), and the consumer layer
    in ``scripts/parked_humans.py`` (E-9). Neither is visible from this pin; both
    remain declared in the M-5 fixture's ``residual.missing_layers``.

    When it reds, revisit the M-5 fixture: the schema may now express the
    incident, and the not-representable verdict is due to supersede.
    """

    # FIRST assertion, deliberately (Bohr msg-2853 D-14 item 1.2): the schema is the
    # trigger, the byte-equivalence below is the documentation.
    _assert_thread_state_schema_unchanged({f.name for f in dataclasses.fields(ThreadState)})

    # Incident: T-stalled-pr-has-no-detector's msg-2712 nominated 'human',
    # Takahito merged PR #237 at 07:17:57Z, the thread stayed silent 19h21m.
    incident_state = ThreadState(status="active", dormant_until_expired=True)
    # Valid counterexample: a legitimate Tier-C wait where the human has
    # not yet acted. Bohr msg-2744 §5-1 walks the same 19h window: this is
    # the shape of any healthy nomination that has simply not been picked
    # up yet, and it is a permanent presence in the loop.
    valid_wait_state = ThreadState(status="active", dormant_until_expired=True)

    assert incident_state == valid_wait_state, (
        "M-5 collision pin: expected ThreadState byte-equivalence between "
        "incident and valid-wait, but the schema now distinguishes them. "
        "Retire the M-5 fixture's not-representable claim and supersede."
    )
    assert needs_actor_thread(incident_state) == needs_actor_thread(valid_wait_state)

    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    last_motion = now - timedelta(hours=19)
    n = timedelta(hours=6)
    incident_stall = stalled(
        kind=UnitKind.THREAD,
        last_participant_motion_at=last_motion,
        now=now,
        needs_actor_now=needs_actor_thread(incident_state),
        n=n,
    )
    valid_wait_stall = stalled(
        kind=UnitKind.THREAD,
        last_participant_motion_at=last_motion,
        now=now,
        needs_actor_now=needs_actor_thread(valid_wait_state),
        n=n,
    )
    assert incident_stall is True, (
        "M-5 collision pin: v1 predicate returned False for the incident's "
        "own state, so the equivalence claim in the fixture no longer holds — "
        "revisit the fixture."
    )
    assert valid_wait_stall is True, (
        "M-5 collision pin: v1 predicate returned False for a valid-wait "
        "counterexample. Either the predicate changed or the byte-equivalence "
        "premise did — revisit the fixture."
    )


def test_m6_collision_pin_incident_equivalent_to_valid_wait() -> None:
    """Collision pin for M-6 (manual PR-gate review, no relay to design thread).

    Same shape as M-5's pin but for a different cross-artifact pairing: a
    GitHub review event vs. the design thread that awaited a relay of it.
    The v1 schema does not carry either half of that pair, so incident and
    valid-wait resolve to the same ``ThreadState``.

    Reds when — as with the M-5 pin, every clause is now backed by a named
    assertion. At e1de998 the first clause was not (PR-gate msg-2851 objection 1).

        * ``ThreadState`` gains ANY field, including an optional one with a
          default — held by ``_assert_thread_state_schema_unchanged``, the FIRST
          assertion below. A review event's timestamp landing on ``ThreadState``
          is one instance of that (schema layer per Bohr §3(b)).
        * OR ``ThreadState`` byte-equivalence between incident and valid-wait
          stops holding, for any reason (fixture drift). Original assertion, kept.

    NOT covered, stated plainly: the cross-artifact pair could equally arrive as a
    NEW UNIT KIND rather than as a ``ThreadState`` field, and this pin would not
    see that. (The M-7 pin holds the ``UnitKind`` registry exactly, so a new kind
    reds THERE — but that is M-7's trigger, not M-6's, and it is not a substitute
    for one.) The consumer layer in ``scripts/parked_humans.py`` is likewise
    invisible here. Both remain declared in M-6's ``residual.missing_layers``.

    When it reds, revisit the M-6 fixture: the schema may now express the
    review-vs-thread pair the incident hinges on.
    """

    # FIRST assertion, deliberately (Bohr msg-2853 D-14 item 1.2), same as M-5's pin.
    _assert_thread_state_schema_unchanged({f.name for f in dataclasses.fields(ThreadState)})

    # Incident: a manually-fired PR-gate review was posted on a mindwire PR,
    # no relay reached the corresponding design thread, thread head still
    # names the role that already acted.
    incident_state = ThreadState(status="active", dormant_until_expired=True)
    # Valid counterexample: the reviewer has legitimately not posted yet.
    # The design thread is correctly waiting.
    valid_wait_state = ThreadState(status="active", dormant_until_expired=True)

    assert incident_state == valid_wait_state, (
        "M-6 collision pin: ThreadState byte-equivalence broke — the schema "
        "now distinguishes 'review posted, no relay' from 'no review yet'. "
        "Retire the M-6 fixture's not-representable claim and supersede."
    )
    assert needs_actor_thread(incident_state) == needs_actor_thread(valid_wait_state)


def test_m7_collision_pin_no_hold_kind_in_registry() -> None:
    """Collision pin for M-7 (loop_control hold's 18-min desired_at / observed_at gap).

    M-7 differs from M-5 / M-6: there is no unit kind at all in which to construct
    the incident-vs-valid-wait pair. The pin therefore holds the ``UnitKind``
    registry EXACTLY. The trigger is the registry changing — which by Bohr §5-2
    order is the schema decision the M-7 fixture is waiting on.

    Reds when:
        * ``UnitKind`` gains, loses or renames any value — the exact-set assertion
          below reds on all three regardless of spelling.

    Previously this pin enumerated four anticipated spellings (``hold``, ``ack``,
    ``control``, ``loop_control``) and its own docstring confessed the hole: "a name
    we did not anticipate would slip through". That is PR-gate msg-2851 objection
    1's defect class — enumerating anticipated shapes instead of pinning the schema
    — reached independently by Bohr msg-2853 item 3, which flags it as beyond the
    gate's own findings. The confession is retired because the hole is: a kind added
    under a name nobody predicted now reds here like any other.

    Accepted cost, same trade as the ``ThreadState`` schema pin: this reds on ANY
    registry change, including one unrelated to holds. That is intended — widen the
    expected set deliberately rather than letting the trigger rot.

    When it reds, revisit the M-7 fixture: the kind registry may now name the shape
    the incident carries.
    """

    assert {k.value for k in UnitKind} == {"pr", "thread", "quarantine"}, (
        f"UnitKind registry changed: observed {sorted(k.value for k in UnitKind)}, "
        "expected ['pr', 'quarantine', 'thread']. If a kind carrying a (desired_at, "
        "observed_at) pair was added, the M-7 fixture may no longer belong in the "
        "not-representable bucket — retire this pin and supersede the fixture. If the "
        "change is unrelated to holds, widen the expected set here explicitly."
    )
