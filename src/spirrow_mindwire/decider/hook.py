"""Conductor-side Tier-C Decider hook (T-decider-conductor-hook step 2 / 2b / 2c, shadow).

Called by :class:`~spirrow_mindwire.conductor.core.Conductor` right after its rule-based routing
(``_route``) has decided the turn. What it does:

0. **entry check** (msg-4237 DECIDED 2c-1, :func:`is_tierc_entry`): only a turn whose author
   wrote ``NEXT: human`` themself and holds the ``proposer`` role in the roster (the caller has
   already resolved the handoff to ``HUMAN``). The rule stop is **not** part of the entry: a
   proposer escalation that ``_route`` turned into a forced naysayer consult (``stop=None``) is
   evaluated too. Any other turn returns at once — no gate, no Lexora call, no log line;
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
   builder the replay uses) with that ``gate_result``;
3. ask the Decider whether the turn is a target (``decider.is_target`` — mode / head; the rule
   lives in the Decider, not here); if the gate gave no result, write the empty-``outcome`` line
   (msg-4196 DECIDED 2) and do not call Lexora (DECIDED 1); otherwise ``decider.evaluate``;
4. ``log_decision(...)`` — every outcome, with ``decision_id`` / ``outcome``, the gate columns
   ``gate_kind`` / ``gate_is_grey_zone``, the rule ``stop`` and ``routed`` on the same line;
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
    )


_NOT_CALLED_FIELDS: dict[str, Any] = {
    "outcome": None,
    "decision_id": None,
    "provider": None,
    "raw_answers": None,
    "verdict": None,
    "policy": None,
    "questions_version": None,
    "latency_ms": None,
    "error": None,
}
"""The ``decision_result_to_dict`` keys, all empty: the line for a turn the hook targeted but
did not send to Lexora (msg-4196 DECIDED 2 — ``outcome`` empty)."""


def log_decision(
    *,
    thread_id: str,
    round_index: int,
    stop: str | None,
    routed: str,
    dr: DecisionResult | None,
    gate_result: AdmissionGateResult | None,
) -> dict[str, Any]:
    """Write one decision record to the conductor log and return it (msg-4182).

    The one place (with the replay record) allowed to read ``dr.verdict`` directly — through
    :func:`decision_result_to_dict` — because the §6.3 tally needs out-of-gate records too.

    ``gate_kind`` / ``gate_is_grey_zone`` (msg-4196 DECIDED 2) are ``None`` when the admission
    gate did not run. ``dr=None`` writes the same keys with ``outcome`` empty — the "hook
    targeted this turn, Decider not called" line.

    ``routed`` (msg-4237 DECIDED 2c-3) is a record-level column like ``stop``, so the called line
    and the empty-``outcome`` line carry it alike — it is not a ``decision_result_to_dict`` key
    and therefore not part of ``_NOT_CALLED_FIELDS``.
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
        **(decision_result_to_dict(dr) if dr is not None else _NOT_CALLED_FIELDS),
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


class RoutingInvariantError(AssertionError):
    """``_route`` returned a combination for a proposer ``NEXT: human`` that no ``routed`` value
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
    author_wrote_next_human: bool,
    author_role: Role | None,
) -> bool:
    """The Tier-C hook's entry condition (msg-4237 DECIDED 2c-1; endorsed msg-4238 / msg-4240).

    Called only for a head the Conductor resolved to ``HandoffKind.HUMAN`` (``_decider_hook``
    returns earlier for any other kind). Both must hold:

    * the author wrote ``NEXT: human`` themself — a field/body mismatch that resolved to HUMAN is
      a conductor safety valve, not someone asking the human;
    * the author's roster role is ``proposer`` — by role, not by persona name, so a renamed
      proposer does not silently drop out. An implementer's merge handoff, a naysayer's exit turn
      (``VERDICT: APPROVE``, including a forced consult that ends the escalation) and an
      off-roster infra author never enter (Einstein msg-4202, Bohr msg-4235 §1).

    The rule stop is deliberately **not** a condition (it was in 2b, msg-4203): a proposer's
    ``NEXT: human`` in a segment with no naysayer message yet is routed to a forced consult with
    ``stop=None``, and requiring ``HUMAN`` hid exactly that first escalation from the Decider
    (Einstein msg-4236). What ``_route`` did is recorded as ``routed`` instead.

    Checked at the hook's entry, before anything else: a non-target turn gets no admission-gate
    computation, no Lexora call and no ``decider_decision`` line — in shadow and active alike.
    """
    return author_wrote_next_human and author_role is Role.PROPOSER


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
    * An exception from the gate yields ``None``: the Decider is then not called (msg-4196
      DECIDED 1) and the hook writes its empty-``outcome`` line. The stop is unaffected.
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
    author_wrote_next_human: bool,
    now: datetime | None = None,
) -> DecisionResult | None:
    """Entry check → admission gate (compute-only) → evaluate + log. Never changes the routing.

    ``stop`` / ``is_forced`` / ``target_role`` / ``spawn_blocked`` are ``_route``'s return values
    for this turn
    (``stop`` is the rule-stop snapshot, D21). They are read to derive ``routed``; ``stop`` and
    ``routed`` are written to the log line. None of them is modified. ``now`` defaults to the
    wall clock.
    """
    head = messages[-1] if messages else None
    if head is None or not is_tierc_entry(
        author_wrote_next_human=author_wrote_next_human,
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
        )
        state = state_builder(
            turn_from_messages(
                thread_id=thread_id,
                round_index=round_index,
                roster=roster,
                messages=messages,
                gate_result=gate_result,
            )
        )
        # The Decider owns "is this turn a target" (mode / head); the hook asks rather than
        # restating the rule (PR-gate advisory 1 on #345). Not a target → nothing is written.
        if not decider.is_target(state):
            return None
        if state.gate_result is None:
            # msg-4196 DECIDED 2: a targeted turn the gate could not classify is not sent
            # (DECIDED 1) but still leaves one line with ``outcome`` empty.
            log_decision(
                thread_id=thread_id,
                round_index=round_index,
                stop=stop,
                routed=routed,
                dr=None,
                gate_result=None,
            )
            return None
        dr = await decider.evaluate(state)
    except Exception:
        logger.warning("decider hook failed; stop decision unaffected", exc_info=True)
        return None
    if dr is None:
        # Not reachable under the Decider contract (target + gate ran ⇒ called); nothing to log.
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
    # refused at build time, so under shadow this branch is structurally dead.
    av = dr.actionable_verdict
    if routed == ROUTED_STOP and decider.tierc_mode in ACTING_TIERC_MODES and av is not None:
        logger.warning(
            "decider tierc mode %r has no acting implementation yet; verdict %s not acted on",
            decider.tierc_mode,
            av.kind.value,
        )
    return dr


__all__ = [
    "BODY_HEAD_M",
    "RECENT_EVENTS_N",
    "ROUTED_FORCED_NAYSAYER",
    "ROUTED_SPAWN_BLOCKED",
    "ROUTED_STOP",
    "Decider",
    "RoutingInvariantError",
    "ThreadMessage",
    "compute_gate_result",
    "gate_result_from_decision",
    "is_tierc_entry",
    "log_decision",
    "never_retry",
    "routed_from_route",
    "run_tierc_hook",
    "turn_from_messages",
]
