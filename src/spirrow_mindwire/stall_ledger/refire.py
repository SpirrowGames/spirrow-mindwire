"""Deliverable 4 — the ``refireable`` gate re-fire predicate + head-sha budget.

The v1 rule is a single sentence (msg-2470 §6, msg-2472 §5, msg-2474 §3):

    * The one automatic action v1 permits is a gate re-fire against a record whose
      class is ``refireable`` — that is, an indefinite verdict was recorded and CI
      has since become definitive.
    * The budget is *once per head sha*. Per D-5' and reinforced in msg-2474 §3:
      once an attempt is recorded for a head sha, no further attempt is permitted
      until the head sha advances — including when the previous attempt lost its
      emitted-event ids. ``id-lost`` MUST NOT unlock the budget or the head-sha cap
      is defeated by silent side effects.
    * Executing the remedy is a two-step call: ``prepare_attempt(now, head_sha)``
      appends an ``in-flight`` ``RemedyAttempt`` BEFORE the caller runs the remedy
      (CON-1 record-first ordering); ``finalize_attempt(...)`` records the
      ``emitted_event_ids`` after the caller returns. Splitting the two guarantees
      the ``a.at`` timestamp exists on disk before the remedy can crash.

The predicate is pure — it consumes a ``StallRecord`` and answers yes/no. The actual
gate-refire mechanics (invoking ``naysayer_review.py``, capturing the created review
id, posting the visibility msg) live in the sweep wrapper; this module is the policy
gate, not the transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from spirrow_mindwire.stall_ledger.classifier import Class
from spirrow_mindwire.stall_ledger.model import (
    EmittedEventIds,
    RemedyAttempt,
    RemedyOutcome,
    RemedyState,
    StallRecord,
)


@dataclass(frozen=True)
class RefireBudget:
    """The result of consulting the budget for a candidate remedy."""

    allowed: bool
    reason: str


def budget_for_head_sha(record: StallRecord, head_sha: str) -> RefireBudget:
    """Return whether a refire is permitted against ``head_sha`` for this record.

    A previous attempt against the SAME head_sha consumes the budget regardless of
    its outcome — an ``id-lost`` attempt still exists on disk and still emitted
    whatever it emitted, so counting it as "not attempted" would let one head
    accumulate arbitrary side effects (msg-2474 §3).

    An attempt against a DIFFERENT head sha does not consume this head's budget —
    the head sha is the axis the budget is keyed on. This is the whole reason the
    field exists on the attempt.
    """

    for attempt in record.remedy_attempts:
        if attempt.head_sha == head_sha:
            return RefireBudget(
                allowed=False,
                reason=(
                    f"already attempted for head_sha={head_sha}"
                    f" (attempt state={attempt.state.value})"
                ),
            )
    return RefireBudget(allowed=True, reason="no prior attempt for this head_sha")


def should_refire(record: StallRecord, head_sha: str) -> RefireBudget:
    """The gate the sweep asks before invoking the actual remedy.

    Two conditions must hold:
        1. The record's current class is ``refireable`` (CI has become definitive
           since an indefinite verdict was recorded).
        2. The head sha's budget is still available.
    """

    if record.klass != Class.REFIREABLE.value:
        return RefireBudget(
            allowed=False,
            reason=f"class is '{record.klass}', not '{Class.REFIREABLE.value}'",
        )
    return budget_for_head_sha(record, head_sha)


def prepare_attempt(record: StallRecord, now: datetime, head_sha: str) -> RemedyAttempt:
    """CON-1: append the attempt BEFORE the remedy executes.

    Returns the newly-appended attempt object; the caller runs the remedy, captures
    the emitted event ids, then calls ``finalize_attempt`` to close the record loop.
    If the caller crashes between the two calls, the attempt sits at ``in-flight``
    until INV-4 expiry kicks in (see ``expire_in_flight``).
    """

    attempt = RemedyAttempt(
        at=now,
        kind="gate-refire",
        head_sha=head_sha,
        state=RemedyState.IN_FLIGHT,
    )
    record.remedy_attempts.append(attempt)
    return attempt


def finalize_attempt(
    attempt: RemedyAttempt,
    *,
    github_review_ids: tuple[str, ...] = (),
    chatroom_msg_ids: tuple[str, ...] = (),
    outcome: RemedyOutcome = "unknown",
) -> None:
    """Close out a prepared attempt with what the remedy actually emitted.

    If the caller reaches this function without any emitted ids, the attempt goes
    to ``id-lost`` rather than ``completed`` — msg-2474 §2 is explicit that
    ``id-lost`` is a *permanent* record, not a stepping stone to ``completed``, so
    we set it here and never rewrite it (the ledger will later update ``outcome``
    to ``state-changed`` if the world moved, but ``state`` stays ``id-lost``).
    """

    attempt.emitted_event_ids = EmittedEventIds(
        github_review_ids=tuple(github_review_ids),
        chatroom_msg_ids=tuple(chatroom_msg_ids),
    )
    attempt.outcome = outcome
    if attempt.emitted_event_ids.is_empty():
        attempt.state = RemedyState.ID_LOST
    else:
        attempt.state = RemedyState.COMPLETED


def expire_in_flight(attempt: RemedyAttempt, now: datetime) -> bool:
    """INV-4 + msg-2474 §3: an ``in-flight`` attempt older than ``T_inflight`` past
    ``a.at`` transitions to ``id-lost`` and is flagged.

    Returns ``True`` iff the attempt transitioned. Comparing ``now`` against
    ``attempt.at`` is a same-clock comparison, so no skew margin applies — CON-2.
    """

    from spirrow_mindwire.stall_ledger.model import T_INFLIGHT

    if attempt.state != RemedyState.IN_FLIGHT:
        return False
    if now <= attempt.at + T_INFLIGHT:
        return False
    attempt.state = RemedyState.ID_LOST
    attempt.flags = (*attempt.flags, "inflight-expired")
    return True
