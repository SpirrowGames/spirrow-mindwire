"""Review-set predicates + receipt/footer helpers for the PR-review write path.

Two responsibilities, one module:

1. :func:`landed` — the ONE definition of "has a matching review already landed
   on GitHub" (three call sites: round-cap accounting, ``_skip_unchanged_response``,
   and the chatroom-replay discharge check). The predicate's ``states`` argument
   is per-caller (DESIGN v3 §2 corrected in msg-1990: round cap is NOT a caller of
   :func:`landed`; its head-independent ``sum(...)`` on prior verdict reviews is a
   different question — a per-PR spend cap, not a per-head landed check).
2. :class:`ReviewReceipt` / :func:`append_verdict_footer` / :func:`parse_verdict_footer`
   — the SINGLE producer/consumer of the ``<!-- mindwire:verdict head_sha=<sha>
   event=<EVENT> -->`` HTML-comment sentinel the chatroom-replay path uses as its
   version gate and machine-readable payload (DESIGN v3 Q5-A: HTML comment
   sentinel, both ``head_sha`` AND ``event`` in the footer, one generation site).
   Putting these next to :func:`landed` keeps footer construction / parsing / the
   dedup predicate under one roof — a body posted here is the same body a replay
   consumer reads, so the round-trip is verified by tests that touch one file.

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

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .client import ReviewEvent, ReviewInfo


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


# ── Verdict-footer sentinel (DESIGN v3 Q5-A) ────────────────────────────────
#
# The single HTML-comment sentinel the chatroom-replay path consumes. Two
# properties that matter (DESIGN v3 §1):
#
#   - **HTML comment**: invisible in a rendered chatroom / GitHub review body,
#     and a shape the review model is exceedingly unlikely to emit on its own.
#     Random prose collisions on ``head_sha=<sha> event=EVENT`` are excluded
#     structurally, not statistically.
#   - **Machine-readable head_sha AND event**: replay does NOT re-parse the
#     model's VERDICT line to know what to POST — the exact ``event`` we
#     ATTEMPTED to submit last time (post-degrade / pre-fallback) is captured
#     in the footer. This is what closes the "REQUEST_CHANGES quoted as APPROVE"
#     class of error the msg-1987 Q5-A rationale calls out as directional.
#
# ``_VERDICT_FOOTER_RE`` is the ONE parser; :func:`parse_verdict_footer` is the
# ONE call site. A body with multiple matches is UNKNOWN (return ``None``) —
# safer to skip replay than to guess which match is "the" verdict when the
# invariant "exactly one footer per body" no longer holds. Same fail-safe
# direction as :class:`LandedState.UNKNOWN`: when in doubt, do not POST.
_VERDICT_FOOTER_RE = re.compile(
    r"<!-- mindwire:verdict head_sha=([A-Fa-f0-9]{7,40}) "
    r"event=(APPROVE|REQUEST_CHANGES|COMMENT) -->"
)

# The head_sha length the footer emits. 40 = full SHA-1 (GitHub commit ids).
# Truncated shas are ACCEPTED by the parser (>=7) so a body carrying a short
# sha (a debounce-skip body uses ``head[:12]``) is still replay-safe as long as
# the short sha unambiguously identifies the current head at the time of
# replay; the ``landed()`` check keys off the actual review's ``commit_id``
# which is always the full 40, so a truncated footer sha is compared against
# the current head_sha the driver holds — a prefix match is required for the
# comparison ``footer_sha == current_head_sha[: len(footer_sha)]``. Callers do
# that comparison themselves; this module returns the raw string.


def append_verdict_footer(body: str, *, head_sha: str, event: ReviewEvent) -> str:
    """Append the ``<!-- mindwire:verdict head_sha=... event=... -->`` sentinel to ``body``.

    ONE call site (the driver's ``_submit_review`` receipt path). Idempotent-shaped:
    if the caller passes an already-marked body, the parser will find TWO footers
    and refuse replay (fail-safe) — so callers must invoke this exactly once per
    body, and the tests pin that.

    ``head_sha`` is stored verbatim. Callers producing a truncated sha (e.g. the
    debounce skip body uses ``head[:12]``) get a footer with that same short form;
    :func:`parse_verdict_footer` accepts >= 7 chars, and the comparison at the
    replay site does a prefix match against the current head_sha.

    ``event`` is stored as its GitHub API name (``APPROVE`` / ``REQUEST_CHANGES``
    / ``COMMENT``). The DESIGN v3 rationale for storing the event in the footer
    is directional-safety: a replay that re-POSTs must submit the SAME verdict
    the driver decided last time, not a re-parse of the model's prose (which
    would risk reading a quoted REQUEST_CHANGES as APPROVE, msg-1987 §1).
    """
    marker = f"<!-- mindwire:verdict head_sha={head_sha} event={event.value} -->"
    return f"{body}\n\n{marker}" if body else marker


def parse_verdict_footer(body: str) -> tuple[str, ReviewEvent] | None:
    """Extract ``(head_sha, event)`` from the footer sentinel; ``None`` on ambiguity.

    Returns ``None`` when the body carries ZERO matches (never marked; not a
    replay candidate) OR MORE THAN ONE (invariant broken; refuse to guess).
    Callers treat ``None`` as "body is not eligible for replay" — either
    because it was posted by an older version of the driver (which did not stamp
    the footer) or because something has re-stamped an already-marked body.
    Either way the fail-safe direction is the same: do not POST from an
    ambiguous body. This matches the :class:`LandedState.UNKNOWN` fail-safe
    direction — when in doubt, do not.

    A KNOWN limitation: the parser is regex-only and does NOT strip Markdown
    context around the marker. That is deliberate — the marker is an HTML
    comment, so any Markdown context wrapping it (a fenced code block, an
    indent quotation) leaves the marker intact and equally parseable. If a
    marker turns out to appear inside a Markdown context that the replay
    consumer should ignore, harden the parser then; the current corpus has no
    such case.
    """
    matches = _VERDICT_FOOTER_RE.findall(body)
    if len(matches) != 1:
        return None
    sha, event_str = matches[0]
    try:
        event = ReviewEvent(event_str)
    except ValueError:
        return None  # unknown event value → refuse to POST (fail-safe)
    return sha, event


@dataclass(frozen=True)
class ReviewReceipt:
    """The evidence of a completed review-post → review-submit sequence (DESIGN v3 Q4-A).

    A :class:`ReviewReceipt` is the SINGLE object that couples the three facts
    the replay path needs to reason about: the ``head_sha`` the verdict was
    formed against, the ``event`` we ATTEMPTED to submit (post-degrade,
    pre-fallback), and the ``chatroom_msg_id`` of the relay post that carries
    the verdict body + footer. It is constructed by the driver's post→submit
    helper — the ONE site that composes the footer, posts to chatroom, and
    submits to GitHub — and is passed as a REQUIRED argument to
    :meth:`~spirrow_mindwire.naysayer.pr_review.NaysayerPrReviewDriver._submit_review`.

    Making the receipt REQUIRED (not optional) is a structural enforcement of
    the Q4 invariant: ``post_critique`` must fire BEFORE ``_submit_review``, and
    a caller cannot invoke ``_submit_review`` without proof that the chatroom
    relay already happened (the ``chatroom_msg_id``). A sixth code path cannot
    be added that bypasses the relay — the type signature refuses to compile
    such a call site (msg-1987 Q4-A).

    ``body`` carries the exact text posted to chatroom + submitted to GitHub,
    including the footer. Same body, both channels — chatroom + GitHub cannot
    show different pictures.
    """

    head_sha: str
    event: ReviewEvent
    chatroom_msg_id: str
    body: str


__all__ = [
    "LandedState",
    "ReviewReceipt",
    "append_verdict_footer",
    "landed",
    "parse_verdict_footer",
]
