"""Which lane of the operator's board a human-parked head belongs in (D7).

Thread: T-next-role-name-stands-down-to-human (Bohr msg-5416 D7, supplemented in msg-5428;
design approved by Einstein msg-5429). Every head that ends ``NEXT: human`` used to be listed as a
decision. Four of the 41 items on the 2026-10-01 board were not decisions at all: they were
``conductor.stand_down unresolved=identity`` notices for a ``NEXT: implementer`` the resolver could
not map (msg-5406). This module splits a parked head into three lanes:

- :attr:`ParkedLane.DECISION` — a Tier-C decision. The default, and where everything not listed
  below stays.
- :attr:`ParkedLane.OPERATOR_WORK` — a validated ``NEXT: operator``: work by hand that the author
  declared to be outside every Tier-C type. ``operator_task`` carries the work.
- :attr:`ParkedLane.MISROUTE` — the conductor's own stand-down for a ``NEXT:`` it could not route
  (a typo, an ambiguous role name, a malformed ``NEXT: operator``). It waits for re-routing, not
  for a decision.

One stand-down is held back in the decision lane: ``operator_tier_c_conflict``. Its author wrote
``TIER-C: <type>`` AND ``NEXT: operator``. The handoff is refused, but the work was declared
Tier-C, so it must stay in front of the human as a decision, marked ``protocol_violation`` so the
reader knows it arrived through a protocol violation (msg-5428 D7 supplement). Losing it into the
misroute lane would be the Tier-C leak msg-5421 closed.

The lane is read from machine facts only: the stand-down's ``mindwire:stop v1`` marker
(:mod:`.stop_marker`) and the ``NEXT:`` grammar's own resolver (:mod:`.handoff`). No prose is
matched. A stand-down notice posted before the marker existed has no marker and stays a decision,
which is how it was listed before this change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .handoff import HandoffKind, HumanAsk, resolve_handoff
from .stand_down import EVENT_KIND_STAND_DOWN, StandDownReason, UnresolvedItem
from .stop_marker import parse_stop_marker


class ParkedLane(StrEnum):
    """The board lane of a human-parked head."""

    DECISION = "decision"
    OPERATOR_WORK = "operator_work"
    MISROUTE = "misroute"


#: Stand-down reasons that mean "the ``NEXT:`` could not be routed" — the misroute lane. Listed
#: rather than derived from ``unresolved=identity`` because one identity reason is NOT a misroute:
#: ``identity_not_spawnable`` is a correct nomination of an identity that has no adapter (a web
#: identity), and ``operator_tier_c_conflict`` stays a decision (module docstring).
MISROUTE_REASONS: frozenset[str] = frozenset(
    {
        StandDownReason.IDENTITY_UNRESOLVED.value,
        StandDownReason.IDENTITY_ROLE_AMBIGUOUS.value,
        StandDownReason.IDENTITY_OPERATOR_NO_TASK.value,
        StandDownReason.OPERATOR_NO_TIER_C_CHECK.value,
    }
)


@dataclass(frozen=True)
class ParkedClassification:
    """The lane, plus what the board shows next to it."""

    lane: ParkedLane
    operator_task: str | None = None
    protocol_violation: bool = False


def classify_parked(body: str) -> ParkedClassification:
    """Classify the body of a head that parks on the human.

    Callers pass heads they have already found to be human-parked; a body that does not park is
    still answered (``DECISION``) rather than raised on, because the caller is a scheduled tick.
    """
    marker = parse_stop_marker(body)
    if (
        marker is not None
        and marker.get("kind") == EVENT_KIND_STAND_DOWN
        and marker.get("unresolved") == UnresolvedItem.IDENTITY.value
    ):
        reason = marker.get("reason")
        if reason == StandDownReason.OPERATOR_TIER_C_CONFLICT.value:
            return ParkedClassification(ParkedLane.DECISION, protocol_violation=True)
        if reason in MISROUTE_REASONS:
            return ParkedClassification(ParkedLane.MISROUTE)
    handoff = resolve_handoff(body, {})
    if handoff.kind is HandoffKind.HUMAN and handoff.human_ask is HumanAsk.OPERATOR_WORK:
        return ParkedClassification(ParkedLane.OPERATOR_WORK, operator_task=handoff.operator_task)
    return ParkedClassification(ParkedLane.DECISION)


__all__ = ["MISROUTE_REASONS", "ParkedClassification", "ParkedLane", "classify_parked"]
