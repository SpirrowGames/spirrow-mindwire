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

Design→implement Tier-C gate (guard (i), msg-543 / ADR-2026-06-03-17 / Tier-C msg-553/557): a
``NEXT:`` to the **implementer** from any non-human author (the proposer or — crucially — an in-band
design-time naysayer) would let an un-reviewed, un-approved design reach code. The conductor
intercepts it and redirects to the human terminal (Obj2 consult → Tier-C decision). The implementer
may be directed by ① a human-authored Tier-C decide; ② the PR-gate REQUEST_CHANGES→fix relay
(PR-2b-2, verdict-driven, gated to its structural marker); or ③ when the project's loop control
state is ``run``, the **independent naysayer's** own proceed-handoff to the implementer — only the
naysayer may advance to code, so the proposer can never bypass an objection, and the next iteration
needs a fresh naysayer proceed after each implementation (the naysayer's handoff IS the latest
message, so a stale review cannot carry). The gate never trusts a non-human role assignment
otherwise (msg-552).

**Stamp gate (P-3, Tier-C msg-954 §2 / msg-970).** Both places that ask "has the independent
naysayer spoken?" — the Obj2 forced consult and carve-out ③ — used to answer from **authorship
alone**, which says nothing about whether the reviewer was the independent distribution. Since P-2
the naysayer adapter cannot spawn without a preflight that reads the gateway's own accounting row
back, and the dispatcher stamps that observation onto the post as the ``attest:`` line, so both
questions are now asked of the stamp (:meth:`Conductor._attested`). Un-attested, carve-out ③ is not
taken and the turn falls through to the human terminal — the pre-existing safe path.
**This is noise-reduction, not authentication**: the chatroom accepts any author with any body, so
the stamp is forgeable by anyone who can post (the same trust model :meth:`Conductor._is_human`
already documents for author names). It closes the ordinary un-attested case, not an adversarial
one, and the authoritative Tier-C guard is still the human's manual ``main`` merge.

This is a structural state machine invariant, not a prompt request: the
adapters are *also* taught to emit a ``NEXT:`` line
(:func:`~spirrow_mindwire.conductor.handoff.build_handoff_protocol_block`) so a cooperating loop
chains, but that prompt is advisory and the guards here are the enforcement.

Loop control (:mod:`spirrow_mindwire.conductor.control`): carve-out ③ used to be authorised by a
per-thread ``DELEGATE`` marker the human re-wrote every turn. It is now a **per-project, latching**
state read from conclair at the top of every round — ``run`` (③ open) / ``supervised`` (③ closed;
the pre-inversion behaviour) / ``hold`` (stop at this round boundary). Reading it per round rather
than once per run is what bounds how long a ``hold`` takes to land: one round, not one process
lifetime. A state that cannot be read is ``hold`` — the control plane being down must never hand
the loop autonomy it was not granted.

The conductor never reaches ``main`` (D-5): merge-to-main stays a human / Tier-C action,
structurally out of the loop.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from ..config import DEFAULT_CONDUCTOR_MAX_ROUNDS
from ..github.client import ReviewEvent, parse_pr_ref
from ..magickit.client import McpToolCaller
from ..source_marker import parse_attestation_marker
from ..thread_context import build_thread_context
from ..value_objects import (
    ChatroomEvent,
    EventType,
    NewMessagePayload,
    Role,
    SessionHandle,
    ThreadRef,
)
from .control import BASELINE_CONTROL_STATE, ControlState, LoopControl
from .handoff import HUMAN_TOKEN, Handoff, HandoffKind, resolve_handoff

if TYPE_CHECKING:
    from ..naysayer.pr_review import PrReviewOutcome

logger = logging.getLogger(__name__)

# Single SOT in config.py so ConductorConfig.max_rounds and this ctor default cannot drift (D-2).
_DEFAULT_MAX_ROUNDS = DEFAULT_CONDUCTOR_MAX_ROUNDS


class ConductorDispatcher(Protocol):
    """The slice of :class:`~spirrow_mindwire.dispatcher.core.Dispatcher` the conductor drives.

    The real ``Dispatcher`` satisfies this structurally; tests inject a scripted fake.
    """

    async def spawn_instance(
        self, thread_ref: ThreadRef, role: Role, instance_id: str
    ) -> SessionHandle: ...

    async def dispatch(self, handle: SessionHandle, event: ChatroomEvent) -> None: ...


class PrGate(Protocol):
    """The orchestrator slice the conductor fires for the Tier B PR-gate (PR-2b-2).

    The real :class:`~spirrow_mindwire.orchestrator.PrReviewOrchestrator` satisfies this
    structurally; tests inject a fake. ``fire_pr_review`` runs the review synchronously
    (CI-gate → Gemini judge → GitHub submit) and posts its critique to the
    ``T-pr-review-<repo>-<n>`` thread; the conductor routes by the returned verdict.
    """

    async def fire_pr_review(
        self, *, project: str, pr_ref: str
    ) -> tuple[ThreadRef, PrReviewOutcome]: ...


# The conductor authors its PR-gate verdict relay under this reserved name when it posts the Tier B
# outcome back into the design thread (PR-2b-2). It is informational only — the conductor routes by
# the deterministic fire_pr_review *verdict*, never by re-parsing this relay — so it is NOT a trust
# marker: the gate trusts the driver outcome, not any author (msg-552/557).
_PR_GATE_RELAY_AUTHOR = "pr-gate-relay"


class StopReason(StrEnum):
    """Why a conductor run stopped (returned in :class:`ConductorOutcome`)."""

    HUMAN = "human"  # NEXT: human — Tier-C decision point
    SETTLED = "none"  # NEXT: none — thread settled
    NO_HANDOFF = "no_handoff_to_human"  # Obj3: missing / unparseable NEXT → human fallback
    NO_PROGRESS = "no_progress_to_human"  # dispatched role posted nothing new → human fallback
    ROUND_CAP = "round_cap"  # runaway backstop
    EMPTY = "empty_thread"  # the thread has no messages to act on
    HOLD = "hold"  # the project's loop control state is `hold` (or could not be read)


@dataclass(frozen=True)
class ConductorOutcome:
    """The result of a :meth:`Conductor.run` — for logging and the daemon's stop handling."""

    rounds: int
    stop_reason: StopReason
    last_msg_id: str | None
    forced_naysayer_turns: int
    # Shadow / observability: forced consults on a NON-explicit-human terminal (a guard-(i)
    # design→implement redirect or an ABSENT / Q-A turn) — exactly the ones that the cost lever
    # ``force_naysayer_only_on_explicit_human`` would drop. With that lever off (default) this
    # is the counterfactual saving; with it on it is ~0 (those consults no longer fire). Counted.
    forced_naysayer_turns_saveable: int = 0


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
        orchestrator: PrGate | None = None,
        force_naysayer_only_on_explicit_human: bool = False,
        control: LoopControl | None = None,
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
        # author identity that counts as the human (Tier-C decide). ``human_identity``
        # defaults to the reserved ``human`` persona (the conventional Tier-C author); an empty
        # value disables the carve-out (fail-safe — every design→implement handoff hard-rejects).
        self._implementer_role = implementer_role
        self._human_identity = human_identity
        # PR-gate (PR-2b-2): the orchestrator that fires the Tier B independent naysayer review on a
        # ``NEXT: pr-review <ref>``, and the implementer dispatched to fix a REQUEST_CHANGES. A
        # ``None`` orchestrator / a roster without exactly one implementer disables the gate path
        # (a pr-review sentinel then routes to the human, fail-safe).
        self._orchestrator = orchestrator
        # Cost lever (default off = baseline Obj2): force the naysayer consult only on an explicit
        # ``NEXT: human`` (real Tier-C handoff), not on a guard-(i) redirect or an ABSENT / Q-A
        # un-routed turn. Narrows WHICH terminals force a consult; the per-segment single-consult
        # bound (``_naysayer_consulted``) is unchanged. Trims redundant design-loop naysayer calls.
        self._force_only_on_explicit_human = force_naysayer_only_on_explicit_human
        # Per-project loop control (Part C). ``None`` means no control plane was wired — NOT that
        # one was consulted and answered; the conductor then holds the pre-inversion
        # ``supervised`` baseline, so a bare Conductor never self-authorises code. The state is
        # re-read every round in ``run`` (see ``_read_control``); this is only the seed.
        self._control = control
        self._control_state: ControlState = BASELINE_CONTROL_STATE
        # The implementer persona is derived from the roster (the single source of truth for role
        # assignment) — not a ctor arg, which would risk disjoint state (Tier B msg-567 #2).
        self._implementer_identity = self._derive_implementer_identity()

    def _derive_implementer_identity(self) -> str:
        """The roster persona filling the implementer role, or "" if there is not exactly one.

        Used for the PR-gate RC→fix dispatch (PR-2b-2). With zero or several implementer personas a
        PR-gate REQUEST_CHANGES routes to the human instead of guessing (fail-safe).
        """
        matches = [name for name, role in self._roster.items() if role is self._implementer_role]
        return matches[0] if len(matches) == 1 else ""

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
        forced_saveable = 0
        for round_index in range(self._max_rounds):
            # Control first: a `hold` then costs one MCP read and no thread fetch, and reading it
            # per round rather than per run is what bounds an operator's HOLD to one round of
            # latency. Unreadable ⇒ `hold` (control.FAILSAFE_CONTROL_STATE) — never fail open.
            if await self._read_control() is ControlState.HOLD:
                return self._stop(
                    round_index, StopReason.HOLD, processed_msg_id, forced, forced_saveable
                )
            messages = await self._fetch_messages()
            if not messages:
                return self._stop(
                    round_index, StopReason.EMPTY, processed_msg_id, forced, forced_saveable
                )
            latest = messages[-1]
            latest_msg_id = _msg_id(latest)
            # No-progress guard: the role dispatched last round posted nothing new (empty reply /
            # self-filtered / handed to itself). Do not spin — flag a human (Obj3 spirit).
            if processed_msg_id is not None and latest_msg_id == processed_msg_id:
                return self._stop(
                    round_index, StopReason.NO_PROGRESS, latest_msg_id, forced, forced_saveable
                )

            handoff = resolve_handoff(_content(latest), self._roster)

            # PR-gate (PR-2b-2): ``NEXT: pr-review <ref>`` fires the Tier B independent naysayer
            # review synchronously (ADR-19 N-1) and routes by the *verdict* — not by any parsed NEXT
            # line — so the gate trusts the deterministic driver outcome, not an author (msg-557).
            # APPROVE / COMMENT → stop at the human (Tier-C merge; the daemon never merges, D-5).
            # REQUEST_CHANGES → dispatch the implementer to fix (carve-out ②: verdict-driven, so
            # guard (i) is never consulted). No orchestrator / no implementer persona → human.
            if handoff.kind is HandoffKind.PR_REVIEW:
                # Validate AND normalize the ref once here: parse it to the canonical owner/repo#n
                # slug so the gate always fires on a canonical ref (a raw URL is normalized here),
                # and an unparseable ref fails safe to the human instead of reaching fire_pr_review
                # (Tier B PR #103 round 4/5).
                parsed_ref = parse_pr_ref(handoff.token) if handoff.token else None
                if self._orchestrator is None or parsed_ref is None:
                    return self._stop(
                        round_index, StopReason.HUMAN, latest_msg_id, forced, forced_saveable
                    )
                verdict, relay_msg = await self._fire_pr_gate(parsed_ref.slug)
                relay_msg_id = _msg_id(relay_msg)
                # A missing relay id (post result with no msg_id) breaks no-progress tracking on
                # the continue path, so fail-safe to the human instead of re-processing the relay
                # next round (Tier B msg-572 #2). APPROVE / COMMENT also stop at the human.
                if (
                    not relay_msg_id
                    or verdict is not ReviewEvent.REQUEST_CHANGES
                    or not self._implementer_identity
                ):
                    last = relay_msg_id or latest_msg_id
                    return self._stop(round_index, StopReason.HUMAN, last, forced, forced_saveable)
                handle = sessions.get(self._implementer_identity)
                if handle is None:
                    handle = await self._dispatcher.spawn_instance(
                        self._thread_ref, self._implementer_role, self._implementer_identity
                    )
                    sessions[self._implementer_identity] = handle
                # Dispatch the implementer on the RELAY event (the verdict + critique), not its own
                # pr-review trigger — else it wakes blind to what it must fix (Tier B msg-567 #1).
                await self._dispatcher.dispatch(handle, self._to_event(relay_msg, messages))
                # Track the relay: a silent implementer leaves the relay as the next latest, so the
                # no-progress guard stops it (the relay's NEXT is never re-routed).
                processed_msg_id = relay_msg_id
                continue

            target_role, target_identity, is_forced, is_saveable, stop_reason = self._route(
                handoff, messages
            )
            if target_role is None:
                assert stop_reason is not None  # _route always sets a reason when it stops
                return self._stop(round_index, stop_reason, latest_msg_id, forced, forced_saveable)
            if is_forced:
                forced += 1
                # ``is_saveable`` comes from _route (the single source of truth for the forcing
                # decision), so the shadow metric can never drift from the lever it shadows. Counted
                # always; read it with the lever off to size the potential saving.
                if is_saveable:
                    forced_saveable += 1

            handle = sessions.get(target_identity)
            if handle is None:
                handle = await self._dispatcher.spawn_instance(
                    self._thread_ref, target_role, target_identity
                )
                sessions[target_identity] = handle
            await self._dispatcher.dispatch(handle, self._to_event(latest, messages))
            processed_msg_id = latest_msg_id
        return self._stop(
            self._max_rounds, StopReason.ROUND_CAP, processed_msg_id, forced, forced_saveable
        )

    def _route(
        self, handoff: Handoff, messages: list[dict[str, Any]]
    ) -> tuple[Role | None, str, bool, bool, StopReason | None]:
        """Decide who to dispatch (``role is None`` = stop with the returned ``StopReason``).

        Returns ``(target_role, target_identity, is_forced, is_saveable, stop_reason)``; exactly one
        of ``target_role`` / ``stop_reason`` is set. ``is_saveable`` (the shadow flag) is ``True``
        iff this forced consult is on a non-explicit-human terminal (a guard-(i) redirect or an
        ABSENT / Q-A turn) — exactly what ``force_naysayer_only_on_explicit_human`` would drop.
        Deciding it HERE, with the forcing logic, keeps it the single source of truth so the
        counterfactual metric cannot drift from the lever it shadows. The routing precedence:

        - **guard (i)** — a handoff to the implementer from any non-human author is the
          design→implement Tier-C gate (msg-543): redirect to the human terminal unless carve-out ①
          (a human-authored Tier-C decide) applies. (② PR-gate verdict relay → PR-2b-2; ③ the
          independent naysayer's proceed while the control state is ``run``.)
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
            if self._is_human(author):
                # carve-out ① human-authored Tier-C decide. Tier-C msg-553 / msg-557.
                assert handoff.identity is not None
                return handoff.role, handoff.identity, False, False, None
            # carve-out ③: when the project's loop control state is ``run``, the INDEPENDENT
            # naysayer's OWN proceed-handoff to the implementer is honored without a per-step human
            # GO. *Only* the naysayer may advance to code — the proposer cannot self-advance, which
            # would let it bypass the naysayer's objection (Einstein msg-601 Fix-1). The naysayer's
            # handoff IS this latest message (reviewing the current state), so a stale review cannot
            # carry: the next iteration needs a fresh naysayer proceed AFTER the implementer's turn
            # (reset-on-implementation). A naysayer escalation (``NEXT: human``) is not a
            # proceed-handoff and falls through to the human terminal below, so the naysayer keeps
            # its pull-the-human-back-in power at every state.
            #
            # P-3b (Tier-C msg-954 §2 / msg-970): the proceed must additionally carry the harness's
            # own preflight stamp (:meth:`_attested`). Un-stamped, the branch is simply not taken
            # and the turn falls through to ``_human_terminal`` below — the EXISTING safe path, so
            # this adds no new failure mode, only a narrower door.
            if (
                author_role is self._naysayer_role
                and self._control_state is ControlState.RUN
                and self._attested(messages[-1])
            ):
                assert handoff.identity is not None
                return handoff.role, handoff.identity, False, False, None
            # guard-(i) redirect is NOT an explicit human handoff: under the cost lever it does not
            # force a consult (explicit_human=False).
            return self._human_terminal(messages, explicit_human=False)

        if handoff.kind is HandoffKind.HUMAN:
            return self._human_terminal(messages, explicit_human=True)

        if handoff.kind is HandoffKind.ROLE:
            assert handoff.role is not None and handoff.identity is not None
            return handoff.role, handoff.identity, False, False, None

        if handoff.kind is HandoffKind.NONE:
            return None, "", False, False, StopReason.SETTLED

        # ABSENT — guard (ii) / Q-A reversal (msg-542 Demand 2): a content-bearing turn that fails
        # to route still terminates at the human, but a non-naysayer's un-reviewed content must
        # get a naysayer consult first. An empty turn / the naysayer's own turn / the human's own
        # turn falls through to the human fallback (the no-progress guard already separates turns
        # that post nothing; the human carve-out keeps Obj2 from policing the human's own message —
        # e.g. an "approved, go" with no ``NEXT:`` line — symmetric with guard (i) and HUMAN above).
        if (
            not self._force_only_on_explicit_human
            and author_role is not self._naysayer_role
            and not self._is_human(author)
            and _content(messages[-1]).strip()
            and not self._naysayer_consulted(messages)
        ):
            # ABSENT / Q-A is a non-explicit-human terminal → saveable.
            return self._naysayer_role, self._naysayer_identity, True, True, None
        return None, "", False, False, StopReason.NO_HANDOFF

    def _human_terminal(
        self, messages: list[dict[str, Any]], *, explicit_human: bool = True
    ) -> tuple[Role | None, str, bool, bool, StopReason | None]:
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
            (explicit_human or not self._force_only_on_explicit_human)
            and author_role is not self._naysayer_role
            and not self._is_human(author)
            and not self._naysayer_consulted(messages)
        ):
            # ``is_saveable`` = NOT an explicit ``NEXT: human`` (a guard-(i) redirect, where _route
            # called us with explicit_human=False). This branch IS reachable with the lever OFF: the
            # condition above is ``explicit_human OR not self._force_only_on_explicit_human``, so it
            # fires for explicit_human=False too. An explicit human handoff (explicit_human=True) is
            # kept (not saveable). Covered by test_forced_naysayer_saveable_counts_guard_i_redirect.
            return self._naysayer_role, self._naysayer_identity, True, not explicit_human, None
        return None, "", False, False, StopReason.HUMAN

    async def _read_control(self) -> ControlState:
        """Refresh the project's loop control state for this round and report what we act on.

        The observed write-back happens **before** the caller acts on the state, and for ``hold``
        as much as for the others: a hold that the loop has actually seen is exactly the fact the
        operator's dashboard is waiting for, so it must not be skipped on the path that stops.

        With no control plane wired (``self._control is None``) the state stays at
        :data:`~spirrow_mindwire.conductor.control.BASELINE_CONTROL_STATE` — deliberately *not* the
        fail-safe value, because "no control source configured" is a different fact from "the
        control source is unreachable": the former is the pre-inversion behaviour, the latter stops
        the loop.
        """
        if self._control is None:
            return self._control_state
        state = await self._control.read()
        self._control_state = state
        await self._control.report_observed(state)
        return state

    def _is_human(self, author: str) -> bool:
        """Is ``author`` the human (Tier-C) identity? Case-insensitive; empty identity ⇒ never (a
        fail-safe default that makes every design→implement handoff hard-reject).

        Author trust is the **environment** trust model (PR-2b-3 D-3): the chatroom accepts any
        ``author`` string, so this carve-out is best-effort loop-level noise-reduction, NOT the
        authoritative Tier-C guard — that is the human's manual ``main`` merge (mirrors the
        implementer allow-list's environment-containment stance). Stronger author authentication
        (ADR-11 normalization) is a deferred hardening."""
        return bool(self._human_identity) and author.casefold() == self._human_identity.casefold()

    def _attested(self, msg: dict[str, Any]) -> bool:
        """Does ``msg`` carry a well-formed harness attestation stamp (P-3, Tier-C msg-970)?

        Since P-2 the naysayer adapter cannot spawn without a preflight that reads the gateway's
        own accounting row back, and the dispatcher stamps the resulting
        :class:`~spirrow_mindwire.value_objects.AttestationRecord` onto the posted body as the
        ``attest:`` line. So a naysayer post WITHOUT that line was not produced by an attested
        session — the thing the loop's carve-outs are named after ("the *independent* naysayer")
        was never shown to be independent for that post.

        ``backend == expected`` is required as well as well-formedness, and what that buys is
        narrow enough to be worth stating exactly. It refuses a stamp whose own fields RECORD a
        mismatch — an observation that the tier resolved somewhere other than the independent
        distribution. P-2 fails closed before such a record is ever rendered, so the harness does
        not emit one today; the check is therefore defence in depth against a later change that
        stamped a mismatch "informationally" instead of refusing, plus a hand-written line that
        names some other backend.

        It buys **no replay protection**, and an earlier version of this docstring wrongly said it
        did (naysayer objection, ``T-pr-review-144`` msg-973 §1 — correct, and this is the
        correction). A stamp copied verbatim out of another thread, another session, or a
        months-old post satisfies the comparison, because ``backend`` and ``expected`` travel
        *inside* the copied line and still agree there. Nothing in the marker binds it to the
        message it sits on — no thread id, no message id, no signature — so nothing here can tell a
        replay from an original. ``test_carve_out_three_cannot_detect_a_replayed_stamp`` drives
        exactly that case through the carve-out to the implementer and passes.

        ★ **This is noise-reduction, not authentication, and the difference is not a caveat — it is
        the accurate description of what this can do.** The input is a chatroom body, and the
        chatroom accepts any ``author`` string with any text in it, so a stamp is forgeable by
        anyone who can post: exactly the trust model :meth:`_is_human` already states for author
        names. What the check buys is that the ordinary way to be un-attested — a naysayer turn
        that ran without a preflight, or a post from outside the harness — stops being
        indistinguishable from an attested one. It does not make a forged gate impossible, and
        nothing downstream should be written as though it did; the authoritative Tier-C guard
        remains the human's manual ``main`` merge.

        Only the LAST non-empty line is examined, because that is precisely where
        :func:`~spirrow_mindwire.source_marker.append_markers` puts the stamp — after the body,
        always last. Scanning the whole body would count a critique that merely *quotes* a marker
        (this arc's own review turns did exactly that) as a stamp. Note this reads a persisted
        chatroom message; msg-834 §2 (b) — "the harness never parses an agent body to *build* a
        marker" — is a constraint on the write path and is untouched.
        """
        record = parse_attestation_marker(_content(msg))
        return record is not None and record.backend == record.expected

    def _naysayer_consulted(self, messages: list[dict[str, Any]]) -> bool:
        """Has an ATTESTED naysayer posted since the last ``NEXT: human`` boundary (excl. latest)?

        The "current segment" is the run of messages after the most recent prior human handoff (or
        the thread start). A naysayer-authored message anywhere in it means this design has already
        had its independent review, so a fresh ``NEXT: human`` may proceed to the human — preventing
        an endless re-review of every disposition while still guaranteeing at least one consult.

        P-3a (Tier-C msg-954 §2 / msg-970): "naysayer-authored" is no longer sufficient — the post
        must also carry the harness's attestation stamp (:meth:`_attested`). Authorship alone said
        nothing about whether the reviewer was the independent distribution; the whole point of Obj2
        is that the consult be independent, so the marker of independence is what has to be present.

        An un-attested naysayer post therefore does not discharge the obligation and the human
        terminal forces one consult — the pre-existing Obj2 path, not a new one. The cost is bounded
        at **one** extra consult per segment: the forced turn runs through the adapter, which cannot
        spawn without attesting, so its reply carries the stamp and every later check in the segment
        is satisfied. (And a forced consult never re-fires on the naysayer's own latest message, so
        the two cannot ping-pong.)
        """
        segment = messages[:-1]  # exclude the latest msg (the one now handing to human)
        boundary = 0
        for i, msg in enumerate(segment):
            if resolve_handoff(_content(msg), self._roster).kind is HandoffKind.HUMAN:
                boundary = i + 1
        return any(
            self._roster_role(_author(msg)) is self._naysayer_role and self._attested(msg)
            for msg in segment[boundary:]
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

    async def _fire_pr_gate(self, pr_ref: str) -> tuple[ReviewEvent, dict[str, Any]]:
        """Fire the Tier B naysayer review on ``pr_ref`` and relay its verdict (PR-2b-2).

        Synchronous (ADR-19 N-1): the orchestrator runs the CI-gate + Gemini judge + GitHub
        submit and posts its critique to the ``T-pr-review-<repo>-<n>`` thread; here the
        conductor relays the verdict (with the critique body, so the implementer has its fix
        context) into the design thread. Returns the verdict and the relay **message**, so the
        implementer is dispatched on that relay event and sees the critique, not its own
        pr-review trigger (Tier B msg-567 #1).
        """
        assert self._orchestrator is not None
        _thread_ref, outcome = await self._orchestrator.fire_pr_review(
            project=self._thread_ref.project_id, pr_ref=pr_ref
        )
        relay_msg = await self._post_pr_relay(pr_ref, outcome)
        return outcome.verdict, relay_msg

    async def _post_pr_relay(self, pr_ref: str, outcome: PrReviewOutcome) -> dict[str, Any]:
        """Post the PR-gate verdict (+ critique body) into the design thread as the relay author.

        Informational only — the conductor has already chosen the route from ``outcome.verdict``;
        this post is the human-readable record and the implementer's fix context on a RC. Its
        ``NEXT:`` line mirrors the chosen route for readability but is never re-parsed by the
        conductor (no author is trusted; msg-557). Returns the posted message as a dict so the
        conductor can dispatch the implementer on it (Tier B msg-567 #1).
        """
        nxt = (
            self._implementer_identity
            if outcome.verdict is ReviewEvent.REQUEST_CHANGES and self._implementer_identity
            else HUMAN_TOKEN
        )
        body = (
            f"PR-gate (Tier B independent naysayer) — {pr_ref}\n\n"
            f"VERDICT: {outcome.verdict.value} (ci={outcome.ci_state.value})\n\n"
            f"{outcome.body}\n\n"
            f"NEXT: {nxt}"
        )
        result = await self._mcp.call_tool(
            "chatroom_post_message",
            {
                "project": self._thread_ref.project_id,
                "thread_id": self._thread_ref.thread_id,
                "msg_type": "report",
                "author": _PR_GATE_RELAY_AUTHOR,
                "content": body,
                # No ``role`` here, deliberately (D-1 sweep, T-dispatched-turn).
                # The other two harness write paths now supply one; this relay does
                # not, because it holds no role. It is the conductor restating a
                # verdict the Tier B driver produced elsewhere, and the honest value
                # for "which role authored this" is none. Claiming ``naysayer``
                # because the content came from one would put a role stamp on a post
                # no reviewer wrote — manufacturing exactly the evidence the I-6
                # invariant exists to make meaningful.
            },
        )
        return {
            "msg_id": _extract_relay_msg_id(result) or "",
            "author": _PR_GATE_RELAY_AUTHOR,
            "content": body,
        }

    def _to_event(self, msg: dict[str, Any], messages: list[dict[str, Any]]) -> ChatroomEvent:
        """Build the event for ``msg``, carrying the thread as ground truth (D-3).

        ``messages`` is this round's freshly-fetched thread. The conductor has always
        read it — to decide *who* is next — and has never handed it to the role it
        woke, which is why a dispatched turn could re-ask a question its own thread
        had already answered (msg-1167 §2).

        The context rides on the **event**, deliberately, not on ``spawn_instance``:
        :meth:`run` spawns one session per identity and reuses it for every
        subsequent round, so a spawn-time snapshot would pin round one's thread for
        the life of the run — the same staleness, one layer down. The event is
        rebuilt every round, so it is the only carrier that stays current. It also
        means no Port signature changes: ``thread_context`` is an optional field on a
        value object every adapter already receives.
        """
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
            thread_context=build_thread_context(messages, trigger_msg_id=msg_id),
        )

    def _stop(
        self,
        rounds: int,
        reason: StopReason,
        last_msg_id: str | None,
        forced: int,
        forced_saveable: int = 0,
    ) -> ConductorOutcome:
        logger.info(
            "conductor stopped: reason=%s rounds=%d forced_naysayer=%d "
            "forced_naysayer_saveable=%d last_msg=%s",
            reason.value,
            rounds,
            forced,
            forced_saveable,
            last_msg_id,
        )
        return ConductorOutcome(
            rounds=rounds,
            stop_reason=reason,
            last_msg_id=last_msg_id,
            forced_naysayer_turns=forced,
            forced_naysayer_turns_saveable=forced_saveable,
        )


def _extract_relay_msg_id(result: Any) -> str | None:
    """The msg_id of the conductor's own ``chatroom_post_message`` relay result, or ``None``."""
    if isinstance(result, dict):
        msg = result.get("msg")
        if isinstance(msg, dict) and msg.get("msg_id"):
            return str(msg["msg_id"])
    return None


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
