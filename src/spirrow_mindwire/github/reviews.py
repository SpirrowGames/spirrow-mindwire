"""Review-set predicates on ``list[ReviewInfo]`` — the ONE definition, three call sites.

This module exists to keep :func:`landed` from being redefined across the naysayer
driver (round cap, ``_skip_unchanged_response``, and the chatroom-replay discharge
check). All three ask the same shape of question — "does the review-set contain a
review that is *ours*, on *this head SHA*, in one of *these states*?" — and only
the ``states`` argument differs (T-gate-review-submit-failure-handling DESIGN v3
§2). Putting the predicate in one place makes that shared shape visible; the
``states`` parameter makes the intentional difference between the three sites
explicit rather than being folded into three near-identical inline loops.

Three-valued: :class:`LandedState` distinguishes ``LANDED`` from ``NOT_LANDED``
from ``UNKNOWN``. The distinction is load-bearing for D-7 (env-terminal reads):
a call site that cannot see the review-set MUST NOT collapse UNKNOWN into
NOT_LANDED, or the dedup guard will vanish in exactly the moment it is most
needed (a fail-soft read that returned ``[]`` because of a 401 would otherwise
authorise a re-POST that duplicates a landed verdict). The upstream fetcher
signals "I could not read" by passing ``reviews=None``; a real empty list still
means "asked and answered, nothing there".
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from .client import ReviewInfo


class LandedState(StrEnum):
    """Whether a verdict has been recorded on GitHub for a given (head, login, states).

    ``LANDED`` — a matching review is present in the read set.
    ``NOT_LANDED`` — the read succeeded and no matching review is present.
    ``UNKNOWN`` — the caller could not read the review set (transport error,
    401 on a fail-soft read, etc). Callers MUST NOT treat this as
    ``NOT_LANDED``; treating it that way lets the dedup guard fail open at
    exactly the moment (a terminal read failure) when a duplicate POST is most
    likely.
    """

    LANDED = "landed"
    NOT_LANDED = "not_landed"
    UNKNOWN = "unknown"


def landed(
    reviews: list[ReviewInfo] | None,
    *,
    head_sha: str | None,
    login: str,
    states: Iterable[str],
) -> LandedState:
    """Has a review matching ``(head_sha, login, states)`` already landed on GitHub?

    ``reviews`` is the read set from
    :meth:`~spirrow_mindwire.github.client.GitHubReviewClient.fetch_pr_reviews`.
    Passing ``None`` (the read failed / was not attempted) → :attr:`LandedState.UNKNOWN`.
    Passing an empty list (the read succeeded, no reviews yet) → :attr:`NOT_LANDED`.

    ``head_sha=None`` is also UNKNOWN — the CI-status path can produce a null
    head (never confirmed against a commit), and the same reasoning applies:
    without a head to compare against, we cannot say a landed review belongs to
    *this* head. The T-gate-silently-suppresses-approve-on-truncated-diff round
    of PR-gate work already learned that discarding this distinction produces
    false-positive matches.

    ``states`` is deliberately per-caller (DESIGN v3 §2):

    - round cap and ``_skip_unchanged_response`` use ``_VERDICT_STATES`` (they
      count Gemini-spent verdicts; a COMMENT-only fallback did not consume a
      round).
    - the chatroom-replay discharge check uses the FULL state set (including
      COMMENT), because it asks "did our write reach GitHub" — a COMMENT
      fallback that landed IS a discharge, and treating it as un-landed would
      loop APPROVE→422→COMMENT indefinitely, per DESIGN v3 §2.

    A review missing its ``commit_id`` on the wire is treated as NOT this head
    (fail-safe: a review that cannot be pinned to a commit cannot discharge
    a specific head).
    """
    if reviews is None or head_sha is None:
        return LandedState.UNKNOWN
    state_set = frozenset(states)
    for r in reviews:
        if r.login != login:
            continue
        if r.state not in state_set:
            continue
        if r.commit_id is None or r.commit_id != head_sha:
            continue
        return LandedState.LANDED
    return LandedState.NOT_LANDED


__all__ = [
    "LandedState",
    "landed",
]
