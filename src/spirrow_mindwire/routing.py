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

Why this predicate is a module of its own — T-operator-board msg-2544 §C-3.
The operator-board's ``R-NEXT-HEIS-GUARD`` transition must consult THE SAME
routing rule the conductor consults; otherwise the two will drift. The rule
is written here once, both call sites import it, and a follow-up test pins
the definition count so a future in-place re-expression cannot silently
re-open the drift. Bohr's v0.2 sequencing constraint (msg-2544 §C-3):
"抽出が landing するまで board の routing を live にしない" — this module
is the extraction; the board routing lands against this import.

The predicate is pure — it operates on booleans lifted from the conductor's
state, takes no message body, does no I/O, and holds no attribution logic.
Everything that decides *whether an author is the human*, *whether an
identity is the naysayer role*, *whether the control state is RUN*, and
*whether the message is attested* stays with its owner (the conductor's
roster / control plane / attestation reader); this predicate only combines
those observations into a routing verdict. That is the drift-resistant
boundary — future carve-out changes edit the enum + this function only,
never the ownership of each observation.

Attribution: T-operator-board thread (msg-2542 orchestrator design, msg-2544
Bohr v0.2 §C-3 single-source extraction). ADR-2026-06-03-17 is cited from
the historical guard-(i) comments in ``conductor/core.py``; the ADR body is
not readable from this repository, so this module reproduces the guard's
behaviour verbatim from that surface, not from the ADR text.
"""

from __future__ import annotations

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


def guard_proposer_to_implementer(
    *,
    author_is_human: bool,
    author_is_naysayer: bool,
    control_state_is_run: bool,
    message_is_attested: bool,
) -> GuardIVerdict:
    """Decide whether a handoff to the implementer may proceed.

    This is the single source of truth for guard (i) — every caller that
    asks "may this handoff to the implementer proceed?" must consult this
    function rather than re-express the rule (T-operator-board msg-2544
    §C-3). Semantics are preserved verbatim from the previous inline
    ``_route`` decision in :mod:`spirrow_mindwire.conductor.core`.

    Parameters are named booleans (rather than raw objects) on purpose:
    lifting the *observations* to the caller keeps the predicate free of
    identity, role-registry, and message-shape dependencies, so the two
    call sites (the conductor and the future operator-board tick) share
    the rule without also sharing a common object graph.

    Carve-out precedence:

    1. **carve-out ①**: ``author_is_human`` — honour immediately. A human-
       authored decide is the Tier-C gate itself; no other check may
       withdraw the authorisation the human just gave.
    2. **carve-out ③**: ``author_is_naysayer AND control_state_is_run AND
       message_is_attested`` — honour. The independent naysayer's own
       proceed under RUN is the only autonomous door to code; un-attested,
       the branch is not taken.
    3. Otherwise — ``REDIRECT``. Guard (i) fires. The conductor's inline
       version returned ``_human_terminal(..., explicit_human=False)``; the
       distinction between "an explicit ``NEXT: human``" and "a guard-(i)
       redirect" (which drives the ``forced_naysayer_turns_saveable``
       metric and the ``force_naysayer_only_on_explicit_human`` lever) is
       the caller's responsibility, not this predicate's.

    Carve-out ② (the PR-gate verdict relay, PR-2b-2) is not represented in
    the parameter list because it is decided BEFORE this predicate is
    consulted: the conductor routes on the deterministic ``fire_pr_review``
    verdict, never on any parsed ``NEXT:`` line for a pr-review sentinel
    (comment in ``core.py`` above ``HandoffKind.PR_REVIEW``). Modelling it
    here would duplicate the marker-gated trust decision that must live
    with the PR-gate.
    """
    # carve-out ①: human-authored Tier-C decide (Tier-C msg-553 / msg-557).
    if author_is_human:
        return GuardIVerdict.HONOR
    # carve-out ③: independent naysayer's own proceed under RUN + attest
    # (P-3b, Tier-C msg-954 §2 / msg-970).
    if author_is_naysayer and control_state_is_run and message_is_attested:
        return GuardIVerdict.HONOR
    return GuardIVerdict.REDIRECT
