"""Phase 0 of the PR-review sweep: classify, measure, and produce a go/no-go number.

Phase 0 answers one question — **how many chatroom threads are left over from a PR
that has finished?** — and answers it without changing anything. Nothing here writes.

The classification is the two-stage predicate frozen at msg-2151 D-6 (revised),
evaluated in the order C > A > B (msg-2149 R-4, "a thread with post-terminal activity
is never swept, even when the proof is complete")::

    S0  PR resolution / terminality
        indeterminate (5xx, rate limit, network)  -> SKIP   (no verdict at all)
        404 (the PR does not exist)               -> AB     reason pr-unresolvable
        state == "open"                           -> C      (unconditional)
        state == "closed"                         -> S1, with terminal_at = closed_at

    S1  thread liveness
        status == "parked"                        -> C      (unconditional)
        status in {active, awaiting_reply} and
            (i)  a post_message at or after (terminal_at - margin), and
            (ii) the last msg names a successor, or status == "awaiting_reply"
                                                  -> C
        anything else                             -> AB     reason terminal-and-quiescent

Phase 0 stops there. Splitting ``a_union_b`` into A (provably closable) and B (not) needs the
ledger's ``can_close()``, which does not exist yet — that is Phase 1, in another
repository. So the number this module produces is deliberately the *union*, and the
threshold it feeds is the union's: ``>= 5`` proceeds, ``< 5`` is a no-go whose record
carries the whole (by definition <= 4 element) list for a human to read by hand
(msg-2162 ④, msg-2172 R-37).

Two things are kept apart on purpose (msg-2166 R-25), and the separation is a type,
not a comment: :class:`ThreadFacts` carries ``classification_events`` and
``measurement_events`` as distinct fields, fed by distinct queries.

* The **classification** query is windowed at ``terminal_at - margin``. It decides.
* The **measurement** query has no window at all. It only reports the raw distribution
  of ``event_ts - terminal_at``, and feeds the margin sensitivity table.

If the measurement were reused for classification, or the classification's window used
to gather the distribution, the margin would be validating itself: a 5-minute window
cannot observe a 6-minute skew, so the measurement would always conclude the margin
was wide enough. Do not merge the two fields, and do not compute one from the other.
Phase 0 does not pick a margin either — it emits the ladder and a human reads it.

----------------------------------------------------------------------------------
Verified assumption, and one that did NOT hold
----------------------------------------------------------------------------------

msg-2162 ③ made S1 (i) conditional on a fact to be checked at implementation time:
that the audit-log event timestamp is a *server* record, not a caller's declaration.
It is only conditionally so. Measured against the primary source
(``spirrow-conclair`` ``src/spirrow_conclair/api/messages.py``): ``PostMessageRequest``
accepts an optional ``timestamp``, and the handler resolves it as ``timestamp or
datetime.now(timezone.utc)`` before writing that same value to BOTH the message row
and the ``post_message`` audit event row. So the event timestamp is server-stamped
**only when the caller omits one**.

In this deployment every write goes through Magickit's MCP surface, which exposes no
``timestamp`` parameter and passes none — so today's events are in fact server-stamped.
But the guarantee is a property of the caller, not of the audit log, and the audit log
is what S1 (i) reads. Phase 0 is write-zero and its errors are recoverable by re-running,
so it proceeds on the audit log as designed; the standing question is what Phase 2 —
which closes threads irreversibly — is allowed to rest on. That is a design decision
and msg-2162's own rebound clause reserves it for the thread, so no alternative
mechanism is invented here.

One related observation, recorded because S1 (ii) rests on it: ``next_participant``
is a structured field chosen over prose ``NEXT:`` parsing (msg-2149 R-2), and the loop
in fact writes its handoff into the message body. Measured on the first real Phase 0
run (2026-09-03, 61 PR-review threads across spirrow-mindwire / -magickit / -verimend):
exactly one thread reached C through limb (ii), and it did so via ``next_participant``.
So the field is populated rarely but not never. Where it is empty, (ii) degenerates to
``status == "awaiting_reply"``. The frozen design has no prose fallback and none is
invented here; :func:`build_report` instead counts how often each limb decided, so the
degeneracy stays visible in the output rather than becoming a silent premise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from spirrow_mindwire.github.client import PrResolution, PrState

#: Provisional until the measurement says otherwise (msg-2164 R-21). Five minutes is
#: Einstein's example value adopted as a placeholder — it has no measured basis, which
#: is exactly why the sensitivity table below exists.
PROVISIONAL_MARGIN_SECONDS = 300

#: The margins ``a_union_b`` is recomputed at (msg-2166 R-26). If the count is flat
#: across the ladder, the go/no-go does not depend on the provisional margin and the guess is
#: harmless. If it moves, the Phase 0 result is not usable until a margin is fixed.
MARGIN_LADDER_SECONDS: tuple[int, ...] = (0, 60, 300, 900, 3600, 21600, 86400)

#: Thread statuses S1 examines for liveness. ``parked`` is handled before this set and
#: is never swept; anything outside both (``resolved`` / ``superseded``) falls straight
#: through to ``a_union_b``, which is what D-6 says verbatim.
_LIVENESS_STATUSES = frozenset({"active", "awaiting_reply"})

GO_THRESHOLD = 5


class Bucket(StrEnum):
    """Where a thread lands. Phase 0 cannot tell A from B, so it reports the union."""

    SKIP = "skip"
    C = "c"
    AB = "a_union_b"


class Verdict(StrEnum):
    GO = "go"
    NO_GO = "no_go"


@dataclass(frozen=True)
class ThreadFacts:
    """Everything Phase 0 needs about one thread, already fetched.

    Keeping this a plain value object is what makes the predicate testable without a
    network: every S0/S1 branch is reachable by constructing one of these.
    """

    thread_id: str
    status: str
    #: ``next_participant`` of the LAST message, or ``None``/"" when it names nobody.
    last_next_participant: str | None = None
    #: post_message timestamps from the WINDOWED query. Decides S1 (i). See module docs.
    classification_events: tuple[datetime, ...] = ()
    #: post_message timestamps from the UNWINDOWED query. Reporting only. See module docs.
    measurement_events: tuple[datetime, ...] = ()


@dataclass(frozen=True)
class Classification:
    """One thread's Phase 0 outcome."""

    thread_id: str
    bucket: Bucket
    reason: str
    pr: str | None = None
    terminal_at: datetime | None = None
    thread_status: str = ""
    #: For AB/C rows reached through S1: which limb of (ii) decided, for observability.
    liveness_limb: str | None = None


def _terminal_at(pr: PrState) -> datetime | None:
    return pr.closed_at


def classify(facts: ThreadFacts, pr: PrState, *, margin: timedelta) -> Classification:
    """Run S0 then S1 for one thread. Pure: no I/O, no clock reads, no writes.

    ``margin`` shifts the S1 (i) cutoff EARLIER only. The asymmetry is the point
    (msg-2164 R-21): mistaking a live thread for a dead one is unrecoverable once
    Phase 2 closes it, whereas mistaking a dead thread for a live one merely defers it
    to the next sweep. So every ambiguity here resolves toward "still alive".
    """
    slug = pr.slug

    # ---- S0 -------------------------------------------------------------------
    if pr.resolution is PrResolution.UNRESOLVABLE:
        # D-7 fail-open: a transient API failure must produce neither a close nor a
        # debt entry. Silence, and the next (idempotent) sweep picks it up.
        return Classification(facts.thread_id, Bucket.SKIP, "pr-indeterminate", slug)
    if pr.resolution is PrResolution.NOT_FOUND:
        # A definite answer, unlike the above: the mapping or the PR is wrong, and
        # that is a debt to report, not a transient to retry.
        return Classification(facts.thread_id, Bucket.AB, "pr-unresolvable", slug)
    if pr.resolution is PrResolution.OPEN:
        return Classification(facts.thread_id, Bucket.C, "pr-open", slug)

    terminal_at = _terminal_at(pr)
    if terminal_at is None:
        # D-6 asserts a closed PR always carries closed_at. If GitHub ever hands us a
        # closed PR without one, the assertion — not the sweep — is what broke, and
        # the S1 comparison would be against nothing. Skip rather than guess.
        return Classification(facts.thread_id, Bucket.SKIP, "closed-without-closed-at", slug)

    # ---- S1 -------------------------------------------------------------------
    status = facts.status
    if status == "parked":
        # A parked thread is waiting on a condition it wrote down itself. It is the
        # single state we most want never to sweep (msg-2149 R-1).
        return Classification(facts.thread_id, Bucket.C, "thread-parked", slug, terminal_at, status)

    if status in _LIVENESS_STATUSES:
        cutoff = terminal_at - margin
        # ``>=`` matches the inclusive lower bound of the audit-log query that fills
        # this field, so the pure predicate and the wire query agree. Inclusive is
        # also the survival side: one more event counted means C, not ``a_union_b``.
        post_terminal = any(ts >= cutoff for ts in facts.classification_events)
        named_successor = bool((facts.last_next_participant or "").strip())
        awaiting = status == "awaiting_reply"
        if post_terminal and (named_successor or awaiting):
            limb = "next_participant" if named_successor else "awaiting_reply"
            return Classification(
                facts.thread_id, Bucket.C, "post-terminal-activity", slug, terminal_at, status, limb
            )

    return Classification(
        facts.thread_id, Bucket.AB, "terminal-and-quiescent", slug, terminal_at, status
    )


def measurement_offsets_seconds(facts: ThreadFacts, terminal_at: datetime) -> list[float]:
    """Raw ``event_ts - terminal_at`` in seconds, from the UNWINDOWED query.

    Raw on purpose (msg-2166 R-27): collapsing to a mean or a percentile here would
    hide the very tail the margin has to cover. The caller emits these values as they
    are and a human reads the shape.
    """
    return [(ts - terminal_at).total_seconds() for ts in facts.measurement_events]


def _ab_count_at_margin(pairs: list[tuple[ThreadFacts, PrState]], margin_seconds: int) -> int:
    delta = timedelta(seconds=margin_seconds)
    return sum(
        1
        for facts, pr in pairs
        if _reclassify_from_measurement(facts, pr, delta).bucket is Bucket.AB
    )


def _reclassify_from_measurement(
    facts: ThreadFacts, pr: PrState, margin: timedelta
) -> Classification:
    """Classify using the MEASUREMENT events instead of the classification ones.

    Only the sensitivity table calls this, and only because msg-2166 R-26 asks for
    ``a_union_b`` as a function of the margin — a question the windowed classification data
    cannot answer, since it was gathered at one fixed margin. This does not feed the
    verdict; :func:`build_report` takes that from :func:`classify`.
    """
    proxy = ThreadFacts(
        thread_id=facts.thread_id,
        status=facts.status,
        last_next_participant=facts.last_next_participant,
        classification_events=facts.measurement_events,
        measurement_events=facts.measurement_events,
    )
    return classify(proxy, pr, margin=margin)


def sensitivity_table(
    pairs: list[tuple[ThreadFacts, PrState]],
    ladder: tuple[int, ...] = MARGIN_LADDER_SECONDS,
) -> list[dict[str, int]]:
    """``a_union_b`` recomputed at each margin on the ladder (msg-2166 R-26)."""
    return [{"margin_seconds": m, "a_union_b": _ab_count_at_margin(pairs, m)} for m in ladder]


@dataclass
class Phase0Report:
    """The whole Phase 0 product. Serialised to stdout; never posted anywhere."""

    margin_seconds: int
    classifications: list[Classification] = field(default_factory=list)
    offsets_seconds: dict[str, list[float]] = field(default_factory=dict)
    sensitivity: list[dict[str, int]] = field(default_factory=list)
    verdict: Verdict = Verdict.NO_GO
    a_union_b: int = 0
    #: Present only on NO_GO. msg-2172 R-37: Phase 0 cannot compute B_gate (that needs
    #: the ledger), so when the sweep parks, the <=4 leftovers travel with the record
    #: and a human inspects them by hand. The threshold already says a set this small
    #: is cheaper to handle manually.
    no_go_full_list: list[Classification] = field(default_factory=list)
    status_breakdown: dict[str, int] = field(default_factory=dict)
    liveness_limbs: dict[str, int] = field(default_factory=dict)


def build_report(pairs: list[tuple[ThreadFacts, PrState]], *, margin_seconds: int) -> Phase0Report:
    """Classify every pair, measure, and decide go/no-go."""
    margin = timedelta(seconds=margin_seconds)
    report = Phase0Report(margin_seconds=margin_seconds)

    for facts, pr in pairs:
        result = classify(facts, pr, margin=margin)
        report.classifications.append(result)
        report.status_breakdown[facts.status] = report.status_breakdown.get(facts.status, 0) + 1
        if result.liveness_limb:
            report.liveness_limbs[result.liveness_limb] = (
                report.liveness_limbs.get(result.liveness_limb, 0) + 1
            )
        terminal_at = _terminal_at(pr)
        if terminal_at is not None and facts.measurement_events:
            report.offsets_seconds[facts.thread_id] = measurement_offsets_seconds(
                facts, terminal_at
            )

    ab = [c for c in report.classifications if c.bucket is Bucket.AB]
    report.a_union_b = len(ab)
    report.sensitivity = sensitivity_table(pairs)
    if report.a_union_b >= GO_THRESHOLD:
        report.verdict = Verdict.GO
    else:
        report.verdict = Verdict.NO_GO
        report.no_go_full_list = ab
    return report


def report_to_json(report: Phase0Report) -> dict[str, Any]:
    """Plain-data view of the report, for ``json.dumps``."""

    def row(c: Classification) -> dict[str, Any]:
        return {
            "thread_id": c.thread_id,
            "bucket": c.bucket.value,
            "reason": c.reason,
            "pr": c.pr,
            "terminal_at": c.terminal_at.isoformat() if c.terminal_at else None,
            "thread_status": c.thread_status,
            "liveness_limb": c.liveness_limb,
        }

    return {
        "phase": 0,
        "wrote_anything": False,
        "margin_seconds": report.margin_seconds,
        "verdict": report.verdict.value,
        "a_union_b": report.a_union_b,
        "go_threshold": GO_THRESHOLD,
        "classifications": [row(c) for c in report.classifications],
        "no_go_full_list": [row(c) for c in report.no_go_full_list],
        "sensitivity": report.sensitivity,
        "offsets_seconds": report.offsets_seconds,
        "status_breakdown": report.status_breakdown,
        "liveness_limbs": report.liveness_limbs,
    }


__all__ = [
    "GO_THRESHOLD",
    "MARGIN_LADDER_SECONDS",
    "PROVISIONAL_MARGIN_SECONDS",
    "Bucket",
    "Classification",
    "Phase0Report",
    "ThreadFacts",
    "Verdict",
    "build_report",
    "classify",
    "measurement_offsets_seconds",
    "report_to_json",
    "sensitivity_table",
]
