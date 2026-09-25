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
from ..decider.hook import Decider, ThreadMessage, run_tierc_hook
from ..gate_admission import RED_CONCLUSIONS, AdmissionResult, GateAdmission, gate_admission
from ..github.client import CheckRollup, PrRef, ReviewEvent, parse_pr_ref
from ..identity.embodiment import blocked_embodiment, normalize_embodiment_table
from ..identity.normalize import normalize_identity_key
from ..magickit.client import McpToolCaller, ThreadResolvedError
from ..routing import GuardIVerdict, guard_proposer_to_implementer
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
from .gate_records import (
    RELAY_AUTHOR,
    ci_route_heads,
    normalize_sha,
    render_ci_route_marker,
    verdict_heads,
)
from .handoff import HUMAN_TOKEN, Handoff, HandoffKind, parse_next_token, resolve_handoff
from .roster import RoleResolutionError, derive_identity_by_role

if TYPE_CHECKING:
    from ..naysayer.pr_review import PrReviewOutcome

logger = logging.getLogger(__name__)

# Single SOT in config.py so ConductorConfig.max_rounds and this ctor default cannot drift (D-2).
_DEFAULT_MAX_ROUNDS = DEFAULT_CONDUCTOR_MAX_ROUNDS

#: The reserved author under which the conductor's guard-(i) redirect write-back is posted
#: (T-human-terminal-overuse D-1, Bohr msg-2540 § approved by Einstein msg-2539). When a non-human,
#: non-attested-naysayer author nominates the implementer, guard (i) redirects to the human
#: terminal and this relay writes ONE observation into the design thread so the head moves off the
#: rejected `NEXT: <implementer>` token. Without it, ``head_skip`` Stage 1 does not SKIP (that
#: stop-token set is a closed set of ``human`` / ``none``) and the sweep re-launches the same head
#: forever — measured 288 times across 5 threads before this landed (msg-2537 §4).
#:
#: DELIBERATELY DISTINCT FROM :data:`~.gate_records.RELAY_AUTHOR`. That relay author is the key
#: :func:`~.gate_records.ci_route_heads` and :func:`~.gate_records.verdict_heads` filter on
#: (``gate_records`` module docstring, "the two readers are deliberately restricted to messages
#: authored by the conductor's relay author"); reusing the same string here would silently mix
#: D-1 write-backs into that reader stream. Einstein's msg-2539 Objection 1 flagged the identity
#: registration side; the same fact keeps the two writers apart for the reader side too.
#:
#: Registered as ``kind=machine`` / ``legitimate=[]`` in ``spec/identity/legitimate_roles.yaml``
#: (I-6 invariant, msg-2540 §1-4: the loader hard-rejects ``kind=machine`` with a non-empty
#: legitimate list, so a route around the invariant is structurally impossible).
CONDUCTOR_RELAY_AUTHOR = "conductor-relay"


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
    (CI-gate → Gemini judge → GitHub submit), posts its critique to the
    ``T-pr-review-<repo>-<n>`` ledger thread **and** relays the verdict into ``design_thread``;
    the conductor routes by the returned verdict and dispatches on the returned relay message.
    """

    async def fire_pr_review(
        self, *, project: str, pr_ref: str, design_thread: str, implementer: str | None
    ) -> tuple[ThreadRef, PrReviewOutcome, dict[str, Any]]: ...


class CheckRollupSource(Protocol):
    """The one read pre-gate admission needs: the head's check rollup + its two clocks.

    The real :class:`~spirrow_mindwire.github.client.GitHubClient` satisfies this structurally.
    A ``None`` return means the rollup could not be READ — never "there are no checks" — and the
    conductor's fail direction for it is its pre-wiring behaviour (fire the gate), so a repo
    whose rollup is unreadable behaves exactly as it did before ``gate_admission`` was wired.

    Kept as a Protocol here rather than taking a ``GitHubClient`` so the conductor holds no
    HTTP dependency of its own and the admission path is drivable from a scripted fake, the same
    shape :class:`PrGate` already uses for the gate itself.
    """

    async def fetch_check_rollup(self, pr: PrRef) -> CheckRollup | None: ...


class StopReason(StrEnum):
    """Why a conductor run stopped (returned in :class:`ConductorOutcome`)."""

    HUMAN = "human"  # NEXT: human — Tier-C decision point
    SETTLED = "none"  # NEXT: none — thread settled
    NO_HANDOFF = "no_handoff_to_human"  # Obj3: missing / unparseable NEXT → human fallback
    NO_PROGRESS = "no_progress_to_human"  # dispatched role posted nothing new → human fallback
    # The head's handoff names its own author (``author == next``). Design §6.1: this used to be
    # discovered one layer down, in the adapter's ``deliver_event`` self-filter, which returned in
    # silence — the session was spawned, ``query()`` never ran, no reply was posted, and the round
    # ended on NO_PROGRESS. Measured on two threads that sat that way for days at one retry per
    # hour. Detecting it in ``_route`` means the spawn never happens and the stop names its cause.
    SELF_HANDOFF = "self_handoff_to_human"
    ROUND_CAP = "round_cap"  # runaway backstop
    EMPTY = "empty_thread"  # the thread has no messages to act on
    HOLD = "hold"  # the project's loop control state is `hold` (or could not be read)
    # Pre-gate admission (design v0.3.1 §5.2A, R1a / R2) said DEFER: CI on the PR head has not
    # concluded and the wait budget has not run out. Deliberately NOT `HUMAN` — nobody is
    # summoned and nothing is posted (deferrals write no record, §5.2A.5), because the answer to
    # "is CI finished yet" is not a question worth stopping a person for. The next scheduled
    # tick re-reads the same handoff and re-derives admission from the fresh rollup; the wait is
    # therefore held by GitHub's state, not by a mindwire-side timer (§A-2 statelessness).
    CI_WAIT = "ci_wait"


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
        rollup_source: CheckRollupSource | None = None,
        identity_embodiment: Mapping[str, str] | None = None,
        decider: Decider | None = None,
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
        # Pre-gate CI-wait admission (design v0.3.1 §5.2A / RES-WIRING). ``None`` disables
        # admission entirely and the PR-gate path is byte-for-byte its pre-wiring self: every
        # ``NEXT: pr-review`` fires the gate immediately. That is also the fail direction when a
        # source IS wired but cannot read (see ``_admit``), so "admission is off" and "admission
        # could not see" produce the same, already-shipped behaviour rather than two new ones.
        self._rollup_source = rollup_source
        # ADR-2026-09-14-21 D-2 / D-3: identities the conductor must NOT spawn, by embodiment.
        # Merged over the ADR's shipped default (Fermi = web_ai_chat) so a loop host that never
        # wrote the config line still refuses to spawn-attempt a web identity. An identity absent
        # from the table is spawnable — the roster is already the operator's statement that these
        # personas are driven from here (see :mod:`..identity.embodiment`).
        self._embodiments = normalize_embodiment_table(identity_embodiment)
        # Cost lever (default off = baseline Obj2): force the naysayer consult only on an explicit
        # ``NEXT: human`` (real Tier-C handoff), not on a guard-(i) redirect or an ABSENT / Q-A
        # un-routed turn. Narrows WHICH terminals force a consult; the per-segment single-consult
        # bound (``_naysayer_consulted``) is unchanged. Trims redundant design-loop naysayer calls.
        self._force_only_on_explicit_human = force_naysayer_only_on_explicit_human
        # Tier-C Decider (T-decider-conductor-hook step 2). ``None`` = off (the default and the
        # pre-step-2 behaviour, byte-for-byte). When wired it is an OBSERVER: ``_decider_hook``
        # logs a decision on every explicit ``NEXT: human`` head and never changes the routing
        # decision ``_route`` already made (D20 monotonicity).
        self._decider = decider
        # Per-project loop control (Part C). ``None`` means no control plane was wired — NOT that
        # one was consulted and answered; the conductor then holds the pre-inversion
        # ``supervised`` baseline, so a bare Conductor never self-authorises code. The state is
        # re-read every round in ``run`` (see ``_read_control``); this is only the seed.
        self._control = control
        self._control_state: ControlState = BASELINE_CONTROL_STATE
        # The implementer persona is derived from the roster (the single source of truth for role
        # assignment) — not a ctor arg, which would risk disjoint state (Tier B msg-567 #2). The
        # resolver lives in :mod:`.roster` and is shared with the hand-run PR-gate driver so both
        # lanes read the same code against the same SOT
        # (T-hand-fired-gate-cannot-name-the-implementer msg-3885). The daemon's fail direction
        # here is fail-safe (``None`` → the RC→fix dispatch falls through to ``NEXT: human``,
        # byte-identical to the pre-refactor ``""`` sentinel); the hand-run driver's fail
        # direction is fail-loud (msg-3849). Scope fence: no ``logger.warning`` in this except —
        # that would be a conductor-side observability change, explicitly out of scope for this
        # fix (msg-3851 §Why the conductor swallows).
        try:
            self._implementer_identity: str | None = derive_identity_by_role(
                self._roster, self._implementer_role
            )
        except RoleResolutionError:
            self._implementer_identity = None

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

            handoff = resolve_handoff(
                _content(latest),
                self._roster,
                # Layer 3 (Bohr msg-179 §3): the structured envelope field wins over the body's
                # ``NEXT:`` line, and the body is used only as a lint against it (§3-2 — same
                # resolver, no second parser). msg-1438's silent 2-day stall was exactly the case
                # a judgement-page decide carried this field but no ``NEXT:`` line; without this
                # read the conductor stopped on NO_HANDOFF. Absent field ⇒ backward-compatible
                # body-only routing (rows 1-2 of §3-1's truth table).
                next_participant=_next_participant(latest),
            )
            # Row 5 of §3-1: the field and the body disagree. The Handoff has been rewritten to
            # HUMAN (safety valve) and carries the divergence so we can log it as observability
            # BEFORE the routing decision below acts on the escalated kind. Recording is loud and
            # non-blocking — the turn still terminates at the human this round, exactly what the
            # truth table requires.
            #
            # T-reconcile-field-mismatch-flag-overloaded: the reason is split into two codes so
            # this log line is self-describing on its own (a human reader tells "the writer named
            # two different targets" apart from "the writer put garbage in the field" without
            # re-deriving it from the raw tokens, and a programmatic consumer counts them apart
            # without duplicating :mod:`.handoff`'s resolver). Same escalation, different cause.
            if handoff.mismatch_reason is not None:
                logger.warning(
                    "conductor next_participant field/body mismatch: msg=%s reason=%s "
                    "field=%r body_target=%r → escalating to human",
                    latest_msg_id,
                    handoff.mismatch_reason.value,
                    handoff.token,
                    handoff.mismatch_body_token,
                )

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
                # Pre-gate CI-wait admission (design v0.3.1 §5.2A). Decided BEFORE the gate is
                # fired, from machine facts only (the head's rollup + two clocks + two head sets
                # read back off this thread) — the third edge of §5.2A.6, which is neither guard
                # (i) nor carve-out ②. ``None`` (no source wired, or the rollup could not be
                # read) falls through to the pre-wiring path below unchanged.
                admitted = await self._admit(parsed_ref, latest, messages)
                if admitted is not None:
                    decision, rollup = admitted
                    if decision.admission is GateAdmission.DEFER:
                        # R1a / R2. Post nothing, summon nobody: the next tick re-derives this
                        # from a fresh rollup. This is where §5.2A.7's "pending gate invocations
                        # 3 → 0" and "relay COMMENT noise 3 → 0" are actually paid.
                        return self._stop(
                            round_index, StopReason.CI_WAIT, latest_msg_id, forced, forced_saveable
                        )
                    if decision.admission is GateAdmission.ALREADY_REVIEWED:
                        # R6. A verdict for this exact head is already in the thread; re-firing
                        # would buy a second opinion on an unchanged diff. Stop at the human, the
                        # same terminal the first verdict's own APPROVE / COMMENT reached — the
                        # earlier verdict's ``NEXT:`` is authoritative and is NOT re-parsed here
                        # (no author is trusted, msg-557).
                        return self._stop(
                            round_index, StopReason.HUMAN, latest_msg_id, forced, forced_saveable
                        )
                    if decision.admission is GateAdmission.ROUTE_HUMAN:
                        # R3 (CI stuck past the cap) / R5 (red twice on one head, no new push).
                        # The escalation is POSTED, not merely returned: a human summoned without
                        # the reason in the thread is a stop nobody can act on, and ``reason``
                        # already names the stuck checks and which clock decided.
                        escalation = await self._post_admission_escalation(
                            parsed_ref.slug, decision
                        )
                        return self._stop(
                            round_index,
                            StopReason.HUMAN,
                            _msg_id(escalation) or latest_msg_id,
                            forced,
                            forced_saveable,
                        )
                    if decision.admission is GateAdmission.ROUTE_IMPLEMENTER:
                        # R4: first red on this head (E-CI-RED — the CI-fix edge, NOT carve-out
                        # ②; no verdict exists to relay). The ci-route marker on this post is
                        # what lets the NEXT tick's R5 tell a second red apart from this one.
                        route_msg = await self._post_ci_route(parsed_ref.slug, decision, rollup)
                        route_msg_id = _msg_id(route_msg)
                        implementer_identity = self._implementer_identity
                        if not route_msg_id or not implementer_identity:
                            # Same two fail-safes as the REQUEST_CHANGES path: without a msg_id
                            # the no-progress guard cannot track the continue path, and without
                            # exactly one implementer persona there is nobody to dispatch.
                            return self._stop(
                                round_index,
                                StopReason.HUMAN,
                                route_msg_id or latest_msg_id,
                                forced,
                                forced_saveable,
                            )
                        handle = sessions.get(implementer_identity)
                        if handle is None:
                            handle = await self._dispatcher.spawn_instance(
                                self._thread_ref,
                                self._implementer_role,
                                implementer_identity,
                            )
                            sessions[implementer_identity] = handle
                        await self._dispatcher.dispatch(handle, self._to_event(route_msg, messages))
                        processed_msg_id = route_msg_id
                        continue
                    # GateAdmission.INVOKE (R0-OVERRIDE / R1b / R7) falls through.
                verdict, relay_msg = await self._fire_pr_gate(parsed_ref.slug)
                relay_msg_id = _msg_id(relay_msg)
                implementer_identity = self._implementer_identity
                # A missing relay id (post result with no msg_id) breaks no-progress tracking on
                # the continue path, so fail-safe to the human instead of re-processing the relay
                # next round (Tier B msg-572 #2). APPROVE / COMMENT also stop at the human.
                if (
                    not relay_msg_id
                    or verdict is not ReviewEvent.REQUEST_CHANGES
                    or not implementer_identity
                ):
                    last = relay_msg_id or latest_msg_id
                    return self._stop(round_index, StopReason.HUMAN, last, forced, forced_saveable)
                handle = sessions.get(implementer_identity)
                if handle is None:
                    handle = await self._dispatcher.spawn_instance(
                        self._thread_ref, self._implementer_role, implementer_identity
                    )
                    sessions[implementer_identity] = handle
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
            # Tier-C Decider hook: right after the rule-based routing decision, before it is acted
            # on. Observation only — neither ``stop_reason`` nor ``target_role`` is read back.
            await self._decider_hook(handoff, messages, round_index, stop_reason)
            if target_role is None:
                assert stop_reason is not None  # _route always sets a reason when it stops
                # Bohr msg-179 §6 invariant: a message that carries a non-null next_participant
                # field cannot stop the conductor on NO_HANDOFF. The Layer-3 resolver rewrites
                # every field-bearing case into ROLE / HUMAN / NONE / PR_REVIEW (target
                # divergence and unresolvable-field both go to HUMAN with a set
                # ``mismatch_reason``), so a NO_HANDOFF here implies the field was None / empty —
                # the pre-Layer-3 path. This branch is structurally unreachable when the field is
                # set; the assertion pins it that way so a future refactor of the resolver cannot
                # silently re-open msg-1438's 2-day silent stall (§3-1 row 3).
                assert not (
                    stop_reason is StopReason.NO_HANDOFF and _next_participant(latest) is not None
                ), (
                    f"§6 invariant broken: NO_HANDOFF on msg with next_participant set "
                    f"(msg={latest_msg_id!r}, field={_next_participant(latest)!r})"
                )
                # Leave the reason in the thread for the stops whose cause is not readable from
                # the head itself (§6.1 / §6.4). The posted id becomes ``last_msg_id`` — the same
                # treatment the R3/R5 admission escalation gets — so the sweep records the stop
                # against the message a human will actually open.
                notice = self._terminal_notice(handoff, _author(latest), stop_reason)
                if notice is not None:
                    posted = await self._post_as_relay(notice)
                    latest_msg_id = _msg_id(posted) or latest_msg_id
                else:
                    # T-human-terminal-overuse D-1 (Bohr msg-2540 approved by Einstein msg-2539).
                    # A guard-(i) redirect stops on ``StopReason.HUMAN`` when the naysayer was
                    # already consulted in the segment; the current head still says ``NEXT:
                    # <implementer>`` and head_skip Stage 1 will not SKIP that token. Post a
                    # write-back under ``CONDUCTOR_RELAY_AUTHOR`` so the head moves — either to
                    # ``NEXT: human`` (self-terminating for the implementer-authored / unknown-
                    # author cases) or to ``NEXT: <author>`` (author-directed autonomous
                    # correction for the proposer / naysayer cases, at most one per episode).
                    redirect_body = self._render_guard_i_redirect_notice(
                        handoff, latest, messages, stop_reason
                    )
                    if redirect_body is not None:
                        posted = await self._post_as_conductor_relay(redirect_body)
                        latest_msg_id = _msg_id(posted) or latest_msg_id
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

    async def _decider_hook(
        self,
        handoff: Handoff,
        messages: list[dict[str, Any]],
        round_index: int,
        stop_reason: StopReason | None,
    ) -> None:
        """Run the Tier-C Decider on an explicit ``NEXT: human`` head (msg-4180 §4).

        Fires only for an author-written human handoff: a field/body mismatch also resolves to
        ``HandoffKind.HUMAN`` but is a conductor safety valve, not somebody asking the human, so
        it is skipped. ``gate_result`` is ``None`` — this conductor does not run the Tier-C
        admission gate — so the result is always recorded as out-of-gate (see
        :mod:`..decider.hook`). ``stop_reason`` is logged beside the decision, never modified.
        """
        if self._decider is None:
            return
        if handoff.kind is not HandoffKind.HUMAN or handoff.mismatch_reason is not None:
            return
        thread_msgs = [
            ThreadMessage(
                msg_id=_msg_id(m),
                author=_author(m),
                content=_content(m),
                parsed_next=parse_next_token(_content(m)),
            )
            for m in messages
        ]
        # The head was resolved to HUMAN (possibly via the structured field with no body line);
        # state its parsed_next as the reserved token so the Decider's own gate agrees.
        head = thread_msgs[-1]
        thread_msgs[-1] = ThreadMessage(
            msg_id=head.msg_id, author=head.author, content=head.content, parsed_next=HUMAN_TOKEN
        )
        await run_tierc_hook(
            self._decider,
            thread_id=self._thread_ref.thread_id,
            round_index=round_index,
            roster=self._roster,
            messages=thread_msgs,
            stop=stop_reason.value if stop_reason is not None else None,
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

        # Spawnability (ADR-2026-09-14-21 D-2 / D-3), decided BEFORE every other branch so it
        # cannot be reached around: an identity whose embodiment is not ``terminal_coding_agent``
        # has no adapter that could run it, and writing one for ``web_ai_chat`` is the ガワ方式
        # ADR-2026-05-31-14 withdrew. The token is checked, not just the resolved roster identity,
        # so a nomination of a non-roster web identity (which resolves to ABSENT, or to an
        # unresolvable field) is caught here rather than falling into the Obj3 fallback and
        # arriving at the human with "NEXT: could not be read" — a true statement that names the
        # wrong cause.
        #
        # The stop is ``StopReason.HUMAN``: the ADR says ``NEXT: Fermi`` is the same stop
        # condition as ``NEXT: human``, and an operator's notification set is keyed on the reason
        # string. What it deliberately does NOT do is route through :meth:`_human_terminal`, i.e.
        # it does not force an Obj2 naysayer consult first. That consult exists to put an
        # independent review in front of an *un-reviewed agent proposal* before a human approves
        # it; a nomination nobody can start is a routing dead end, not a proposal. Forcing a
        # Gemini call on it would spend the review budget on a turn that has nothing to review,
        # and it would also delay the thread post below by a round.
        if (blocked := self._spawn_blocked(handoff)) is not None:
            identity, embodiment = blocked
            logger.warning(
                "conductor spawn-unavailable target: identity=%s embodiment=%s kind=%s "
                "→ stopping at the human (ADR-2026-09-14-21 D-3)",
                identity,
                embodiment,
                handoff.kind.value,
            )
            return None, "", False, False, StopReason.HUMAN

        # Self-handoff (design §6.1): the head hands to its own author. Detected here, before
        # ``spawn_instance``, because one layer down the adapter's self-filter drops the delivery
        # in silence — the session is spawned, ``query()`` is never called, nothing is posted, and
        # the round ends on NO_PROGRESS with no statement of what happened. Measured on
        # spirrow-magickit/T-human-outage-degrade-close-only (head msg-244, author=Bohr,
        # next=Bohr) and spirrow-mindwire/T-scoped-driver-verdict-never-reaches-chatroom (head
        # msg-2775, same shape): 72 retries, no reply, no record, $0 spent and nothing learned.
        #
        # Only ROLE handoffs can self-hand: HUMAN / NONE stop anyway, PR_REVIEW routes by verdict
        # rather than by name, and ABSENT has no target to compare against.
        if handoff.kind is HandoffKind.ROLE and self._is_self_handoff(handoff, author):
            logger.warning(
                "conductor self-handoff: author=%s hands to itself (target=%s) "
                "→ stopping for a human, not spawning",
                author,
                handoff.identity,
            )
            return None, "", False, False, StopReason.SELF_HANDOFF

        # guard (i): design→implement Tier-C gate. The predicate itself lives in
        # :mod:`spirrow_mindwire.routing` (T-operator-board msg-2544 §C-3 single-source extraction);
        # the operator-board's ``R-NEXT-HEIS-GUARD`` transition consults the same import, so the
        # two callers cannot drift. Semantics preserved verbatim from the pre-extraction inline
        # form — carve-out ① (human author), carve-out ③ (attested independent naysayer under
        # RUN); carve-out ② (PR-gate verdict relay) is decided BEFORE this branch via
        # ``HandoffKind.PR_REVIEW`` above (see the fire_pr_review path). Un-attested carve-out ③
        # falls through to ``_human_terminal`` — the pre-existing safe path, no new failure mode.
        # The verdict enum keeps the observations lifted booleans rather than raw objects so a
        # future consumer (the board's transition table) can share the rule without also sharing
        # this module's roster / control / attestation ownership.
        if handoff.kind is HandoffKind.ROLE and handoff.role is self._implementer_role:
            # Pass the attestation observation as a nullary thunk so the predicate — and only
            # the predicate — decides when it must fire (PR-review msg-2554 BLOCKING). The
            # earlier iteration lifted the observation into an eager bool, which forced the
            # caller to re-express carve-out ③'s (naysayer ∧ RUN) short-circuit here just to
            # avoid an unnecessary attestation read; that re-expression put the "which carve-
            # outs consume the attest bit" decision in two places at once. With a thunk, the
            # observation-scope short-circuit becomes ``routing``'s job — Python's ``and``
            # in ``guard_proposer_to_implementer`` reproduces the pre-extraction inline
            # form's scope exactly (``_attested`` is reached only for a non-human naysayer
            # under RUN), and a future carve-out that needs the attest bit for a different
            # role/state combination edits ``routing.py`` only.
            verdict = guard_proposer_to_implementer(
                author_is_human=self._is_human(author),
                author_is_naysayer=author_role is self._naysayer_role,
                control_state_is_run=self._control_state is ControlState.RUN,
                message_is_attested=lambda: self._attested(messages[-1]),
            )
            if verdict is GuardIVerdict.HONOR:
                assert handoff.identity is not None
                return handoff.role, handoff.identity, False, False, None
            # guard-(i) redirect is NOT an explicit human handoff: under the cost lever it does not
            # force a consult (explicit_human=False).
            return self._human_terminal(messages, explicit_human=False)

        if handoff.kind is HandoffKind.HUMAN:
            # C (T-human-terminal-overuse msg-890 §3): record the TIER-C: <label> the author put
            # on the line above their `NEXT: human` — presence and absence both. This is the
            # calibration input for the pre-registered A2 threshold (>20% ∧ ≥3 uncalled explicit
            # human terminals from proposer disposition turns, in a 14-day / 20-turn window,
            # msg-890 §2). NON-BLOCKING by design: nothing about the routing changes on the tag's
            # presence, absence, or value — if it did the calibrator would be destroying its own
            # calibration (missing labels would stop being observable).
            logger.info(
                "conductor human terminal: author=%s author_role=%s tier_c_label=%s",
                _author(messages[-1]),
                author_role.value if author_role is not None else None,
                handoff.tier_c_label,
            )
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

    def _spawn_blocked(self, handoff: Handoff) -> tuple[str, str] | None:
        """``(identity, embodiment)`` when ``handoff`` names a target that must not be spawned.

        Pure, and the single definition of the rule: :meth:`_route` acts on it and
        :meth:`_terminal_notice` re-derives the same answer to write the thread record, so the
        two can never disagree about why a turn stopped.

        ``handoff.identity`` (the roster's canonical spelling) is preferred over ``handoff.token``
        (what the author typed) when both are present, but the token is what makes the check work
        for an identity that is deliberately NOT in the roster — which is the normal case for a
        web identity, since a roster entry means "this daemon drives this persona".
        """
        if handoff.kind in (HandoffKind.NONE, HandoffKind.PR_REVIEW):
            return None
        name = handoff.identity or handoff.token or ""
        embodiment = blocked_embodiment(name, self._embodiments)
        return None if embodiment is None else (name, embodiment)

    def _is_self_handoff(self, handoff: Handoff, author: str) -> bool:
        """Does ``handoff`` hand back to ``author``?

        Compared on ADR-2026-05-29-11 partition keys, not raw strings: ``Bohr`` handing to
        ``bohr`` is the same self-handoff, and the adapter's own self-filter one layer down
        compares instance ids that have been through the same normalisation on the way in.
        A comparison that missed on case would leave exactly the silent path this check exists
        to close.
        """
        target = handoff.identity or ""
        if not target or not author:
            return False
        return normalize_identity_key(target) == normalize_identity_key(author)

    def _terminal_notice(self, handoff: Handoff, author: str, reason: StopReason) -> str | None:
        """The body to leave in the thread for a stop a reader could not otherwise explain.

        ``None`` for every stop that already explains itself: ``NEXT: human`` is the author's own
        statement, ``NEXT: none`` is a settle, NO_HANDOFF is visible in the head the human is
        about to read, and CI_WAIT posts nothing by design (§5.2A.5).

        The two that DO need a record are the two this change adds, and for the same reason: the
        thread's own text says a participant was nominated, and the truth is that nobody was
        started. Without a line saying so, the human opens a thread whose last message asks for
        work and finds no evidence that anything happened at all — which is what "静かに止まる"
        cost on the two measured threads.

        The body ends on ``NEXT: human`` deliberately. It is the honest handoff (a person has to
        act), and it is also what parks the sweep: ``head_skip``'s Stage 1 SKIPs a head whose
        token is a stop token, so this post ends the retry loop instead of becoming its next
        input.
        """
        if reason is StopReason.SELF_HANDOFF:
            return (
                f"Conductor stop — 自己ハンドオフ (author == next)\n\n"
                f"head の author は `{author}` で、その `NEXT:` も "
                f"`{handoff.identity}` を指しています。"
                f"自分自身へのハンドオフは進行しません "
                f"(セッションを起こしても、配送側の自己フィルタが自分の投稿を落とすため"
                f"何も起きない)。\n\n"
                f"∴ spawn せず、人間の介入が必要な停止として扱いました。\n\n"
                f"次にやること: このスレッドの head に、別の参加者を指す `NEXT:` を書くか、"
                f"`NEXT: human` で明示的に預けてください。head が動くまでループは再開しません。\n\n"
                f"NEXT: {HUMAN_TOKEN}"
            )
        blocked = self._spawn_blocked(handoff)
        if blocked is not None:
            identity, embodiment = blocked
            return (
                f"Conductor stop — spawn できない identity への handoff\n\n"
                f"`NEXT:` は `{identity}` を指していますが、この identity の稼働形態は "
                f"`{embodiment}` で、Conductor が起動できるのは `terminal_coding_agent` だけです "
                f"(ADR-2026-09-14-21 D-2 / D-3)。\n\n"
                f"∴ spawn せず、`NEXT: human` と同じ停止として扱いました。"
                f"これは特例ではなく、adapter を持たない稼働形態すべてに適用される一般則です。\n\n"
                f"次にやること: `{identity}` に依頼する内容であれば、その本人が投稿してから"
                f"スレッドを進めてください。\n\n"
                f"NEXT: {HUMAN_TOKEN}"
            )
        return None

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
            # Layer 3: a past message that ended a segment with ``next_participant: human`` (with
            # or without a matching body NEXT: line) is a segment boundary just as a body ``NEXT:
            # human`` is. Passing the field here keeps the boundary rule symmetric with the
            # latest-message routing above — both sides read the same field via the same resolver
            # (Bohr msg-179 §3-2).
            past = resolve_handoff(
                _content(msg), self._roster, next_participant=_next_participant(msg)
            )
            if past.kind is HandoffKind.HUMAN:
                boundary = i + 1
        return any(
            self._roster_role(_author(msg)) is self._naysayer_role and self._attested(msg)
            for msg in segment[boundary:]
        )

    def _roster_role(self, author: str) -> Role | None:
        """Resolve a message author (persona name) to its role, case-insensitively."""
        return _roster_role(self._roster, author)

    async def _fetch_messages(self) -> list[dict[str, Any]]:
        # T-error-envelope-read-as-data (msg-1115 §2): a refused read used to
        # come back as an error envelope inside a nominally-successful response,
        # ``result.get("messages", [])`` yielded ``[]``, ``run`` saw an empty
        # thread and stopped on :attr:`StopReason.EMPTY` with no trace of the
        # refusal. The client now elevates the envelope to
        # :class:`MagickitMcpError` (parse_tool_result), so a refusal surfaces
        # as an exception at ``call_tool`` and cannot be mistaken for "no
        # messages yet". The ``isinstance(result, dict)`` defence below is kept
        # for a *different* class of bug (a well-formed but oddly-shaped
        # response); the envelope path used to hide inside it and no longer
        # does.
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
        """Fire the Tier B naysayer review on ``pr_ref`` and take back its verdict (PR-2b-2).

        Synchronous (ADR-19 N-1): the orchestrator runs the CI-gate + Gemini judge + GitHub
        submit, posts its critique to the ``T-pr-review-<repo>-<n>`` ledger and relays the
        verdict (with the critique body, so the implementer has its fix context) into the design
        thread named here. The relay used to live in this class, which made the destination a
        property of the *caller*: a hand-run driver held no design thread and silently relayed
        nothing. Returns the verdict and the relay **message**, so the implementer is dispatched
        on that relay event and sees the critique, not its own trigger (Tier B msg-567 #1).
        """
        assert self._orchestrator is not None
        _thread_ref, outcome, relay_msg = await self._orchestrator.fire_pr_review(
            project=self._thread_ref.project_id,
            pr_ref=pr_ref,
            design_thread=self._thread_ref.thread_id,
            implementer=self._implementer_identity,
        )
        return outcome.verdict, relay_msg

    async def _post_as_relay(self, body: str) -> dict[str, Any]:
        """Post ``body`` into the design thread under the reserved relay author.

        Shared by the two conductor-authored PR-gate posts — the R3/R5 admission escalation and
        the R4 ci-route dispatch — because they need identical handling of the one failure that
        is not an error: a design thread resolved out from under an in-flight gate (W3). Writing
        that handling once is what keeps the two from drifting into two different answers to the
        same question. The verdict relay is a third writer under the same author, but it lives
        in :meth:`~spirrow_mindwire.orchestrator.PrReviewOrchestrator._post_design_relay` —
        "posting the verdict" is part of *firing* the gate, not of the conductor's own
        record-keeping (T-pr-gate-relay-belongs-to-the-conductor D-1: no destination, no fire).
        The single-definition author name shared with that writer is
        :data:`~spirrow_mindwire.conductor.gate_records.RELAY_AUTHOR`.
        """
        try:
            result = await self._mcp.call_tool(
                "chatroom_post_message",
                {
                    "project": self._thread_ref.project_id,
                    "thread_id": self._thread_ref.thread_id,
                    "msg_type": "report",
                    "author": RELAY_AUTHOR,
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
        except ThreadResolvedError as exc:
            # W3 (T-sweeper-posts-into-resolved-thread-blocks-r2-deploy Bohr
            # msg-536): the design thread is resolved. This is the same
            # async-verdict race Einstein msg-531 Objection 2 named,
            # measured from the receiving side: a human resolved the design
            # thread while the PR-gate was still computing, and the relay
            # has nowhere to land as a chatroom record. The admission
            # escalation / ci-route posts are the conductor's own
            # observations of CI — a human summoned by R3 or R5 needs the
            # reason on a durable surface; the R4 marker exists to key R5
            # on the next tick. Of OBL-CHATROOM-PRODUCER-READER-SURFACE's
            # three allowed surfaces this is **disposition (3)**: fail
            # loudly (log warning) and return a stub so the caller reads
            # the empty ``msg_id`` as "the record did not land". Non-
            # retryable (msg-536 W5), terminal for this thread; the
            # refusal is never itself posted into a chatroom thread
            # (msg-534 W4a). Duplicating this handling here — instead of
            # sharing one helper with ``_post_design_relay`` — is the
            # accepted cost of the split: two files' worth of "handle a
            # dropped relay" is a Principle-2 hybrid, but merging them
            # would put the writer of the *verdict* back in this module,
            # which is exactly what T-pr-gate-relay-belongs-to-the-
            # conductor's D-1 rules out (msg-2836 §advisory objection).
            logger.warning(
                "pr-gate design-thread relay dropped (thread %r resolved): %s. "
                "GitHub PR review is the primary artifact and still stands; "
                "no retry — refusal is terminal for this thread.",
                self._thread_ref.thread_id,
                exc,
            )
            return {
                "msg_id": "",
                "author": RELAY_AUTHOR,
                "content": body,
            }
        msg = result.get("msg") if isinstance(result, dict) else None
        msg_id = str(msg.get("msg_id") or "") if isinstance(msg, dict) else ""
        return {
            "msg_id": msg_id,
            "author": RELAY_AUTHOR,
            "content": body,
        }

    async def _post_as_conductor_relay(self, body: str) -> dict[str, Any]:
        """Post ``body`` into the design thread under :data:`CONDUCTOR_RELAY_AUTHOR`.

        The T-human-terminal-overuse D-1 write-back (Bohr msg-2540 approved by Einstein msg-2539).
        Same shape and same fail-loud-on-resolved-thread disposition as :meth:`_post_as_relay`, but
        under a distinct reserved author — deliberately. The two writers do different jobs and are
        keyed on by different readers:

        - ``pr-gate-relay`` — PR-gate verdict / R3+R5 admission / R4 ci-route.
          Read by :func:`~.gate_records.verdict_heads` (R6 dedupe) and
          :func:`~.gate_records.ci_route_heads` (R5 second-red input). Those readers narrow to
          this author for noise rejection (module docstring, "the two readers are deliberately
          restricted to messages authored by the conductor's relay author").
        - ``conductor-relay`` — the D-1 redirect write-back. Nothing reads this author for
          decision-making yet; the D-1 mechanism only requires that head_skip see a moved head
          (which any author with a stop-token ``NEXT:`` line would do). Isolating it from the
          PR-gate readers is what msg-2539 Objection 1 named as necessary.

        Fail-safe on :class:`~spirrow_mindwire.magickit.client.ThreadResolvedError`: the design
        thread was resolved out from under this write. Same disposition as ``_post_as_relay`` —
        log a warning and return a stub with an empty msg_id; the D-1 write-back is transitional
        (no reader keyed on it), so losing one is not silent gap-creation the way losing a
        verdict relay would be.
        """
        try:
            result = await self._mcp.call_tool(
                "chatroom_post_message",
                {
                    "project": self._thread_ref.project_id,
                    "thread_id": self._thread_ref.thread_id,
                    "msg_type": "report",
                    "author": CONDUCTOR_RELAY_AUTHOR,
                    "content": body,
                    # No ``role`` here, deliberately. Same reasoning as ``_post_as_relay`` and
                    # the ``pr-gate-relay`` yaml entry: the body is the conductor's own framing
                    # of the redirect it just performed, not any role's verbatim speech.
                    # Claiming a role would fabricate the very evidence the I-6 invariant exists
                    # to make meaningful (msg-2540 §1-4).
                },
            )
        except ThreadResolvedError as exc:
            logger.warning(
                "guard-(i) redirect write-back dropped (thread %r resolved): %s. "
                "No retry — refusal is terminal for this thread "
                "(OBL-CHATROOM-PRODUCER-READER-SURFACE disposition 3, fail loudly).",
                self._thread_ref.thread_id,
                exc,
            )
            return {
                "msg_id": "",
                "author": CONDUCTOR_RELAY_AUTHOR,
                "content": body,
            }
        msg = result.get("msg") if isinstance(result, dict) else None
        msg_id = str(msg.get("msg_id") or "") if isinstance(msg, dict) else ""
        return {
            "msg_id": msg_id,
            "author": CONDUCTOR_RELAY_AUTHOR,
            "content": body,
        }

    def _render_guard_i_redirect_notice(
        self,
        handoff: Handoff,
        latest: dict[str, Any],
        messages: list[dict[str, Any]],
        stop_reason: StopReason,
    ) -> str | None:
        """The T-human-terminal-overuse D-1 write-back body, or ``None`` when not applicable.

        Fires only when the stop is a guard-(i) redirect: the handoff is a ROLE handoff to the
        implementer, and the stop reason is :attr:`StopReason.HUMAN` — an explicit ``NEXT: human``
        (``handoff.kind is HUMAN``) does not qualify (the author's own decision needs no
        write-back, and the head already carries a stop token so head_skip already handles it).

        The final ``NEXT:`` line's target is determined by :meth:`_guard_i_redirect_target`
        (msg-2540 §2-5 rule):

        - author is the implementer (self-nomination, msg-2537 §4's 3/5 stuck threads) or the
          author's role is unknown to the roster → ``NEXT: human`` (self-terminating: head_skip
          Stage 1 will SKIP this new head on the next tick, ending the bounce loop).
        - author is proposer / naysayer, first redirect this episode → ``NEXT: <author>``
          (author-directed autonomous correction: the author is dispatched on the next tick to
          read the notice and rewrite the routing itself, guard (i) does not re-fire because the
          relay author's role is not implementer).
        - author is proposer / naysayer, second redirect this episode (D-1c episode limit) →
          ``NEXT: human`` (bounded cost of the author-directed path).

        Quoted violation tokens (``NEXT: <implementer>`` shown as an example inside prose) are
        kept OUT of line-start position by wrapping them in backticks and preceding them with
        prose — :func:`~.handoff.parse_next_token` reads the LAST line-start ``NEXT:`` and the
        body has exactly one such line (the final ``NEXT: <target>`` at the bottom), so no
        quoted example can hijack the parse (msg-2540 §4 D-1b, test-pinned by
        ``test_guard_i_redirect_body_parse_is_hijack_safe``).
        """
        if stop_reason is not StopReason.HUMAN:
            return None
        if handoff.kind is not HandoffKind.ROLE or handoff.role is not self._implementer_role:
            return None
        author = _author(latest)
        author_role = self._roster_role(author)
        target = self._guard_i_redirect_target(author, author_role, messages)
        return self._format_guard_i_redirect_body(
            author=author, author_role=author_role, handoff=handoff, target=target
        )

    def _guard_i_redirect_target(
        self,
        author: str,
        author_role: Role | None,
        messages: list[dict[str, Any]],
    ) -> str:
        """The final ``NEXT:`` target for a D-1 redirect write-back (msg-2540 §2-5).

        Implementer / unknown author → ``human``: self-terminating, bounces stop next tick
        (msg-2540 §2-2: an author-directed relay with role=None re-enters guard (i) and repeats).

        Proposer / naysayer author, first redirect this episode → ``<author>``: author-directed
        autonomous correction (msg-2540 §2-1: relay's own author has role=None so guard (i) does
        not fire on the relay's own turn; the AUTHOR is dispatched next and can read the notice).

        Second redirect in the same episode → ``human``: the D-1c bound (msg-2540 §2-4). Without
        it a stubborn author looping on the same mistake would spin the loop at 1 launch/tick,
        which is a strictly-worse regression than the free spin we replaced.
        """
        if author_role is None or author_role is self._implementer_role:
            return HUMAN_TOKEN
        if self._has_prior_guard_i_relay_in_episode(messages, author):
            return HUMAN_TOKEN
        return author

    def _has_prior_guard_i_relay_in_episode(
        self, messages: list[dict[str, Any]], current_author: str
    ) -> bool:
        """Is there already a D-1 relay in the current episode? (msg-2540 §2-4 candidate impl.)

        An "episode" is the run of turns bounded by turns routed WITHOUT a redirect. Walk back
        from the message BEFORE the head (``messages[-2]``); if we see a prior
        ``CONDUCTOR_RELAY_AUTHOR`` post before we cross an episode boundary (any author that is
        neither the current author nor this relay), a prior redirect happened this episode and
        the D-1c bound applies.

        The predicate is a pure function of the message list — no persisted state — so a test
        can drive it directly (``test_guard_i_second_redirect_in_episode_uses_human``).
        """
        for msg in reversed(messages[:-1]):
            msg_author = _author(msg)
            if msg_author == CONDUCTOR_RELAY_AUTHOR:
                return True
            if msg_author != current_author:
                return False
        return False

    def _format_guard_i_redirect_body(
        self,
        *,
        author: str,
        author_role: Role | None,
        handoff: Handoff,
        target: str,
    ) -> str:
        """Render the D-1 write-back body. See :meth:`_render_guard_i_redirect_notice` for shape.

        The body is one screen of prose (nothing computed at read time), so the whole notice is
        the author's ``NEXT:`` line + the redirect verdict + the routing that comes next. The
        final line is the ONLY line-start ``NEXT:`` line — every mention of ``NEXT: <role>``
        elsewhere is inside prose (preceded by Japanese text, then backticks around the token),
        because :func:`~.handoff.parse_next_token` takes the last line-start match and a quoted
        example that started a line would hijack the parse (msg-2540 §4 D-1b).
        """
        author_role_label = author_role.value if author_role is not None else "roster に未登録"
        implementer_token = handoff.identity or handoff.token or "<implementer>"
        # msg-2540 §2-5 explains the observed audience for each of the two possible targets. This
        # framing is written for the author who is being redirected (the one whose next event
        # will be the relay itself, if target is the author) OR for the human reader who opens
        # the thread when it has come to rest (if target is human). Both need to see the same
        # facts, so the body does not branch on target for the diagnostic prose.
        target_line = f"NEXT: {target}"
        return (
            "Conductor stop — guard (i) redirect (design→implement Tier-C gate)\n\n"
            f"直近の post ({author}, role: {author_role_label}) の `NEXT:` は "
            f"implementer (`{implementer_token}`) を指しました。guard (i) はこの handoff を "
            "Tier-C の gate として拦截し、実装へは通しません "
            "(ADR-2026-06-03-17, T-human-terminal-overuse D-1)。\n\n"
            "carve-out の該当状況:\n"
            f"- ① human-authored Tier-C decide — 不適合: author (`{author}`) は human "
            "identity ではありません。\n"
            "- ② PR-gate verdict relay — 不適合: この handoff は PR-gate の verdict relay "
            "ではありません。\n"
            "- ③ attested independent naysayer proceed under control=`run` — 不適合: "
            "author が naysayer でない、あるいは attest 済でない、あるいは control が "
            "`run` ではありません。\n\n"
            "実装へ進める経路は 2 つだけです — human が直接 `NEXT: <implementer>` を "
            "書く (carve-out ①)、あるいは attested naysayer が control=`run` 下で "
            "`NEXT: <implementer>` を書く (carve-out ③) — どちらも proposer が "
            "自己前進で implementer を指名する形は取れません "
            "(Einstein msg-601 Fix-1: *Only* the naysayer may advance to code)。\n\n"
            "この post は conductor 自身の書き戻しです (author: "
            f"`{CONDUCTOR_RELAY_AUTHOR}`、role: なし)。head_skip Stage 1 の SKIP token 集合 "
            "は closed set `{human, none}` — この relay の `NEXT:` が動くことで、"
            "同じ head が永続 relaunch されるループ (msg-2537 §4 実測 288 回) が終端します。\n\n"
            f"{target_line}"
        )

    async def _admit(
        self, pr: PrRef, latest: dict[str, Any], messages: list[dict[str, Any]]
    ) -> tuple[AdmissionResult, CheckRollup] | None:
        """Run pre-gate CI-wait admission for ``pr``, or ``None`` to keep the pre-wiring path.

        ``None`` is returned for exactly two situations, and they are deliberately given the
        same answer: no rollup source is wired at all, and a wired source that could not READ
        the rollup. Both mean "admission has nothing to judge on", and the only honest response
        to that is the behaviour that shipped before admission existed — fire the gate. Mapping
        an unread rollup onto ``rollup=[]`` instead would hand ``gate_admission`` a *measured*
        empty and let R1a / R1b rule on a fiction, which is precisely what that function's own
        parameter documentation forbids ("the caller must not conflate 'empty' with 'not yet
        fetched'").

        ``nomination_is_self`` — the one input this call site has to interpret rather than read
        ------------------------------------------------------------------------------------

        The parameter is documented as "``True`` if the latest ``NEXT: pr-review <ref>`` was
        authored by the conductor's own pr-gate-relay". Taken as a literal predicate at THIS
        call site it is a constant ``False``, and the measurement is one line: the three writers
        that post under :data:`RELAY_AUTHOR` — the verdict relay in
        :meth:`~spirrow_mindwire.orchestrator.PrReviewOrchestrator._post_design_relay`,
        :meth:`_post_admission_escalation`, and :meth:`_post_ci_route` — each emit a ``NEXT:``
        naming the implementer or the human, never ``pr-review``. So the relay author cannot
        self-nominate, no message it writes can carry a pr-review handoff, and a literal reading
        sends **every** admission through R0-OVERRIDE to INVOKE. That is not a conservative
        wiring; it is a no-op wearing one, and it would close RES-WIRING while leaving §5.2A.7's
        entire expected effect unrealised.

        What R0-OVERRIDE is FOR is stated in its own rationale: a human summoned by R3 or R5 must
        be able to break the escalation loop by re-writing the handoff by hand (PR-review
        msg-(gate) BLOCKING-2). The discriminator that delivers that, and that this call site can
        actually evaluate, is authorship by the human identity — the same :meth:`_is_human` test
        the conductor already uses for the Tier-C carve-out. So:

        * human-authored ``NEXT: pr-review`` → ``nomination_is_self=False`` → R0-OVERRIDE →
          INVOKE now. The escape hatch works, which it does not under the literal reading either
          (there, everything invokes, so a summoned human cannot be trapped — but neither can
          anything else be deferred).
        * role-authored (the implementer's ordinary "I pushed, review it") →
          ``nomination_is_self=True`` → the R1-R7 table applies. This is the autonomous path, and
          it is the only path on which the design's savings exist.

        This departs from the docstring's parenthetical "(or any other role)" and is reported as
        a deviation rather than absorbed, because it is the single decision that determines
        whether the mechanism runs at all. Nothing about it weakens a guard: every rule it makes
        reachable ends in the conductor doing *less* (defer, don't re-fire, or stop and ask).
        """
        if self._rollup_source is None:
            return None
        rollup = await self._rollup_source.fetch_check_rollup(pr)
        if rollup is None:
            logger.info(
                "gate admission skipped for %s: rollup unread — firing the gate (pre-wiring "
                "behaviour)",
                pr.slug,
            )
            return None
        relay_bodies = [_content(msg) for msg in messages if _author(msg) == RELAY_AUTHOR]
        decision = gate_admission(
            rollup=rollup.rows,
            head_sha=normalize_sha(rollup.head_sha),
            head_committed_date=rollup.head_committed_date,
            head_pushed_at=rollup.head_pushed_at,
            now=datetime.now(UTC),
            nomination_is_self=not self._is_human(_author(latest)),
            verdict_heads=verdict_heads(relay_bodies),
            ci_red_routed_heads=ci_route_heads(relay_bodies),
        )
        logger.info(
            "gate admission for %s: rule=%s admission=%s reason=%s",
            pr.slug,
            decision.rule,
            decision.admission.value,
            decision.reason,
        )
        return decision, rollup

    async def _post_admission_escalation(
        self, pr_ref: str, decision: AdmissionResult
    ) -> dict[str, Any]:
        """Post the R3 / R5 escalation so the summoned human can see WHY they were summoned.

        ``decision.reason`` already names the stuck checks and which clock decided (R3) or that
        the same head went red twice with no intervening push (R5), so this adds no judgement of
        its own — it carries a machine string to a human surface. Rare by construction: R3 needs
        a 6 h / 12 h cap to elapse and R5 needs a second red on an unchanged head.
        """
        body = (
            f"PR-gate admission (pre-gate CI wait) — {pr_ref}\n\n"
            f"ADMISSION: {decision.admission.value} (rule={decision.rule})\n\n"
            f"{decision.reason}\n\n"
            f"The gate was NOT fired. This is a machine observation of CI, not a review.\n\n"
            f"NEXT: {HUMAN_TOKEN}"
        )
        return await self._post_as_relay(body)

    async def _post_ci_route(
        self, pr_ref: str, decision: AdmissionResult, rollup: CheckRollup
    ) -> dict[str, Any]:
        """Post the R4 CI-fix dispatch, carrying the one new record design §5.2A.5 sanctions.

        The marker is written **only here** — never on a deferral, never on a verdict relay —
        which is what keeps the thread cost of this whole mechanism at "one line, on the rare
        red". It is also the only reason R5 can exist: a deferral leaves no message, so the
        thread cannot otherwise distinguish a first red from a second one on the same head.
        """
        failing = [row.name for row in rollup.rows if (row.conclusion or "") in RED_CONCLUSIONS]
        marker = render_ci_route_marker(
            head=rollup.head_sha,
            conclusion="failure",
            checks=failing,
        )
        checks_line = ", ".join(failing) if failing else "<no named failing check>"
        body = (
            f"PR-gate admission (pre-gate CI wait) — {pr_ref}\n\n"
            f"ADMISSION: {decision.admission.value} (rule={decision.rule})\n\n"
            f"CI is red on {rollup.head_sha[:12]}: {checks_line}\n\n"
            f"{decision.reason}\n\n"
            f"The gate was NOT fired — there is no review verdict to act on. Fix CI and push; "
            f"the next tick re-reads the rollup on the new head.\n\n"
            f"NEXT: {self._implementer_identity or HUMAN_TOKEN}\n\n"
            f"{marker}"
        )
        return await self._post_as_relay(body)

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


def _msg_id(msg: dict[str, Any]) -> str:
    return str(msg.get("msg_id", ""))


def _author(msg: dict[str, Any]) -> str:
    return str(msg.get("author", ""))


def _content(msg: dict[str, Any]) -> str:
    return str(msg.get("content", ""))


def _next_participant(msg: dict[str, Any]) -> str | None:
    """The structured ``next_participant`` envelope field on ``msg``, or ``None``.

    Layer 3 (Bohr msg-179 §3): ``next_participant`` is a top-level field on the message shipped
    by ``chatroom_get_thread`` in the second segment of the roll-out. Reads that carry it are
    routed by the FIELD (with the body's ``NEXT:`` line kept only as a lint); reads that do not
    fall through to body-only resolution — the pre-Layer-3 behaviour.

    An explicitly-null field is treated the same as an absent one (a magickit that ships the key
    with a null value has stated the same fact as omitting it: the sender did not name a target).
    Non-string values are ignored defensively: the JSON contract says "string or null", but a bad
    downstream must never crash the read — the field is dropped and the body's ``NEXT:`` line
    stays authoritative (which is what the field's absence already meant).
    """
    value = msg.get("next_participant")
    if isinstance(value, str) and value.strip():
        return value
    return None


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
    "CheckRollupSource",
    "Conductor",
    "ConductorDispatcher",
    "ConductorOutcome",
    "StopReason",
]
