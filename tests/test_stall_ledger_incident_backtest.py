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


# ─── Coverage allowlist for kinds the v1 predicate cannot decide (msg-2833 §3) ────────
#
# Einstein E-11 (msg-2749) named the shape this dict fixes: the previous summary check
# built ``representable_kinds`` from stall / not-stall fixtures only, so a kind that
# appeared exclusively in ``not-representable`` fixtures registered as absent. A
# corpus containing incidents of a kind whose ``representable_kinds`` membership is
# zero was "silently absorbed" — the docstring's own name for the failure mode.
#
# The remedy Bohr msg-2833 §3 lands: read the kind from ALL fixtures, then for each
# observed kind demand EITHER at least one representable fixture (predicate covers
# it) OR an entry here (predicate does not cover it, but the absence is DECLARED,
# named, and mechanically routed to the design thread that would ship the fix). When
# the schema grows the missing layers, the entry is REMOVED — that removal is the
# mechanical proof that coverage arrived.
#
# That removal is FORCED, not merely expected. Until 1747db7 the sentence above was
# a claim no assertion backed: a kind could gain a representable fixture and keep its
# entry here forever while the suite stayed green (PR-gate msg-2844 objection 2,
# reproduced by construction in Bohr msg-2845 §2). The enforcement now lives in
# ``_assert_allowlist_is_exactly_the_residual`` below, which holds
# ``set(_KNOWN_UNCOVERED_KINDS) == all_kinds - representable_kinds``: its
# stale-on-coverage assertion is what makes the deletion mechanical, and its orphan
# assertion additionally catches an entry that no fixture mentions at all.
#
# What belongs in an entry: ``receipt_thread`` is the mechanical routing dependency
# (a chatroom thread name a schema fix would arrive on) and ``missing_layers`` is
# the machine-readable list of what the schema does not carry today. What does NOT
# belong here: project-management state (e.g. "no thread owns this yet"). Ownership
# state goes stale when the code has not changed — Einstein msg-2832 ADVISORY / Bohr
# msg-2833 §2. The receipt-thread field is stable because it names a routing target,
# not a claim about who is working on it.

_KNOWN_UNCOVERED_KINDS: dict[str, dict[str, Any]] = {
    "thread": {
        "receipt_thread": "T-next-line-carries-who-not-why",
        "missing_layers": (
            "schema: no field carries the completion time of the awaited act",
            "granularity: a fulfilment field would have to live on the individual "
            "request, not on ThreadState — a single 'NEXT: human' line can carry "
            "more than one request (Einstein E-10)",
            "consumer: scripts/parked_humans.py reads only the thread head's last "
            "NEXT: line and has no concept of act-fulfilment (Einstein E-9), so a "
            "ledger-side fix alone cannot preserve the §7 disjointness",
        ),
    },
    "hold": {
        "receipt_thread": "T-hold-ack-lands-only-as-a-side-effect-of-launching",
        "missing_layers": (
            "schema: UnitKind enumerates only {pr, thread, quarantine}; no unit "
            "kind carries a (desired_at, observed_at) pair — the shape a hold's "
            "incident is defined by",
        ),
    },
}


def _assert_allowlist_is_exactly_the_residual(
    all_kinds: set[str],
    representable_kinds: set[str],
    allowlist: dict[str, dict[str, Any]],
) -> None:
    """Hold ``set(allowlist) == all_kinds - representable_kinds``.

    Three assertions that together ARE that set equality. The equality — rather
    than the PR-gate's own prescription in msg-2844, "the sets must be mutually
    exclusive" — is what Bohr msg-2845 §2 settled on, because exclusivity is too
    weak: an entry naming a kind that NO fixture mentions is in neither
    ``representable_kinds`` nor ``all_kinds``, so it satisfies exclusivity
    trivially and lingers forever. Named directions:

    (a) forward — every observed-but-uncovered kind has a well-formed entry,
        i.e. ``all_kinds - representable_kinds ⊆ set(allowlist)``;
    (b) stale-on-coverage — no representable kind keeps an entry, i.e. no member
        of ``set(allowlist)`` lies in ``representable_kinds``;
    (c) orphan — ``set(allowlist) - all_kinds`` is empty.

    (b) ∧ (c) give ``set(allowlist) ⊆ all_kinds - representable_kinds``; with (a)
    that is the equality. (b) and (c) are the new ones — at 1747db7 the loop hit a
    bare ``continue`` on the representable branch and never looked at the
    allowlist, which is PR-gate msg-2844 objection 2.

    This is a module-level helper, not inline test body, so the repair is itself
    pinned: ``test_allowlist_invariant_*`` below drive it with in-memory inputs.
    A fix that is not under test can regress as silently as the hole it closed.
    """

    for kind in sorted(all_kinds):
        if kind in representable_kinds:
            assert kind not in allowlist, (
                f"kind {kind!r} now HAS a representable fixture but is STILL listed "
                "in _KNOWN_UNCOVERED_KINDS. Coverage arrived — delete the entry. "
                "That deletion is the mechanical proof the residual closed; leaving "
                "it turns the allowlist into a record of what was once missing "
                "rather than what is missing now."
            )
            continue
        assert kind in allowlist, (
            f"kind {kind!r} appears in a fixture but has no representable "
            f"fixture and no _KNOWN_UNCOVERED_KINDS entry. Either the "
            "predicate silently lost coverage of this kind (representable "
            "fixtures got retired — forbidden by the monotonic obligation), "
            "or a not-representable fixture was added without declaring the "
            "residual. Both are the shape the monotonic-fixture obligation "
            "exists to prevent."
        )
        entry = allowlist[kind]
        assert entry.get("receipt_thread"), (
            f"_KNOWN_UNCOVERED_KINDS[{kind!r}] must name receipt_thread "
            "(the design thread a schema fix would arrive on). Without it, "
            "the allowlist entry has no mechanical routing target and is "
            "indistinguishable from prose."
        )
        assert entry.get("missing_layers"), (
            f"_KNOWN_UNCOVERED_KINDS[{kind!r}] must list missing_layers so "
            "the residual is machine-readable, not prose. Each layer is a "
            "one-line description of what the schema does not carry."
        )

    # Asserted outside the loop on purpose: the loop iterates observed kinds, so an
    # entry naming a kind no fixture mentions is unreachable from inside it.
    orphans = sorted(set(allowlist) - all_kinds)
    assert not orphans, (
        f"_KNOWN_UNCOVERED_KINDS names kind(s) {orphans} that NO fixture mentions. "
        "The per-kind loop above can never reach them, so they would linger "
        "indefinitely. Either the fixture that motivated the entry was retired "
        "(forbidden by the monotonic obligation) or the entry outlived its residual "
        "and must be deleted."
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
    """Coverage check across ALL observed kinds (rewritten per Einstein msg-2749 E-11).

    Old form (retired): built ``representable_kinds`` from stall / not-stall
    fixtures only and hard-coded ``pr`` / ``quarantine`` asserts. A fixture
    whose kind appeared exclusively in ``not-representable`` verdicts —
    exactly what M-5 / M-6 / M-7 introduce for ``thread`` and ``hold`` —
    registered as absent, so the docstring's own "silently absorbed" state
    became reachable while the test stayed green.

    New form (Bohr msg-2833 §3): read the kind from EVERY fixture,
    independent of verdict, then require that each observed kind is either
    represented by ≥1 stall / not-stall fixture (predicate covers it) OR
    listed in ``_KNOWN_UNCOVERED_KINDS`` (predicate does not cover it, but
    the absence carries a receipt thread and machine-readable missing
    layers). Deleting an allowlist entry becomes the mechanical proof that
    coverage arrived — Bohr Q-4 / Einstein msg-2832 BLOCKING invariant.

    That last sentence is now ENFORCED rather than merely asserted here: the
    check below delegates to ``_assert_allowlist_is_exactly_the_residual``,
    whose stale-on-coverage direction fails the day a kind gains a representable
    fixture and keeps its allowlist entry. At 1747db7 this docstring overclaimed
    — nothing forced the deletion, and PR-gate msg-2844 objection 2 was right
    about that (Bohr msg-2845 §2 reproduced it by construction).

    Deliberately NOT done here (Einstein msg-2749 E-11 "採ってはならない修正"):
    a bare ``assert "thread" in representable_kinds``. That would block the
    corpus red until the schema shipped, and the corpus is monotonic — the
    allowlist path is the shape that lets the residual be RECORDED without
    blocking further fixture additions.
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
    assert "pr" in representable_kinds, (
        "no PR fixture is representable — the predicate lost PR coverage."
    )
    assert "quarantine" in representable_kinds, (
        "no quarantine fixture is representable — the predicate lost quarantine coverage."
    )

    _assert_allowlist_is_exactly_the_residual(
        all_kinds=all_kinds,
        representable_kinds=representable_kinds,
        allowlist=_KNOWN_UNCOVERED_KINDS,
    )


# A self-contained entry for the invariant pins below. Deliberately NOT one of the
# live ``_KNOWN_UNCOVERED_KINDS`` values: those two are supposed to be DELETED the day
# their schema residual closes, and a pin that read them would break on that deletion —
# exactly when the invariant matters most.
_SYNTHETIC_ALLOWLIST_ENTRY: dict[str, Any] = {
    "receipt_thread": "T-synthetic-pin-target-not-a-real-thread",
    "missing_layers": ("synthetic: exists only inside the allowlist-invariant pins",),
}


def test_allowlist_invariant_rejects_stale_entry_once_coverage_arrives() -> None:
    """Pin for the stale-on-coverage direction — PR-gate msg-2844 objection 2.

    At 1747db7 this exact input passed: ``thread`` was representable, the loop hit
    a bare ``continue``, and the entry survived. Bohr msg-2845 §2 reproduced that
    by dropping a scratch fixture into the corpus and observing
    ``test_representability_summary PASSED``.

    Pinned in memory because §3.4 forbids pinning it the way it was reproduced:
    a probe file written into ``tests/data/stall_ledger_incidents/`` would pollute
    the corpus, which is monotonic and therefore cannot take the file back out.
    """

    with pytest.raises(AssertionError, match="STILL listed"):
        _assert_allowlist_is_exactly_the_residual(
            all_kinds={"thread"},
            representable_kinds={"thread"},
            allowlist={"thread": _SYNTHETIC_ALLOWLIST_ENTRY},
        )


def test_allowlist_invariant_rejects_orphan_entry_no_fixture_mentions() -> None:
    """Pin for the orphan direction — the shape mutual exclusivity misses.

    ``ghost`` is in neither ``all_kinds`` nor ``representable_kinds``, so the
    PR-gate's own remedy ("the two sets must be mutually exclusive") is satisfied
    and the entry would still linger. Only the set EQUALITY catches it, and only
    from outside the per-kind loop, which iterates observed kinds.
    """

    with pytest.raises(AssertionError, match="NO fixture mentions"):
        _assert_allowlist_is_exactly_the_residual(
            all_kinds={"pr"},
            representable_kinds={"pr"},
            allowlist={"ghost": _SYNTHETIC_ALLOWLIST_ENTRY},
        )


def test_unit_kinds_are_consistent_with_registry() -> None:
    """Sanity: any 'kind' string in a fixture matches a UnitKind value.

    A fixture that names a kind the ledger does not know is a schema drift
    warning — the predicate for it does not exist yet.
    """

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


def test_m5_collision_pin_incident_equivalent_to_valid_wait() -> None:
    """Collision pin for M-5 (stale NEXT: human after PR #237 merged).

    Materialises the incident and a valid-wait counterexample as
    ``ThreadState`` values and asserts the v1 predicate cannot distinguish
    them. This is the machine-readable form of Einstein msg-2749 §2 —
    "incident と反例が等価な `ThreadState` に落ちる".

    Reds when:
        * ``ThreadState`` gains any field carrying the completion time of
          the awaited act (schema layer per Einstein E-9);
        * OR request-level fulfilment lands elsewhere in a shape the
          backtest can plumb through (granularity layer per Einstein E-10);
        * OR the byte-equivalence of the two states below stops holding
          for any reason (a signal the fixture's capture drifted).

    When it reds, revisit the M-5 fixture: the schema now can express the
    incident, and the not-representable verdict is due to supersede.
    """

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

    Reds when:
        * a unit kind (or field on ``ThreadState``) carries a review
          event's timestamp alongside the design thread reference the
          review was meant to relay into (schema layer per Bohr §3(b));
        * OR ``ThreadState`` byte-equivalence between incident and
          valid-wait stops holding, for any reason (fixture drift).

    When it reds, revisit the M-6 fixture: the schema now can express
    the review-vs-thread pair the incident hinges on.
    """

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

    M-7 differs from M-5 / M-6: there is no unit kind at all in which to
    construct the incident-vs-valid-wait pair. The pin therefore asserts
    the ABSENCE of a hold-carrying kind in ``UnitKind``. The trigger is
    the registry growing that kind — which by Bohr §5-2 order is the
    schema decision the M-7 fixture is waiting on.

    Reds when:
        * ``UnitKind`` gains any of the plausible hold-carrying values
          (``hold``, ``ack``, ``control``, ``loop_control``);
        * OR any other value whose semantics carry a (desired_at,
          observed_at) pair lands as a UnitKind. (The list below is
          representative, not exhaustive — a name we did not anticipate
          would slip through, and that is a limit of a spelling-based
          pin. If a schema change adds a differently-named kind for the
          same shape, retire this pin explicitly rather than relying on
          it to catch the rename.)

    When it reds, revisit the M-7 fixture: the kind registry can now
    name the shape the incident carries.
    """

    known = {k.value for k in UnitKind}
    for candidate in ("hold", "ack", "control", "loop_control"):
        assert candidate not in known, (
            f"M-7 collision pin: UnitKind now contains {candidate!r}. The "
            "registry can carry a hold's shape and the M-7 fixture may no "
            "longer belong in the not-representable bucket. Retire this "
            "pin and supersede the fixture."
        )
