"""``needs_actor`` and ``stalled`` — the two state predicates the ledger runs (§3-1/§3-4).

Both predicates are author-blind and origin-blind. Only the clock (via
``last_participant_motion_at``) knows about origin, and only INV-1' entitles it to.

The ``needs_actor`` table is the settled form from msg-2470 §3-4 with the msg-2470
carve-out preserved: an APPROVE-only-awaiting-human-merge PR is NOT ``needs_actor``,
because the sole remaining act is Tier-C ``merge`` and the digest's existing
human-parked section already reports it (§7 coverage rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from spirrow_mindwire.stall_ledger.model import UnitKind

# ─── N thresholds (§3-5) ───────────────────────────────────────────────────────────────
#
# W-3: the values are provisional. The ledger's own data will tune them in operation.
# Every value is documented as "the shortest interval after which silence deserves a
# level-triggered notification" — so the wrong direction (making them shorter) fails
# noisy, and the wrong direction (making them longer) fails quiet. When a value is
# uncertain the design leans toward the shorter side.

N_THRESHOLDS: dict[str, timedelta] = {
    "pr_with_verdict": timedelta(hours=12),
    "thread_with_nomination": timedelta(hours=6),
    "quarantine": timedelta(0),  # any quarantine entry is immediately stalled
    "other_thread": timedelta(hours=72),
}


@dataclass(frozen=True)
class PrState:
    """The subset of a PR's state the ``needs_actor`` predicate reads (§3-4).

    All fields are pulled from GitHub API responses the sweep already reads. No new
    probes.
    """

    is_open: bool
    is_draft: bool
    has_verdict: bool
    verdict_is_request_changes: bool
    verdict_recorded_as_indefinite_input: bool  # e.g. gate posted "ci=pending"
    merge_state_is_executable: bool  # False for DIRTY / BLOCKED / UNSTABLE etc.
    is_approve_awaiting_human_merge: bool  # verdict=APPROVE and human is the only actor


@dataclass(frozen=True)
class ThreadState:
    """The subset of a thread's state the ``needs_actor`` predicate reads (§3-4)."""

    status: str  # "open" / "resolved" / "human" / ...
    dormant_until_expired: bool  # True iff no dormant-until pin OR its pin has expired


@dataclass(frozen=True)
class QuarantineState:
    """Any entry in ``quarantine.json`` — presence alone is enough (§3-4, N=0)."""

    is_present: bool


def needs_actor_pr(state: PrState) -> bool:
    """§3-4 PR row.

    An APPROVE-only PR awaiting a human merge is NOT needs_actor — the human-parked
    section already reports it and the coverage rule (§7) would then have to exclude
    it here, so we exclude at the source instead. This preserves the ledger's
    invariant that its rows and the human-parked digest's rows are disjoint.
    """

    if not state.is_open or state.is_draft:
        return False
    if state.is_approve_awaiting_human_merge:
        return False
    if not state.has_verdict:
        return True  # no verdict = the review actor still owes a call
    if state.verdict_is_request_changes:
        return True
    if state.verdict_recorded_as_indefinite_input:
        return True  # M-4 shape — gate posted an indefinite verdict, needs re-fire
    # M-3 shape — a non-executable merge state (DIRTY / BLOCKED / …) means the author
    # must rebase before any actor can move forward. Inline for SIM103; the intent is
    # documented on the last branch above and this line just returns the residual.
    return not state.merge_state_is_executable


def needs_actor_thread(state: ThreadState) -> bool:
    """§3-4 thread row."""

    if state.status == "resolved":
        return False
    # An active dormant-until pin was the participant's own act; treat the pin as
    # motion for the ledger's purposes so we don't flag intentionally-parked threads.
    return state.dormant_until_expired


def needs_actor_quarantine(state: QuarantineState) -> bool:
    """§3-4 quarantine row — presence IS the predicate."""

    return state.is_present


def stalled(
    kind: UnitKind,
    last_participant_motion_at: datetime,
    now: datetime,
    needs_actor_now: bool,
    n: timedelta,
) -> bool:
    """§3-1 ``stalled(unit) := needs_actor(unit) and (now - last_participant_motion_at) > N``.

    Parameter ``n`` is the spec's ``N`` threshold; PEP-8 forces the lowercase spelling
    here — the tests use ``N_THRESHOLDS`` and the spec's ``N`` throughout, so the two
    conventions coexist across the boundary.

    The ``last_participant_motion_at`` parameter is what INV-1' says it must be —
    advanced ONLY by ``origin=participant`` events, never by loop remedies. The caller
    is responsible for the advance; this function is the pure predicate.
    """

    del kind  # signature keeps ``kind`` for readability at the call site; unused here
    if not needs_actor_now:
        return False
    return (now - last_participant_motion_at) > n
