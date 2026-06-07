"""NEXT-handoff parsing + identity→role resolution for the conductor.

The cross-thread relay conductor (``T-cross-thread-relay-conductor`` msg-520 / Tier-C decide
msg-523) drives a single design thread by reading the **last** ``NEXT: <participant>`` line of the
latest message and dispatching that one participant. This module is the pure, side-effect-free
parsing + resolution layer:

- :func:`parse_next_token` extracts the participant token from a message body. The **last**
  ``NEXT:`` line wins (mirroring the PR-review verdict parser, ``naysayer/pr_review.py``), so an
  earlier quoted ``NEXT:`` — e.g. inside a relayed critique — cannot hijack the real, final handoff.
- :func:`resolve_handoff` maps that token to a :class:`Handoff`: a participant role (via the
  operator's identity→role *roster*), the reserved ``human`` (Tier-C stop) / ``none`` (settled)
  sentinels, or :attr:`HandoffKind.ABSENT` when there is no parseable handoff — which the conductor
  routes to a human fallback rather than halting silently (Obj3 / D-4, msg-522).

The NEXT vocabulary is the chatroom **identity / persona name** (e.g. ``Bohr`` / ``Heisenberg`` /
``Einstein`` / ``human``), not the internal role string; the roster is the persona→role map supplied
by config, and the conductor authors each reply under the persona name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ..value_objects import Role

# A handoff line must stand on its own (``^...$`` with MULTILINE). We take the LAST one so a
# ``NEXT:`` quoted earlier in the body cannot override the author's real, final handoff — the same
# defence as the naysayer verdict parser.
_NEXT_LINE_RE = re.compile(r"^\s*NEXT:\s*(?P<token>\S.*?)\s*$", re.MULTILINE)

# The participant name is the leading run before any whitespace or an opening paren (ASCII or
# fullwidth CJK): real handoffs carry a trailing gloss after the name, and only the name selects the
# participant. The fullwidth left paren U+FF08 is matched too so a CJK gloss is split off.
_NAME_SPLIT_RE = re.compile(r"[\s(（]")  # noqa: RUF001 (the fullwidth paren is intentional)

# Trailing punctuation stripped from the parsed name (a stray comma / period after the persona name
# should not defeat the roster lookup).
_TRAILING_PUNCT = " \t.,;:!?、。）)"  # noqa: RUF001 (fullwidth/CJK punctuation is intentional)

# Reserved sentinels (case-insensitive). Not roster participants.
_HUMAN_TOKEN = "human"
_NONE_TOKEN = "none"


class HandoffKind(StrEnum):
    """What a parsed ``NEXT:`` directive resolves to."""

    ROLE = "role"  # a roster participant → dispatch that role's adapter
    HUMAN = "human"  # NEXT: human — a Tier-C decision point; the conductor stops
    NONE = "none"  # NEXT: none — the thread is settled; the conductor stops
    ABSENT = "absent"  # no parseable NEXT (missing / unknown participant) → human fallback (Obj3)


@dataclass(frozen=True)
class Handoff:
    """The resolved handoff target of a message's final ``NEXT:`` line.

    ``identity`` / ``role`` are set only when ``kind is HandoffKind.ROLE`` (``identity`` is the
    roster's canonical persona name). ``token`` is the raw participant name parsed (observability),
    ``None`` when no ``NEXT:`` line was found at all.
    """

    kind: HandoffKind
    identity: str | None = None
    role: Role | None = None
    token: str | None = None


def parse_next_token(body: str) -> str | None:
    """Return the participant name from the **last** ``NEXT:`` line, or ``None`` if there is none.

    The trailing parenthetical gloss many handoffs carry is stripped — only the leading name is
    returned (e.g. a name followed by a CJK or ASCII parenthetical gloss yields just the name).
    """
    matches = _NEXT_LINE_RE.findall(body)
    if not matches:
        return None
    raw = matches[-1].strip()
    name = _NAME_SPLIT_RE.split(raw, maxsplit=1)[0].strip(_TRAILING_PUNCT)
    return name or None


def resolve_handoff(body: str, roster: Mapping[str, Role]) -> Handoff:
    """Parse + resolve the latest ``NEXT:`` directive against the identity→role ``roster``.

    Resolution order: the reserved ``human`` / ``none`` sentinels first, then the roster
    (case-insensitive on the identity name). A missing ``NEXT:`` line, the empty token, or a name
    that is not a known participant all resolve to :attr:`HandoffKind.ABSENT` — the conductor treats
    every ABSENT as "route to human" (Obj3 / D-4) so a malformed handoff flags a human rather than
    silently stranding the thread.
    """
    token = parse_next_token(body)
    if token is None:
        return Handoff(HandoffKind.ABSENT)
    folded = token.casefold()
    if folded == _HUMAN_TOKEN:
        return Handoff(HandoffKind.HUMAN, token=token)
    if folded == _NONE_TOKEN:
        return Handoff(HandoffKind.NONE, token=token)
    match = _roster_lookup(roster, token)
    if match is None:
        return Handoff(HandoffKind.ABSENT, token=token)
    identity, role = match
    return Handoff(HandoffKind.ROLE, identity=identity, role=role, token=token)


def _roster_lookup(roster: Mapping[str, Role], name: str) -> tuple[str, Role] | None:
    """Case-insensitive identity→role lookup; returns the **canonical** (identity, role)."""
    direct = roster.get(name)
    if direct is not None:
        return name, direct
    folded = name.casefold()
    for identity, role in roster.items():
        if identity.casefold() == folded:
            return identity, role
    return None


__all__ = [
    "Handoff",
    "HandoffKind",
    "parse_next_token",
    "resolve_handoff",
]
