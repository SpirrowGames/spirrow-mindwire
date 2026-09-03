"""Stall ledger — level-triggered detector for units whose next actor was never summoned.

The spec is the ``T-stalled-pr-has-no-detector`` design thread; the concrete confirmations
this module implements are Bohr's msg-2470 (D-1'/D-2'/D-4'/D-5' + §7 differential
definition) as amended by msg-2472 (§3-3 replaced with origin-by-emitted-event-id),
msg-2474 (INV-4 + CON-1 + T_uncertain/T_inflight + origin-uncertain window), and
msg-2476 (T_skew + CON-2 + window lower-bound margin). Einstein's approval to move to
implementation is msg-2477 (``NEXT: Heisenberg``).

Deliverables carried here (v1' §8, the three that do not depend on the sibling
``T-digest-exceeds-discord-limit-and-is-dropped`` landing):

    #1 — stall ledger (§3 predicates · §5 record · §4 classification)
    #4 — ``refireable`` auto-refire (D-5' single automatic remedy)
    #6 — ``failure_class`` extraction + persistence at quarantine time

All three share one asymmetry, from msg-2354 §5 forward: *quiet failure is worse than
noisy failure*. Every default in this module is picked so that when the module is
uncertain, the record it produces is louder — not that it is silently dropped.

The design's load-bearing invariants (msg-2470 §2-2, msg-2472 §2-4, msg-2474 §1,
msg-2476 §3), verbatim:

    INV-0   Existing notifications report units whose state has CHANGED (edge-triggered).
            This ledger reports units whose state has NOT changed (level-triggered).

    INV-1'  ``origin`` (loop vs. participant) affects only whether
            ``last_participant_motion_at`` advances. It does not enter any other judgement.

    INV-2   ``close`` is origin-blind AND author-blind. A record closes when
            (a) ``needs_actor(unit)`` becomes false, or (b) ``last_participant_motion_at``
            exceeds ``stall_epoch_start``. Neither condition mentions who did what.

    INV-3   The loop's own remedies never rejuvenate a record. If a remedy actually
            changes the world's state the record closes on INV-2; if it doesn't, the
            record's age keeps growing and the notification ladder keeps escalating.

    INV-4   Any state that removes a unit from scan, or that suspends the default
            origin rule, MUST have a time-based automatic exit. Its expiry clock is
            timestamped BEFORE the dangerous operation runs.

    CON-1   ``remedy_attempts`` are appended BEFORE the remedy executes. The scan
            excludes an attempt only while ``state='in-flight'`` and the exclusion
            expires at ``a.at + T_inflight`` regardless of whether the remedy replied.

    CON-2   When two timestamps come from different clocks, the comparison MUST
            include a skew margin, and the margin is applied only on the side where
            widening it fails loudly. Same-clock comparisons take no margin.

The three windows the design distinguishes (msg-2476 §2):

    T_inflight = T_uncertain = 30 min   — same physical delay measured with two clocks
    T_skew                    = 5 min   — different clocks; lower bound only
"""

from spirrow_mindwire.stall_ledger.classifier import Class, classify
from spirrow_mindwire.stall_ledger.failure_class import classify_failure
from spirrow_mindwire.stall_ledger.model import (
    T_INFLIGHT,
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
)
from spirrow_mindwire.stall_ledger.origin import origin
from spirrow_mindwire.stall_ledger.predicates import N_THRESHOLDS, stalled
from spirrow_mindwire.stall_ledger.refire import RefireBudget, should_refire

__all__ = [
    "N_THRESHOLDS",
    "T_INFLIGHT",
    "T_SKEW",
    "T_UNCERTAIN",
    "Class",
    "EmittedEventIds",
    "Event",
    "OriginKind",
    "RefireBudget",
    "RemedyAttempt",
    "RemedyState",
    "StallRecord",
    "Unit",
    "UnitKind",
    "classify",
    "classify_failure",
    "origin",
    "should_refire",
    "stalled",
]
