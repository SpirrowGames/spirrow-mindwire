"""Conductor-side Tier-C Decider hook (T-decider-conductor-hook step 2 / 2b / 2c, tierc-v2; shadow).

Called by :class:`~spirrow_mindwire.conductor.core.Conductor` right after its rule-based routing
(``_route``) has decided the turn. What it does:

0. **entry check** (msg-4237 DECIDED 2c-1, :func:`is_tierc_entry`): only a turn whose author
   wrote ``NEXT: human`` themself and whose roster role is in :data:`TIERC_ENTRY_ROLES` —
   ``proposer`` / ``implementer`` / ``naysayer`` (T-decider-tierc-v2 msg-4360 / msg-4382,
   widening 2c-1's ``proposer`` only; the caller has already resolved the handoff to
   ``HUMAN``). The rule stop is **not** part of the entry: an escalation that ``_route`` turned
   into a forced naysayer consult (``stop=None``) is evaluated too. Any other turn returns at
   once — no gate, no Lexora call, no log line;
   ``routed`` (msg-4239 DECIDED 2c-1 revised, :func:`routed_from_route`) then records what
   ``_route`` actually did with the turn — ``stop`` / ``forced_naysayer`` / ``spawn_blocked``
   (msg-4280 DECIDED 2c-4) — read from its return values, never inferred. Each value requires
   the **whole** combination of ``stop`` / ``is_forced`` / ``target_role`` / ``spawn_blocked``
   that names it; any other combination (unreachable today) is a
   :class:`RoutingInvariantError`, logged at ERROR with no row (see below);
1. run the admission gate **compute-only** (msg-4200 DECIDED 2b-1 / 2b-2,
   :func:`compute_gate_result`): ``retry_lookup`` always ``False``, the decision's log entries
   discarded (nothing is written to the decisions log), a gate exception → ``gate_result=None``;
2. build the :class:`~.state.DecisionState` (``turn_from_messages`` → ``state_builder`` — the same
   builder the replay uses) with that ``gate_result`` and the rule_5 feature ``dispute_rounds``
   (:func:`count_dispute_rounds`, over the whole thread — msg-4380 Δ5);
3. ask the Decider whether the turn is a target (``decider.is_target`` — mode / head; the rule
   lives in the Decider, not here), then ``decider.evaluate``. Under tierc-v2 a turn with
   ``gate_result=None`` is sent as well (msg-4380 Δ2, overriding msg-4196 DECIDED 1), so the
   msg-4196 DECIDED 2 "empty-``outcome`` line" no longer exists. (A v1-configured Decider still
   skips such a turn; ``evaluate`` then returns ``None`` and no line is written.)
4. ``log_decision(...)`` — every outcome, with ``decision_id`` / ``outcome``, the gate columns
   ``gate_kind`` / ``gate_is_grey_zone``, the rule ``stop``, ``routed`` and (v2)
   ``matched_rule`` / ``matched_rule_source`` / ``rules_sha256`` on the same line;
5. the acting branch is gated on ``routed == "stop"`` (msg-4237 DECIDED 2c-2), on
   ``dr.actionable_verdict`` only (msg-4184) and on a mode of ``annotate`` / ``bounce`` — which is
   refused at build time, so in shadow it never runs. ``forced_naysayer`` and ``spawn_blocked``
   rows are record-only in every mode.

**Fail loud, not fail open (Einstein msg-4240 advisory; PR-gate observation on #348).** A routing
combination the mapping does not name raises :class:`RoutingInvariantError` (an
``AssertionError``) instead of being labelled. It is caught at this module's boundary and logged
at ERROR with the traceback, and **no** ``decider_decision`` row is written and Lexora is not
called — the telemetry never carries a meaningless label. It is not re-raised into the Conductor
loop: D20 forbids the observer from changing the routing, and an escaping exception would stop
the thread.

**Monotonicity (D20).** Nothing here returns a new stop, and the gate's admit / bounce is never
used for stop, notification or routing — only as Decider input and a log column. The hook returns
the result only so a caller/test can observe it; the Conductor discards it. Any exception is
caught and logged — it must never reach the stop decision.

**Reader of the log line.** The payload is a structured ``logger.info`` record under the logger
``spirrow_mindwire.decider.hook`` (prefix ``decider_decision``), read by the operator / the §6
evaluation join (thread_id + round + decision_id) off the conductor's own log. It is not posted
to any chatroom thread, so no chatroom fallback surface is involved.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from spirrow_mindwire.decider.result import DecisionResult, decision_result_to_dict
from spirrow_mindwire.decider.state import (
    AdmissionGateResult,
    DecisionState,
    EventSummary,
    SimpleTurn,
    state_builder,
)
from spirrow_mindwire.decider.verdict import TierCV2Verdict, TierCVerdictKind
from spirrow_mindwire.tier_c_admission_gate import (
    AdmissionDecision,
    LogKind,
    RetryAdmitReason,
    RetryLookup,
    decide_admission,
)
from spirrow_mindwire.value_objects import Role

logger = logging.getLogger(__name__)

RECENT_EVENTS_N = 5
"""§10 provisional N (EventSummary docstring: N=5)."""
BODY_HEAD_M = 500
"""§10 provisional M (EventSummary docstring: M=500 chars)."""

ACTING_TIERC_MODES: frozenset[str] = frozenset({"annotate", "bounce"})


class Decider(Protocol):
    """What the Conductor needs from a Decider (satisfied by ``DeciderLexoraAdapter``)."""

    @property
    def tierc_mode(self) -> str: ...

    def is_target(self, state: DecisionState) -> bool: ...

    async def evaluate(self, state: DecisionState) -> DecisionResult | None: ...

    async def clear_proceed(self, state: DecisionState) -> DecisionResult | None:
        """D-4' G3 (veto): evaluate a naysayer's proceed handoff; ``None`` = not called."""
        ...


@dataclass(frozen=True)
class ThreadMessage:
    """The conductor-agnostic slice of one chatroom message the state needs."""

    msg_id: str
    author: str
    content: str
    parsed_next: str | None


def turn_from_messages(
    *,
    thread_id: str,
    round_index: int,
    roster: Mapping[str, Role],
    messages: Sequence[ThreadMessage],
    gate_result: AdmissionGateResult | None = None,
    dispute_rounds: int | None = None,
) -> SimpleTurn:
    """Thread (oldest → newest) → ``SimpleTurn`` for ``state_builder``.

    ``recent_events`` newest → oldest, capped at N; bodies capped at M; ``head_summary`` is the
    latest body head; ``prev_next`` is the handoff of the message before the latest;
    ``diff_stat`` is ``None`` (Tier-C does not use it). ``parsed_next`` is lower-cased so the
    reserved ``human`` token compares exactly.
    """
    latest = messages[-1] if messages else None
    prev = messages[-2] if len(messages) >= 2 else None
    recent = tuple(
        EventSummary(
            msg_id=m.msg_id,
            author=m.author,
            parsed_next=m.parsed_next,
            body_head=m.content[:BODY_HEAD_M],
        )
        for m in reversed(messages[-RECENT_EVENTS_N:])
    )
    parsed_next = latest.parsed_next if latest is not None else None
    return SimpleTurn(
        thread_id=thread_id,
        round_index=round_index,
        roster=dict(roster),
        head_summary=latest.content[:BODY_HEAD_M] if latest is not None else "",
        recent_events=recent,
        parsed_next=parsed_next.lower() if parsed_next is not None else None,
        prev_next=prev.parsed_next if prev is not None else None,
        diff_stat=None,
        gate_result=gate_result,
        dispute_rounds=dispute_rounds,
    )


_VERDICT_APPROVE_RE = re.compile(r"^[\s>*_`]*VERDICT:\s*APPROVE\b", re.MULTILINE)
"""A naysayer ``VERDICT: APPROVE`` line (markdown emphasis / quote tolerated)."""


def count_dispute_rounds(roster: Mapping[str, Role], messages: Sequence[ThreadMessage]) -> int:
    """The rule_5 feature ``dispute_rounds`` (msg-4380 Δ5): proposer↔naysayer rounds since the
    last naysayer ``VERDICT: APPROVE``, over the **whole** thread (not the N=5 ``recent_events``).

    Walking oldest → newest, only messages whose author's roster role is ``proposer`` or
    ``naysayer`` count. A round is a naysayer message without ``VERDICT: APPROVE`` whose
    preceding counted message is the proposer's — i.e. the naysayer pushed back on a proposer
    submission. A naysayer ``VERDICT: APPROVE`` resets the count to 0. Other authors
    (implementer, the human, off-roster relays) neither count nor reset.

    A feature only: nothing escalates on it (automatic escalation after N rounds would change
    the routing — D20 — and is out of scope, msg-4380 Δ5). The rule_5 note in the rules file
    names 3 rounds as the guideline the model reads.
    """
    rounds = 0
    last: Role | None = None
    for m in messages:
        role = _roster_role(roster, m.author)
        if role is Role.NAYSAYER:
            if _VERDICT_APPROVE_RE.search(m.content):
                rounds = 0
            elif last is Role.PROPOSER:
                rounds += 1
            last = role
        elif role is Role.PROPOSER:
            last = role
    return rounds


def log_decision(
    *,
    thread_id: str,
    round_index: int,
    stop: str | None,
    routed: str,
    dr: DecisionResult,
    gate_result: AdmissionGateResult | None,
) -> dict[str, Any]:
    """Write one decision record to the conductor log and return it (msg-4182).

    The one place (with the replay record) allowed to read ``dr.verdict`` directly — through
    :func:`decision_result_to_dict` — because the §6.3 tally needs out-of-gate records too.

    ``gate_kind`` / ``gate_is_grey_zone`` (msg-4196 DECIDED 2) are ``None`` when the admission
    gate did not run. The msg-4196 DECIDED 2 "targeted but not called" line (``dr=None``) is
    gone with tierc-v2 (msg-4380 Δ2): a targeted turn is always called.

    ``routed`` (msg-4237 DECIDED 2c-3) is a record-level column like ``stop`` — it is not a
    ``decision_result_to_dict`` key.
    """
    record: dict[str, Any] = {
        "thread_id": thread_id,
        "round_index": round_index,
        "stop": stop,
        "routed": routed,
        "gate_kind": (
            gate_result.kind.value
            if gate_result is not None and gate_result.kind is not None
            else None
        ),
        "gate_is_grey_zone": gate_result.is_grey_zone if gate_result is not None else None,
        **decision_result_to_dict(dr),
    }
    logger.info("decider_decision %s", json.dumps(record, ensure_ascii=False, sort_keys=True))
    return record


_STOP_HUMAN = "human"
"""``StopReason.HUMAN.value``. Spelled as the string because the Conductor passes
``stop_reason.value`` and ``conductor.core`` imports this module (importing ``StopReason`` back
would be circular)."""

ROUTED_STOP = "stop"
"""``_route`` stopped the turn at the human (``original_stop == HUMAN``)."""
ROUTED_FORCED_NAYSAYER = "forced_naysayer"
"""``_route`` sent the turn to a forced naysayer consult (``is_forced`` and target = naysayer)."""
ROUTED_SPAWN_BLOCKED = "spawn_blocked"
"""``_route`` stopped the turn on its ``_spawn_blocked`` branch (msg-4280 DECIDED 2c-4): the stop
is ``HUMAN`` but it is a routing dead end, not a proposal stopped at the human — record-only in
every mode. There is no ``StopReason.SPAWN_BLOCKED``; the fact comes from ``_route``'s own
``spawn_blocked`` return value (msg-4278 / msg-4280)."""


TIERC_ENTRY_ROLES: frozenset[Role] = frozenset({Role.PROPOSER, Role.IMPLEMENTER, Role.NAYSAYER})
"""Roster roles whose ``NEXT: human`` enters the Tier-C hook — Takahito's "三者" (Bohr /
Heisenberg / Einstein), msg-4360 decision 1 as fixed by msg-4382 (Einstein msg-4381 Objection 1:
existing ``Role`` members only). Today this is every ``Role``; a test pins that, so adding a role
forces an explicit decision here."""


class RoutingInvariantError(AssertionError):
    """``_route`` returned a combination for an entered ``NEXT: human`` that no ``routed`` value
    names. Unreachable today (``_human_terminal`` / ``_spawn_blocked`` are the only exits for a
    ``HUMAN`` handoff, and each maps to a named value); raised so a new ``_route`` branch fails
    loudly instead of being labelled."""


def _roster_role(roster: Mapping[str, Role], author: str) -> Role | None:
    """``roster[author]``, falling back to a case-insensitive match (the rule the Conductor routes
    by), else ``None``. An author absent from the roster (e.g. ``pr-gate-relay``) is ``None`` and
    therefore not a Tier-C target — the safe, non-intercepting side (msg-4203)."""
    direct = roster.get(author)
    if direct is not None:
        return direct
    folded = author.casefold()
    for identity, role in roster.items():
        if identity.casefold() == folded:
            return role
    return None


def is_tierc_entry(
    *,
    author_requested_human: bool,
    author_role: Role | None,
) -> bool:
    """The Tier-C hook's entry condition (msg-4237 DECIDED 2c-1; endorsed msg-4238 / msg-4240;
    roles widened by T-decider-tierc-v2 msg-4360 / msg-4382).

    Called only for a head the Conductor resolved to ``HandoffKind.HUMAN`` (``_decider_hook``
    returns earlier for any other kind). Both must hold:

    * the author named the human themself (body ``NEXT: human`` or field ``human`` — the
      :attr:`Handoff.author_requested_human` fact) — a field/body mismatch that resolved to HUMAN
      is a conductor safety valve, not someone asking the human (kept by msg-4360);
    * the author's roster role is in :data:`TIERC_ENTRY_ROLES` (``proposer`` / ``implementer`` /
      ``naysayer``) — by role, not by persona name, so a renamed agent does not silently drop
      out. An off-roster author (e.g. ``pr-gate-relay``) has no roster role and never enters:
      its APPROVE → ``NEXT: human`` is a merge-wait **notification**, not a question — main
      merges are escalated when the PR is opened (msg-4361). The naysayer's own in-thread
      ``NEXT: human`` does enter.

    The rule stop is deliberately **not** a condition (it was in 2b, msg-4203): a proposer's
    ``NEXT: human`` in a segment with no naysayer message yet is routed to a forced consult with
    ``stop=None``, and requiring ``HUMAN`` hid exactly that first escalation from the Decider
    (Einstein msg-4236). What ``_route`` did is recorded as ``routed`` instead.

    Checked at the hook's entry, before anything else: a non-target turn gets no admission-gate
    computation, no Lexora call and no ``decider_decision`` line — in shadow and active alike.
    """
    return author_requested_human and author_role is not None and author_role in TIERC_ENTRY_ROLES


def routed_from_route(
    *,
    stop: str | None,
    is_forced: bool,
    target_role: Role | None,
    spawn_blocked: bool,
    naysayer_role: Role,
) -> str:
    """``routed`` from ``_route``'s own return values (msg-4239 DECIDED 2c-1 revised).

    Read, not inferred, and exhaustive over the combination — no field short-circuits the others
    (PR-gate observation on #348):

    * ``stop`` — rule stop ``HUMAN`` **and** not forced **and** no dispatch target **and** not
      ``spawn_blocked``;
    * ``spawn_blocked`` — the same combination with ``spawn_blocked`` (msg-4280 DECIDED 2c-4);
    * ``forced_naysayer`` — no rule stop **and** ``is_forced`` **and** target = naysayer role
      **and** not ``spawn_blocked`` (``_route`` checks spawnability before every other branch,
      so both at once is a broken invariant).

    Anything else — e.g. ``stop=HUMAN`` together with ``is_forced`` (mutually exclusive by
    definition) — raises :class:`RoutingInvariantError` (Einstein msg-4240 advisory: fail loudly
    rather than record an ``other`` label).
    """
    if stop == _STOP_HUMAN and not is_forced and target_role is None:
        return ROUTED_SPAWN_BLOCKED if spawn_blocked else ROUTED_STOP
    if stop is None and is_forced and target_role is naysayer_role and not spawn_blocked:
        return ROUTED_FORCED_NAYSAYER
    raise RoutingInvariantError(
        f"decider routing invariant broken: stop={stop} is_forced={is_forced} "
        f"spawn_blocked={spawn_blocked} "
        f"target={target_role.value if target_role is not None else None}"
        " — _route に HUMAN 用の新しい分岐がないか確認"
    )


def never_retry(uuid_: str, author: str) -> bool:
    """msg-4200 DECIDED 2b-1: the compute-only gate's ``retry_lookup`` — always ``False``.

    No live path has ever bounced anyone (``append_log_entry`` / ``build_retry_lookup`` have no
    caller in ``src/``), so "no unresolved bounce exists" is the true answer today. A ``RETRY:``
    prefix therefore falls through to label evaluation — what a real gate does for a retry that
    matches nothing. When the gate is wired for real and writes the decisions log, that PR swaps
    this for ``build_retry_lookup(log_path)`` in one step (no mixed period).
    """
    return False


_GATE_KINDS: frozenset[LogKind] = frozenset(
    {LogKind.ADMIT_UNSURE, LogKind.BOUNCED, LogKind.RETRY_ADMIT}
)


def gate_result_from_decision(decision: AdmissionDecision) -> AdmissionGateResult:
    """``AdmissionDecision`` → the compact §3.5 / D17 ``AdmissionGateResult``.

    ``kind`` is the decision's ``ADMIT_UNSURE`` / ``BOUNCED`` / ``RETRY_ADMIT`` entry, or ``None``
    for a plain or label-migration admit (``AdmissionGateResult`` docstring). ``log_entries`` are
    read here and then dropped — nothing writes them.
    """
    kind: LogKind | None = None
    retry_reason: RetryAdmitReason | None = None
    for entry in decision.log_entries:
        if entry.kind in _GATE_KINDS:
            kind = entry.kind
            if entry.kind is LogKind.RETRY_ADMIT:
                retry_reason = RetryAdmitReason(str(entry.payload["reason"]))
    return AdmissionGateResult(
        verdict=decision.verdict,
        kind=kind,
        label=decision.normalized_label,
        retry_admit_reason=retry_reason,
        bounce_reason=decision.bounce_reason,
    )


def _new_bounce_uuid() -> str:
    return str(uuid.uuid4())


def compute_gate_result(
    *,
    body: str,
    author: str,
    now: datetime,
    retry_lookup: RetryLookup = never_retry,
    bounce_uuid_factory: Callable[[], str] = _new_bounce_uuid,
) -> AdmissionGateResult | None:
    """Run the admission gate compute-only (msg-4200 DECIDED 2b-1 / 2b-2).

    * **Nothing is written.** ``AdmissionDecision.log_entries`` are discarded: a ``BOUNCED`` row
      for a bounce that never happened would, once the gate is wired for real, poison the retry
      state and admit a phantom ``RETRY_ADMIT``.
    * ``bounce_uuid`` is fresh per call and discarded (``decide_admission`` docstring contract).
    * An exception from the gate yields ``None``. Under tierc-v2 the turn is still sent, with
      ``gate_result: null`` (msg-4380 Δ2); a v1 Decider skips it. The stop is unaffected.
    * The result feeds the Decider and the log only — never stop, notification or routing.
    """
    try:
        decision = decide_admission(
            body=body,
            author=author,
            retry_lookup=retry_lookup,
            now=now,
            bounce_uuid=bounce_uuid_factory(),
        )
        return gate_result_from_decision(decision)
    except Exception:
        logger.warning("admission gate failed in the decider hook; gate_result=None", exc_info=True)
        return None


async def run_tierc_hook(
    decider: Decider,
    *,
    thread_id: str,
    round_index: int,
    roster: Mapping[str, Role],
    messages: Sequence[ThreadMessage],
    stop: str | None,
    is_forced: bool,
    target_role: Role | None,
    spawn_blocked: bool,
    naysayer_role: Role,
    author_requested_human: bool,
    now: datetime | None = None,
    retry_lookup: RetryLookup = never_retry,
) -> DecisionResult | None:
    """Entry check → admission gate (compute-only) → evaluate + log. Never changes the routing.

    ``retry_lookup`` is the gate's RETRY store. ``never_retry`` while the gate is compute-only;
    under ``[tierc_gate] mode = "enforce"`` the Conductor passes the live decisions-log lookup so
    the ``gate_result`` the Decider sees is the verdict the enforced gate acts on (msg-5143).

    ``stop`` / ``is_forced`` / ``target_role`` / ``spawn_blocked`` are ``_route``'s return values
    for this turn
    (``stop`` is the rule-stop snapshot, D21). They are read to derive ``routed``; ``stop`` and
    ``routed`` are written to the log line. None of them is modified. ``now`` defaults to the
    wall clock.
    """
    head = messages[-1] if messages else None
    if head is None or not is_tierc_entry(
        author_requested_human=author_requested_human,
        author_role=_roster_role(roster, head.author),
    ):
        return None
    try:
        routed = routed_from_route(
            stop=stop,
            is_forced=is_forced,
            target_role=target_role,
            spawn_blocked=spawn_blocked,
            naysayer_role=naysayer_role,
        )
    except RoutingInvariantError:
        # Fail loud (ERROR + traceback), write no row, call nothing. Not re-raised: the hook must
        # never affect the routing (D20), and an escaping exception would stop the Conductor loop.
        logger.error("decider routing invariant broken; no decider_decision row", exc_info=True)
        return None
    try:
        gate_result = compute_gate_result(
            body=head.content,
            author=head.author,
            now=now if now is not None else datetime.now(UTC),
            retry_lookup=retry_lookup,
        )
        state = state_builder(
            turn_from_messages(
                thread_id=thread_id,
                round_index=round_index,
                roster=roster,
                messages=messages,
                gate_result=gate_result,
                dispute_rounds=count_dispute_rounds(roster, messages),
            )
        )
        # The Decider owns "is this turn a target" (mode / head); the hook asks rather than
        # restating the rule (PR-gate advisory 1 on #345). Not a target → nothing is written.
        if not decider.is_target(state):
            return None
        # tierc-v2: sent whether or not the gate produced a result (msg-4380 Δ2).
        dr = await decider.evaluate(state)
    except Exception:
        logger.warning("decider hook failed; stop decision unaffected", exc_info=True)
        return None
    if dr is None:
        # Only a v1-configured Decider with gate_result=None (msg-4196 DECIDED 1); nothing to log.
        return None
    log_decision(
        thread_id=thread_id,
        round_index=round_index,
        stop=stop,
        routed=routed,
        dr=dr,
        gate_result=state.gate_result,
    )

    # msg-4184 §2: acting code reads ``actionable_verdict`` only. msg-4237 DECIDED 2c-2: only a
    # turn ``_route`` stopped at the human (``routed == "stop"``) may be acted on; a forced
    # consult row, and a spawn-blocked dead end (msg-4280 DECIDED 2c-4), is record-only in every
    # mode (acting before the forced consult is a
    # separate Takahito decision, like ``skip_naysayer_when_confirmed``). annotate / bounce are
    # refused at build time, so under shadow this branch is structurally dead. Before bounce is
    # enabled on tierc-v2 its entry condition must be redefined (v2 has no ``fired_reason``,
    # msg-4380 Δ4).
    av = dr.actionable_verdict
    if routed == ROUTED_STOP and decider.tierc_mode in ACTING_TIERC_MODES and av is not None:
        logger.warning(
            "decider tierc mode %r has no acting implementation yet; verdict %s not acted on",
            decider.tierc_mode,
            av.kind.value,
        )
    return dr


def proceed_vetoed(dr: DecisionResult | None) -> bool:
    """G3's single reading of a proceed result, as a **veto** (T-pr-2b-3-human-identity-delegate
    msg-5219 Takahito "a" decide, Bohr msg-5227 R2): vetoed iff the Decider was called, produced
    an actionable verdict (``EVALUATED`` ∧ ``IN_GATE`` — msg-4184: acting code reads
    ``actionable_verdict`` only), that verdict is a tierc-v2 one, and its kind is ``CONFIRMED``
    ("ask the human": ``should_ask_human >= tierc_v2_ask_min``, pre-registered 0.60).

    Everything else is *not vetoed* — G3 then stays out of the way and G1 / G2 decide:
    ``None`` (not called / off), a null / malformed / transport-error outcome, a v1 verdict,
    ``UNSURE`` (the grey zone) and ``LIKELY_NOT``."""
    if dr is None:
        return False
    av = dr.actionable_verdict
    return isinstance(av, TierCV2Verdict) and av.kind is TierCVerdictKind.CONFIRMED


def veto_ask_score(dr: DecisionResult | None) -> float | None:
    """The ``ask_score`` of an actionable v2 result, for the log line and the redirect notice;
    else ``None``. Reads ``actionable_verdict`` like every acting path (msg-4184 §2-3); the raw
    verdict still reaches the log through ``decision_result_to_dict``."""
    if dr is None:
        return None
    av = dr.actionable_verdict
    return av.ask_score if isinstance(av, TierCV2Verdict) else None


async def run_proceed_veto(
    decider: Decider | None,
    *,
    thread_id: str,
    round_index: int,
    roster: Mapping[str, Role],
    messages: Sequence[ThreadMessage],
) -> tuple[bool, float | None]:
    """D-4' G3 as a veto (T-pr-2b-3-human-identity-delegate msg-5219 Takahito "a" decide, Bohr
    msg-5227 R2 / msg-5229 R3', Einstein msg-5228 / msg-5230): did the Tier-C Decider judge that
    the naysayer's proceed handoff at the head of ``messages`` must go to the human?

    Returns ``(vetoed, ask_score)``. **This is a gate, not an observer**, and it is monotone in the
    safe direction: a veto can only *close* carve-out ③, never open a route the rule-based guard
    (G1 / G2) would have closed. Only an actionable tierc-v2 ``CONFIRMED`` vetoes
    (:func:`proceed_vetoed`). Every way of not getting such a verdict answers ``False`` — the
    Decider being off (``decider is None``, ``backend=off``), declining to call (``None``),
    raising, a v1 configuration, null / malformed, ``UNSURE``, ``LIKELY_NOT`` — so a Decider
    outage does not block the loop; G1 / G2 decide (msg-5219: "off・判定を出さない・エラー・
    grey-zone・LIKELY_NOT のときは G3 は閉じず").

    The distinction between "the Decider was consulted and did not veto" and "the Decider was
    never consulted" is NOT made here: it is the conductor's cache (``_prefetch_proceed_veto``
    writes an entry on every consult, including ``decider is None``; a missing entry closes the
    door — Bohr msg-5229 R3').

    No admission gate runs (``gate_result=None``): the gate classifies ``NEXT: human`` labels and
    has nothing to say about a proceed turn.

    **Reader of the log line.** One ``decider_proceed_clearance`` ``logger.info`` record per call
    that reached the Decider (prefix kept so the shadow log stays greppable across the change;
    field ``vetoed`` plus ``ask_score``), read by the operator off the conductor's own log to
    reconcile the redirects counted in msg-5219. Nothing is posted to a chatroom thread, so no
    chatroom fallback surface is involved.
    """
    if decider is None or not messages:
        return False, None
    try:
        state = state_builder(
            turn_from_messages(
                thread_id=thread_id,
                round_index=round_index,
                roster=roster,
                messages=messages,
                gate_result=None,
                dispute_rounds=count_dispute_rounds(roster, messages),
            )
        )
        dr = await decider.clear_proceed(state)
    except Exception:
        logger.warning(
            "decider proceed veto failed; G3 veto unavailable; G1/G2 decide", exc_info=True
        )
        return False, None
    vetoed = proceed_vetoed(dr)
    ask_score = veto_ask_score(dr)
    if dr is not None:
        record: dict[str, Any] = {
            "thread_id": thread_id,
            "round_index": round_index,
            "head_msg_id": messages[-1].msg_id,
            "vetoed": vetoed,
            "ask_score": ask_score,
            **decision_result_to_dict(dr),
        }
        logger.info(
            "decider_proceed_clearance %s", json.dumps(record, ensure_ascii=False, sort_keys=True)
        )
    return vetoed, ask_score


__all__ = [
    "BODY_HEAD_M",
    "RECENT_EVENTS_N",
    "ROUTED_FORCED_NAYSAYER",
    "ROUTED_SPAWN_BLOCKED",
    "ROUTED_STOP",
    "TIERC_ENTRY_ROLES",
    "Decider",
    "RoutingInvariantError",
    "ThreadMessage",
    "compute_gate_result",
    "count_dispute_rounds",
    "gate_result_from_decision",
    "is_tierc_entry",
    "log_decision",
    "never_retry",
    "proceed_vetoed",
    "routed_from_route",
    "run_proceed_veto",
    "run_tierc_hook",
    "turn_from_messages",
    "veto_ask_score",
]
