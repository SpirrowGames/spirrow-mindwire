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
when a non-naysayer, non-human participant terminates a turn at ``human`` and no naysayer has
posted in the current segment (since the last ``NEXT: human`` boundary), the conductor forces a
single naysayer turn first, then lets the flow proceed. This enforces *consultation*, not approval
— the human decides. The Q-A reversal (msg-542 Demand 2) extends this: a content-bearing turn that
fails to route (``ABSENT``) is also a human-terminal turn, so it too gets the forced consult before
the human sees the un-reviewed design. The forced consult targets *un-reviewed agent* proposals: it
is skipped when the latest turn is the naysayer's own or the **human's own**, so Obj2 never polices
the human's instructions (an explicit ``NEXT: human`` or an "approved, go" with no ``NEXT:`` line).

Design→implement Tier-C gate (guard (i), msg-543 §PR-2b-1 / ADR-2026-06-03-17): a ``NEXT:`` to the
**implementer** from a non-human, non-naysayer author (i.e. the proposer) would let an un-reviewed,
un-approved design reach code. The conductor intercepts it and redirects to the human terminal
(Obj2 consult → Tier-C decision). The implementer may be directed only by ① a human-authored
Tier-C decide, ② the naysayer-name PR-gate REQUEST_CHANGES→fix relay (PR-2b-2), or ③ a thread-wide
human ``DELEGATE: design→impl thread`` declaration — which lifts only the human STOP (the naysayer
consult still runs, and the PR-gate + Tier-C merge stay hard backstops). This is a structural state
machine invariant, not a prompt request: the adapters are *also* taught to emit a ``NEXT:`` line
(:func:`~spirrow_mindwire.conductor.handoff.build_handoff_protocol_block`) so a cooperating loop
chains, but that prompt is advisory and the guards here are the enforcement.

The conductor never reaches ``main`` (D-5): merge-to-main stays a human / Tier-C action,
structurally out of the loop.
"""

from __future__ import annotations

import logging
import re
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
from .handoff import HUMAN_TOKEN, Handoff, HandoffKind, resolve_handoff

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ROUNDS = 40

# The human-only, thread-wide delegation declaration that lifts guard (i)'s design→implement Tier-C
# stop (msg-543 §PR-2b-1). Recognised only on a *human-authored* message (the same trust basis as
# ADR-2026-06-04-19 D-5's human-only ``naysayer_override_reason`` override), it removes only the
# human STOP — the Obj2 naysayer consult still runs and the PR-gate + Tier-C merge stay hard
# backstops. The arrow may be written ``→`` or ``->``; the line must stand on its own (MULTILINE).
_DELEGATE_RE = re.compile(r"^\s*DELEGATE:\s*design\s*(?:→|->)\s*impl\s+thread\b", re.MULTILINE)


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
        implementer_role: Role = Role.IMPLEMENTER,
        human_identity: str = HUMAN_TOKEN,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        if not naysayer_identity.strip():
            raise ValueError("naysayer_identity must be non-empty (it authors a forced review)")
        if _roster_role(roster, naysayer_identity) is not naysayer_role:
            raise ValueError(
                f"naysayer_identity {naysayer_identity!r} must map to role {naysayer_role.value!r} "
                f"in the roster: Obj2 recognises a forced naysayer turn by this mapping, so a "
                f"mismatch would force a naysayer every round to ROUND_CAP (Tier B msg-529)"
            )
        self._mcp = mcp
        self._dispatcher = dispatcher
        self._thread_ref = thread_ref
        self._roster = dict(roster)
        self._naysayer_identity = naysayer_identity
        self._max_rounds = max_rounds
        self._naysayer_role = naysayer_role
        # guard (i): the role whose direct handoff from a proposer is gated behind Tier-C, and the
        # author identity that counts as the human (Tier-C decide / DELEGATE). ``human_identity``
        # defaults to the reserved ``human`` persona (the conventional Tier-C author); an empty
        # value disables the carve-out (fail-safe — every design→implement handoff hard-rejects).
        self._implementer_role = implementer_role
        self._human_identity = human_identity

    async def run(self) -> ConductorOutcome:
        """Drive the thread turn-by-turn until a stop condition; return the outcome.

        Each turn re-reads the thread (the prior turn's reply was posted synchronously by
        ``dispatch`` → gateway). **Precondition**: the gateway is read-your-writes consistent for a
        single thread (magickit/conclair is; the shipped ``ChatroomWatcher`` relies on the same), so
        the next read reflects the just-posted reply. Under a merely eventually-consistent transport
        the no-progress guard could stop a round early — fail-safe (it routes to a human, no
        corruption); a confirm-poll for that case is a PR-2 follow-up (Tier B msg-530).
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
            target_role, target_identity, is_forced, stop_reason = self._route(handoff, messages)
            if target_role is None:
                assert stop_reason is not None  # _route always sets a reason when it stops
                return self._stop(round_index, stop_reason, latest_msg_id, forced)
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
    ) -> tuple[Role | None, str, bool, StopReason | None]:
        """Decide who to dispatch (``role is None`` = stop with the returned ``StopReason``).

        Returns ``(target_role, target_identity, is_forced, stop_reason)``; exactly one of
        ``target_role`` / ``stop_reason`` is set. The routing precedence:

        - **guard (i)** — a handoff to the implementer from a non-human, non-naysayer author is the
          design→implement Tier-C gate (msg-543): redirect to the human terminal unless a carve-out
          (human-authored / naysayer-name relay) or a human thread delegation applies.
        - **human terminal** — an explicit ``NEXT: human``, or guard (i)'s redirect: force a single
          naysayer consult if none in this segment (Obj2), else stop at the human.
        - **role** — any other named participant dispatches as named.
        - **none / absent** — settle, or (guard (ii) / Q-A) force a naysayer consult on a
          content-bearing un-routed turn before falling back to the human.
        """
        author = _author(messages[-1])
        author_role = self._roster_role(author)

        # guard (i): design→implement Tier-C gate.
        if handoff.kind is HandoffKind.ROLE and handoff.role is self._implementer_role:
            if self._is_human(author) or author_role is self._naysayer_role:
                # carve-out ① human-authored Tier-C decide / ② naysayer-name PR-gate fix relay.
                assert handoff.identity is not None
                return handoff.role, handoff.identity, False, None
            if self._design_to_impl_delegated(messages):
                # carve-out ③ human delegation: lift only the human STOP — still consult the
                # naysayer at least once (advisory), then let the implementer proceed.
                if not self._naysayer_consulted(messages):
                    return self._naysayer_role, self._naysayer_identity, True, None
                assert handoff.identity is not None
                return handoff.role, handoff.identity, False, None
            return self._human_terminal(messages)  # default: hard-reject → human terminal

        if handoff.kind is HandoffKind.HUMAN:
            return self._human_terminal(messages)

        if handoff.kind is HandoffKind.ROLE:
            assert handoff.role is not None and handoff.identity is not None
            return handoff.role, handoff.identity, False, None

        if handoff.kind is HandoffKind.NONE:
            return None, "", False, StopReason.SETTLED

        # ABSENT — guard (ii) / Q-A reversal (msg-542 Demand 2): a content-bearing turn that fails
        # to route still terminates at the human, but a non-naysayer's un-reviewed content must
        # get a naysayer consult first. An empty turn / the naysayer's own turn / the human's own
        # turn falls through to the human fallback (the no-progress guard already separates turns
        # that post nothing; the human carve-out keeps Obj2 from policing the human's own message —
        # e.g. an "approved, go" with no ``NEXT:`` line — symmetric with guard (i) and HUMAN above).
        if (
            author_role is not self._naysayer_role
            and not self._is_human(author)
            and _content(messages[-1]).strip()
            and not self._naysayer_consulted(messages)
        ):
            return self._naysayer_role, self._naysayer_identity, True, None
        return None, "", False, StopReason.NO_HANDOFF

    def _human_terminal(
        self, messages: list[dict[str, Any]]
    ) -> tuple[Role | None, str, bool, StopReason | None]:
        """Resolve a turn that terminates at the human (explicit ``NEXT: human`` or a guard (i)
        redirect): force one naysayer consult if none has happened in this segment (Obj2 —
        consultation, not veto), otherwise stop at the human for the Tier-C decision.

        The forced consult protects the Tier-C gate from *un-reviewed agent* proposals, so it is
        skipped when the latest message is the naysayer's own or the **human's own** — Obj2 must
        not police the human's own instructions (e.g. an explicit ``NEXT: human``). This mirrors
        guard (i)'s ``self._is_human(author) or author_role is self._naysayer_role`` carve-out.
        """
        author = _author(messages[-1])
        author_role = self._roster_role(author)
        if (
            author_role is not self._naysayer_role
            and not self._is_human(author)
            and not self._naysayer_consulted(messages)
        ):
            return self._naysayer_role, self._naysayer_identity, True, None
        return None, "", False, StopReason.HUMAN

    def _is_human(self, author: str) -> bool:
        """Is ``author`` the human (Tier-C) identity? Case-insensitive; empty identity ⇒ never (a
        fail-safe default that makes every design→implement handoff hard-reject)."""
        return bool(self._human_identity) and author.casefold() == self._human_identity.casefold()

    def _design_to_impl_delegated(self, messages: list[dict[str, Any]]) -> bool:
        """Has the human delegated the design→implement Tier-C stop for this thread (guard (i) ③)?

        Recognised only on a **human-authored** ``DELEGATE: design→impl thread`` declaration (the
        human-only trust basis of ADR-2026-06-04-19 D-5). It lifts only the human STOP; the Obj2
        naysayer consult and the PR-gate + Tier-C merge backstops are unaffected. Absent the
        declaration the default is hard-reject.
        """
        return any(
            self._is_human(_author(m)) and _DELEGATE_RE.search(_content(m)) for m in messages
        )

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
        return _roster_role(self._roster, author)

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


def _roster_role(roster: Mapping[str, Role], author: str) -> Role | None:
    """Resolve an author (persona name) to its role via the roster, case-insensitively."""
    direct = roster.get(author)
    if direct is not None:
        return direct
    folded = author.casefold()
    for identity, role in roster.items():
        if identity.casefold() == folded:
            return role
    return None


def _parse_occurred_at(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)  # 3.11 handles the trailing 'Z'
        except ValueError:
            pass
    return datetime.now(UTC)


__all__ = [
    "Conductor",
    "ConductorDispatcher",
    "ConductorOutcome",
    "StopReason",
]
