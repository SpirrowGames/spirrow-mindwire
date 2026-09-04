"""Five-class classifier for a stalled unit (D-2', msg-2470 §4).

``class`` is a RECORD ATTRIBUTE, not part of the record's identity (msg-2470 §2-2 —
identity is fixed at ``(unit, stall_epoch_start)``). The scan re-evaluates ``class``
every pass and appends to ``class_history[]`` on transition.

The five classes and their v1 dispositions (from msg-2470 §4, unchanged by later
amendments):

    resumable         — ``next_participant`` is populated and every external prereq
                        holds. Phase 2 auto-wake territory. v1 lists only.
    refireable        — a gate verdict was recorded with an indefinite input (e.g.
                        ``ci=pending``) and CI has since become definitive. The single
                        automatic action v1 permits (D-5').
    externally-blocked — a prerequisite the loop cannot satisfy is missing (M-3's
                        ``DIRTY`` PR, preflight failure, ...). Listed with the
                        specific missing action named; the ledger does NOT auto-fix.
    quarantined       — present in ``quarantine.json``. Never auto-cleared; listed
                        with age + per-signature daily hit count.
    unclassified      — hits none of the above. Listed. This class exists on purpose:
                        the ledger's most important row is the one for a stall shape
                        the classifier does not yet know how to name.

The `unclassified` bucket is the ledger's canary. If it grows over time, that means
either a new stall shape has appeared (and the classifier deserves a new arm), or the
predicate for one of the four named classes is too tight. Both readings tell an
operator to look; the classifier's silence would tell them nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Class(StrEnum):
    RESUMABLE = "resumable"
    REFIREABLE = "refireable"
    EXTERNALLY_BLOCKED = "externally-blocked"
    QUARANTINED = "quarantined"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class ClassifierInput:
    """The features the classifier reads (§4 table).

    Every field is derived from surfaces the sweep already probes — no new
    observation is required.
    """

    is_quarantined: bool = False
    is_externally_blocked: bool = False  # DIRTY PR, preflight failure, ...
    verdict_is_indefinite: bool = False  # gate recorded ci=pending or similar
    ci_became_definitive: bool = False  # since the indefinite verdict was posted
    has_next_participant: bool = False  # `next_participant` field is populated
    external_prereqs_hold: bool = False  # every external requirement is met


def classify(x: ClassifierInput) -> Class:
    """Return the current class for a stalled unit.

    Order matters: ``quarantined`` beats every other class because ``quarantine.json``
    membership is the strongest signal and its remedy is uniformly human-only.
    ``externally-blocked`` beats ``refireable`` because a re-fire against a DIRTY PR
    accomplishes nothing (M-3, msg-2354 §1) — the class must name the actual blocker
    so the human sees "rebase needed", not "gate stuck". ``refireable`` beats
    ``resumable`` because a definitive CI verdict is closer to unblocking the PR than
    a nomination-based wake.
    """

    if x.is_quarantined:
        return Class.QUARANTINED
    if x.is_externally_blocked:
        return Class.EXTERNALLY_BLOCKED
    if x.verdict_is_indefinite and x.ci_became_definitive:
        return Class.REFIREABLE
    if x.has_next_participant and x.external_prereqs_hold:
        return Class.RESUMABLE
    return Class.UNCLASSIFIED
