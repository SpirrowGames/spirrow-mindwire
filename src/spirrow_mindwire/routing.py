"""Single-source predicate for guard (i) — the design→implement Tier-C gate.

Guard (i) (msg-543 / ADR-2026-06-03-17 / Tier-C msg-553/557) intercepts a
``NEXT:`` handoff to the implementer from any non-human author and redirects
it to the human terminal, unless one of the following carve-outs applies:

* **carve-out ①** — the author IS the human (a human-authored Tier-C decide,
  msg-553 / msg-557); the handoff is honoured directly.
* **carve-out ②** — the PR-gate REQUEST_CHANGES→fix relay (PR-2b-2). This is
  a *verdict-driven* branch that never reaches guard (i) at all: the caller
  routes on the deterministic ``fire_pr_review`` outcome BEFORE consulting
  this predicate. It is not modelled here on purpose — modelling it would
  duplicate the marker-gated trust decision that lives with the PR-gate.
* **carve-out ③** — the independent naysayer's OWN proceed-handoff to the
  implementer, while the project's loop control state is ``run``, AND the
  triggering message carries the harness's preflight attest (P-3b, Tier-C
  msg-954 §2 / msg-970). Un-attested, the branch is not taken and the turn
  falls through to the human terminal (the pre-existing safe path).
  **D-4' guardrails** (T-pr-2b-3-human-identity-delegate, Bohr msg-4856 /
  msg-4858, naysayer-endorsed msg-4857 / msg-4859, Takahito "B" decide) narrow
  carve-out ③ further — see :func:`carve_out_iii_admissible`.

Why this predicate is a module of its own — T-operator-board msg-2544 §C-3.
The operator-board's ``R-NEXT-HEIS-GUARD`` transition must consult THE SAME
routing rule the conductor consults; otherwise the two will drift. The rule
is written here once and both call sites import it. Two file-scoped tests
in :mod:`tests.test_routing` fire on obvious accidental duplication (a
second function named ``guard_proposer_to_implementer``; a deleted import
in ``conductor/core.py``); a re-inlined chain under a different name is
NOT caught by those alarms, and is caught instead by the conductor's
carve-out behaviour suite (:mod:`tests.test_conductor_core`) — any inline
chain that differs from this predicate on any truth-table row breaks a
test there. The honest split, tightened after PR-review msg-2551: the
file-scoped tests are cheap early warnings; the load-bearing invariant is
"the truth table stays exactly this table", enforced by the behaviour
suite. Bohr's v0.2 sequencing constraint (msg-2544 §C-3): "抽出が landing
するまで board の routing を live にしない" — this module is the
extraction; the board routing lands against this import.

The predicate is pure — it operates on booleans (and a bool-returning
thunk) lifted from the conductor's state, takes no message body, does no
I/O, and holds no attribution logic. Everything that decides *whether an
author is the human*, *whether an identity is the naysayer role*, *whether
the control state is RUN*, and *whether the message is attested* stays
with its owner (the conductor's roster / control plane / attestation
reader); this predicate only combines those observations into a routing
verdict. The attestation observation is passed in as a nullary callable
so the predicate — and only the predicate — decides *when* it must be
consulted (PR-review msg-2554 BLOCKING). That is the drift-resistant
boundary: future carve-out changes edit the enum + this function only,
never the ownership of each observation, and never a caller-side
re-expression of "which carve-outs actually need the attest bit".

Attribution: T-operator-board thread (msg-2542 orchestrator design, msg-2544
Bohr v0.2 §C-3 single-source extraction). ADR-2026-06-03-17 is cited from
the historical guard-(i) comments in ``conductor/core.py``; the ADR body is
not readable from this repository, so this module reproduces the guard's
behaviour verbatim from that surface, not from the ADR text.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum


class GuardIVerdict(StrEnum):
    """The routing outcome for a proposer→implementer (or any non-human→
    implementer) handoff.

    * ``HONOR`` — dispatch the handoff to the implementer as named. A carve-
      out fires (① human author or ③ attested naysayer under RUN).
    * ``REDIRECT`` — guard (i) fires: the caller must redirect the turn to
      the human terminal instead of dispatching the implementer. Under the
      conductor this reaches ``_human_terminal(..., explicit_human=False)``;
      under the operator-board's ``R-NEXT-HEIS-GUARD`` it moves the card to
      ``ready_for_human``.
    """

    HONOR = "honor"
    REDIRECT = "redirect"


def carve_out_iii_admissible(
    *,
    author_is_naysayer: bool,
    control_state_is_run: bool,
    message_is_attested: Callable[[], bool],
    segment_declares_tier_c: Callable[[], bool],
    naysayer_declared_no_tier_c: Callable[[], bool],
) -> bool:
    """Every carve-out ③ condition EXCEPT the Decider's clearance (G3).

    Split out so the conductor can ask "is the (network-bound) Decider worth
    calling for this turn?" through the same rule the predicate uses, instead of
    re-expressing the conjunction at the call site (the drift PR-review msg-2554
    flagged). :func:`guard_proposer_to_implementer` calls this and then the
    ``decider_clears`` thunk — so the Decider is only consulted, by either
    caller, when this returns ``True``.

    The conjunction, cheapest first (each thunk only fires if everything before
    it held):

    * ``author_is_naysayer`` ∧ ``control_state_is_run`` — pre-D-4' ③.
    * ``message_is_attested()`` — P-3b preflight stamp (pre-D-4' ③).
    * **G1** ``not segment_declares_tier_c()`` — no ``TIER-C: <label>`` line
      (any label, ``other:`` included) in any message since the most recent
      human-authored message (msg-4858 §2: the human is the ONLY reset
      boundary; implementer / proposer / naysayer turns and conductor-relay
      write-backs never reset it). Once raised it latches until the human
      speaks. Fail-closed: a quoted declaration closes the door, a human turn
      reopens it.
    * **G2** ``naysayer_declared_no_tier_c()`` — the naysayer's proceed turn
      carries ``TIER-C-CHECK: none`` on the line directly above its final
      ``NEXT:`` line (msg-4856 §3 G2). Absent / any other value ⇒ closed.

    Trust model (msg-4857, D-3 msg-598 Q2=yes): the declaration line, the
    attest stamp and the human boundary are all chatroom text and forgeable by
    anyone who can post. These gates remove the ordinary ways to reach code
    *without having judged* — they are noise reduction, not authentication; the
    authoritative Tier-C guard remains the human's manual ``main`` merge.
    """
    return (
        author_is_naysayer
        and control_state_is_run
        and message_is_attested()
        and not segment_declares_tier_c()
        and naysayer_declared_no_tier_c()
    )


def guard_proposer_to_implementer(
    *,
    author_is_human: bool,
    author_is_naysayer: bool,
    control_state_is_run: bool,
    message_is_attested: Callable[[], bool],
    segment_declares_tier_c: Callable[[], bool],
    naysayer_declared_no_tier_c: Callable[[], bool],
    decider_clears: Callable[[], bool],
) -> GuardIVerdict:
    """Decide whether a handoff to the implementer may proceed.

    This is the single source of truth for guard (i) — every caller that
    asks "may this handoff to the implementer proceed?" must consult this
    function rather than re-express the rule (T-operator-board msg-2544
    §C-3).

    Parameters are named booleans plus bool-returning nullary callables:
    lifting the *observations* to the caller keeps the predicate free of
    identity, role-registry, message-shape and network dependencies. Every
    observation that costs something (a body parse, a thread scan, a Decider
    round-trip) is a thunk so the predicate — and only the predicate — decides
    when it must be consulted (PR-review msg-2554 BLOCKING). None of the D-4'
    parameters has a default: a caller that forgets one fails at the call, it
    does not silently get the open side.

    Carve-out precedence:

    1. **carve-out ①**: ``author_is_human`` — honour immediately. A human-
       authored decide is the Tier-C gate itself. No thunk is invoked.
    2. **carve-out ③**: :func:`carve_out_iii_admissible` (naysayer ∧ RUN ∧
       attested ∧ G1 ∧ G2) **and** ``decider_clears()`` (**G3**, Takahito
       "B" decide: the Tier-C Decider evaluated this proceed turn and judged
       it NOT a matter for the human). ``decider_clears`` must be ``False``
       whenever the Decider is off (``backend=off``), undecided, or failed —
       so with no Decider in production, ``run`` behaves as ``supervised``
       for code handoffs (msg-4856 §3 G3: the accepted price of G3).
    3. Otherwise — ``REDIRECT``. Guard (i) fires; the caller decides whether
       it is an explicit-human terminal or a redirect.

    Carve-out ② (the PR-gate verdict relay, PR-2b-2) is not represented here
    because it is decided BEFORE this predicate is consulted, on the
    deterministic ``fire_pr_review`` verdict.
    """
    # carve-out ①: human-authored Tier-C decide (Tier-C msg-553 / msg-557).
    if author_is_human:
        return GuardIVerdict.HONOR
    # carve-out ③ + D-4' G1/G2, then G3 last (the only network-bound thunk).
    if (
        carve_out_iii_admissible(
            author_is_naysayer=author_is_naysayer,
            control_state_is_run=control_state_is_run,
            message_is_attested=message_is_attested,
            segment_declares_tier_c=segment_declares_tier_c,
            naysayer_declared_no_tier_c=naysayer_declared_no_tier_c,
        )
        and decider_clears()
    ):
        return GuardIVerdict.HONOR
    return GuardIVerdict.REDIRECT
