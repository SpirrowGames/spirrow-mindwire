"""Which identities the Conductor may spawn, and which it must hand to a human.

ADR-2026-09-14-21 D-2 / D-3. The 4-layer identity model's fourth layer —
``embodiment`` (稼働形態, ADR-2026-05-29-12) — decides whether a nominated
target can be *started* at all. The conductor's adapters drive one embodiment:
a terminal coding agent. An identity whose embodiment is anything else has no
adapter that could run it, and the ADR forbids writing one for ``web_ai_chat``
(that is ADR-2026-05-31-14's ガワ方式, withdrawn over a ToS conflict).

So the rule this module carries is the ADR's general one, not a Fermi special
case:

    embodiment != terminal_coding_agent  ⟹  do not spawn; stop at the human.

Fermi is the first identity it applies to, which is why it is the one default
entry. A second such identity is a table row, not a code change.

**The vocabulary is Prismind's, not this module's.** ``EMBODIMENT_VALUES``
mirrors ``spirrow_prismind.integrations.memory_client.EMBODIMENT_VALUES`` —
the enum ``upsert_identity`` validates against. Keeping a copy here is
deliberate (mindwire must decide spawnability without a network read) and the
copy is a *subset check only*: nothing here may invent a value Prismind would
reject.

**Silence about an identity is not a claim that it is web-driven.** An identity
the table does not name is treated as spawnable, because the roster — which an
operator writes by hand — is already the statement "these personas are driven
by this daemon". Defaulting the other way would stop every thread on the first
tick after a new persona was added, which is a worse failure than the one this
module prevents.
"""

from __future__ import annotations

from collections.abc import Mapping

from .normalize import normalize_identity_key

__all__ = [
    "DEFAULT_IDENTITY_EMBODIMENT",
    "EMBODIMENT_VALUES",
    "SPAWNABLE_EMBODIMENT",
    "blocked_embodiment",
    "normalize_embodiment_table",
]

# Mirrors Prismind's enum (see module docstring). ``unknown`` is the value ADR-12 gives an actor
# no name applies to; it is NOT spawnable — "we do not know what runs this" is not a licence to
# start a terminal session under its name.
EMBODIMENT_VALUES: tuple[str, ...] = ("web_ai_chat", "terminal_coding_agent", "unknown")

SPAWNABLE_EMBODIMENT = "terminal_coding_agent"
"""The one embodiment the conductor's adapters can actually start."""

DEFAULT_IDENTITY_EMBODIMENT: Mapping[str, str] = {
    # ADR-2026-09-14-21: the claude.ai conversational session that posts to the chatroom.
    # independence_class=human, role=可変, embodiment=web_ai_chat, never spawned.
    "Fermi": "web_ai_chat",
}
"""Shipped defaults, merged under any operator table from ``[conductor.identity_embodiment]``.

Shipped rather than left to configuration because the decision is an ADR, not a deployment
choice: a loop host that forgot the config line would spawn-attempt Fermi, which is precisely
what the ADR rules out.
"""


def normalize_embodiment_table(
    raw: Mapping[str, str] | None, *, include_defaults: bool = True
) -> dict[str, str]:
    """Return the identity→embodiment table keyed by ADR-11 partition keys.

    Operator entries win over :data:`DEFAULT_IDENTITY_EMBODIMENT` on the same key, so a
    deployment can correct a shipped default (for instance, if Fermi ever gained a terminal
    embodiment) without a release. Keys are normalised on the way in, which is what makes
    ``fermi`` / ``Fermi`` / ``FERMI`` one row rather than three.
    """
    table: dict[str, str] = {}
    if include_defaults:
        for name, embodiment in DEFAULT_IDENTITY_EMBODIMENT.items():
            table[normalize_identity_key(name)] = embodiment
    for name, embodiment in (raw or {}).items():
        key = normalize_identity_key(name)
        if key:
            table[key] = embodiment
    return table


def blocked_embodiment(identity: str, table: Mapping[str, str]) -> str | None:
    """The embodiment blocking ``identity`` from being spawned, or ``None`` if it may be.

    ``None`` is returned both for an identity the table does not name (see the module
    docstring: silence means spawnable) and for one it names as ``terminal_coding_agent``.
    Every other value — including one this module does not recognise — blocks, because an
    embodiment nobody here can interpret is not one any adapter here can run.
    """
    if not identity:
        return None
    embodiment = table.get(normalize_identity_key(identity))
    if embodiment is None or embodiment == SPAWNABLE_EMBODIMENT:
        return None
    return embodiment
