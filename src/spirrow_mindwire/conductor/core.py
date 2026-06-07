"""Conductor — the NEXT-driven single-thread design-loop driver (msg-520 / Tier-C decide msg-523).

The conductor replaces the design-loop ``ChatroomWatcher`` auto-reply intake (Obj1, msg-522):
instead of every participant auto-replying to every message (which ping-pongs / steals events,
msg-385), the conductor reads ONE task thread, parses the latest message's ``NEXT: <participant>``
handoff, and dispatches **exactly that one participant** for a single serial turn. The dispatched
role's reply (posted by the existing :class:`~spirrow_mindwire.dispatcher.core.Dispatcher` ->
gateway path) carries the next ``NEXT:``; the conductor reads it and repeats. There is therefore
one actor per turn and no concurrent ``_seen`` watcher engine on this path.

Stop conditions (D-4):

- ``NEXT: human`` — a Tier-C decision point: stop and leave the thread for the human.
- ``NEXT: none`` — the thread is settled.
- a missing / unparseable / unknown ``NEXT:`` (Obj3) — stop and flag a human, never silently halt.
- no progress (the dispatched role posted nothing new) — stop and flag a human.
- the round cap — a runaway backstop.

Naysayer enforcement (Obj2, msg-522/523): the design-time naysayer is *advisory, not a veto*
(ADR-17 D-5), but it must be **consulted at least once** before a design reaches the human. So
when a non-naysayer participant hands to ``human`` and no naysayer has posted in the current
segment (since the last ``NEXT: human`` boundary), the conductor forces a single naysayer turn
first, then lets the flow proceed. This enforces *consultation*, not approval — the human decides.

The conductor never reaches ``main`` (D-5): merge-to-main stays a human / Tier-C action,
structurally out of the loop. NB: the role adapters must end their replies with a ``NEXT:`` line
for the loop to chain — teaching the proposer / implementer / naysayer adapters to emit one is the
daemon-wiring follow-up (PR-2); the conductor degrades to a human fallback when one omits it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from ..magickit.client import McpToolCaller
from ..value_objects import (
    ChatroomEvent,
    EventType,
    NewMessagePayload,
    Role,
    SessionHandle,
    ThreadRef,
)
from .handoff import Handoff, HandoffKind, resolve_handoff

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ROUNDS = 40


class ConductorDispatcher(Protocol):
    """The slice of :class:`~spirrow_mindwire.dispatcher.core.Dispatcher` the conductor drives.

    The real ``Dispatcher`` satisfies this structurally; tests inject a scripted fake.
    """

    async def spawn_instance(
        self, thread_ref: ThreadRef, role: Role, instance_id: str
    ) -> SessionHandle: ...

    async def dispatch(self, handle: SessionHandle, event: ChatroomEvent) -> None: ...


class StopReason(StrEnum):
    """Why a conductor run stopped (returned in :class:`ConductorOutcome`)."""

    HUMAN = "human"  # NEXT: human — Tier-C decision point
    SETTLED = "none"  # NEXT: none — thread settled
    NO_HANDOFF = "no_handoff_to_human"  # Obj3: missing / unparseable NEXT → human fallback
    NO_PROGRESS = "no_progress_to_human"  # dispatched role posted nothing new → human fallback
    ROUND_CAP = "round_cap"  # runaway backstop
    EMPTY = "empty_thread"  # the thread has no messages to act on


@dataclass(frozen=True)
class ConductorOutcome:
    """The result of a :meth:`Conductor.run` — for logging and the daemon's stop handling."""

    rounds: int
    stop_reason: StopReason
    last_msg_id: str | None
    forced_naysayer_turns: int


class Conductor:
    """Serial, NEXT-driven driver for one design thread (the autonomous relay, msg-520)."""

    def __init__(
        self,
        *,
        mcp: McpToolCaller,
        dispatcher: ConductorDispatcher,
        thread_ref: ThreadRef,
        roster: Mapping[str, Role],
        naysayer_identity: str,
        max_rounds: int = _DEFAULT_MAX_ROUNDS,
        naysayer_role: Role = Role.NAYSAYER,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        if not naysayer_identity.strip():
            raise ValueError("naysayer_identity must be non-empty (it authors a forced review)")
        self._mcp = mcp
        self._dispatcher = dispatcher
        self._thread_ref = thread_ref
        self._roster = dict(roster)
        self._naysayer_identity = naysayer_identity
        self._max_rounds = max_rounds
        self._naysayer_role = naysayer_role

    async def run(self) -> ConductorOutcome:
        """Drive the thread turn-by-turn until a stop condition; return the outcome.

        Each turn re-reads the thread (the prior turn's reply was posted synchronously by
        ``dispatch`` → gateway), so the loop sees a fresh latest message and never needs to poll.
        One adapter session is kept per participant **identity** and reused across turns (so a
        participant accumulates context in its session); each identity is (re-)spawned lazily.
        """
        # Keyed by identity, not role: two distinct personas sharing a role must each get their own
        # session, else the second persona's turn is misrouted to the first (Tier B msg-526).
        sessions: dict[str, SessionHandle] = {}
        processed_msg_id: str | None = None
        forced = 0
        for round_index in range(self._max_rounds):
            messages = await self._fetch_messages()
            if not messages:
                return self._stop(round_index, StopReason.EMPTY, processed_msg_id, forced)
            latest = messages[-1]
            latest_msg_id = _msg_id(latest)
            # No-progress guard: the role dispatched last round posted nothing new (empty reply /
            # self-filtered / handed to itself). Do not spin — flag a human (Obj3 spirit).
            if processed_msg_id is not None and latest_msg_id == processed_msg_id:
                return self._stop(round_index, StopReason.NO_PROGRESS, latest_msg_id, forced)

            handoff = resolve_handoff(_content(latest), self._roster)
            target_role, target_identity, is_forced = self._route(handoff, messages)
            if target_role is None:
                return self._stop(round_index, _stop_for(handoff), latest_msg_id, forced)
            if is_forced:
                forced += 1

            handle = sessions.get(target_identity)
            if handle is None:
                handle = await self._dispatcher.spawn_instance(
                    self._thread_ref, target_role, target_identity
                )
                sessions[target_identity] = handle
            await self._dispatcher.dispatch(handle, self._to_event(latest))
            processed_msg_id = latest_msg_id
        return self._stop(self._max_rounds, StopReason.ROUND_CAP, processed_msg_id, forced)

    def _route(
        self, handoff: Handoff, messages: list[dict[str, Any]]
    ) -> tuple[Role | None, str, bool]:
        """Decide the participant to dispatch (``None`` = stop) + whether the turn was forced.

        Obj2 enforcement: a ``NEXT: human`` from a non-naysayer author, with no naysayer post in the
        current segment, is overridden to a single forced naysayer turn (consultation, not veto).
        ``ROLE`` handoffs dispatch as named; ``NONE`` / ``ABSENT`` stop.
        """
        if handoff.kind is HandoffKind.HUMAN:
            author_role = self._roster_role(_author(messages[-1]))
            if author_role is not self._naysayer_role and not self._naysayer_consulted(messages):
                return self._naysayer_role, self._naysayer_identity, True
            return None, "", False
        if handoff.kind is HandoffKind.ROLE:
            assert handoff.role is not None and handoff.identity is not None
            return handoff.role, handoff.identity, False
        return None, "", False  # NONE / ABSENT

    def _naysayer_consulted(self, messages: list[dict[str, Any]]) -> bool:
        """Has the naysayer posted since the last ``NEXT: human`` boundary (excl. the latest msg)?

        The "current segment" is the run of messages after the most recent prior human handoff (or
        the thread start). A naysayer-authored message anywhere in it means this design has already
        had its independent review, so a fresh ``NEXT: human`` may proceed to the human — preventing
        an endless re-review of every disposition while still guaranteeing at least one consult.
        """
        segment = messages[:-1]  # exclude the latest msg (the one now handing to human)
        boundary = 0
        for i, msg in enumerate(segment):
            if resolve_handoff(_content(msg), self._roster).kind is HandoffKind.HUMAN:
                boundary = i + 1
        return any(
            self._roster_role(_author(msg)) is self._naysayer_role for msg in segment[boundary:]
        )

    def _roster_role(self, author: str) -> Role | None:
        """Resolve a message author (persona name) to its role, case-insensitively."""
        direct = self._roster.get(author)
        if direct is not None:
            return direct
        folded = author.casefold()
        for identity, role in self._roster.items():
            if identity.casefold() == folded:
                return role
        return None

    async def _fetch_messages(self) -> list[dict[str, Any]]:
        result = await self._mcp.call_tool(
            "chatroom_get_thread",
            {
                "project": self._thread_ref.project_id,
                "thread_id": self._thread_ref.thread_id,
                "mode": "full",
            },
        )
        messages = result.get("messages", []) if isinstance(result, dict) else []
        return [m for m in messages if isinstance(m, dict)]

    def _to_event(self, msg: dict[str, Any]) -> ChatroomEvent:
        msg_id = _msg_id(msg)
        return ChatroomEvent(
            # thread-namespaced stable id (mirrors ChatroomWatcher) for the dispatcher's I4 dedup.
            event_id=f"{self._thread_ref.thread_id}:{msg_id}",
            event_type=EventType.NEW_MESSAGE,
            thread_ref=self._thread_ref,
            occurred_at=_parse_occurred_at(msg.get("timestamp")),
            payload=NewMessagePayload(
                msg_id=msg_id,
                author=_author(msg),
                body=_content(msg),
                parent_msg_id=msg.get("reply_to") or None,
            ),
        )

    def _stop(
        self, rounds: int, reason: StopReason, last_msg_id: str | None, forced: int
    ) -> ConductorOutcome:
        logger.info(
            "conductor stopped: reason=%s rounds=%d forced_naysayer=%d last_msg=%s",
            reason.value,
            rounds,
            forced,
            last_msg_id,
        )
        return ConductorOutcome(
            rounds=rounds, stop_reason=reason, last_msg_id=last_msg_id, forced_naysayer_turns=forced
        )


def _msg_id(msg: dict[str, Any]) -> str:
    return str(msg.get("msg_id", ""))


def _author(msg: dict[str, Any]) -> str:
    return str(msg.get("author", ""))


def _content(msg: dict[str, Any]) -> str:
    return str(msg.get("content", ""))


def _parse_occurred_at(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)  # 3.11 handles the trailing 'Z'
        except ValueError:
            pass
    return datetime.now(UTC)


def _stop_for(handoff: Handoff) -> StopReason:
    if handoff.kind is HandoffKind.HUMAN:
        return StopReason.HUMAN
    if handoff.kind is HandoffKind.NONE:
        return StopReason.SETTLED
    return StopReason.NO_HANDOFF  # ABSENT


__all__ = [
    "Conductor",
    "ConductorDispatcher",
    "ConductorOutcome",
    "StopReason",
]
