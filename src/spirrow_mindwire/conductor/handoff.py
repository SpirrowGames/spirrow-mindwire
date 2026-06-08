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

# Reserved sentinels (case-insensitive). Not roster participants. Public because they are the
# single source of truth for the NEXT vocabulary shared by the *parser* (below) and the *emission*
# instructions injected into the adapters (:func:`build_handoff_protocol_block`) — the two must use
# the same words, so they read them from here rather than re-spelling the literals.
HUMAN_TOKEN = "human"
NONE_TOKEN = "none"

# The PR-gate sentinel: ``NEXT: pr-review <owner/repo#n>`` fires the Tier B independent naysayer
# review on the named PR (PR-2b-2). Unlike a persona handoff, the whole rest of the line is the PR
# ref (it carries ``/`` and ``#``), so it is parsed off the RAW NEXT line before the name-split.
PR_REVIEW_TOKEN = "pr-review"
_PR_REVIEW_RE = re.compile(rf"^{PR_REVIEW_TOKEN}\s+(?P<ref>\S.*?)\s*$", re.IGNORECASE)


class HandoffKind(StrEnum):
    """What a parsed ``NEXT:`` directive resolves to."""

    ROLE = "role"  # a roster participant → dispatch that role's adapter
    HUMAN = "human"  # NEXT: human — a Tier-C decision point; the conductor stops
    NONE = "none"  # NEXT: none — the thread is settled; the conductor stops
    PR_REVIEW = "pr_review"  # NEXT: pr-review <owner/repo#n> — fire the Tier B PR-gate (PR-2b-2)
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


def _last_next_raw(body: str) -> str | None:
    """The raw text of the **last** ``NEXT:`` line (last wins; see module docstring), or ``None``.

    The ``pr-review <ref>`` PR-gate sentinel needs the whole line (the ref carries ``/`` and ``#``),
    so resolution works off this raw text and only name-splits for a persona handoff.
    """
    matches = _NEXT_LINE_RE.findall(body)
    if not matches:
        return None
    return str(matches[-1]).strip()


def _name_from_raw(raw: str) -> str | None:
    """The persona name from a raw NEXT token (gloss + trailing punctuation stripped)."""
    name = _NAME_SPLIT_RE.split(raw, maxsplit=1)[0].strip(_TRAILING_PUNCT)
    return name or None


def parse_next_token(body: str) -> str | None:
    """Return the participant name from the **last** ``NEXT:`` line, or ``None`` if there is none.

    The trailing parenthetical gloss many handoffs carry is stripped — only the leading name is
    returned (e.g. a name followed by a CJK or ASCII parenthetical gloss yields just the name).
    """
    raw = _last_next_raw(body)
    return _name_from_raw(raw) if raw is not None else None


def resolve_handoff(body: str, roster: Mapping[str, Role]) -> Handoff:
    """Parse + resolve the latest ``NEXT:`` directive against the identity→role ``roster``.

    Resolution order: the ``pr-review <ref>`` PR-gate sentinel (PR-2b-2) first, then the reserved
    ``human`` / ``none`` sentinels, then the roster (case-insensitive on the identity name). A
    missing ``NEXT:`` line, the empty token, or a non-participant name all resolve to
    :attr:`HandoffKind.ABSENT` — the conductor treats every ABSENT as "route to human" (Obj3 / D-4)
    so a malformed handoff flags a human rather than silently stranding the thread.
    """
    raw = _last_next_raw(body)
    if raw is None:
        return Handoff(HandoffKind.ABSENT)
    pr_review = _PR_REVIEW_RE.match(raw)
    if pr_review is not None:
        # NEXT: pr-review <owner/repo#n> — the whole ref is the token (not a leading name); the
        # conductor validates it via parse_pr_ref and fires the synchronous Tier B review (PR-2b-2).
        return Handoff(HandoffKind.PR_REVIEW, token=pr_review.group("ref"))
    token = _name_from_raw(raw)
    if token is None:
        return Handoff(HandoffKind.ABSENT, token=raw)
    folded = token.casefold()
    if folded == HUMAN_TOKEN:
        return Handoff(HandoffKind.HUMAN, token=token)
    if folded == NONE_TOKEN:
        return Handoff(HandoffKind.NONE, token=token)
    match = _roster_lookup(roster, token)
    if match is None:
        return Handoff(HandoffKind.ABSENT, token=token)
    identity, role = match
    return Handoff(HandoffKind.ROLE, identity=identity, role=role, token=token)


# --------------------------------------------------------------------------- #
# Emission side: the NEXT-protocol block injected into the adapter system prompts
# (PR-2b-1). This is the counterpart of resolve_handoff (the parser) and lives in
# the same module so the sentinel vocabulary (HUMAN_TOKEN / NONE_TOKEN + persona
# names) has one source of truth and cannot drift between emit and parse.
# --------------------------------------------------------------------------- #

_HANDOFF_PROTOCOL_CORE = f"""\
---
Conductor handoff protocol (REQUIRED)

A conductor drives this thread one turn at a time: after your reply is posted it \
reads the LAST line of your message to decide who acts next. So end EVERY reply \
with exactly one handoff line, and make it the FINAL line of your reply:

    NEXT: <name>

`<name>` is either another participant's persona name (spelled exactly as it \
appears as a message author in this thread) or one of two reserved words:

  - `NEXT: {HUMAN_TOKEN}` — hand to the human for a Tier-C decision (e.g. \
approving a design for implementation, or merging to the main branch).
  - `NEXT: {NONE_TOKEN}` — the thread is settled; there is nothing left to do.

The handoff line is part of your verbatim reply, not meta-commentary: write it \
out literally (for example `NEXT: {HUMAN_TOKEN}`) and put nothing after it."""

_ROLE_HANDOFF_GUIDANCE: dict[Role, str] = {
    Role.PROPOSER: (
        "As the proposer: after you propose or revise a design, hand to the independent naysayer "
        "for a design review (`NEXT: <naysayer persona>`). Do NOT hand a design straight to the "
        "implementer — a design must clear an independent naysayer review and a human Tier-C "
        f"decision before implementation, so when it is ready for that decision hand to "
        f"`{HUMAN_TOKEN}`. (The conductor enforces this structurally: a `NEXT:` from you to the "
        "implementer is redirected to the human.)"
    ),
    Role.IMPLEMENTER: (
        "As the implementer: when you open or update a develop→main pull request, hand to the "
        f"PR-gate — end your reply with `NEXT: {PR_REVIEW_TOKEN} <owner/repo#n>` (the PR ref) so "
        "the independent naysayer review runs before any human merge. For other work, hand back "
        "to the proposer for a spec-review (`NEXT: <proposer persona>`); for a Tier-C decision "
        f"such as merging, hand to `{HUMAN_TOKEN}` — you never merge to the main branch yourself."
    ),
    Role.NAYSAYER: (
        "As the naysayer: after your critique, hand back to the proposer if your objections need a "
        f"disposition (`NEXT: <proposer persona>`), or to `{HUMAN_TOKEN}` if the design is clean "
        "and ready for the human's Tier-C decision. You are advisory, not a veto."
    ),
}


def build_handoff_protocol_block(role: Role) -> str:
    """The NEXT-emission instruction block to append to ``role``'s adapter system prompt (PR-2b-1).

    Teaching the proposer / implementer / naysayer adapters to end every reply with a ``NEXT:`` line
    is what lets the conductor chain the design loop autonomously (msg-540 / Tier-C decide msg-543).
    The block is the emission counterpart of :func:`resolve_handoff`; both read the reserved
    sentinels from this module so emit and parse never diverge.

    This is a *prompt* (a polite request to a well-behaved model), **not** the safety boundary: the
    conductor's routing guards are the structural enforcement — design→implement handoffs from a
    non-human / non-naysayer author are redirected to the human (Tier-C gate, ADR-2026-06-03-17),
    and a human-terminal turn forces an independent naysayer consult first (Obj2). The role guidance
    here only nudges a cooperating model toward the same outcome.
    """
    return f"{_HANDOFF_PROTOCOL_CORE}\n\n{_ROLE_HANDOFF_GUIDANCE[role]}\n"


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
    "HUMAN_TOKEN",
    "NONE_TOKEN",
    "PR_REVIEW_TOKEN",
    "Handoff",
    "HandoffKind",
    "build_handoff_protocol_block",
    "parse_next_token",
    "resolve_handoff",
]
