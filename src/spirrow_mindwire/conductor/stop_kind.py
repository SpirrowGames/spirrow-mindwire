"""``facts.stop_kind`` vocabulary for the board's ``stalled`` column (design §18.3, W-4).

Thread: T-agmsg-transport-lessons-readiness-session-claim-board (Bohr msg-5496 W-4, kept in
msg-5498; endorsed by Einstein msg-5497 / msg-5499). §18.2 rule 3 gives a card in column
``stalled`` the state ``facts.stop_kind``. Before this, four ``StopReason`` values that §6.3 routes
to ``stalled`` had no ``stop_kind`` (the gap Heisenberg reported in msg-5249), and the wall-clock
budget adds two more ways to stop. This module closes the set and records, in code, which value
each entry path produces, so a test can check the whole of it.

There is no consumer yet: the board body is frozen (design §F). The tables are here so the
vocabulary has one source the day a reader is written, and so a new ``StopReason`` or a new stop
event cannot be added without deciding its ``stop_kind`` (the exhaustive tests fail until it is).
"""

from __future__ import annotations

from enum import StrEnum

from .core import StopReason
from .run_budget import EVENT_KIND_RUN_KILLED, EVENT_KIND_RUN_TIMEOUT
from .spawn_timeout import EVENT_KIND_SPAWN_TIMEOUT
from .stall import EVENT_KIND_STALLED
from .stand_down import EVENT_KIND_STAND_DOWN


class StopKind(StrEnum):
    """Every value ``facts.stop_kind`` may take (§18.3)."""

    STALLED = "stalled"
    STAND_DOWN = "stand_down"
    SPAWN_TIMEOUT = "spawn_timeout"
    QUARANTINE = "quarantine"
    SILENT = "silent"
    HUMAN_SPIN = "human_spin"
    # W-1 / W-2: the run's wall-clock budget.
    RUN_TIMEOUT = "run_timeout"
    RUN_KILLED = "run_killed"
    # W-4: the StopReasons §6.3 sends to ``stalled``, named after the StopReason itself.
    EMPTY_THREAD = "empty_thread"
    NO_HANDOFF = "no_handoff"
    NO_PROGRESS = "no_progress"
    ROUND_CAP = "round_cap"


#: R-RELAY-STOP (§18.4): the stop marker's ``kind`` → ``stop_kind``. Also the event names the
#: §18.5 log-tail classification looks for on a non-zero exit.
STOP_KIND_BY_EVENT_KIND: dict[str, StopKind] = {
    EVENT_KIND_STALLED: StopKind.STALLED,
    EVENT_KIND_STAND_DOWN: StopKind.STAND_DOWN,
    EVENT_KIND_SPAWN_TIMEOUT: StopKind.SPAWN_TIMEOUT,
    EVENT_KIND_RUN_TIMEOUT: StopKind.RUN_TIMEOUT,
    EVENT_KIND_RUN_KILLED: StopKind.RUN_KILLED,
}

#: Exit-0 runs: the ``StopReason`` on the ``conductor stopped:`` line → ``stop_kind`` when the
#: card lands in ``stalled``. ``None`` = §6.3 does not send this reason to ``stalled``; the comment
#: says where it goes instead. Every member has an entry (a test enforces it).
STOP_KIND_BY_STOP_REASON: dict[StopReason, StopKind | None] = {
    StopReason.HUMAN: None,  # R-HUMAN → ready_for_human (or R-RELAY-STOP via its marker)
    StopReason.SETTLED: None,  # R-NONE-*
    StopReason.HOLD: None,  # waiting
    # Not in §6.3 (added after it). A DEFER, not a stop; the next tick re-derives admission.
    StopReason.CI_WAIT: None,
    # Not in §6.3. Like CI_WAIT: a silent retry of the same head, posting nothing; when it does
    # not clear, the stall watchdog's STALLED notice (StopKind.STALLED) is the stop.
    StopReason.RESUME_RETRY: None,
    # Not in §6.3. A merge waiting on the PR list, not a stall; 1b resumes it.
    StopReason.MERGE_WAIT: None,
    # Not in §6.3. Its notice ends ``NEXT: human``, so it is read as a human stop like HUMAN.
    StopReason.SELF_HANDOFF: None,
    # Not in §6.3 (T-next-operator-is-silent). A valid ``NEXT: operator`` park, not a stall: the
    # head ends on the reserved token, so the board reads it as operator work via ``parked_lane``.
    StopReason.OPERATOR_WORK: None,
    # Its notice carries the stop marker, so R-RELAY-STOP gives the same value.
    StopReason.STALLED: StopKind.STALLED,
    # Never returned by ``Conductor.run``: the run exits 1, and R-QUAR's log tail decides.
    StopReason.ADAPTER_ERROR: None,
    StopReason.EMPTY: StopKind.EMPTY_THREAD,
    # These three reach ``stalled`` only when J-STALL sends them there; when it sends them to
    # ``ready_for_human`` the value is unused.
    StopReason.NO_HANDOFF: StopKind.NO_HANDOFF,
    StopReason.NO_PROGRESS: StopKind.NO_PROGRESS,
    StopReason.ROUND_CAP: StopKind.ROUND_CAP,
}


__all__ = ["STOP_KIND_BY_EVENT_KIND", "STOP_KIND_BY_STOP_REASON", "StopKind"]
