"""Resuming the implementer from a PR-gate post left at the head of a thread.

Thread: T-sweep-starves-deep-candidates. Design: Bohr msg-6316 (PR-A) as amended by msg-6318
(four outcomes), after Einstein msg-6317's objection; endorsed by Einstein (msg-6317, final go).

Why this exists
---------------

Carve-out ② (the PR-gate fix relay) used to exist only inside one conductor run: the run that
fired the gate dispatched the implementer straight from the verdict ``fire_pr_review`` returned.
If a run ever ended with the relay (or the R4 ci-route post) still at the head, the next run
handed that head to ``Conductor._route``. Its author is ``pr-gate-relay`` — neither the human nor
the naysayer — so guard (i) redirected it to the human, after a forced naysayer consult. The
implementer was never started (reproduced in msg-6315).

This module lets the next run pick the head up again, but **never on the author name**. The
chatroom accepts any author string (see :mod:`.gate_records`), so trusting ``pr-gate-relay``
here would open guard (i) to anyone who posts under it. What is trusted instead is what GitHub
says now, read back (msg-557: trust the deterministic result, never an author). A forged post
gains nothing: the implementer starts only when the facts that would have started it from a
genuine post are true on the PR's current head.

The four outcomes (msg-6318)
----------------------------

=================  ==========================================  ================================
outcome            when                                        what the conductor does
=================  ==========================================  ================================
``VERIFIED``       the post's SHA is the PR head, and (RC) the  dispatch the implementer
                   gate login's latest review on that SHA is
                   CHANGES_REQUESTED / (R4) the rollup on that
                   SHA is red
``UNAVAILABLE``    a read failed (network, rate limit, 5xx,    stop ``resume_retry``: post
                   timeout, unparseable answer)                nothing, retry next tick
``STALE``          the post's SHA is not the PR head (a newer   stop ``resume_retry``
                   push the thread has not caught up with)
``CONTRADICTED``   same SHA, but the facts disagree, or a       fall through to ``_route``
                   source needed to check is not wired          (guard (i) → human)
=================  ==========================================  ================================

Only ``VERIFIED`` opens guard (i). ``UNAVAILABLE`` / ``STALE`` are bounded by the existing stall
watchdog (:mod:`.stall`): ``resume_retry`` posts nothing, so the same head is launched again and
counted, and the third launch stands down with a STALLED notice. No retry state is kept here.

The resume never calls :func:`~spirrow_mindwire.gate_admission.gate_admission`. It only reads the
rollup, so resuming after R4 is not counted as a second red on the head (R5); R5 is still judged
on the next ``NEXT: pr-review`` after the implementer's turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from ..gate_admission import rollup_is_red
from ..github.client import ReviewEvent, parse_pr_ref
from ..github.reviews import parse_verdict_footer
from .gate_records import (
    RELAY_AUTHOR,
    admission_heading_pr_ref,
    ci_route_heads,
    normalize_sha,
    relay_heading_pr_ref,
)

if TYPE_CHECKING:
    from ..github.client import CheckRollup, PrRef, ReviewInfo

logger = logging.getLogger(__name__)

#: GitHub's review state for a formal REQUEST_CHANGES.
_CHANGES_REQUESTED = "CHANGES_REQUESTED"


class ResumeKind(StrEnum):
    """Which PR-gate post the head is."""

    REQUEST_CHANGES = "request_changes"  # (i) a verdict relay whose footer says REQUEST_CHANGES
    CI_ROUTE = "ci_route"  # (ii) the R4 ci-route post (carries the ci-route marker)


class ResumeOutcome(StrEnum):
    """The four outcomes of :func:`verify_resume` (msg-6318)."""

    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    CONTRADICTED = "contradicted"


@dataclass(frozen=True, kw_only=True, slots=True)
class ResumeCandidate:
    """A head that may resume the implementer, pending :func:`verify_resume`."""

    kind: ResumeKind
    pr: PrRef
    head_sha: str  # normalised; the SHA the post was about


@dataclass(frozen=True, kw_only=True, slots=True)
class ResumeCheck:
    """What :func:`verify_resume` found. ``detail`` is for the log line, never parsed."""

    outcome: ResumeOutcome
    detail: str
    pr_head: str | None = None


class ReviewSource(Protocol):
    """The one read the RC resume needs: every submitted review on a PR.

    Must RAISE when the reviews could not be read. An empty list means "read, and there are
    none", which is CONTRADICTED; a failed read is UNAVAILABLE. The fail-soft
    ``GitHubClient.fetch_pr_reviews`` collapses the two and must not be used here; the real
    client's ``fetch_pr_reviews_strict`` has this contract.
    """

    async def fetch_pr_reviews_strict(self, pr: PrRef) -> list[ReviewInfo]: ...


class RollupSource(Protocol):
    """Structural twin of ``core.CheckRollupSource`` (``None`` = could not read)."""

    async def fetch_check_rollup(self, pr: PrRef) -> CheckRollup | None: ...


def resume_candidate(*, author: str, body: str, names_implementer: bool) -> ResumeCandidate | None:
    """Is this head a PR-gate post the implementer may be resumed from? ``None`` = no.

    All of: authored by :data:`~.gate_records.RELAY_AUTHOR`; its handoff names the implementer
    (``names_implementer``, resolved by the caller through the roster); and either (i) a relay
    heading plus exactly one verdict footer with ``event=REQUEST_CHANGES``, or (ii) an admission
    heading plus exactly one ci-route marker. The author test is noise rejection only — what
    opens guard (i) is :func:`verify_resume`, never this function. A STALLED or other notice
    under the same author ends ``NEXT: human`` and is rejected by ``names_implementer``.
    """
    if author != RELAY_AUTHOR or not names_implementer:
        return None
    raw_ref = relay_heading_pr_ref(body)
    if raw_ref is not None:
        footer = parse_verdict_footer(body)
        pr = parse_pr_ref(raw_ref)
        if footer is None or pr is None or footer[1] is not ReviewEvent.REQUEST_CHANGES:
            return None
        sha = normalize_sha(footer[0])
        if not sha:
            return None
        return ResumeCandidate(kind=ResumeKind.REQUEST_CHANGES, pr=pr, head_sha=sha)
    raw_ref = admission_heading_pr_ref(body)
    if raw_ref is not None:
        heads = ci_route_heads([body])
        pr = parse_pr_ref(raw_ref)
        if pr is None or len(heads) != 1:
            return None
        return ResumeCandidate(kind=ResumeKind.CI_ROUTE, pr=pr, head_sha=next(iter(heads)))
    return None


def _latest_gate_review(reviews: list[ReviewInfo], *, login: str, sha: str) -> ReviewInfo | None:
    """The gate login's most recent review on ``sha``; list order breaks submitted_at ties."""
    mine = [
        r for r in reviews if r.login == login and r.commit_id and normalize_sha(r.commit_id) == sha
    ]
    if not mine:
        return None
    # ``max`` keeps the first maximum; reverse so the LATER entry wins a tie.
    return max(reversed(mine), key=lambda r: r.submitted_at or "")


async def verify_resume(
    candidate: ResumeCandidate,
    *,
    rollup_source: RollupSource | None,
    review_source: ReviewSource | None,
    review_login: str,
) -> ResumeCheck:
    """Read GitHub and sort ``candidate`` into one of the four outcomes. Never raises.

    The PR head is read from the rollup for both kinds, so both share one "is this still the
    head?" test. A missing source is CONTRADICTED, not UNAVAILABLE: it is configuration, and
    retrying cannot fix it (msg-6318).
    """
    if rollup_source is None:
        return ResumeCheck(outcome=ResumeOutcome.CONTRADICTED, detail="rollup_source_not_wired")
    if candidate.kind is ResumeKind.REQUEST_CHANGES and review_source is None:
        return ResumeCheck(outcome=ResumeOutcome.CONTRADICTED, detail="review_source_not_wired")
    try:
        rollup = await rollup_source.fetch_check_rollup(candidate.pr)
    except Exception as exc:  # a source that raises is a failed read, never a verdict
        return ResumeCheck(outcome=ResumeOutcome.UNAVAILABLE, detail=f"rollup: {exc!r}")
    if rollup is None:
        return ResumeCheck(outcome=ResumeOutcome.UNAVAILABLE, detail="rollup unread")
    pr_head = normalize_sha(rollup.head_sha)
    if pr_head != candidate.head_sha:
        return ResumeCheck(outcome=ResumeOutcome.STALE, detail="head moved", pr_head=pr_head)
    if candidate.kind is ResumeKind.CI_ROUTE:
        if rollup_is_red(rollup.rows):
            return ResumeCheck(outcome=ResumeOutcome.VERIFIED, detail="rollup red", pr_head=pr_head)
        return ResumeCheck(
            outcome=ResumeOutcome.CONTRADICTED, detail="rollup_not_red", pr_head=pr_head
        )
    assert review_source is not None  # checked above for REQUEST_CHANGES
    try:
        reviews = await review_source.fetch_pr_reviews_strict(candidate.pr)
    except Exception as exc:
        return ResumeCheck(
            outcome=ResumeOutcome.UNAVAILABLE, detail=f"reviews: {exc!r}", pr_head=pr_head
        )
    latest = _latest_gate_review(reviews, login=review_login, sha=candidate.head_sha)
    if latest is None:
        return ResumeCheck(
            outcome=ResumeOutcome.CONTRADICTED, detail="no_gate_review_on_head", pr_head=pr_head
        )
    if latest.state != _CHANGES_REQUESTED:
        return ResumeCheck(
            outcome=ResumeOutcome.CONTRADICTED,
            detail=f"latest_gate_review_is_{latest.state.lower() or 'unknown'}",
            pr_head=pr_head,
        )
    return ResumeCheck(outcome=ResumeOutcome.VERIFIED, detail="changes requested", pr_head=pr_head)


def log_resume_check(candidate: ResumeCandidate, check: ResumeCheck) -> None:
    """The three WARN lines msg-6316 / msg-6318 name, and an INFO line on VERIFIED."""
    pr = candidate.pr.slug
    if check.outcome is ResumeOutcome.VERIFIED:
        logger.info(
            "conductor.gate_resume.verified pr=%s kind=%s sha=%s",
            pr,
            candidate.kind.value,
            candidate.head_sha,
        )
    elif check.outcome is ResumeOutcome.STALE:
        logger.warning(
            "conductor.gate_resume.stale pr=%s relay_sha=%s pr_head=%s",
            pr,
            candidate.head_sha,
            check.pr_head,
        )
    elif check.outcome is ResumeOutcome.UNAVAILABLE:
        logger.warning("conductor.gate_resume.unavailable pr=%s err=%s", pr, check.detail)
    else:
        logger.warning(
            "conductor.gate_resume.unverified pr=%s reason=%s sha=%s",
            pr,
            check.detail,
            candidate.head_sha,
        )


__all__ = [
    "ResumeCandidate",
    "ResumeCheck",
    "ResumeKind",
    "ResumeOutcome",
    "ReviewSource",
    "RollupSource",
    "log_resume_check",
    "resume_candidate",
    "verify_resume",
]
