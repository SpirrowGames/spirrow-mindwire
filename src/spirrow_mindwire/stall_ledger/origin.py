"""``origin`` predicate — the one place authorship / id membership enters the ledger.

Rule of composition (verbatim, msg-2476 §1 = the final settled form):

    origin(event, unit) :=
        loop        if event.id in union(unit.remedy_attempts[*].emitted_event_ids)
        loop        if there exists a in unit.remedy_attempts :
                         a.state in {in-flight, id-lost}
                     and a.at - T_skew <= event.at <= a.at + T_uncertain
        participant otherwise

Rules of composition (msg-2472 §2-4 + msg-2474 §1 + msg-2476 §1):

    * The predicate is the sole consumer of ``emitted_event_ids`` (INV-1'). Nothing
      else in the ledger reads it, and nothing else in the ledger reads authorship.
    * The default when the two loop-clauses miss is ``participant``. The safety of
      that default rests on ``emitted_event_ids`` being written by the same process
      that emitted the event — msg-2472 §2-3, "the writer and reader are the same
      process, so completeness is self-guaranteed." An event that MIGHT be ours but
      whose id we did not capture is caught by the origin-uncertain window (clause 2),
      not by the default.
    * ``T_skew`` is applied only on the LOWER bound. Widening the upper bound would
      catch participant motion after the remedy's window is physically over, which is
      the quiet failure direction (msg-2476 §1). Widening the lower bound catches
      remote events that appear to precede our local ``a.at`` because our clock is
      ahead — the noisy failure direction (E-106).
"""

from __future__ import annotations

from spirrow_mindwire.stall_ledger.model import (
    T_SKEW,
    T_UNCERTAIN,
    Event,
    OriginKind,
    RemedyState,
    StallRecord,
)


def origin(event: Event, record: StallRecord) -> OriginKind:
    """Return whether ``event`` is loop-caused or participant-caused for this record.

    The signature takes a full ``StallRecord`` (not just its ``remedy_attempts``)
    because origin is asked *per unit*: an event can be participant motion for unit A
    while being loop motion for unit B, if only B has a matching in-flight attempt.
    """

    # Clause 1 — id membership. This clause is the terminal answer whenever it fires;
    # id membership beats every clock-based judgement because it does not depend on the
    # clocks agreeing.
    for attempt in record.remedy_attempts:
        if event.id in attempt.emitted_event_ids:
            return OriginKind.LOOP

    # Clause 2 — origin-uncertain window. Only ``in-flight`` and ``id-lost`` attempts
    # keep the window open; a ``completed`` attempt has its ids known, so clause 1
    # handles it directly and clause 2 must not linger past it (that would treat known-
    # participant events as loop-caused, which is the quiet failure direction).
    for attempt in record.remedy_attempts:
        if attempt.state == RemedyState.COMPLETED:
            continue
        lower = attempt.at - T_SKEW
        upper = attempt.at + T_UNCERTAIN
        if lower <= event.at <= upper:
            return OriginKind.LOOP

    # Default — participant. Msg-2472 §2-3: this is the noisy-failure direction only
    # because ``emitted_event_ids`` completeness is self-guaranteed by CON-1.
    return OriginKind.PARTICIPANT
