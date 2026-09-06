"""Pre-gate CI-wait admission: may the PR gate be invoked now, and if not who?

This module is the mindwire-side landing for T-operator-board design v0.3.1 §A
(the current-conductor version of ``R-CI-WAIT``). It answers exactly one
question for every ``NEXT: pr-review <ref>`` handoff:

    "Given the CI rollup for the PR's current head, may the PR-gate fire on
    this tick? If not, who is next?"

It is the same primitive the future operator-board's ``reconcile.py`` will call
from a ``gate`` column tick — extracted into a stand-alone pure function so the
two call sites (the current conductor and the future board) cannot drift, just
as :mod:`spirrow_mindwire.routing`'s ``guard_proposer_to_implementer`` is the
single source for guard (i).

Why the function is shaped this way
-----------------------------------

*Statelessness (design §A-2).* The conductor and the future operator both use
GitHub as the state store — the wait budget's reference timestamp is derived
from the rollup itself, never from a mindwire-side counter or ``next_at``
file. Two consequences fall out for free: (a) ``head`` moving replaces the
whole input, so head-skip is a *property* of the function, not a feature; and
(b) a second node running the same tick reaches the same admission without a
shared cache. The trade — one extra ``gh`` read per tick — is deliberate and
sized in §A-2.

*Signature-level enforcement of the boundary (design §B-2, INV-CI-2 改).* The
function takes ``rollup``, ``head_sha``, ``head_committed_date``, ``now``,
``nomination_is_self``, ``verdict_heads``, ``ci_red_routed_heads`` — and no
``verdict``. Reading the PR-gate verdict content is therefore *structurally*
out of reach here. That is the invariant PR-review msg-2554 asked for by
name: carve-out ② (the PR-gate verdict relay) cannot be re-expressed inside
this function even by accident, because there is no verdict input to
re-express with. A dedicated signature test in
:mod:`tests.test_gate_admission` pins that absence so a future refactor
cannot silently re-open the door. The signature is the enforcement; the test
is the early warning.

*Total time-clock (design §A-1 / §A-2 / §A-4).* The completion and colour
judgements read ``status`` and ``conclusion`` only — they NEVER consult a
timestamp — so a queued check with ``started_at=None`` cannot crash the
concluded/red logic. The wait-clock lives in :func:`ci_clock_start`, which is
a total function: it uses ``started_at ?? created_at`` per check, and if every
check has both null, it falls back to the head commit's ``committed_date``.
The two clocks carry different caps (:data:`CAP_CHECK` = 6 h, :data:`CAP_NOCLOCK`
= 12 h) and the escalation string names which clock it used, so a false-early
escalation (the only direction the ``committed_date`` fallback can err in — an
old commit that was pushed today) is distinguishable at the escalation point.

The admission table (rules R1-R7)
---------------------------------

Numbered exactly as in design §A-3. The ``rule`` field on :class:`AdmissionResult`
carries the ID for auditability and metrics.

* **R1** — rollup is empty (no CI configured for this PR). ``INVOKE``. The
  gate runs; the naysayer's own CI-gate short-circuit (``fetch_ci_status`` →
  ``CiState.UNKNOWN``) is the fail-closed default for repositories without
  configured Actions.
* **R2** — CI incomplete and ``now - ci_clock_start.at <= cap``. ``DEFER``.
  Self-nominate the next ``NEXT: pr-review <ref>`` — do NOT invoke the model
  (INV-CI-1).
* **R3** — CI incomplete and CAP exceeded. ``ROUTE_HUMAN``. Escalation reason
  names the incomplete check and the clock used, so an operator can tell "the
  CI is genuinely stuck" from "we fell back to the commit clock and it fired
  because the commit is old".
* **R4** — CI red and this head has NOT been routed to the implementer for a
  CI fix in this thread. ``ROUTE_IMPLEMENTER``. The conductor writes a
  machine-readable ``<!-- mindwire:ci-route v1 ... -->`` marker on the relay
  message so R5 can distinguish "first red on this head" from "second red on
  the same head without a new push".
* **R5** — CI red and this head has already been routed to the implementer.
  ``ROUTE_HUMAN``. Loop-safety: an implementer that could not fix the CI in
  one round should not be dispatched again without a human deciding.
* **R6** — CI green, a prior gate verdict already lives on this head in this
  thread, and the latest handoff was a self-nomination (i.e., the DEFER path
  woke us up on the same head we already reviewed). ``ALREADY_REVIEWED``. Do
  not re-invoke the model; the earlier verdict's ``NEXT:`` line is authoritative.
  Operator's manual ``NEXT: pr-review <ref>`` (``nomination_is_self=False``)
  is a deliberate override — it bypasses this rule and falls through to R7.
* **R7** — CI green (default). ``INVOKE``.

INV-CI-1 (design §A-1): the naysayer model is invoked at most once per
``(head_sha, ci_conclusion)`` pair. The pending observation costs zero model
calls; R6 dedupes a re-invocation on the same reviewed head.

E-CI-RED (design §B-3): the ``red → implementer`` edge is the CI-fix routing,
NOT carve-out ②. Carve-out ② is the *verdict content* being relayed by the
PR-gate; this module never sees a verdict. The two edges have different
inputs, different decision points, and different owners.

Attribution
-----------

T-operator-board msg-2566 §A (design v0.3), msg-2568 §A/§B (v0.3.1 —
total-function ``ci_clock_start`` and the ``gate_admission`` rename with
signature-level INV-CI-2 enforcement).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class GateAdmission(StrEnum):
    """The admission verdict for a ``NEXT: pr-review <ref>`` handoff.

    See the module docstring for what each value means; the ``rule`` field on
    :class:`AdmissionResult` names the row of §A-3 that produced the verdict.
    """

    INVOKE = "invoke"
    DEFER = "defer"
    ROUTE_IMPLEMENTER = "route_implementer"
    ROUTE_HUMAN = "route_human"
    ALREADY_REVIEWED = "already_reviewed"


class Clock(StrEnum):
    """Which reference clock :func:`ci_clock_start` used.

    ``CHECK`` — at least one check in the rollup carried a ``started_at`` or
    ``created_at`` and we took the minimum. This is the normal path and pairs
    with :data:`CAP_CHECK`.

    ``COMMIT`` — no check contributed a timestamp (a rare rollup shape where
    every entry is still queued with null timestamps *and* has no
    ``created_at`` either — most often when the rollup itself is empty and the
    caller still wants a clock). We fell back to the head commit's
    ``committed_date``. That clock is guaranteed to be earlier than the true
    CI start (a commit exists before its checks queue), so it can only
    escalate *early*, never late. The escalation reason names ``clock=commit``
    so an operator can tell "genuinely stuck" from "old commit pushed today".
    Pairs with :data:`CAP_NOCLOCK`.
    """

    CHECK = "check"
    COMMIT = "commit"


@dataclass(frozen=True)
class CheckRow:
    """One row of the PR head's status rollup, normalised for admission input.

    Callers (a future ``fetch_check_rollup`` method on ``GitHubClient``, or the
    operator tick's ``observe.py``) are responsible for mapping GraphQL's
    ``CheckRun`` and ``StatusContext`` variants onto this shape. The fields
    admission actually needs are minimal and stable across both:

    * ``name`` — the check's display name (used only in the R3 escalation
      string; not a routing key).
    * ``status`` — the completion state. Compared *equal* to :data:`COMPLETED`
      and nothing else, so any value other than ``"completed"`` means
      "not concluded".
    * ``conclusion`` — the coloured outcome, or ``None`` when not concluded.
      Compared against :data:`RED_CONCLUSIONS`.
    * ``started_at`` / ``created_at`` — either may be ``None`` (queued checks
      have no ``started_at``; ``StatusContext`` has no ``started_at`` field
      at all). The clock takes the first non-null in that order; when both
      are null the row simply contributes nothing to the clock.

    Deliberately not present: ``head_sha`` (single-valued at the rollup
    level), ``url`` (not a routing input), or the raw GraphQL node. This keeps
    the row a trivially-constructible test fixture and prevents the admission
    function from growing back-channel dependencies on the caller's client.
    """

    name: str
    status: str
    conclusion: str | None
    started_at: datetime | None
    created_at: datetime | None


@dataclass(frozen=True)
class ClockStart:
    """The reference timestamp for the CI wait budget, and which clock it came from."""

    at: datetime
    source: Clock


@dataclass(frozen=True)
class AdmissionResult:
    """The admission verdict + audit trail for one ``gate_admission`` call.

    * ``admission`` — the routing decision (:class:`GateAdmission`).
    * ``rule`` — the design §A-3 row that fired (``"R1"`` … ``"R7"``); useful
      for metrics and for the ``board_events.reason`` column when this lands
      in the operator board.
    * ``reason`` — a one-line human-readable justification. For R3/R5 this is
      the escalation message the caller passes to the human; for R2 it names
      the clock and start time so a debug log can reconstruct the wait budget.
    """

    admission: GateAdmission
    rule: str
    reason: str


#: The GitHub CheckRun status value that means "concluded" — every other
#: value (``"queued"``, ``"in_progress"``, ``None``, or any StatusContext
#: pre-completion state normalised by the caller) counts as incomplete.
COMPLETED: str = "completed"

#: The conclusion values that make a completed rollup ``red`` (design §A-1
#: 完了判定と時計を分離する). ``neutral`` and ``skipped`` are green per
#: existing gate policy (:data:`~spirrow_mindwire.github.client._CI_OK_CONCLUSIONS`);
#: ``success`` is the primary green.
RED_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)

#: CAP for the ``CHECK`` clock (§A-2). Sized against the observed maximum in
#: T-operator-board PR #222 (2.5 h between push and CI success on msg-2562);
#: 6 h leaves headroom for a self-hosted runner queue while still firing
#: within one working day.
CAP_CHECK: timedelta = timedelta(hours=6)

#: CAP for the ``COMMIT`` clock (§A-2). Doubled because the ``committed_date``
#: fallback is guaranteed to be earlier than the true CI start — a
#: ``CAP_CHECK``-sized budget on that clock would systematically escalate
#: early when a several-hours-old commit is pushed. 12 h is the smallest cap
#: that keeps the "old commit pushed today" case out of the human's queue
#: without opening a genuinely-stuck CI beyond one working day. The
#: R3-escalation reason always names ``clock=commit`` in this branch so the
#: operator can distinguish a real stall from a fallback-clock false-early.
CAP_NOCLOCK: timedelta = timedelta(hours=12)


def _concluded(rollup: Sequence[CheckRow]) -> bool:
    """Rollup is non-empty and every check's ``status`` equals :data:`COMPLETED`.

    Reads no timestamp — a queued check with ``started_at=None`` cannot crash
    this function (design §A-1: 完了判定は timestamp を一切読まない).
    """
    if not rollup:
        return False
    return all(c.status == COMPLETED for c in rollup)


def _red(rollup: Sequence[CheckRow]) -> bool:
    """Rollup is concluded and at least one conclusion is in :data:`RED_CONCLUSIONS`.

    ``conclusion or ""`` normalises ``None`` to a value that cannot match any
    element of the red set; a completed check without a conclusion (a race
    on a re-run) is therefore treated as not-red, matching the
    naysayer's fail-closed handling of the same rollup shape.
    """
    return _concluded(rollup) and any((c.conclusion or "") in RED_CONCLUSIONS for c in rollup)


def ci_clock_start(
    rollup: Sequence[CheckRow],
    head_committed_date: datetime,
) -> ClockStart:
    """Return the reference timestamp for CI's wait budget.

    Total function (design §A-4 property test #1): every combination of null
    timestamps in ``rollup`` — including an empty rollup — returns a valid
    :class:`ClockStart` without raising. The rule:

    1. For each check, use ``started_at`` if non-null, else ``created_at``.
       A check whose both fields are null contributes nothing.
    2. If at least one check contributed a timestamp, return ``min(...)``
       with :attr:`Clock.CHECK` (paired with :data:`CAP_CHECK`).
    3. Otherwise, fall back to ``head_committed_date`` with
       :attr:`Clock.COMMIT` (paired with :data:`CAP_NOCLOCK`).

    ``min`` of the observed set is chosen (not ``max``) so that as required
    checks appear later in the tick — matrix expansion, delayed scheduling,
    a required-workflow set change mid-flight — the clock only moves
    *earlier*. That means the CAP can fire early on a legitimately slow
    rollup, never late on one that was silently stalled: the direction of the
    error is fixed toward the human's queue rather than toward silent
    infinite waiting.
    """
    observed: list[datetime] = []
    for check in rollup:
        ts = check.started_at if check.started_at is not None else check.created_at
        if ts is not None:
            observed.append(ts)
    if observed:
        return ClockStart(at=min(observed), source=Clock.CHECK)
    return ClockStart(at=head_committed_date, source=Clock.COMMIT)


def _r3_escalation(
    rollup: Sequence[CheckRow],
    clock: ClockStart,
    head_sha: str,
) -> str:
    """Build the R3 human-terminal reason string.

    Names (a) which checks are still incomplete and their status, (b) which
    clock decided the CAP, and (c) the start timestamp. The clock name lets
    an operator distinguish a genuinely-stuck CI from a false-early
    escalation on the ``committed_date`` fallback (§A-2).
    """
    incomplete = [f"{c.name}({c.status})" for c in rollup if c.status != COMPLETED]
    checks_part = ", ".join(incomplete) if incomplete else "<no incomplete checks>"
    return (
        f"CI stuck on {head_sha[:12]}: {checks_part}; "
        f"clock={clock.source.value}, started {clock.at.isoformat()}"
    )


def gate_admission(
    *,
    rollup: Sequence[CheckRow],
    head_sha: str,
    head_committed_date: datetime,
    now: datetime,
    nomination_is_self: bool,
    verdict_heads: frozenset[str],
    ci_red_routed_heads: frozenset[str],
) -> AdmissionResult:
    """Answer whether the PR gate may be invoked now, and if not who is next.

    See the module docstring for the admission table (R1-R7). This function is
    pure — no I/O, no clock reads (``now`` is passed in), no mutable state.
    Every observation the caller must lift from the world is a parameter, so
    a fake CI rollup + a fixed ``now`` deterministically drives the entire
    decision surface.

    ``verdict`` is deliberately absent from the signature (INV-CI-2 改,
    design §B-2). Reading the PR-gate verdict *content* is therefore
    structurally impossible in this function — carve-out ② lives with the
    PR-gate driver, not here. A signature test in
    :mod:`tests.test_gate_admission` pins that absence.

    Parameters
    ----------
    rollup :
        The head SHA's status rollup, one :class:`CheckRow` per check. Empty
        means "no CI configured for this PR" (R1); the caller must not
        conflate "empty" with "not yet fetched" — a failed fetch should be
        raised at the caller, not passed in as empty.
    head_sha :
        The PR's current head SHA. Used to key ``verdict_heads`` and
        ``ci_red_routed_heads`` and to identify the PR in the escalation
        message (R3/R5).
    head_committed_date :
        The commit's ``committedDate`` (the ``authored_date`` is not right —
        an old commit can be committed today). Used only when the rollup
        contributes no timestamp (see :func:`ci_clock_start`).
    now :
        The current wall clock. Passed as a parameter so tests can drive
        the CAP boundary deterministically.
    nomination_is_self :
        ``True`` if the latest ``NEXT: pr-review <ref>`` was authored by the
        conductor's own pr-gate-relay (a DEFER wake-up). ``False`` if the
        operator or a role wrote the handoff by hand — the manual override
        path bypasses R6, so the caller cannot silently suppress a manual
        re-review.
    verdict_heads :
        The set of head SHAs on which a PR-gate verdict already exists in
        this thread. Derived by the caller from the thread's ``pr-gate-relay``
        author messages (a marker or heading naming the head is enough).
    ci_red_routed_heads :
        The set of head SHAs the implementer has already been dispatched for
        a CI-red fix on. Derived from ``<!-- mindwire:ci-route v1 ... -->``
        markers the conductor writes on R4 (design §A-5). A head appears
        here at most once per red-then-push cycle: R5 fires the second time
        the same head goes red *without* an intervening push.

    Returns
    -------
    :class:`AdmissionResult`
        The verdict, the design §A-3 rule that fired, and a one-line reason.
    """
    # R1: no CI configured. Fresh invocation — the naysayer's own CI-gate is
    # the fail-closed default when the rollup is genuinely empty at fetch time.
    if not rollup:
        return AdmissionResult(
            admission=GateAdmission.INVOKE,
            rule="R1",
            reason=f"no rollup for {head_sha[:12]}: fresh gate",
        )

    concluded = _concluded(rollup)

    # R2 / R3: CI not concluded. The clock never crashes even when every
    # check has null timestamps — ci_clock_start falls back to the commit
    # clock and CAP_NOCLOCK takes over (design §A-2 total-function property).
    if not concluded:
        clock = ci_clock_start(rollup, head_committed_date)
        cap = CAP_CHECK if clock.source is Clock.CHECK else CAP_NOCLOCK
        if now - clock.at <= cap:
            return AdmissionResult(
                admission=GateAdmission.DEFER,
                rule="R2",
                reason=(
                    f"CI incomplete on {head_sha[:12]}; "
                    f"clock={clock.source.value}, started {clock.at.isoformat()}"
                ),
            )
        return AdmissionResult(
            admission=GateAdmission.ROUTE_HUMAN,
            rule="R3",
            reason=_r3_escalation(rollup, clock, head_sha),
        )

    # From here concluded is True. Timestamps are not consulted; the wait
    # clock is meaningless once every check has a conclusion.
    if _red(rollup):
        # R5 wins over R4 for a repeat red on the same head — an implementer
        # who could not fix the CI in one round should not be dispatched a
        # second time without a human deciding.
        if head_sha in ci_red_routed_heads:
            return AdmissionResult(
                admission=GateAdmission.ROUTE_HUMAN,
                rule="R5",
                reason=(
                    f"CI red twice on {head_sha[:12]} with no new push — "
                    f"implementer loop-safety escalation"
                ),
            )
        # R4: first red on this head. Caller stamps the ci-route marker on
        # the relay message so the next tick's R5 branch can see it.
        return AdmissionResult(
            admission=GateAdmission.ROUTE_IMPLEMENTER,
            rule="R4",
            reason=f"CI red on {head_sha[:12]}: routing to implementer for a fix",
        )

    # concluded ∧ ¬red past this point.
    # R6: same head + prior verdict + self-nominated wake-up. Do NOT re-invoke;
    # the earlier verdict's NEXT: line is authoritative. A manual (non-self)
    # nomination falls through to R7 as an explicit override.
    if head_sha in verdict_heads and nomination_is_self:
        return AdmissionResult(
            admission=GateAdmission.ALREADY_REVIEWED,
            rule="R6",
            reason=(f"gate already produced a verdict on {head_sha[:12]}; self-nomination dedup"),
        )
    # R7: CI green (or manual override on a re-reviewed head). Fresh gate.
    return AdmissionResult(
        admission=GateAdmission.INVOKE,
        rule="R7",
        reason=f"CI green on {head_sha[:12]}: fresh gate",
    )
