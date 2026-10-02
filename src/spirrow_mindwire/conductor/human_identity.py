"""Who counts as the human (Tier-C) identity — one rule, shared.

T-decider-conductor-hook DECIDED 2d-14 (Bohr msg-5582, endorsed Einstein msg-5583): the
conductor's carve-out ① / G1 reset (:meth:`~spirrow_mindwire.conductor.core.Conductor._is_human`)
and the shadow exporter's bounce-chain break (``scripts/export_shadow_eval_set.py``) must not hold
two definitions of "human". Both call :func:`is_human_identity`.

The rule is the one ``_is_human`` has always run: case-insensitive equality against the configured
``human_identity``; an empty ``human_identity`` matches nobody (fail-safe — every design→implement
handoff hard-rejects, and no bounce chain is ever broken).

It deliberately does **not** apply the ADR-11 separator normalisation
(:func:`~spirrow_mindwire.identity.normalize.normalize_identity_key`): widening who counts as the
human widens carve-out ①, and 2d-14 requires the conductor's behaviour to stay unchanged
(DECIDED in the thread, on Einstein's msg-5583 advisory).
"""

from __future__ import annotations

__all__ = ["is_human_identity"]


def is_human_identity(author: str, human_identity: str) -> bool:
    """Is ``author`` the configured human identity? Case-insensitive; empty identity ⇒ never."""
    return bool(human_identity) and author.casefold() == human_identity.casefold()
