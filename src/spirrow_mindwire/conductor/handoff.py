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
- **the token** — :data:`_PARTICIPANT_NAME_RE` matches the shape of a participant name, so whatever
  the author put *after* the name — ``**``, ``,``, ``。``, ``— a gloss`` — is outside the match and
  therefore falls away for free.
- **the PR ref** — *not matched here at all*. What counts as a PR reference is owned by
  :func:`~spirrow_mindwire.github.client.parse_pr_ref`, and this module asks it rather than
  re-spelling its grammar. The revision before this one did spell it out a second time, with a
  comment claiming the two "agree by construction"; they already disagreed —
  ``acme/widgets#7abc`` yielded ``acme/widgets#7`` here and ``None`` there (msg-1158 §5). A second
  spelling also silently withholds whatever the owner learns later (an enterprise host, a new
  short-link shape), which is the concrete cost: the gate would keep firing on the owner's *old*
  vocabulary. So the ``pr-review`` route asks the owner and records the owner's answer.

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
from dataclasses import dataclass, replace
from enum import StrEnum

from ..github.client import parse_pr_ref
from ..tier_c_admission_gate import (
    ADMIT_LABELS,
    LEGACY_LABEL_MAP,
    RELEASE_CROSS_REPO_LABEL,
    UNSURE_LABEL,
    require_admitted,
)
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

# The same carve-out, stated for the other end of a word. ``\b`` is defined by ``\w``, and ``\w``
# counts ``_`` as a word character — the exact character ``_DECORATION`` just moved to the
# decoration side. So ``\b`` and ``_DECORATION`` disagree about ``_``, and any token this module
# terminates with ``\b`` re-opens the hole the line rule closed: ``NEXT: _pr-review_ <ref>`` found
# no boundary between ``pr-review`` and ``_``, failed to match the sentinel, and fell out as ABSENT
# while ``*pr-review*`` and ``` `pr-review` ``` at the same position routed (msg-1163 §1 / §3).
# One fact — ``_`` is ``\w`` — had by then opened the same hole in three separate places, so the
# boundary is written once, here, with the carve-out already applied: "not followed by an
# alphanumeric". Unicode-aware like ``\w``, so ``pr-reviewing`` and ``pr-reviewあ`` stay refused.
_NOT_WORD_CONTINUATION = r"(?![^\W_])"

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
# ``NEXT: operator`` (T-next-role-name-stands-down-to-human D5 / D4'''): a human has to do work by
# hand, and the author has checked that the work is none of the Tier-C types. It parks exactly like
# ``human`` (``HandoffKind.HUMAN``) but carries :attr:`HumanAsk.OPERATOR_WORK`, so the board can
# show it as operator work rather than as a decision. Not a :class:`~..value_objects.Role`: the
# conductor never spawns an operator, and the role registry's role set is left unchanged (D5).
OPERATOR_TOKEN = "operator"
# D4'-a: the operator form's own keyword, held once. The protocol text, the parser and the
# stand-down notice are all built from it (and from ``TIER_C_CHECK_KEYWORD`` / ``_NONE`` below),
# so what the prompt teaches and what the parser accepts cannot drift apart.
OPERATOR_TASK_KEYWORD = "OPERATOR-TASK"

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
# Decoration is admitted on BOTH sides of the sentinel word, not just before it: an author who
# italicises the sentinel alone (``NEXT: _pr-review_ <ref>``) is asking for the gate exactly as
# much as one who italicises the whole line, and the emphasis close lands between the word and its
# operand. The terminator is ``_NOT_WORD_CONTINUATION`` rather than ``\b`` for that reason; the
# closing marker is then just leading decoration on the operand, which the payload rule below eats.
_PR_REVIEW_RE = re.compile(
    rf"{_DECORATION}{PR_REVIEW_TOKEN}{_NOT_WORD_CONTINUATION}(?P<rest>.*)", re.IGNORECASE
)
# The operand's PAYLOAD: the operand with this module's own decoration removed from either end.
# This is deliberately not a statement about PR refs — it is the same closed ``_DECORATION``
# property the line rule uses, applied to the one place the ref route needs it.
#
# What it is for is now only the LEADING end. A ref's trailing wrappers are the owner's business
# and it handles them; a ref's *opening* wrapper is not, because ``_`` is a legal character in the
# middle of a repository name and so cannot be skipped by a pattern that has already started
# matching one. Measured against ``parse_pr_ref`` on this head:
#
#     acme/widgets#7**   -> acme/widgets#7      acme/widgets#7.    -> acme/widgets#7
#     acme/widgets#7_    -> acme/widgets#7      _acme/widgets#7    -> None
#
# So ``NEXT: pr-review _acme/widgets#7_`` needs the leading ``_`` gone before the owner is asked;
# with it gone, the owner answers. Trimming cannot reach into a ref: an accepted ref ends in a
# digit and begins with an alphanumeric, and neither is decoration.
_OPERAND_PAYLOAD_RE = re.compile(rf"\A{_DECORATION}(?P<payload>.*?){_DECORATION}\Z")


class HandoffKind(StrEnum):
    """What a parsed ``NEXT:`` directive resolves to."""

    ROLE = "role"  # a roster participant → dispatch that role's adapter
    HUMAN = "human"  # NEXT: human — a Tier-C decision point; the conductor stops
    NONE = "none"  # NEXT: none — the thread is settled; the conductor stops
    PR_REVIEW = "pr_review"  # NEXT: pr-review <owner/repo#n> — fire the Tier B PR-gate (PR-2b-2)
    ABSENT = "absent"  # no parseable NEXT (missing / unknown participant) → human fallback (Obj3)


class HumanAsk(StrEnum):
    """What a ``HandoffKind.HUMAN`` asks the human for, when it is not a decision.

    ``None`` on :attr:`Handoff.human_ask` is the default meaning of a human handoff (a Tier-C
    decision). ``OPERATOR_WORK`` is a validated ``NEXT: operator``: work by hand that the author
    has declared to be outside every Tier-C type (D5 / D4''').
    """

    OPERATOR_WORK = "operator_work"


class OperatorFault(StrEnum):
    """Why a ``NEXT: operator`` was refused (D6''''). Values are the stand-down reason strings.

    Checked in this order, and the first that applies wins, so a contradictory message is always
    reported as the contradiction rather than as a missing line:

    1. ``TIER_C_CONFLICT`` — the message also declares a Tier-C (a ``TIER-C: <type>`` line
       anywhere, :func:`declares_tier_c`).
    2. ``NO_TASK`` — no ``OPERATOR-TASK: <work>`` line two lines above the final ``NEXT:``.
    3. ``NO_TIER_C_CHECK`` — the line directly above the final ``NEXT:`` is not
       ``TIER-C-CHECK: none`` (the same check as guard (i)'s G2, :func:`declares_no_tier_c`).
    """

    TIER_C_CONFLICT = "operator_tier_c_conflict"
    NO_TASK = "identity_operator_no_task"
    NO_TIER_C_CHECK = "operator_no_tier_c_check"


class MismatchReason(StrEnum):
    """Why a Layer-3 field/body reconciliation escalated to ``HandoffKind.HUMAN``.

    This is the split of the overloaded ``field_mismatch`` bool that the merged #184 shipped
    (naysayer msg-1788 "Weakest point" / T-reconcile-field-mismatch-flag-overloaded). Both cases
    make the same **routing** decision — escalate to the human — but their **causes** and
    **remedies** are different, so a programmatic consumer (dashboard / alert / metric) that
    reads only this field cannot conflate them into one number:

    - ``TARGET_DIVERGENCE`` — Layer-3 §3-1 row 5. The field AND the body both resolved to a
      valid target, but the two targets differ. This is a genuine **write-side drift**: two
      places in the same message named different next actors, and the one measuring it wants
      to see this counted (dashboard = "how often does the writer disagree with itself?").
    - ``FIELD_UNRESOLVABLE`` — the field itself resolved to :attr:`HandoffKind.ABSENT`
      (unknown persona, garbage like ``Schrodinger``, empty-after-trim junk that survived
      write-side validation). The remedy is on the **write side**, not the writer's choice.
      A dashboard that mixes this into "target_divergence" is measuring a bug in a different
      component; the write-side validation gap (``NextParticipantUnknownError`` should have
      caught it) is what needs fixing.

    A programmatic consumer reads :attr:`Handoff.mismatch_reason` directly. The WARNING log
    also carries this as ``reason=<name>`` so a human reader gets the same distinction from
    a single log line without having to re-derive it from the raw tokens (which would mean
    duplicating the resolver on the reader side and drifting from this module the moment
    it changes).
    """

    TARGET_DIVERGENCE = "target_divergence"  # both sides resolved, they named different targets
    FIELD_UNRESOLVABLE = "field_unresolvable"  # field value did not resolve to any known target


# ---- C (T-human-terminal-overuse, human GO msg after Einstein ACCEPT msg-891) ---------------- #
# TIER-C: <label> — a **non-blocking measurement tag** the author may place on the line
# immediately above their `NEXT: human`, so the conductor can record what CLASS of Tier-C decision
# each explicit human terminal claims to be about. v1 is a calibrator, not a definition: the
# labels below are the CATEGORIES OBSERVED SO FAR in the 11-turn sample msg-882 counted (a
# release, a merge, a scope grow), rounded up by Bohr's msg-890 §3 enumeration. They will change
# as the sample grows and MUST NOT be read as an authoritative Tier-C ontology.
#
# The measurement is enabled by MEASURING BOTH SIDES: presence is recorded, absence is recorded,
# neither is redirected or rejected. If we blocked on missing labels we would lose the very
# denominator A2's threshold (Bohr msg-890 §2 pre-registration: >20% ∧ ≥3 in a 14-day / 20-turn
# window) needs to fire on — the calibrator would be destroying its own calibration.
#
# `other:<one-line reason>` is a VISIBLE GAP, not an escape hatch. If it dominates the observed
# distribution that is the readable signal that the closed enum below is wrong. Refusing `other:`
# would force silent mis-classification into whichever closed label was closest, and the
# observation would be corrupted at exactly the moment the enum's insufficiency became evident.
#
# Placement rule: `handoff.py`, NOT `obligations.yaml` (human msg §1: "両方 `handoff.py`"). This
# tag is parsed by the conductor's routing, so it lives with the routing — same defence Bohr made
# in msg-890 §1 for placing the revised proposer guidance here rather than in obligations. Adding
# a net-new entry to obligations.yaml is explicitly out of scope for this change.
#
# T-tier-c-admission-gate U2 (Bohr msg-4768 / msg-4776): the enum is no longer defined here. It
# was a second, stale copy of the closed set the admission gate owns (msg-3630 §2.1 — goal /
# cost / irreversible / merge-protected), and the emission guidance below was teaching authors
# the old set while the gate judged them by the new one. :data:`TIER_C_LABELS` is now DERIVED
# from :data:`~spirrow_mindwire.tier_c_admission_gate.ADMIT_LABELS` (sorted, so the prompt text
# is stable). The prose that DEFINES each label, and the stated count, are rendered from
# ``_TIER_C_LABEL_DEFINITIONS`` (below, next to the prompt), whose key set is checked against
# ``ADMIT_LABELS`` at import: a label added to or removed from the gate without a matching
# definition fails the import of this module rather than leaving the prompt teaching the old set.
# (PR #365 review: the first cut hardcoded the four names and the word "four" in the prose.)
#
# The PARSER still accepts the legacy labels (``scope`` / ``billing`` / ``release-cross-repo``)
# and the gate's unsure label. That is measurement, not admission: a legacy label is exactly the
# residual usage the 14-day audit wants to count, and dropping it from the parse would record it
# as ABSENT — the silent mis-classification this block's opening paragraph forbids. Only
# :data:`TIER_C_LABELS` is ever taught to an author.
TIER_C_LABELS: tuple[str, ...] = tuple(sorted(ADMIT_LABELS))
_TIER_C_PARSE_LABELS: tuple[str, ...] = (
    *TIER_C_LABELS,
    *sorted(LEGACY_LABEL_MAP),
    RELEASE_CROSS_REPO_LABEL,
    UNSURE_LABEL,
)
# `other:<reason>` is admitted separately (its reason text is free-form). Enum alternatives are
# joined into a single non-capturing alternation; case is folded on match. Whitespace between
# `other:` and the reason is optional (`other:Foo` and `other: Foo` normalise to the same tag),
# but the reason itself is required — an empty `other:` would defeat the point of naming a novel
# type, so it does not match at all and is recorded as absent.
#
# Whitespace between the ``TIER-C:`` keyword and the label is ALSO optional (`\s*`, not `\s+`):
# `TIER-C:scope` and `TIER-C: scope` normalise to the same tag. Requiring a space here would
# silently misclassify the unspaced form as ABSENT and inflate the "missing label" baseline —
# and because this whole parser is non-blocking by design, the failure would be silent. That is
# the exact shape of corruption the calibration must not admit (Gemini PR-gate critique on #173:
# "silently misclassifying `TIER-C:scope` as an absent tag corrupts the observation data").
# The colon is still required (`TIER-Cscope` does not match) — only the whitespace after it is
# relaxed.
_TIER_C_LABEL_RE = re.compile(
    r"\A\s*TIER-C:\s*"
    r"(?P<label>"
    + "|".join(re.escape(lbl) for lbl in _TIER_C_PARSE_LABELS)
    + r"|other:\s*\S[^\r\n]*?)"
    r"\s*\Z",
    re.IGNORECASE,
)


# ---- STOP: disposition line (T-next-line-carries-who-not-why, Slice 1 — Bohr msg-4718 §1) ---- #
# `NEXT:` says WHO acts next; it cannot say WHY nobody does. `NEXT: none` today collapses four
# situations (msg-2014 §1: done / awaiting a decision / waiting on a condition / forgot to
# nominate). The design keeps the `NEXT:` grammar untouched (D-1) and carries the "why" on a
# sibling line immediately above the final `NEXT: none`:
#
#     STOP: done
#     STOP: blocked-on <thread:|pr:|deploy:|queue-empty:><operand> wake:<agent>
#
# Slice 1 is a DARK LAUNCH (msg-4718 §1): this module PARSES the line into a typed value and the
# conductor LOGS it on the measurement-only path TIER-C already uses. Nothing is rejected, nothing
# is forwarded to magickit, routing is unchanged, and the emission prompt
# (`_HANDOFF_PROTOCOL_CORE`) deliberately does NOT teach the form — a prompt that promises
# "write this and the thread resolves / parks" must ship in the same unit as the mechanism that
# keeps that promise (Einstein msg-4717 BLOCKING, accepted in msg-4718 §0/§2).
#
# The form is FIXED and read strictly (msg-4718 §1-1: "崩れた行は寛容に読まず malformed"). The
# `STOP:` keyword itself is detected loosely (case, leading decoration, whitespace after the
# colon) on purpose: a loosely-written STOP line must land in MALFORMED, where it is visible, and
# never fall through to ABSENT, where it would silently inflate the unclassified baseline — the
# same corruption TIER-C's relaxed keyword guards against. Everything AFTER the keyword must match
# the fixed form exactly.
#
# `human` as a trigger or a wake is MALFORMED (B-5, msg-4716 §2): an agent's only route to stop
# on a human is `NEXT: human`, which is the Decider's entry. `parked(human, human)` is reserved
# for the mechanism's own D-8 ③ fallback and is never read off an author's line.
STOP_TRIGGER_ARMS: tuple[str, ...] = ("thread", "pr", "deploy", "queue-empty")

# Decoration or whitespace BETWEEN the keyword and the colon (`**STOP**: done`, `STOP : done`) is
# also detected (PR-gate #363 finding 1): without it those lines fell through to ABSENT. Detection
# only — a non-empty `gap` makes the line MALFORMED, it is never accepted.
_STOP_KEYWORD_RE = re.compile(
    r"\A[\s*_`>]*(?P<keyword>STOP)(?P<gap>[\s*_`>]*):\s*(?P<rest>.*?)\s*\Z", re.IGNORECASE
)
_STOP_BLOCKED_ON_RE = re.compile(
    r"\Ablocked-on (?P<arm>"
    + "|".join(re.escape(arm) for arm in STOP_TRIGGER_ARMS)
    + r"):(?P<operand>\S+) wake:(?P<wake>\S+)\Z"
)


class StopStatus(StrEnum):
    """What the line above a final ``NEXT: none`` says about why the thread stops.

    ``ABSENT`` is the measurement Slice 1 exists for (msg-4718 §1-2(b)): a ``NEXT: none`` with no
    ``STOP:`` line is the pre-cutover ``unclassified`` denominator. ``DONE`` / ``BLOCKED_ON`` are
    the two accepted forms; together they are "present". ``MALFORMED`` is a line that announced
    itself as ``STOP:`` but did not match the fixed form (including any ``human`` trigger/wake).
    """

    ABSENT = "absent"
    DONE = "done"
    BLOCKED_ON = "blocked_on"
    MALFORMED = "malformed"

    @property
    def presence(self) -> str:
        """The 3-valued presence the log records: ``present`` / ``absent`` / ``malformed``."""
        if self is StopStatus.ABSENT:
            return "absent"
        if self is StopStatus.MALFORMED:
            return "malformed"
        return "present"


@dataclass(frozen=True)
class StopLine:
    """The typed ``STOP:`` disposition parsed off the line above a final ``NEXT: none``.

    ``trigger_arm`` / ``trigger_operand`` / ``wake`` are set only for ``BLOCKED_ON``; ``wake`` is
    the roster's canonical identity. ``raw`` is the stripped line for every status except
    ``ABSENT`` (so a MALFORMED line can be read back from the log). Slice 1's only consumer is the
    measurement log; the typed value exists so the later slice's forwarding has one parser.
    """

    status: StopStatus
    trigger_arm: str | None = None
    trigger_operand: str | None = None
    wake: str | None = None
    raw: str | None = None


@dataclass(frozen=True)
class Handoff:
    """The resolved handoff target of a message's final ``NEXT:`` line.

    ``identity`` / ``role`` are set only when ``kind is HandoffKind.ROLE`` (``identity`` is the
    roster's canonical persona name). ``token`` is the participant name parsed (observability),
    ``None`` when no ``NEXT:`` line was found at all.

    On the ``PR_REVIEW`` route ``token`` is the **canonical slug** (``owner/repo#n``) whenever
    :func:`~spirrow_mindwire.github.client.parse_pr_ref` recognised one — including for a PR URL,
    which it normalises. It is *not* the substring the author typed: this module no longer knows
    the shape of a ref, so it cannot report where one started and ended, only what the owner of
    that grammar made of it. When the owner recognised nothing, ``token`` is the raw operand, and
    the conductor's re-validation (``core.py``, same function) fails safe to the human.

    ``tier_c_label`` is the **calibration tag** parsed off the line immediately above the final
    ``NEXT: human`` (C, msg-890 §3): the enum value from :data:`TIER_C_LABELS`, or a raw
    ``other:<reason>`` string, or ``None`` when no such line was written OR the handoff was not
    ``HUMAN``. It is NON-BLOCKING — the conductor records its presence/absence and the turn
    otherwise routes exactly as it would without the tag. This field is a measurement label; it
    is NOT the definition of what Tier-C IS (that stays owned by ADR/Tier-C decides).

    ``mismatch_reason`` records **why** a Layer-3 field/body reconciliation escalated to
    :attr:`HandoffKind.HUMAN` (Bohr msg-179 §3-1 row 5, and the ABSENT-field fail-safe).
    ``None`` on every non-escalated path; otherwise a :class:`MismatchReason` value. The
    two reasons — target divergence vs. an unresolvable field — make the same routing
    decision (escalate that turn to the human, safety valve) but come from different
    causes and take different remedies, so a programmatic consumer reads them apart from
    this field rather than re-deriving the distinction from the raw tokens
    (T-reconcile-field-mismatch-flag-overloaded: PR #184's ``field_mismatch`` bool
    conflated them, forcing any reader to duplicate the resolver to tell them apart).

    ``mismatch_body_token`` carries the body's parsed target (the token the fallback would
    have gone to) so the mismatch event names what the two sides said. It is ``None``
    outside a mismatch escalation. Its cross-field consistency with ``mismatch_reason`` /
    ``token`` is intentionally NOT enforced by the type: the invariant is inherited from
    #184 and left as-is here (the scope of this change is the reason-code split, not the
    surrounding token invariants — see PR body §「非目標」).

    ``author_requested_human`` is ``True`` iff the **author themself** named the human: the
    body's final ``NEXT: human`` or a ``human`` ``next_participant`` field (including a field
    ``human`` the body agrees with or is silent on). It is set in exactly those two resolver
    branches and nowhere else. It exists so a consumer asking "did the author ask for the
    human?" reads a positive fact instead of deriving it from ``mismatch_reason is None`` —
    that negation was correct only while every other ``HUMAN`` producer was a mismatch, and
    would silently turn every future non-mismatch escalation into an author request
    (T-reconcile-field-mismatch-flag-overloaded msg-4861 / msg-4864 U1).

    Because it is a second field describing the cause of a ``HUMAN`` handoff, the combination
    is checked at construction (msg-4864 U1, "不正な組は構築時に落とす"): ``True`` requires
    ``kind is HUMAN`` and ``mismatch_reason is None``, and :meth:`__post_init__` raises
    :class:`ValueError` otherwise. The check is a local guardrail — every ``Handoff`` is built
    in this module — not a substitute for a single sum type (the naysayer reply to msg-4864
    accepted this trade-off over widening :class:`MismatchReason`).

    ``via_role_alias`` is ``True`` when a ``ROLE`` handoff was reached through a role name rather
    than an identity name (D1 / D3): ``NEXT: implementer`` with exactly one implementer on the
    roster. ``role_alias_unresolved`` is ``True`` on an ``ABSENT`` whose token IS a role name but
    whose role has zero or several holders on the roster (``IDENTITY_ROLE_AMBIGUOUS``), so it can
    be told apart from a typo.

    ``human_ask`` / ``operator_task`` are set on a valid ``NEXT: operator`` (kind ``HUMAN``,
    :attr:`HumanAsk.OPERATOR_WORK`, and the text of the ``OPERATOR-TASK:`` line).
    ``operator_fault`` is set, with kind ``ABSENT``, when a ``NEXT: operator`` failed the D6''''
    checks; the conductor stands down on it.

    ``stop_line`` is the ``STOP:`` disposition (T-next-line-carries-who-not-why Slice 1): set to a
    :class:`StopLine` exactly when ``kind is HandoffKind.NONE`` — including ``StopStatus.ABSENT``
    when no such line was written — and ``None`` on every other kind (not measured there). Like
    ``tier_c_label`` it is measurement only: nothing routes differently on it.
    """

    kind: HandoffKind
    identity: str | None = None
    role: Role | None = None
    token: str | None = None
    tier_c_label: str | None = None
    mismatch_reason: MismatchReason | None = None
    mismatch_body_token: str | None = None
    stop_line: StopLine | None = None
    author_requested_human: bool = False
    via_role_alias: bool = False
    role_alias_unresolved: bool = False
    human_ask: HumanAsk | None = None
    operator_task: str | None = None
    operator_fault: OperatorFault | None = None

    def __post_init__(self) -> None:
        if not self.author_requested_human:
            return
        if self.kind is not HandoffKind.HUMAN:
            raise ValueError(
                f"author_requested_human=True requires kind=HUMAN, got kind={self.kind.value}"
            )
        if self.mismatch_reason is not None:
            raise ValueError(
                "author_requested_human=True is incompatible with a conductor escalation "
                f"(mismatch_reason={self.mismatch_reason.value})"
            )


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


def _line_above_last_next(body: str) -> str | None:
    """The line immediately above the **last** ``NEXT:`` line, or ``None`` if there is none.

    Shared by the two annotation readers (``TIER-C:`` and ``STOP:``) so both look at exactly the
    same line: n-1 where n is the final ``NEXT:``, no blank-line skipping, no wider window.
    """
    matches = list(_NEXT_LINE_RE.finditer(body))
    if not matches:
        return None
    last_next_start = matches[-1].start()
    if last_next_start == 0:
        return None
    # The ``^`` of the last NEXT: line sits at ``last_next_start``; the previous line's ``\n``
    # is at ``last_next_start - 1`` (if the file starts at 0, MULTILINE's ``^`` also matches
    # position 0, which we already excluded above).
    prev_line_end = last_next_start - 1  # exclusive of the delimiting \n
    prev_line_start = body.rfind("\n", 0, prev_line_end) + 1  # rfind returns -1 → 0
    return body[prev_line_start:prev_line_end]


def _stop_line_above_last_next(body: str, roster: Mapping[str, Role]) -> StopLine:
    """Parse the ``STOP:`` disposition on the line above the last ``NEXT:`` (Slice 1, msg-4718 §1).

    Never raises and never returns ``None``: the caller only asks on a ``NEXT: none`` terminal,
    where "no STOP line" is itself the measurement (``StopStatus.ABSENT``). ``wake`` must resolve
    on the roster (a registered agent of this thread); ``human`` / ``none`` / an unknown name is
    ``MALFORMED``. There is no ``human`` trigger arm, so ``blocked-on human`` is ``MALFORMED`` too.
    """
    line = _line_above_last_next(body)
    if line is None:
        return StopLine(StopStatus.ABSENT)
    keyword = _STOP_KEYWORD_RE.match(line)
    if keyword is None:
        return StopLine(StopStatus.ABSENT)
    rest = keyword.group("rest")
    raw = line.strip()
    if keyword.group("keyword") != "STOP" or keyword.group("gap"):
        # Detected (so not ABSENT) but not the fixed spelling: `stop: done`, `**STOP**: done` and
        # `STOP : done` are MALFORMED.
        return StopLine(StopStatus.MALFORMED, raw=raw)
    if rest == "done":
        return StopLine(StopStatus.DONE, raw=raw)
    blocked = _STOP_BLOCKED_ON_RE.match(rest)
    if blocked is None:
        return StopLine(StopStatus.MALFORMED, raw=raw)
    wake = blocked.group("wake")
    if wake.casefold() in (HUMAN_TOKEN, NONE_TOKEN):
        return StopLine(StopStatus.MALFORMED, raw=raw)
    resolved = _roster_lookup(roster, wake)
    if resolved is None:
        return StopLine(StopStatus.MALFORMED, raw=raw)
    return StopLine(
        StopStatus.BLOCKED_ON,
        trigger_arm=blocked.group("arm"),
        trigger_operand=blocked.group("operand"),
        wake=resolved[0],
        raw=raw,
    )


def _tier_c_label_above_last_next(body: str) -> str | None:
    """``TIER-C: <label>`` on the line above the **last** ``NEXT:`` line, or ``None``.

    C (msg-890 §3 / Einstein msg-891 §4): the look-back is limited to line ``n-1`` where line ``n``
    is the final ``NEXT:``. This is deliberately narrow — a broader window (skipping blank lines,
    scanning the whole body) would false-positive on quoted TIER-C: text elsewhere in the reply,
    and the point of this measurement is that BOTH sides (presence AND absence) are recorded
    accurately.

    The label is casefolded to canonical form (``"scope"`` regardless of ``"SCOPE:"`` / ``"Scope:"``
    on the wire) so aggregation is stable. For ``other:<reason>`` the reason text keeps its case,
    but leading whitespace on the reason is trimmed.

    Non-blocking: this function returns ``None`` on any of {no last ``NEXT:``, no preceding line,
    the preceding line does not match the closed enum}. None of those cases changes what
    :func:`resolve_handoff` reports for :attr:`Handoff.kind` — the tag is additive observability
    only.
    """
    prev_line = _line_above_last_next(body)
    if prev_line is None:
        return None
    match = _TIER_C_LABEL_RE.match(prev_line)
    if match is None:
        return None
    label = match.group("label")
    # `other:<reason>` — preserve reason case, trim inner leading whitespace after the colon.
    lowered = label.lower()
    if lowered.startswith("other:"):
        return "other:" + label[len("other:") :].lstrip()
    return lowered


# ---- D-4' guardrails (T-pr-2b-3-human-identity-delegate, Bohr msg-4856 / msg-4858) ---- #
# Unlike the calibration reader above, these two ARE routing inputs: carve-out ③
# (:func:`spirrow_mindwire.routing.carve_out_iii_admissible`) reads them. The
# measurement path (``Handoff.tier_c_label``) keeps its non-blocking contract; these
# are separate functions so neither side's semantics can leak into the other.
TIER_C_CHECK_KEYWORD = "TIER-C-CHECK"
"""The naysayer's G2 self-declaration keyword (``TIER-C-CHECK: none``)."""
TIER_C_CHECK_NONE = "none"
"""The only value that opens carve-out ③ (G2). Anything else — including a Tier-C label — keeps
it closed."""

_TIER_C_CHECK_RE = re.compile(r"\A\s*TIER-C-CHECK:\s*(?P<value>\S.*?)\s*\Z", re.IGNORECASE)


def declares_tier_c(body: str) -> bool:
    """G1: does ANY line of ``body`` declare a Tier-C (``TIER-C: <label>``, ``other:`` included)?

    Same line grammar as the calibration reader (:data:`_TIER_C_LABEL_RE`, anchored to the whole
    line) but over every line, not only the one above ``NEXT:`` — a declaration anywhere in the
    segment must latch the gate (msg-4858 §2). Prose that merely mentions ``TIER-C: scope``
    mid-sentence does not match; a line-quoted declaration does, which closes the gate: the
    fail-closed direction, undone by one human turn (msg-4858 §3).
    """
    return any(_TIER_C_LABEL_RE.match(line) for line in body.splitlines())


def declares_no_tier_c(body: str) -> bool:
    """G2: is the line directly above the final ``NEXT:`` exactly ``TIER-C-CHECK: none``?

    Strict on purpose — this is the side that OPENS the gate, so it must not be satisfied by a
    quote elsewhere in the body. Keyword case-insensitive, value ``none`` case-insensitive,
    surrounding whitespace ignored; anything else (missing, other value, decorated) is ``False``.
    """
    prev_line = _line_above_last_next(body)
    if prev_line is None:
        return False
    match = _TIER_C_CHECK_RE.match(prev_line)
    return match is not None and match.group("value").casefold() == TIER_C_CHECK_NONE


_OPERATOR_TASK_RE = re.compile(
    r"\A\s*" + re.escape(OPERATOR_TASK_KEYWORD) + r":\s*(?P<task>\S.*?)\s*\Z", re.IGNORECASE
)


def _operator_task_line(body: str) -> str | None:
    """The work on ``OPERATOR-TASK: <work>`` two lines above the final ``NEXT:``, or ``None``.

    The position is the template's (D4''' item 4): the task, then ``TIER-C-CHECK: none``, then
    ``NEXT: operator``. A task line quoted anywhere else in the body does not count, for the same
    reason the ``TIER-C:`` / ``STOP:`` readers look at one fixed line.
    """
    matches = list(_NEXT_LINE_RE.finditer(body))
    if not matches:
        return None
    # The last element is the empty tail after the newline that ends the line above ``NEXT:``.
    lines = body[: matches[-1].start()].split("\n")
    if len(lines) < 3:
        return None
    match = _OPERATOR_TASK_RE.match(lines[-3])
    return match.group("task") if match is not None else None


def _check_operator(body: str, handoff: Handoff) -> Handoff:
    """D6'''': accept a ``NEXT: operator`` only in its 3-line form; otherwise stand down.

    The order is fixed (msg-5428): a Tier-C declaration anywhere in the message is checked first,
    so a contradictory message is reported as the contradiction, never as a missing line. There is
    no ``decided <msg-ref>`` form (msg-5425 / msg-5426): an agent cannot claim that a human already
    approved Tier-C work. Tier-C work goes to ``NEXT: human``.
    """
    task = _operator_task_line(body)
    fault: OperatorFault | None = None
    if declares_tier_c(body):
        fault = OperatorFault.TIER_C_CONFLICT
    elif task is None:
        fault = OperatorFault.NO_TASK
    elif not declares_no_tier_c(body):
        fault = OperatorFault.NO_TIER_C_CHECK
    if fault is not None:
        return Handoff(HandoffKind.ABSENT, token=handoff.token, operator_fault=fault)
    return replace(handoff, operator_task=task)


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


def resolve_handoff(
    body: str,
    roster: Mapping[str, Role],
    *,
    next_participant: str | None = None,
) -> Handoff:
    """Parse + resolve the latest ``NEXT:`` directive against the identity→role ``roster``.

    Resolution order: the ``pr-review <ref>`` PR-gate sentinel (PR-2b-2) first, then the reserved
    ``human`` / ``none`` / ``operator`` sentinels, then the roster (case-insensitive on the
    identity name), then a role name held by exactly one roster identity (D1). A ``NEXT:
    operator`` is then checked against its 3-line form (:func:`_check_operator`). A
    missing ``NEXT:`` line, the empty token, or a non-participant name all resolve to
    :attr:`HandoffKind.ABSENT` — the conductor treats every ABSENT as "route to human" (Obj3 / D-4)
    so a malformed handoff flags a human rather than silently stranding the thread.

    **Layer 3 — the structured ``next_participant`` envelope field (Bohr msg-179 §3):** when the
    caller passes a non-empty ``next_participant``, the field is the source of truth for routing
    and the body's ``NEXT:`` line is used only as a **lint** against it — same resolver, no second
    parser (§3-2). The 5 quadrants of ``(field, body)`` and their outcomes:

    ==============  ==============  ==============================  =========================
    field           body            routing                         side-effect
    ==============  ==============  ==============================  =========================
    None            no NEXT         ABSENT (fallback)                —
    None            NEXT present    follow body (fallback)           —
    present         no NEXT         follow field                     — (**quiet normal path**)
    present         NEXT agrees     follow field                     —
    present         NEXT disagrees  escalate: kind=HUMAN             ``TARGET_DIVERGENCE``
    ==============  ==============  ==============================  =========================

    Row 3 is msg-1438's silent quadrant: a judgement-page decide carries the field but no ``NEXT:``
    in the body, and the fallback resolver returns ABSENT. Without this wiring the consumer sees
    the field-null-and-body-absent case and stops on ``NO_HANDOFF`` even though the field named a
    target — the exact silent 2-day stall this module now closes.

    A field value that itself fails to resolve (unknown persona, empty after trim, non-token
    junk) is treated as an escalation too: the return is ``kind=HUMAN`` with
    ``mismatch_reason=FIELD_UNRESOLVABLE`` so the divergence is loud rather than a silent drop.
    The write side (`chatroom_post_message`) is responsible for rejecting such fields with
    ``NextParticipantUnknownError`` — this read-side treatment is the fail-safe for the case a
    bad field slipped past validation (§3-3's escape hatch is "drop the field and re-send", which
    a mismatch-to-human turn asks the human to do). The two reason codes share the routing
    verdict but not the cause: a programmatic consumer reads :attr:`Handoff.mismatch_reason` to
    tell them apart without duplicating this resolver.
    """
    body_handoff = _resolve_body(body, roster)
    if body_handoff.human_ask is HumanAsk.OPERATOR_WORK:
        # PR #402 gate (82ec032): a malformed body ``NEXT: operator`` stands down whatever the
        # field says. head_skip reads only the body, so it LAUNCHes every malformed operator head
        # on the promise that the conductor posts a notice that moves the head. If a diverging
        # field turned this into a quiet TARGET_DIVERGENCE park instead, the head would never
        # move and every tick would relaunch the same head. A well-formed body still reconciles
        # against the field as before (head_skip SKIPs it, so a quiet park is safe there).
        checked = _check_operator(body, body_handoff)
        if checked.operator_fault is not None:
            return checked
    field_value = next_participant.strip() if next_participant is not None else ""
    if not field_value:
        resolved = body_handoff
    else:
        resolved = _reconcile(_resolve_field(field_value, roster), body_handoff)
    if resolved.human_ask is HumanAsk.OPERATOR_WORK:
        resolved = _check_operator(body, resolved)
    if resolved.kind is HandoffKind.NONE:
        # STOP: disposition (Slice 1, measurement only). Attached AFTER reconciliation so a
        # field-driven NONE whose body also says `NEXT: none` keeps the body's STOP line instead
        # of being miscounted as ABSENT. A field NONE with no body NEXT: has no line above any
        # NEXT:, which reads as ABSENT — correct: nobody wrote a STOP line.
        resolved = replace(resolved, stop_line=_stop_line_above_last_next(body, roster))
    return resolved


def _resolve_body(body: str, roster: Mapping[str, Role]) -> Handoff:
    """The body-only resolution — the pre-Layer-3 path, factored out for reuse by the lint."""
    raw = _last_next_raw(body)
    if raw is None:
        return Handoff(HandoffKind.ABSENT)
    sentinel = _PR_REVIEW_RE.match(raw)
    if sentinel is not None and (operand := sentinel.group("rest").strip()):
        # NEXT: pr-review <owner/repo#n>. This module says where the operand starts and where its
        # decoration ends; it does not say what a PR ref is. ``parse_pr_ref`` owns that, is
        # documented to extract one from *free text*, and its answer is recorded verbatim (the
        # canonical slug), so a ref shape only the owner knows arrives here without an edit.
        # An operand the owner does not recognise is still the sentinel (the author asked for a
        # gate) and is carried forward raw; the conductor re-validates with the same function and
        # fails safe to the human rather than firing (Tier B PR #103 round 4).
        payload = _OPERAND_PAYLOAD_RE.match(operand)
        ref = parse_pr_ref(payload.group("payload") if payload is not None else operand)
        return Handoff(HandoffKind.PR_REVIEW, token=ref.slug if ref is not None else operand)
    token = _name_from_raw(raw)
    if token is None:
        return Handoff(HandoffKind.ABSENT, token=raw)
    folded = token.casefold()
    if folded == HUMAN_TOKEN:
        # C (msg-890 §3): look 1 line above the final NEXT: for a TIER-C: <label> and attach it
        # as a non-blocking measurement tag. Absence is also observable — Handoff.tier_c_label
        # simply stays None. This is intentionally parsed ONLY on the HUMAN terminal: the ROLE /
        # PR_REVIEW / NONE / ABSENT paths do not carry a Tier-C claim in v1.
        return Handoff(
            HandoffKind.HUMAN,
            token=token,
            tier_c_label=_tier_c_label_above_last_next(body),
            author_requested_human=True,
        )
    if folded == NONE_TOKEN:
        return Handoff(HandoffKind.NONE, token=token)
    if folded == OPERATOR_TOKEN:
        # D5: parks like ``human``. Whether the message is a well-formed operator request is
        # decided once, after reconciliation, by :func:`_check_operator` (D6'''', msg-5428).
        return Handoff(HandoffKind.HUMAN, token=token, human_ask=HumanAsk.OPERATOR_WORK)
    return _resolve_participant(roster, token)


def _resolve_field(field_value: str, roster: Mapping[str, Role]) -> Handoff:
    """Resolve the raw ``next_participant`` field value.

    The field vocabulary is the SAME as the body's — sentinels + persona name — because §3-2
    forbids a second grammar for the lint (that is what makes the mismatch check well-defined:
    "would the fallback resolver have routed differently?"). Concretely, the field admits
    every shape the body's NEXT-token admits:

    - the reserved ``human`` / ``none`` sentinels (case-insensitive),
    - a persona name looked up on the roster (case-insensitive), and
    - the ``pr-review <owner/repo#n>`` PR-gate sentinel (PR-2b-2) — the same word plus operand
      the body path matches, resolved by the same :func:`~parse_pr_ref` owner of the ref grammar.

    The field just skips the ``NEXT:``-line scaffolding: the value IS the decision, so there is
    no keyword to strip off the front. Everything else — sentinel matching, roster lookup,
    ref parsing — is byte-identical to :func:`_resolve_body` so the two sides cannot disagree
    on what a "valid participant" is. The write side (`chatroom_post_message`) enforces the
    same vocabulary with :class:`NextParticipantUnknownError`; this read-side resolution is the
    fail-safe when a bad field slipped past that validation (§3-3's escape hatch is "drop the
    field and re-send", which the row-5 escalation asks the human to do).

    Missing the ``pr-review`` sentinel on this side would break the field-driven PR-gate
    (ADR-19 N-1): a field of ``pr-review acme/widgets#7`` would fall out of every branch
    below, resolve to ABSENT, be seen as a field/body mismatch by :func:`_reconcile`, and
    escalate to the human — silently disabling the synchronous Tier B review whenever the
    envelope field is the authoritative handoff. So the sentinel is matched here just as it
    is on the body path, using the same ``_PR_REVIEW_RE`` + ``parse_pr_ref`` pair.
    """
    sentinel = _PR_REVIEW_RE.match(field_value)
    if sentinel is not None and (operand := sentinel.group("rest").strip()):
        # Same trim + delegate as the body path: this module knows where the operand starts and
        # where its leading decoration ends; ``parse_pr_ref`` owns what a PR ref IS. An operand
        # the owner does not recognise is still the sentinel (the sender asked for a gate) and
        # is carried forward raw; the conductor's re-validation in ``core.py`` (same function)
        # then fails safe to the human rather than firing the gate on an unparseable ref.
        payload = _OPERAND_PAYLOAD_RE.match(operand)
        ref = parse_pr_ref(payload.group("payload") if payload is not None else operand)
        return Handoff(HandoffKind.PR_REVIEW, token=ref.slug if ref is not None else operand)
    token = _name_from_raw(field_value)
    if token is None:
        return Handoff(HandoffKind.ABSENT, token=field_value)
    folded = token.casefold()
    if folded == HUMAN_TOKEN:
        # No tier_c_label on the field route: the calibration tag is a body-only annotation
        # (msg-890 §3 reads it off the line above the NEXT:). A field-driven HUMAN records its
        # class through the mismatch event or through absence, not through a body scan.
        return Handoff(HandoffKind.HUMAN, token=token, author_requested_human=True)
    if folded == NONE_TOKEN:
        return Handoff(HandoffKind.NONE, token=token)
    if folded == OPERATOR_TOKEN:
        # Same token, same meaning as the body route; the 3-line form is checked against the
        # body after reconciliation. A field-only ``operator`` (body has no NEXT:) takes
        # :func:`_reconcile` row 3 (body ABSENT -> field wins), keeps ``OPERATOR_WORK``, and so
        # reaches :func:`_check_operator`, which stands it down with ``NO_TASK`` (no lines above
        # any NEXT:). Pinned by test_field_only_operator_stands_down_with_the_missing_task_reason.
        return Handoff(HandoffKind.HUMAN, token=token, human_ask=HumanAsk.OPERATOR_WORK)
    return _resolve_participant(roster, token)


def _reconcile(field_handoff: Handoff, body_handoff: Handoff) -> Handoff:
    """Merge a field-derived Handoff with the body-derived Handoff per §3-1 rows 3-5.

    - Body ABSENT ⇒ field wins quietly (row 3: the silent quadrant that stalled msg-1438).
    - Same target on both sides ⇒ field wins quietly (row 4).
    - Different targets ⇒ escalate the turn to the human with
      ``mismatch_reason=TARGET_DIVERGENCE`` (row 5). The body's target is preserved as
      ``mismatch_body_token`` so the caller's event names both sides.
    - Field itself unresolvable ⇒ same escalation, but with
      ``mismatch_reason=FIELD_UNRESOLVABLE`` (a bad field survived write-side validation, or
      was posted by a client that skipped it; making it loud here is the fail-safe).

    T-reconcile-field-mismatch-flag-overloaded (naysayer msg-1788 "Weakest point"): the two
    escalations share the routing verdict (HUMAN) but come from different causes and take
    different remedies. Merging them under a single ``field_mismatch=True`` bool (as PR #184
    shipped) made any programmatic consumer duplicate the resolver to tell them apart — every
    dashboard that counts "target divergence" would silently inflate by every unresolvable-field
    event too. Splitting the reason into :class:`MismatchReason` puts one bit back that the
    branch structure here already had (this function KNOWS which branch it took) but was
    collapsing on the way out.
    """
    if field_handoff.kind is HandoffKind.ABSENT:
        return Handoff(
            HandoffKind.HUMAN,
            token=field_handoff.token,
            mismatch_reason=MismatchReason.FIELD_UNRESOLVABLE,
            mismatch_body_token=body_handoff.token,
        )
    if body_handoff.kind is HandoffKind.ABSENT:
        return field_handoff
    if _same_target(field_handoff, body_handoff):
        return field_handoff
    return Handoff(
        HandoffKind.HUMAN,
        token=field_handoff.token,
        mismatch_reason=MismatchReason.TARGET_DIVERGENCE,
        mismatch_body_token=body_handoff.token,
    )


def _same_target(a: Handoff, b: Handoff) -> bool:
    """Do two Handoffs point at the same actor? Only used to decide "field agrees with body"."""
    if a.kind is not b.kind:
        return False
    if a.kind is HandoffKind.ROLE:
        # identity is the canonical persona name; both went through the same roster lookup so
        # they will be byte-identical when they agree.
        return a.identity == b.identity
    if a.kind is HandoffKind.PR_REVIEW:
        return a.token == b.token
    if a.kind is HandoffKind.HUMAN:
        # ``human`` and ``operator`` both park on the human but ask for different things (D5);
        # a field saying one and a body saying the other is a divergence, not agreement.
        return a.human_ask == b.human_ask
    return True  # NONE — the kind IS the target


# --------------------------------------------------------------------------- #
# Emission side: the NEXT-protocol block injected into the adapter system prompts
# (PR-2b-1). This is the counterpart of resolve_handoff (the parser) and lives in
# the same module so the sentinel vocabulary (HUMAN_TOKEN / NONE_TOKEN + persona
# names) has one source of truth and cannot drift between emit and parse.
# --------------------------------------------------------------------------- #

# PR #365 review (invariant): the per-label prose is data keyed by label, not a hand-written
# sentence, and the count is len(), not the literal word "four". The key set MUST equal
# ``ADMIT_LABELS``; the check runs at import, so the prompt can neither teach a label the gate
# does not admit nor omit one it does. Order follows :data:`TIER_C_LABELS` (sorted), the same
# order the "Allowed labels" line uses.
_TIER_C_LABEL_DEFINITIONS: dict[str, str] = {
    "goal": "the product's goal, spec, scope or direction changes",
    "cost": "money spent changes: a new external service, API billing up or down",
    "irreversible": (
        "cannot be undone: data deletion, public release, destructive migration, history "
        "rewrite, an external side effect"
    ),
}


def _check_label_definitions(definitions: dict[str, str], admitted: frozenset[str]) -> None:
    """Fail loudly when the prompt's label definitions and the gate's admitted set diverge."""
    if frozenset(definitions) != admitted:
        raise RuntimeError(
            f"handoff._TIER_C_LABEL_DEFINITIONS keys {sorted(definitions)} != "
            f"tier_c_admission_gate.ADMIT_LABELS {sorted(admitted)}: define every admitted "
            "label (and only those) before the handoff prompt can teach it"
        )


_check_label_definitions(_TIER_C_LABEL_DEFINITIONS, ADMIT_LABELS)

_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def _count_word(n: int) -> str:
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def _render_label_definitions(labels: tuple[str, ...]) -> str:
    parts = [f"`{label}` ({_TIER_C_LABEL_DEFINITIONS[label]})" for label in labels]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


_TIER_C_COUNT_WORD = _count_word(len(TIER_C_LABELS))
_TIER_C_DEFINITIONS_PROSE = _render_label_definitions(TIER_C_LABELS)

# D4'-a: the one written example of the operator form. The protocol text quotes it, the stand-down
# notices quote it, and a test feeds it through :func:`resolve_handoff` and requires
# ``HumanAsk.OPERATOR_WORK`` — so the example the prompt teaches is, by construction, a form the
# parser accepts. Built from the keyword constants, never re-spelled.
OPERATOR_FORM_EXAMPLE = (
    f"{OPERATOR_TASK_KEYWORD}: <the concrete work, one line>\n"
    f"{TIER_C_CHECK_KEYWORD}: {TIER_C_CHECK_NONE}\n"
    f"NEXT: {OPERATOR_TOKEN}"
)
_OPERATOR_FORM_INDENTED = "\n".join(f"    {line}" for line in OPERATOR_FORM_EXAMPLE.splitlines())

_HANDOFF_PROTOCOL_CORE = f"""\
---
Conductor handoff protocol (REQUIRED)

A conductor drives this thread one turn at a time: after your reply is posted it \
reads the LAST line of your message to decide who acts next. So end EVERY reply \
with exactly one handoff line, and make it the FINAL line of your reply:

    NEXT: <name>

`<name>` is either another participant's persona name (spelled exactly as it \
appears as a message author in this thread — the persona name, not a role name: \
write the name of the persona who holds the implementer role here, not \
`implementer`) or one of three reserved words:

  - `NEXT: {HUMAN_TOKEN}` — hand to the human ONLY for a Tier-C decision. Tier-C \
is a closed set of {_TIER_C_COUNT_WORD}: {_TIER_C_DEFINITIONS_PROSE}. Anything else is NOT \
Tier-C — decide it yourself and proceed: the implementation approach, whether and how \
to address review findings (advisory or REQUEST_CHANGES), test strategy, naming, \
refactor extent, work order, splitting PRs or threads, approving an internal \
mechanism's design, whether to fix a finding in the current PR or a follow-up \
(fixed rule: fix now; split only if the PR's gate-measured diff would exceed the \
gate's warn threshold — measure with `mindwire pr-diff-size`), and "may I proceed?".
  - `NEXT: {NONE_TOKEN}` — the thread is settled; there is nothing left to do.
  - `NEXT: {OPERATOR_TOKEN}` — a person has to do work by hand, and that work is none \
of the Tier-C types defined above. It is not a way to ask for a decision. Tier-C \
comes first: if the work is, or could be, any of those types, hand it to \
`{HUMAN_TOKEN}` as a Tier-C decision instead, never `NEXT: {OPERATOR_TOKEN}`. \
Use exactly these three lines at the end of your reply:

{_OPERATOR_FORM_INDENTED}

A `NEXT: {OPERATOR_TOKEN}` without the `{OPERATOR_TASK_KEYWORD}:` line, without \
`{TIER_C_CHECK_KEYWORD}: {TIER_C_CHECK_NONE}` directly above it, or in a reply that \
also declares a Tier-C type, is refused and the conductor stops.

The handoff line is part of your verbatim reply, not meta-commentary: write it \
out literally (for example `NEXT: {HUMAN_TOKEN}`) and put nothing after it."""

# U2 (T-tier-c-admission-gate msg-4768): the label list taught to proposer and implementer is the
# admission gate's closed set, rendered from :data:`TIER_C_LABELS` (itself derived from
# ``ADMIT_LABELS``), so the prompt cannot list a label the gate does not admit. ``other:<reason>``
# is no longer taught: msg-3630 §2.2 makes it a non-ticket. The unsure label is the one sanctioned
# way to ask "does this touch the goal?" (msg-3630 §2.1, "迷ったら相談してよい"). The text says
# what the labels ARE and deliberately promises no bounce: the gate's bounce is not wired into
# routing (msg-4768 U1, moved to the Decider threads), so promising one would be a false claim.
_TIER_C_LABEL_GUIDANCE = (
    f"Allowed labels: `{'` / `'.join(TIER_C_LABELS)}` — the {_TIER_C_COUNT_WORD} Tier-C "
    "types above, and nothing else. If you genuinely cannot tell whether a decision touches "
    f"the goal, write `TIER-C: {UNSURE_LABEL}` and say in one line what is unclear. "
    "`other:<reason>` or a missing label is not a Tier-C admission: if none of the "
    f"{_TIER_C_COUNT_WORD} applies, it is not Tier-C, "
    "so decide it yourself and proceed."
)

_ROLE_HANDOFF_GUIDANCE: dict[Role, str] = {
    # A (T-human-terminal-overuse, human GO msg after Einstein ACCEPT msg-891): after you
    # DISPOSITION the naysayer's objections, hand BACK to the naysayer — not to the human. The
    # naysayer is the one who decides whether the design proceeds; the conductor then either
    # builds it directly (control state `run` + attested proceed → carve-out ③) or routes that
    # `go` to the human (any other state), so this proposer text stays TRUE in every state and
    # never has to be re-conditioned on the loop control state (Bohr msg-890 §1: "proposer は
    # `run` / `supervised` / `hold` のいずれでも同じ振る舞いをすればよく、状態を知る必要がない").
    #
    # The previous text ("a design must clear an independent naysayer review AND a human Tier-C
    # decision before implementation, so … hand to `human`") was not merely a nudge in the wrong
    # direction: with `control=run` it was strictly FALSE (carve-out ③ removes the per-step human
    # Tier-C from that path). Its removal is not a heuristic tune — it is deleting a claim that
    # contradicted the routing code. The independence-preserving property is kept in words too:
    # only the naysayer may advance a design to code (Einstein msg-601 Fix-1); a NEXT: from the
    # proposer to the implementer is structurally redirected by guard (i) in `core.py`.
    Role.PROPOSER: (
        "As the proposer: after you propose or revise a design, hand to the independent naysayer "
        "for a design review (`NEXT: <naysayer persona>`). After you have dispositioned the "
        "naysayer's objections, hand BACK to the naysayer — not to the human. The naysayer "
        "decides whether the design proceeds; the conductor then either builds it directly or "
        "routes that go to the human, depending on this project's loop control state. Do NOT "
        "hand a design straight to the implementer — only the naysayer may advance a design to "
        "code, so that you cannot bypass its objections (the conductor structurally redirects "
        f"such a handoff). Hand to `{HUMAN_TOKEN}` only for a decision that is genuinely Tier-C, "
        "and name the type on the line above your handoff, e.g.:\n\n"
        f"    TIER-C: {require_admitted('goal', where='handoff example')}\n"
        f"    NEXT: {HUMAN_TOKEN}\n\n" + _TIER_C_LABEL_GUIDANCE
    ),
    # D-3 (T-human-terminal-overuse, Bohr msg-2540 §4 D-3 approved by Einstein msg-2539 Obj-3):
    # implementer receives the same TIER-C: <label> emission guidance the proposer already has (A
    # msg-890 §3 shipped the calibration tag ONLY on the proposer side; the parser reads it on
    # every human-terminal path, so 25/25 implementer human terminals recorded a null label in the
    # 08-24..09-04 window purely because the emitter was never told). Naysayer is deliberately NOT
    # given this guidance — msg-2540 §3 / Einstein Obj-3: a naysayer's ``NEXT: human`` is an
    # escalation of a design concern, not a Tier-C decision request, and forcing a Tier-C label
    # onto that surface would push two distinct concepts into one field (hybrid complexity).
    Role.IMPLEMENTER: (
        "As the implementer: when you open or update a develop→main pull request, hand to the "
        f"PR-gate — end your reply with `NEXT: {PR_REVIEW_TOKEN} <owner/repo#n>` (the PR ref) so "
        "the independent naysayer review runs before any human merge. For other work, hand back "
        "to the proposer for a spec-review (`NEXT: <proposer persona>`). You never merge to the "
        "main branch yourself, and you never ask the human to merge: the open PR already requests "
        "the merge and the merge-wait PR list carries it, so a merge is not a Tier-C decision. "
        f"For a genuine Tier-C decision, hand to `{HUMAN_TOKEN}` and name the Tier-C type on the "
        "line above your handoff, e.g.:\n\n"
        f"    TIER-C: {require_admitted('cost', where='handoff example')}\n"
        f"    NEXT: {HUMAN_TOKEN}\n\n" + _TIER_C_LABEL_GUIDANCE
    ),
    Role.NAYSAYER: (
        "As the naysayer: after your critique, hand back to the proposer if your objections need a "
        "disposition (`NEXT: <proposer persona>`); if the design is sound and ready to build, hand "
        "to the implementer (`NEXT: <implementer persona>`) — while this project's loop is running "
        "autonomously the conductor builds it directly, otherwise it routes your go to the human "
        f"for the Tier-C decision; or hand to `{HUMAN_TOKEN}` to escalate a concern that needs the "
        "human now. You are advisory, not a veto — but your escalation pulls the human back in "
        "however autonomously the loop is running. When you hand to the implementer, put exactly "
        f"`{TIER_C_CHECK_KEYWORD}: {TIER_C_CHECK_NONE}` on the line directly above your `NEXT:` "
        "line, and only after checking that what you approve to build needs no human decision: "
        "no increase in money spent, no addition / removal / change to a spec already decided for "
        "the project, nothing irreversible or externally published, no task only the human can "
        "do, and no proposer-naysayer conflict you could not settle. If any of those applies, "
        "hand to the human instead. Without the check line the conductor "
        "does not build autonomously — it routes your go to the human."
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


def _resolve_participant(roster: Mapping[str, Role], token: str) -> Handoff:
    """Resolve a non-sentinel token: identity name first, then a role name (D1).

    The identity match always wins, so a roster where an identity happens to be spelled like a
    role keeps its current behaviour. Only when no identity matches is the token compared with the
    :class:`Role` values; it resolves when exactly one identity holds that role in THIS roster at
    THIS moment, so no fixed role→persona map exists anywhere (identity and role stay separate
    axes; the roster decides). Zero or several holders is ``ABSENT`` with
    ``role_alias_unresolved`` set (``IDENTITY_ROLE_AMBIGUOUS``), so it is not mistaken for a typo.
    Everything downstream (guard (i), the self-handoff check, the spawnability check) reads
    ``Handoff.identity`` and so sees the resolved identity (D2).
    """
    match = _roster_lookup(roster, token)
    if match is not None:
        identity, role = match
        return Handoff(HandoffKind.ROLE, identity=identity, role=role, token=token)
    alias = _role_alias(token)
    if alias is None:
        return Handoff(HandoffKind.ABSENT, token=token)
    holders = [identity for identity, role in roster.items() if role is alias]
    if len(holders) != 1:
        return Handoff(HandoffKind.ABSENT, token=token, role_alias_unresolved=True)
    return Handoff(
        HandoffKind.ROLE, identity=holders[0], role=alias, token=token, via_role_alias=True
    )


def _role_alias(token: str) -> Role | None:
    """The :class:`Role` whose value is ``token`` (casefolded), or ``None``."""
    folded = token.casefold()
    for role in Role:
        if role.value == folded:
            return role
    return None


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
    "OPERATOR_FORM_EXAMPLE",
    "OPERATOR_TASK_KEYWORD",
    "OPERATOR_TOKEN",
    "PR_REVIEW_TOKEN",
    "STOP_TRIGGER_ARMS",
    "TIER_C_CHECK_KEYWORD",
    "TIER_C_CHECK_NONE",
    "TIER_C_LABELS",
    "Handoff",
    "HandoffKind",
    "HumanAsk",
    "MismatchReason",
    "OperatorFault",
    "StopLine",
    "StopStatus",
    "build_handoff_protocol_block",
    "declares_no_tier_c",
    "declares_tier_c",
    "parse_next_token",
    "resolve_handoff",
]
