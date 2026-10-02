"""Phase 1 of the PR-review sweep: say which ledgers Phase 2 would try to close.

Phase 1 is write-zero, like Phase 0. It sorts every in-scope ``T-pr-review-*`` ledger by
the state of its PR, and stops there. Phase 2 (a separate PR) does the closing.

This is the 1a restart design (``T-pr-review-threads-outlive-their-prs`` msg-5808,
which the naysayer answered with PROCEED). The earlier Phase 1 plan needed magickit's
``can_close()`` to split ``a_union_b`` into A and B. That plan (msg-3214's PR-2 and PR-4)
is **withdrawn**, not deferred, for two reasons:

* A PR-review ledger maps one-to-one to a PR, and the human decision in v0.4 (Operator
  Board §F.1 row 1a) is that such a ledger may be closed **unconditionally** once its PR
  is merged or closed. Nothing is left to split by strength of proof.
* Whether a close is *allowed* stays with magickit's ``_enforce_close_policies``.
  mindwire does not predict it and does not copy it, because two copies of one policy
  drift apart. Phase 2 sends the close and records a refusal; it does not decide.

The table (msg-5808 §2)::

    S-pre   the Phase 0 intake filter, unchanged (resolved/superseded/unknown -> out of scope)

    ID      the thread id does not name a configured project's PR  -> UNPARSEABLE
    PR      404 (the PR does not exist)                            -> UNPARSEABLE
            5xx / rate limit / network / auth                      -> SKIP
            open                                                   -> OPEN
            closed, but no closed_at                               -> SKIP
            closed and merged                                      -> TERMINAL_MERGED
            closed and not merged                                  -> TERMINAL_CLOSED

Phase 2 tries to close the two ``TERMINAL_*`` classes and touches nothing else.

Three choices in this module were not spelled out by msg-5808's table:

1. **404 goes to UNPARSEABLE.** The table has no row for a PR that does not exist. A
   404 is a definite answer, so SKIP (which means "retry next tick") would be wrong:
   it would retry forever. UNPARSEABLE already means "this id does not lead to a
   PR we can act on; a human reads the list". The ``reason`` field keeps the two
   causes apart (``id-unparseable`` / ``pr-not-found``).
2. **UNPARSEABLE covers every ``T-pr-review-`` thread whose tail does not parse.**
   That includes the old repo-less ids like ``T-pr-review-173`` and also any design
   thread that happens to share the prefix (this very thread,
   ``T-pr-review-threads-outlive-their-prs``, is one). msg-5808 §4 asks whether
   unparseable ids other than the four known old ones exist. Listing everything that
   does not parse answers that question; filtering here would hide the answer.
   Phase 2 never touches the class, so a wide class costs nothing but a longer list.
3. **Phase 0's S1 is not a gate here.** msg-5808 §2 removes it on purpose: for a 1:1
   ledger the close is unconditional. The number of ``post_message`` events at or after
   ``closed_at`` is still reported, as the column ``post_terminal_messages``, so a
   human can see a ledger that was written to after its PR ended. Nothing branches on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from spirrow_mindwire.github.client import PrResolution, PrState

from .phase0 import Excluded

#: Every ledger id starts with this, whatever the project. Used only to recognise ids
#: that look like a ledger but do not parse as one; parsing itself stays with
#: :meth:`spirrow_mindwire.pr_review_sweep.config.ProjectEntry.pr_number_for_thread`.
LEDGER_ID_PREFIX = "T-pr-review-"

REASON_ID_UNPARSEABLE = "id-unparseable"
REASON_PR_NOT_FOUND = "pr-not-found"
REASON_PR_INDETERMINATE = "pr-indeterminate"
REASON_CLOSED_WITHOUT_CLOSED_AT = "closed-without-closed-at"
REASON_PR_OPEN = "pr-open"
REASON_PR_MERGED = "pr-merged"
REASON_PR_CLOSED_UNMERGED = "pr-closed-unmerged"


class Phase1Class(StrEnum):
    """Where one ledger lands. Exactly the five rows of msg-5808 §2."""

    TERMINAL_MERGED = "terminal_merged"
    TERMINAL_CLOSED = "terminal_closed"
    OPEN = "open"
    SKIP = "skip"
    UNPARSEABLE = "unparseable"


#: The classes Phase 2 will try to close. Anything outside this set is never touched.
CLOSE_CANDIDATE_CLASSES = frozenset({Phase1Class.TERMINAL_MERGED, Phase1Class.TERMINAL_CLOSED})


@dataclass(frozen=True)
class LedgerRow:
    """One ledger's Phase 1 outcome."""

    thread_id: str
    project: str
    cls: Phase1Class
    reason: str
    thread_status: str = ""
    #: ``owner/repo#n``, or ``None`` when the id did not lead to a PR.
    pr: str | None = None
    closed_at: datetime | None = None
    #: ``post_message`` events at or after ``closed_at``. Reported only; never a gate.
    #: ``None`` when the PR is not terminal, so there is no ``closed_at`` to count from.
    post_terminal_messages: int | None = None

    @property
    def is_close_candidate(self) -> bool:
        return self.cls in CLOSE_CANDIDATE_CLASSES


def unparseable_row(thread_id: str, project: str, status: str) -> LedgerRow:
    """A ``T-pr-review-*`` thread whose id names no configured project's PR."""
    return LedgerRow(thread_id, project, Phase1Class.UNPARSEABLE, REASON_ID_UNPARSEABLE, status)


def classify_ledger(
    thread_id: str,
    project: str,
    status: str,
    pr: PrState,
    *,
    post_terminal_messages: int | None = None,
) -> LedgerRow:
    """Classify one ledger whose id parsed. Pure: no I/O, no clock, no writes.

    The caller has already applied S-pre, so ``status`` is one of the intake statuses.
    """
    slug = pr.slug
    if pr.resolution is PrResolution.UNRESOLVABLE:
        # D-7 fail-open, as in Phase 0: no answer means no verdict. The next tick retries.
        return LedgerRow(
            thread_id, project, Phase1Class.SKIP, REASON_PR_INDETERMINATE, status, slug
        )
    if pr.resolution is PrResolution.NOT_FOUND:
        return LedgerRow(
            thread_id, project, Phase1Class.UNPARSEABLE, REASON_PR_NOT_FOUND, status, slug
        )
    if pr.resolution is PrResolution.OPEN:
        return LedgerRow(thread_id, project, Phase1Class.OPEN, REASON_PR_OPEN, status, slug)

    if pr.closed_at is None:
        # D-6, as in Phase 0: a closed PR always carries closed_at. If one does not, the
        # assumption broke, not the ledger. Skip rather than guess.
        return LedgerRow(
            thread_id, project, Phase1Class.SKIP, REASON_CLOSED_WITHOUT_CLOSED_AT, status, slug
        )
    if pr.merged:
        cls, reason = Phase1Class.TERMINAL_MERGED, REASON_PR_MERGED
    else:
        cls, reason = Phase1Class.TERMINAL_CLOSED, REASON_PR_CLOSED_UNMERGED
    return LedgerRow(
        thread_id, project, cls, reason, status, slug, pr.closed_at, post_terminal_messages
    )


@dataclass
class Phase1Report:
    """The whole Phase 1 product. Printed to stdout; never posted anywhere."""

    rows: list[LedgerRow] = field(default_factory=list)
    #: S-pre's exclusions, reported for the same reason Phase 0 reports them.
    out_of_scope: list[Excluded] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {c.value: 0 for c in Phase1Class}
        for row in self.rows:
            out[row.cls.value] += 1
        return out

    @property
    def close_candidates(self) -> list[LedgerRow]:
        return [r for r in self.rows if r.is_close_candidate]

    @property
    def human_list(self) -> list[LedgerRow]:
        """Ledgers no machine step will clear: a human has to read these."""
        return [r for r in self.rows if r.cls is Phase1Class.UNPARSEABLE]


def build_report(
    rows: list[LedgerRow], *, out_of_scope: list[Excluded] | None = None
) -> Phase1Report:
    return Phase1Report(rows=list(rows), out_of_scope=list(out_of_scope or []))


def report_to_json(report: Phase1Report) -> dict[str, Any]:
    """Plain-data view of the report, for ``json.dumps``."""

    def row(r: LedgerRow) -> dict[str, Any]:
        return {
            "thread_id": r.thread_id,
            "project": r.project,
            "class": r.cls.value,
            "reason": r.reason,
            "thread_status": r.thread_status,
            "pr": r.pr,
            "closed_at": r.closed_at.isoformat() if r.closed_at else None,
            "post_terminal_messages": r.post_terminal_messages,
        }

    return {
        "phase": 1,
        "wrote_anything": False,
        "counts": report.counts(),
        "close_candidate_count": len(report.close_candidates),
        "close_candidates": [r.thread_id for r in report.close_candidates],
        "human_list": [row(r) for r in report.human_list],
        "rows": [row(r) for r in report.rows],
        "out_of_scope_count": len(report.out_of_scope),
        "out_of_scope": [
            {"thread_id": e.thread_id, "thread_status": e.status, "reason": e.reason}
            for e in report.out_of_scope
        ],
    }


__all__ = [
    "CLOSE_CANDIDATE_CLASSES",
    "LEDGER_ID_PREFIX",
    "REASON_CLOSED_WITHOUT_CLOSED_AT",
    "REASON_ID_UNPARSEABLE",
    "REASON_PR_CLOSED_UNMERGED",
    "REASON_PR_INDETERMINATE",
    "REASON_PR_MERGED",
    "REASON_PR_NOT_FOUND",
    "REASON_PR_OPEN",
    "LedgerRow",
    "Phase1Class",
    "Phase1Report",
    "build_report",
    "classify_ledger",
    "report_to_json",
    "unparseable_row",
]
