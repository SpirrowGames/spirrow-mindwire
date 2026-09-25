"""Conductor-side Tier-C Decider hook (T-decider-conductor-hook step 2, shadow).

Called by :class:`~spirrow_mindwire.conductor.core.Conductor` right after its rule-based routing
(``_route``) has produced a stop (or a forced consult) for a ``NEXT: human`` head. What it does:

1. build the :class:`~.state.DecisionState` from the thread (``turn_from_messages`` →
   ``state_builder`` — the same builder the replay uses);
2. ``dr = await decider.evaluate(state)`` — ``None`` only if the Decider was not called;
3. ``log_decision(...)`` — **always** when called, every outcome, with ``decision_id`` and
   ``outcome``, the gate columns ``gate_kind`` / ``gate_is_grey_zone`` and the rule ``stop`` on
   the same line (msg-4182 / msg-4186 §2: pre-emption by a rule stop is read off the ``stop``
   column; ``dr`` is never rewritten), and also once, with ``outcome`` empty, when the Decider
   was not called because the admission gate did not run (msg-4196 DECIDED 2);
4. the acting branch is gated on ``dr.actionable_verdict`` only (msg-4184) and on a mode of
   ``annotate`` / ``bounce`` — which step 2 refuses at build time, so in shadow it never runs.

**Monotonicity (D20).** Nothing here returns a new stop. The hook returns the result only so a
caller/test can observe it; the Conductor discards it. Any exception from the Decider is caught
and logged — it must never reach the stop decision.

**Reader of the log line.** The payload is a structured ``logger.info`` record under the logger
``spirrow_mindwire.decider.hook`` (prefix ``decider_decision``), read by the operator / the §6
evaluation join (thread_id + round + decision_id) off the conductor's own log. It is not posted
to any chatroom thread, so no chatroom fallback surface is involved.

**Admission gate not wired yet (msg-4196).** The live Conductor does not run the Tier-C
admission gate (``tier_c_admission_gate.decide_admission`` has no caller in ``conductor/``), so
``gate_result`` is ``None`` on every live turn. msg-4196 DECIDED 1: such a turn is **not** sent to
Lexora (``evaluate`` returns ``None``); DECIDED 2: the hook still writes one ``decider_decision``
line for it with an empty ``outcome`` and ``gate_kind`` / ``gate_is_grey_zone`` = ``None``, so the
turns missed before the gate is wired (step 2b) can be counted, and pre-/post-wiring lines are
told apart by the gate columns rather than by date.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from spirrow_mindwire.decider.result import DecisionResult, decision_result_to_dict
from spirrow_mindwire.decider.state import (
    AdmissionGateResult,
    DecisionState,
    EventSummary,
    SimpleTurn,
    state_builder,
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
    dr: DecisionResult | None,
    gate_result: AdmissionGateResult | None,
) -> dict[str, Any]:
    """Write one decision record to the conductor log and return it (msg-4182).

    The one place (with the replay record) allowed to read ``dr.verdict`` directly — through
    :func:`decision_result_to_dict` — because the §6.3 tally needs out-of-gate records too.

    ``gate_kind`` / ``gate_is_grey_zone`` (msg-4196 DECIDED 2) are ``None`` when the admission
    gate did not run. ``dr=None`` writes the same keys with ``outcome`` empty — the "hook
    targeted this turn, Decider not called" line.
    """
    record: dict[str, Any] = {
        "thread_id": thread_id,
        "round_index": round_index,
        "stop": stop,
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


_HUMAN_NEXT = "human"
"""The reserved ``parsed_next`` of a human handoff (same value the adapter gates on)."""


def _is_tierc_target(decider: Decider, state: DecisionState) -> bool:
    """Whether this turn is one the Tier-C Decider is meant to evaluate at all.

    Mirrors the non-gate half of the adapter's own skip condition: a Decider in mode ``off``
    or a head that is not ``NEXT: human`` is not a target, so a ``None`` from ``evaluate`` there
    is not a "missed" turn.
    """
    return decider.tierc_mode != "off" and state.parsed_next == _HUMAN_NEXT


async def run_tierc_hook(
    decider: Decider,
    *,
    thread_id: str,
    round_index: int,
    roster: Mapping[str, Role],
    messages: Sequence[ThreadMessage],
    stop: str | None,
    gate_result: AdmissionGateResult | None = None,
) -> DecisionResult | None:
    """Evaluate + log. Returns the result for observation; it never changes ``stop``."""
    try:
        state = state_builder(
            turn_from_messages(
                thread_id=thread_id,
                round_index=round_index,
                roster=roster,
                messages=messages,
                gate_result=gate_result,
            )
        )
        dr = await decider.evaluate(state)
    except Exception:
        logger.warning("decider hook failed; stop decision unaffected", exc_info=True)
        return None
    if dr is None:
        # msg-4196 DECIDED 2: a turn the hook TARGETED but did not send because the admission
        # gate did not run is still recorded (outcome empty), so the turns missed before step 2b
        # wires the gate can be counted. ``evaluate`` also returns ``None`` for turns that were
        # never targets (mode off, head not ``NEXT: human``); those write nothing — ``gate_result
        # is None`` alone cannot tell the two apart (PR-gate on #345).
        if _is_tierc_target(decider, state) and state.gate_result is None:
            log_decision(
                thread_id=thread_id,
                round_index=round_index,
                stop=stop,
                dr=None,
                gate_result=None,
            )
        return None
    log_decision(
        thread_id=thread_id,
        round_index=round_index,
        stop=stop,
        dr=dr,
        gate_result=state.gate_result,
    )

    # msg-4184 §2: acting code reads ``actionable_verdict`` only. annotate / bounce are refused
    # at build time in step 2, so under shadow this branch is structurally dead; the shape is
    # fixed here so later steps start from it.
    av = dr.actionable_verdict
    if decider.tierc_mode in ACTING_TIERC_MODES and av is not None and stop is None:
        logger.warning(
            "decider tierc mode %r has no acting implementation yet; verdict %s not acted on",
            decider.tierc_mode,
            av.kind.value,
        )
    return dr


__all__ = [
    "BODY_HEAD_M",
    "RECENT_EVENTS_N",
    "Decider",
    "ThreadMessage",
    "log_decision",
    "run_tierc_hook",
    "turn_from_messages",
]
