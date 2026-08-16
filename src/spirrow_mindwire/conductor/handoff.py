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

Markdown tolerance is **additive, not subtractive** (msg-1129 §3). Two earlier rounds of this
parser tried to *remove* the decoration an author had written — first at end-of-line, then at word
edges — and both shipped green tests while the real failing shape stayed broken, because "the set
of characters to strip" does not close: add ``**`` and ``**,`` arrives; add that and ``**。``
arrives. So nothing is stripped. Instead each thing we are willing to dispatch is matched by a
pattern that describes *it*:

- **the line** — a handoff line is one where nothing but decoration surrounds the ``NEXT:``
  keyword. "Decoration" is defined negatively-but-closed: :data:`_DECORATION` = **no word
  characters, plus the underscore** (an ordered-list number is the one allowance). That covers
  ``>``, ``#``, ``-``/``*``/``+``, ``**``, ``_``, ``` ` ```, ``|`` table pipes and the ``→`` a real
  handoff used (chatroom ``msg-494``) without enumerating any of them, and it still refuses a line
  of prose that merely mentions ``NEXT:``. Decoration is admitted at all three positions a wrapper
  can close in — ``**NEXT: X**``, ``**NEXT**: X`` and ``**NEXT:** X`` — because a rule that admits
  only some of them is another enumeration wearing a closed rule's clothes.
- **the token** — :data:`_PR_REVIEW_RE` / :data:`_PR_REF_RE` match a PR ref in exactly the shapes
  :func:`~spirrow_mindwire.github.client.parse_pr_ref` accepts, and
  :data:`_PARTICIPANT_NAME_RE` matches the shape of a participant name. Whatever the author put
  *after* the name — ``**``, ``,``, ``。``, ``— a gloss`` — is outside the match and therefore
  falls away for free. This is also what protects the payload (Einstein's principle-3 objection):
  ``#`` and ``_`` are matched as *part of the ref pattern*, so ``owner/my_repo#7`` survives whole
  because it is the thing being matched, not because a strip was carefully aimed away from it.

This tolerance is a **transitional bridge**, not a permanent legacy fallback: Layer 3 will add a
structured ``next_participant`` field on the message itself, and when that lands this whole regex
scaffold becomes the compatibility path scheduled for removal, not a coequal parser kept forever.

``tests/data/next_line_corpus.tsv`` pins this against **real** traffic: every distinct
``NEXT:``-bearing line shape in the live ``spirrow-mindwire`` + ``spirrow-voxelworld`` chatrooms,
with its expected resolution. Imagined shapes only close imagined holes (msg-1129 §4).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ..value_objects import Role

# A handoff line must stand on its own (``^...$`` with MULTILINE). We take the LAST one so a
# ``NEXT:`` quoted earlier in the body cannot override the author's real, final handoff — the same
# defence as the naysayer verdict parser. That last-wins rule is also what makes the leading
# tolerance below safe: a permissive line rule matches quoted examples more often, and the real
# handoff is the one at the bottom.
#
# The tolerance rule is stated as a CLOSED property rather than a list of Markdown shells: on a
# handoff line everything that is not the keyword, the colon or the token is DECORATION, and
# decoration carries no word meaning. One allowance is carved out for an ordered-list number
# (``1.`` / ``2)``), whose digits are word characters.
#
# ``_DECORATION`` is that property, and it is ``\W`` **plus the underscore**. Python's ``\w``
# counts ``_`` as a word character, so the plain ``[^\w\n]*`` this module shipped in `45b767d`
# could not consume a single character of ``_NEXT: Bohr_`` and the whole match failed — a shape the
# comment right below it advertised as supported, and one the revision before this rewrite had
# actually handled (msg-1148 §5-4: a regression, not a shortfall).
#
# ``\w`` is Unicode-aware here, which is load-bearing: the CJK prose that surrounds most of these
# handoffs counts as word characters, so a Japanese sentence mentioning the keyword is refused for
# the same reason an English one is. Carving out ``_`` does not weaken that — ``_`` is the only
# character moved from "word" to "decoration", and no prose is made of underscores.
_DECORATION = r"(?:[^\w\n]|_)*"

# Decoration is admitted at every position where it can occur, because a wrapper's two halves do
# not both land in the same place. There are three, and the previous revision handled only the
# first:
#   1. before the keyword          ``**NEXT: Bohr**``   ``> NEXT: Bohr``   ``→ **NEXT: human**(…)``
#   2. inside the keyword's shell  ``**NEXT**: Bohr``   ``NEXT : Bohr``  (and the fullwidth colon)
#   3. between the colon and token ``**NEXT:** Bohr``   ``` `NEXT:` Bohr ```   ``NEXT:** Bohr**``
# Position 3 is the second axis of the `45b767d` regression and it is NOT the underscore bug: ``*``
# is not a word character, so widening the character class alone leaves ``**NEXT:** Bohr``
# unroutable (msg-1148 §5-5 / msg-1150 §1). Position 3 is owned by the TOKEN patterns below rather
# than by this one, so ``_last_next_raw`` keeps handing both resolution routes the same raw text.
_NEXT_KEYWORD = "NEXT" + _DECORATION + r"[:：]"  # noqa: RUF001 (fullwidth colon intentional)
_NEXT_LINE_RE = re.compile(
    r"^"
    + _DECORATION
    + r"(?:\d+[.)]"
    + _DECORATION
    + r")?"
    + _NEXT_KEYWORD
    + r"\s*(?P<token>\S.*?)\s*$",
    re.MULTILINE,
)

# The participant name: one identifier-shaped word, matched at the head of the token past any
# decoration (position 3 above: the ``**`` of ``**NEXT:** Bohr``, the closing ``` ` ``` of
# ``` `NEXT:` Bohr ```). Separators are allowed only BETWEEN alphanumerics, never at either end —
# which is why the leading ``_`` of ``_NEXT:_ Bohr`` is decoration and the trailing ``_`` of
# ``_NEXT: human_`` falls outside the match, while ``some_bot`` keeps its underscore. Everything
# after the name (``**``, ``,``, ``。``, ``— a gloss``, a fullwidth parenthetical) is not part of
# the pattern either, so no list of trailing characters has to be maintained.
_PARTICIPANT_NAME_RE = re.compile(_DECORATION + r"(?P<name>[A-Za-z0-9]+(?:[_-]+[A-Za-z0-9]+)*)")

# Reserved sentinels (case-insensitive). Not roster participants. Public because they are the
# single source of truth for the NEXT vocabulary shared by the *parser* (below) and the *emission*
# instructions injected into the adapters (:func:`build_handoff_protocol_block`) — the two must use
# the same words, so they read them from here rather than re-spelling the literals.
HUMAN_TOKEN = "human"
NONE_TOKEN = "none"

# The standing-autonomy ``DELEGATE`` marker that used to live here is gone. It authorised carve-out
# ③ per *thread*, from the most recent human message, and non-stickily — so it had to be re-written
# on every human turn and forgetting it stopped the loop. Authorisation is now per *project* and
# latching, in :mod:`spirrow_mindwire.conductor.control`; there is nothing to parse out of a message
# body for it, which is the point (a marker in a thread is a second source of the same truth).


# The PR-gate sentinel: ``NEXT: pr-review <owner/repo#n>`` fires the Tier B independent naysayer
# review on the named PR (PR-2b-2). Unlike a persona handoff the target is a PR ref, so it is
# resolved before the participant-name match.
PR_REVIEW_TOKEN = "pr-review"
# The sentinel is the word plus an operand: a bare ``NEXT: pr-review`` with nothing after it is not
# a PR-gate directive at all (it falls through to the participant path and out as ABSENT → human).
_PR_REVIEW_RE = re.compile(rf"{_DECORATION}{PR_REVIEW_TOKEN}\b(?P<rest>.*)", re.IGNORECASE)
# The ref, matched by its own shape rather than as "the next non-whitespace run". These are exactly
# the two forms :func:`~spirrow_mindwire.github.client.parse_pr_ref` accepts, so the sentinel and
# the validator agree by construction. Because the pattern says what a ref *is*, anything an author
# appends to it is outside the match: ``acme/widgets#7**, please gate`` yields ``acme/widgets#7``
# without a rule about ``*`` or ``,`` existing anywhere in this module. It is also what keeps the
# payload intact — ``#`` and ``_`` are matched *as part of the ref*, so ``acme/my_repo#7`` and an
# underscored PR URL survive whole (Einstein's principle-3 objection, msg-1107).
_PR_REF_RE = re.compile(
    r"(?:https?://)?(?:[A-Za-z0-9-]+\.)*github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/\d+"
    r"|[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#\d+"
)


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
    """The raw text after the keyword on the **last** ``NEXT:`` line, or ``None`` if there is none.

    Raw really is raw: nothing is removed here. The ``pr-review`` route and the persona route both
    read this same text and each matches its own target out of it, so the two cannot disagree about
    what the author wrote (msg-1074 §4-1 wanted one owner for the token; making both routes read an
    unmodified string is a stronger form of that than making both read the same *edited* string).
    """
    matches = _NEXT_LINE_RE.findall(body)
    if not matches:
        return None
    return str(matches[-1]).strip() or None


def _name_from_raw(raw: str) -> str | None:
    """The participant name at the head of a raw NEXT token, or ``None`` if there is not one."""
    match = _PARTICIPANT_NAME_RE.match(raw)
    return match.group("name") if match is not None else None


def parse_next_token(body: str) -> str | None:
    """Return the participant name from the **last** ``NEXT:`` line, or ``None`` if there is none.

    Only the name is returned. The gloss most real handoffs carry after it — a parenthetical
    (ASCII or CJK), an em-dash sentence, a closing ``**`` — is not part of the name pattern and so
    never reaches the caller. This function deliberately takes no roster: ``head_skip`` depends on
    being able to read a token for a persona it has never heard of (fail-open on unknown persona).
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
    sentinel = _PR_REVIEW_RE.match(raw)
    if sentinel is not None and (operand := sentinel.group("rest").strip()):
        # NEXT: pr-review <owner/repo#n> — take the ref by its own shape, so whatever the author
        # wrapped or appended it with is outside the match: ``acme/widgets#7**, please gate``
        # resolves to ``acme/widgets#7`` with no rule about ``*`` or ``,`` anywhere in this module.
        # An operand with no ref shape in it is still the sentinel (the author asked for a gate);
        # the conductor re-validates with parse_pr_ref and fails safe to the human rather than
        # firing (Tier B PR #103 round 4).
        ref = _PR_REF_RE.search(operand)
        return Handoff(HandoffKind.PR_REVIEW, token=ref.group(0) if ref else operand)
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
        "disposition (`NEXT: <proposer persona>`); if the design is sound and ready to build, hand "
        "to the implementer (`NEXT: <implementer persona>`) — while this project's loop is running "
        "autonomously the conductor builds it directly, otherwise it routes your go to the human "
        f"for the Tier-C decision; or hand to `{HUMAN_TOKEN}` to escalate a concern that needs the "
        "human now. You are advisory, not a veto — but your escalation pulls the human back in "
        "however autonomously the loop is running."
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
