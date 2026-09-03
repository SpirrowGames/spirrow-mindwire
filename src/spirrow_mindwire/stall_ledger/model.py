"""Ledger domain model — units, events, remedy attempts, and stall records.

The record schema is Bohr's msg-2470 §5-1 as amended by msg-2472 §3 (``emitted_event_ids``
under each remedy attempt) and msg-2474 §4 (``state`` field replacing the standalone
``remedy_in_flight`` flag; ``outcome`` values including ``state-changed`` / ``unknown``).

The ``a.at`` field of each remedy attempt is *load-bearing*: it is timestamped by the
loop's local clock BEFORE the remedy executes (CON-1), so the expiry the scan uses to
release an ``in-flight`` attempt is derivable even if the remedy call itself never
returns. That is the whole point of writing the attempt before executing it — the
symmetric "write after remedy returned" ordering has no time-based exit on remedy crash
and would silently latch the way M-2 already does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

# ─── Time windows (msg-2474 §2, msg-2476 §1) ───────────────────────────────────────────
#
# T_INFLIGHT and T_UNCERTAIN measure the *same physical delay* — how long a remedy's side
# effects take to appear on GitHub / chatroom — with two different clock pairings.
# T_INFLIGHT compares two local timestamps (loop's now vs. ``a.at``) so it takes no skew
# margin; T_UNCERTAIN compares a local ``a.at`` against a remote ``event.at`` and the
# lower bound is opened by T_SKEW (msg-2476 §1).
#
# W-3 / W-8: neither value is a measurement of this environment. 30 min is a comfortable
# upper bound on ``naysayer_review.py``'s observed runtime; 5 min is the customary
# tolerance for cross-host clock skew when NTP is degraded but not broken. Deliverable 1
# collects the raw data (``a.at`` vs. each entry in ``emitted_event_ids``) that will
# retire both values with a measurement.

T_INFLIGHT: timedelta = timedelta(minutes=30)
T_UNCERTAIN: timedelta = timedelta(minutes=30)  # invariant: T_UNCERTAIN == T_INFLIGHT
T_SKEW: timedelta = timedelta(minutes=5)


class UnitKind(StrEnum):
    """Units the ledger enumerates. Same values as the identifier prefixes in §3-2."""

    PR = "pr"
    THREAD = "thread"
    QUARANTINE = "quarantine"


class OriginKind(StrEnum):
    """The two values the ``origin`` predicate produces (msg-2472 §2-1)."""

    LOOP = "loop"
    PARTICIPANT = "participant"


class RemedyState(StrEnum):
    """Attempt lifecycle (msg-2474 §3, msg-2472 CON-1).

    ``in-flight``   — appended BEFORE the remedy executes; excluded from scan while so.
    ``completed``   — remedy returned and its ``emitted_event_ids`` are recorded.
    ``id-lost``     — the remedy is over (either returned without ids, or its inflight
                      window expired) but we could not capture what it emitted. The
                      origin-uncertain window keeps its footprint out of participant
                      motion for T_UNCERTAIN past ``a.at`` (msg-2474 §2).
    """

    IN_FLIGHT = "in-flight"
    COMPLETED = "completed"
    ID_LOST = "id-lost"


RemedyOutcome = Literal["no-state-change", "state-changed", "unknown"]
RemedyKind = Literal["gate-refire"]  # v1 only permits one remedy (D-5')


@dataclass(frozen=True)
class Unit:
    """A ``(kind, id)`` unit the ledger reasons about (§3-2)."""

    kind: UnitKind
    identifier: str  # e.g. "spirrow-mindwire#206", "spirrow-mindwire/T-foo", ...

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.identifier}"


@dataclass(frozen=True)
class EmittedEventIds:
    """Ids the loop knows it emitted (msg-2472 §2-1).

    One remedy tick can produce BOTH a GitHub review AND a chatroom msg (the visibility
    post), so the tracking has to admit both. Storing them under separate fields (rather
    than one merged list) preserves the distinction that lets the origin predicate stay
    a set-membership check without needing to know which surface an event came from.
    """

    github_review_ids: tuple[str, ...] = ()
    chatroom_msg_ids: tuple[str, ...] = ()

    def __contains__(self, event_id: str) -> bool:
        return event_id in self.github_review_ids or event_id in self.chatroom_msg_ids

    def is_empty(self) -> bool:
        return not self.github_review_ids and not self.chatroom_msg_ids


@dataclass
class RemedyAttempt:
    """One record-then-execute attempt (msg-2472 CON-1, msg-2474 §3).

    ``at`` is written BEFORE execution. That is the whole point of the record-first
    ordering: if the remedy never returns, the expiry clock still has an origin.
    """

    at: datetime  # loop's local clock, written before the remedy executes
    kind: RemedyKind
    head_sha: str  # once-per-head-sha budget lives here (D-5')
    state: RemedyState = RemedyState.IN_FLIGHT
    emitted_event_ids: EmittedEventIds = field(default_factory=EmittedEventIds)
    outcome: RemedyOutcome = "unknown"
    flags: tuple[str, ...] = ()

    def is_open_window(self, now: datetime) -> bool:
        """The origin-uncertain window is open while ``state`` may still catch events
        the loop caused.

        - ``in-flight``: the remedy has not returned, so we do not know which events
          are ours yet.
        - ``id-lost``: the remedy IS over but we lost track of what it emitted; the
          window keeps events near ``a.at`` out of participant motion.
        - ``completed``: the ids are known and the window is unnecessary — set-
          membership on ``emitted_event_ids`` handles origin directly.

        The window has an absolute upper bound at ``a.at + T_UNCERTAIN``.
        """

        if self.state == RemedyState.COMPLETED:
            return False
        return now <= self.at + T_UNCERTAIN


@dataclass
class Event:
    """The two motion-relevant events the scan sees (§3-3, msg-2472 §2-2).

    ``at`` is the timestamp *the remote surface* wrote — for a GitHub review, its
    ``submitted_at``; for a chatroom msg, the msg's ``created_at``. It is NOT the loop's
    clock. That difference is the entire reason CON-2 exists and T_SKEW enters the
    origin window's lower bound.
    """

    id: str  # the surface's id (review id, msg id, head sha, etc.)
    at: datetime  # the remote surface's clock

    # "author" is deliberately absent from origin judgement (msg-2472 §2-1 replaces
    # author-based rules with id-based). The scan may still read it for display; the
    # ledger does not consult it for motion or close.


@dataclass
class ClassEntry:
    """One row in a record's ``class_history[]`` (msg-2470 §5-1)."""

    at: datetime
    klass: str  # one of ``Class`` values, but stored as string for JSON round-trips


@dataclass
class StallRecord:
    """The ledger's record type (msg-2470 §5-1, amended by msg-2472/2474/2476).

    Identity is ``(unit, stall_epoch_start)`` — msg-2470 §2-2. Neither the class nor the
    condition-first-seen-at is part of identity, so a record does NOT rejuvenate when
    the class is re-evaluated. This is what INV-3 (loop remedies do not rejuvenate)
    protects; the choice of identity is what makes the protection derivable.
    """

    unit: Unit
    stall_epoch_start: datetime  # never rewritten after the record is opened
    condition_first_seen_at: datetime
    klass: str  # current class (from Class values); rewritten each scan
    class_history: list[ClassEntry] = field(default_factory=list)
    remedy_attempts: list[RemedyAttempt] = field(default_factory=list)
    ladder_stage: str = "initial"
    flags: list[str] = field(default_factory=list)
    evidence: dict[str, object] = field(default_factory=dict)

    @property
    def record_id(self) -> str:
        return f"{self.unit.key}@{self.stall_epoch_start.isoformat()}"

    def age(self, now: datetime) -> timedelta:
        """The record's age. Does not shrink when a remedy attempt appends (INV-3)."""

        return now - self.stall_epoch_start
