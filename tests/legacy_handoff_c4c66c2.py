"""The NEXT-handoff parser exactly as it stood at ``c4c66c2`` — a migration reference.

**This module is not part of the conductor.** Nothing outside
``tests/test_conductor_handoff_migration.py`` imports it, and it never resolves a real handoff. It
exists so the rewrite in this PR can be checked against the thing it replaced, mechanically, over
an input set nobody enumerated by hand.

Why it is here (``T-handoff-parser-markdown-tolerance`` msg-1150 §3): the additive rewrite closed
the shapes it aimed at and silently dropped seven the subtractive version had handled
(``_NEXT: Bohr_``, ``**NEXT:** Heisenberg``, …). Neither the suite nor the real-traffic corpus
could see it — a corpus contains what people *wrote*, not what the code *could do*, so nothing in
it records a capability being lost. Comparing the two implementations directly is what does, and
it is how the loss was actually found (by hand, in review; this file is that hand-work mechanised).

Provenance — this file was generated, not typed::

    git show c4c66c2:src/spirrow_mindwire/conductor/handoff.py

with exactly three whole-section deletions/edits and no line edits inside what remains:

1. the module docstring was replaced by this one;
2. the emission half (``_HANDOFF_PROTOCOL_CORE`` / ``_ROLE_HANDOFF_GUIDANCE`` /
   ``build_handoff_protocol_block``) and ``__all__`` were deleted — they are prompt text, not
   parsing, and keeping a stale copy of a prompt invites someone to read it as current;
3. the one relative import (``from ..value_objects import Role``) was made absolute.

Diff it against the git output above to confirm; every remaining line, comment included, is
verbatim. In particular the comments still describe the *old* design (a strip step, a name split,
trailing-punctuation trimming) — that is the point, and they must not be "corrected" to match the
current module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from spirrow_mindwire.value_objects import Role

# A handoff line must stand on its own (``^...$`` with MULTILINE). We take the LAST one so a
# ``NEXT:`` quoted earlier in the body cannot override the author's real, final handoff — the same
# defence as the naysayer verdict parser.
#
# Layer 2 — Markdown-noise tolerance (transitional; see module docstring for the sunset plan).
# An LLM sometimes wraps its final handoff line in a small set of Markdown shell characters:
#   > NEXT: Bohr        (blockquote)
#   # NEXT: Bohr        (ATX heading)
#   - NEXT: Bohr        (list bullet — also `*` / `+`)
#   **NEXT: Bohr**      (bold wrap — also `*`, `_`, `` ` ``)
# The tolerance is intentionally narrow — a closed enumeration of Markdown shells that consume
# characters BEFORE ``NEXT:``, plus the ATX heading close on the line tail. The token's own
# emphasis close is NOT handled here (it is usually followed by a gloss, so it is not at ``$``);
# that job belongs to ``_strip_md_emphasis`` below, and splitting it this way keeps each character
# with exactly one owner.
# Critically, ``#`` is only stripped as a leading heading marker (``# `` or ``## ``, whitespace
# required) or as an optional ATX close on the outermost line-tail. It is NEVER stripped from
# elsewhere on the line — a `pr-review owner/repo#7` ref must survive intact (Einstein's design
# review, principle 3). The extraction is still a single ``findall`` pass (no RAW-vs-normalised
# double loop) so "which line is the handoff" has one source of truth.
_MD_LEADING_SHELL = (
    r"(?:"
    r"[>#]+\s+"  # blockquote `>` or ATX heading `#`/`##`/... (whitespace REQUIRED after)
    r"|[-*+]\s+"  # list bullet (whitespace REQUIRED, so a bold `**...**` is not eaten char-by-char)
    r"|\d+\.\s+"  # ordered list
    r")*"
)
_MD_INLINE_OPEN = r"(?:\*\*|__|\*|_|`)?"
# An ATX heading may end with a run of `#` chars separated from the content by whitespace
# (``## Heading ##``). We tolerate that shape on the *outermost* tail only, and — because
# whitespace is required — this never touches a `pr-review ...#7` ref (there is no space
# between `owner/repo` and `#7`).
_MD_TRAILING_SHELL = r"(?:\s+#+)?\s*"
_NEXT_LINE_RE = re.compile(
    r"^\s*"
    + _MD_LEADING_SHELL
    + _MD_INLINE_OPEN
    + r"\s*NEXT:\s*(?P<token>\S.*?)\s*"
    + _MD_TRAILING_SHELL
    + r"$",
    re.MULTILINE,
)

# ★ The emphasis CLOSE cannot live in the line regex, because it is not always at the end of the
# line. msg-1074 §2 is explicit that this bug fails at BOTH ends and that fixing one leaves it
# broken ("前を直しても後ろで落ちる。片方だけの修正は無効"), and the line that actually stopped the
# loop carries a gloss AFTER the closing ``**``:
#
#     **NEXT: Heisenberg** — ③ fixture field-fidelity audit（…）に着手する。  # noqa: RUF003
#
# Anchoring the close to ``$`` handles ``**NEXT: X**`` but not that shape: the token comes out as
# ``Heisenberg** — …``, ``_NAME_SPLIT_RE`` cuts it at the space, and the roster is asked for
# ``Heisenberg**``. So the closing side is owned by exactly ONE normalisation, applied once in
# :func:`_last_next_raw` — before the ``pr-review`` raw read AND before the name-split, so both
# resolution paths see the same cleaned text (msg-1074 §4-1: "正規化を 1 箇所に置く").
#
# The two owners do not overlap, which is what keeps this from becoming the very "2 箇所を別々に
# 緩める" failure §4-1 warns about:
#   - the regex owns the LINE shell    — `>` `#` bullets + the opening emphasis (it must, since
#     locating the line at all depends on them);
#   - this normalisation owns the TOKEN's emphasis — `*` `_` `` ` `` only, never `#`.
#
# It strips emphasis runs only at WORD EDGES (start/end of the token, or adjacent to whitespace).
# That is what protects payload characters: `#` is never in the character class at all, so a
# ``pr-review owner/repo#7`` ref keeps its PR number, and an `_` *inside* a word — ``acme/my_repo``,
# or an underscored URL — is not at an edge and therefore survives (Einstein's principle-3
# objection: normalisation must not damage the ref).
_MD_EMPHASIS_EDGE_RE = re.compile(r"(?<![^\s])[*_`]+|[*_`]+(?![^\s])")


def _strip_md_emphasis(text: str) -> str:
    """Remove Markdown emphasis runs at word edges, leaving payload characters intact."""
    return _MD_EMPHASIS_EDGE_RE.sub("", text).strip()


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

# The standing-autonomy ``DELEGATE`` marker that used to live here is gone. It authorised carve-out
# ③ per *thread*, from the most recent human message, and non-stickily — so it had to be re-written
# on every human turn and forgetting it stopped the loop. Authorisation is now per *project* and
# latching, in :mod:`spirrow_mindwire.conductor.control`; there is nothing to parse out of a message
# body for it, which is the point (a marker in a thread is a second source of the same truth).


# The PR-gate sentinel: ``NEXT: pr-review <owner/repo#n>`` fires the Tier B independent naysayer
# review on the named PR (PR-2b-2). Unlike a persona handoff, the whole rest of the line is the PR
# ref (it carries ``/`` and ``#``), so it is parsed off the RAW NEXT line before the name-split.
PR_REVIEW_TOKEN = "pr-review"
# The ref is the single non-whitespace token after ``pr-review`` (an ``owner/repo#n`` or a URL —
# neither contains a space). ``\S+`` (not ``.*?$``) so a trailing gloss an LLM appends, e.g.
# ``pr-review acme/repo#7 (please review)``, is ignored rather than swallowed into the ref and
# handed to GitHub as an invalid ref (Tier B PR #103 round 2).
_PR_REVIEW_RE = re.compile(rf"^{PR_REVIEW_TOKEN}\s+(?P<ref>\S+)", re.IGNORECASE)


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
    so resolution works off this text and only name-splits for a persona handoff. "Raw" here means
    *before the name-split*, not *un-normalised*: the Markdown emphasis strip is applied here, once,
    so the ``pr-review`` path and the persona path cannot disagree about the token (msg-1074 §4-1
    requires the normalisation to reach the ``pr-review`` route too — otherwise a wrapped ref is
    handed to GitHub as ``owner/repo#7**``).
    """
    matches = _NEXT_LINE_RE.findall(body)
    if not matches:
        return None
    return _strip_md_emphasis(str(matches[-1])) or None


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
        # NEXT: pr-review <owner/repo#n> — the ref is the first non-whitespace token, with trailing
        # punctuation stripped (as persona names are) so a natural ``...#7.`` / ``...#7,`` does not
        # reach GitHub as an invalid ref (Tier B PR #103 round 3). The conductor validates it via
        # parse_pr_ref and fires the synchronous Tier B review (PR-2b-2).
        return Handoff(HandoffKind.PR_REVIEW, token=pr_review.group("ref").strip(_TRAILING_PUNCT))
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
