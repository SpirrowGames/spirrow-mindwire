"""Tier-C admission gate — is a ``NEXT: human`` handoff eligible for the human?

This is the label-based entry gate the Fermi/Bohr/Einstein design in
T-tier-c-admission-gate converged on: only handoffs that name one of three
Tier-C intents (goal / cost / irreversible; security and dispute were added by
T-tier-c-six-articles-security-boundary) may reach the human.
``merge-protected`` was the fourth until 2026-10-03: a merge into a protected
branch is requested by opening the PR and is carried by the merge-wait PR
list, so it is not a Tier-C escalation (Takahito, msg-4361); the label now
bounces with :attr:`BounceReason.MERGE_IS_NOT_TIER_C`. Every other label class is
either bounced back to the author for
re-labelling, migrated forward (``scope`` → ``goal``, ``billing`` → ``cost``),
or held for author choice (``release-cross-repo``).

This module is deliberately narrow: it is the label-checking core of the
admission gate as fully specified by the v8 pseudocode in msg-3710. The
transport, JSONL persistence, bounce delivery, and RETRY store are the
caller's responsibility — passed in through the ``retry_lookup`` and
returned to the caller as ``log_entries`` for append. Making the core a
pure function lets tests drive every arm of the 8-line decision table
deterministically.

Scope boundary
--------------

The DIFFERENT ``gate_admission`` in :mod:`spirrow_mindwire.gate_admission`
is a CI-wait admission for the PR gate — it decides whether the naysayer
model may run given the current CI rollup. It shares the word "admission"
but nothing else. The two modules have disjoint inputs, outputs, and
caller sets. Keeping them in separate files ensures a future reader
searching for "tier_c" or "admission_gate" cannot accidentally edit the
wrong module.

Reading the pseudocode
----------------------

The v8 decision tree in :func:`decide_admission` is a direct translation of
the msg-3710 pseudocode. The RETRY prologue runs first — a valid RETRY
prefix that matches an unresolved bounce for the same author always
admits (msg-3648 v1 §2's "書き直されていれば admit、書き直されていなくても
2 回目として admit" rule). Legacy label normalisation (``scope``/``billing``
→ ``goal``/``cost``) happens both in the RETRY prologue and in the main
path (msg-3710 §1 0a). ``release-cross-repo`` is NEVER auto-rewritten —
the author must decide whether it is ``irreversible`` (a public release) or
not a Tier-C escalation at all (a merge, which the PR itself requests).

Attribution
-----------

Design: T-tier-c-admission-gate. Fermi kickoff msg-3630 (measurement:
179 human-terminals, 2 goal-class); Bohr disposition chain
v1 (msg-3648) → v2 (msg-3650) → v3 (msg-3652) → v4 (msg-3700) →
v5 (msg-3702) → v6 (msg-3704) → v7 (msg-3706) → v8 (msg-3708) →
final (msg-3710). Independent naysayer critique by Einstein
across msg-3701 / 3703 / 3705 / 3707 / 3709 with APPROVE at msg-3711.

The msg-3646 (D1/D3), msg-3696 (D8.0-D8.10 implementation order),
msg-3698 (read-back), msg-3648 (v1), msg-3650 (v2), and msg-3652 (v3)
message bodies are NOT part of the visible thread transcript this
implementer had; the visible dispositions cite them but their full
content was not readable at implementation time. This module implements
the v8 pseudocode from msg-3710 verbatim, which the visible naysayer
approved as the settled design.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AdmissionVerdict(StrEnum):
    """The routing verdict for one Tier-C admission call.

    ``ADMIT`` — the handoff is authored to the human and reaches them
    (main-chain proceeds without change).

    ``BOUNCE`` — the handoff is rejected at the gate and delivered back
    to the author with a ``reason`` code and (where applicable) a
    ``hint``. The author may correct the label and re-send with a
    ``RETRY: <uuid>`` prefix; on the second bounce the RETRY prologue
    admits unconditionally to prevent an infinite loop (msg-3648 v1 §2,
    integrated into the v8 pseudocode's RETRY prologue).
    """

    ADMIT = "admit"
    BOUNCE = "bounce"


class LogKind(StrEnum):
    """The JSONL decision-log kind enum (msg-3708 §2, msg-3710 §1).

    Every event emitted by the admission gate carries one of these six
    values as its ``kind`` field. The 6-value enum was closed by Bohr in
    msg-3708 §2 to keep the single-JSONL-file discipline established in
    msg-3648 v1 §4 (no separate ledgers per event class — a single
    append-only file with a ``kind`` field is one physical source of
    truth).

    * ``DECIDED`` — a proposer or implementer recorded a discretionary
      decision, such as fixing an advisory in the same PR or choosing an
      approach (source: workspace LLM via conductor extraction; the
      ``author_role`` field tells them apart, msg-5657).
    * ``DEFERRED`` — a proposer or implementer chose to skip something,
      typically an advisory (source: workspace LLM via conductor
      extraction).
    * ``BOUNCED`` — admission gate rejected a handoff (source: infra;
      this module's :func:`decide_admission`).
    * ``LABEL_MIGRATION`` — legacy label auto-rewritten to the new enum
      (source: infra; this module's :func:`decide_admission`).
    * ``ADMIT_UNSURE`` — admission gate admitted a ``unsure:goal?``
      handoff (source: infra; this module's :func:`decide_admission`).
    * ``RETRY_ADMIT`` — admission gate admitted via the RETRY prologue
      (source: infra; this module's :func:`decide_admission`).
    * ``ESCALATED`` — the conductor actually stopped the thread on the human, after the gate
      admitted and the Jev bounce passed (source: infra; the conductor). The seventh value,
      added by T-tier-c-six-articles-security-boundary (Bohr msg-9650 §4) so arrivals can be
      counted per label and ``msg_id``. It re-opens the enum msg-3708 closed at six (recorded
      as a DECIDED there). Never emitted by :func:`decide_admission`.
    """

    DECIDED = "DECIDED"
    DEFERRED = "DEFERRED"
    BOUNCED = "BOUNCED"
    LABEL_MIGRATION = "LABEL_MIGRATION"
    ADMIT_UNSURE = "ADMIT_UNSURE"
    RETRY_ADMIT = "RETRY_ADMIT"
    ESCALATED = "ESCALATED"


class RetryAdmitReason(StrEnum):
    """The reason enum on a ``RETRY_ADMIT`` log entry (msg-3710 §1).

    Populated as the ``reason`` field in the payload of a
    :attr:`LogKind.RETRY_ADMIT` entry so the ``/decisions`` dashboard
    can split RETRY admissions three ways:

    * ``label_corrected`` — the author corrected the label to one of
      the four Tier-C intents (or a legacy alias which was normalised
      to one). Success case.
    * ``unsure_after_retry`` — the author routed the retry via
      ``unsure:goal?``. Admitted per the Takahito 09-05 rule that
      unsure handoffs must not be silently dropped.
    * ``second_time_force_admit`` — the author retried with a label the
      gate cannot admit (``other:``, ``release-cross-repo``, unknown,
      absent). The gate admits anyway to break the infinite-loop guard
      msg-3648 v1 §2 asked for — the failure to disambiguate is now the
      human's problem, and a high count on this bucket is the signal that
      the gate's bounce ``hint`` text is insufficient.
    """

    LABEL_CORRECTED = "label_corrected"
    UNSURE_AFTER_RETRY = "unsure_after_retry"
    SECOND_TIME_FORCE_ADMIT = "second_time_force_admit"


class BounceReason(StrEnum):
    """The reason enum on a ``BOUNCED`` log entry (msg-3710 §1).

    Every bounce reaches the author with one of these codes so the
    author knows which arm of the decision tree fired.

    * ``NO_LABEL`` — the handoff carried no ``TIER-C:`` line.
    * ``OTHER_NOT_ADMITTED`` — the label was ``other:<reason>``. The
      calibration escape hatch is not an entry ticket (msg-3630 §2.2).
    * ``RELEASE_CROSS_REPO_NEEDS_AUTHOR_CHOICE`` — the label was
      ``release-cross-repo``. The gate refuses to guess whether this
      is ``irreversible`` or not Tier-C at all (a merge); the author must
      choose (msg-3646 D1, echoed in msg-3706 §1).
    * ``UNKNOWN_LABEL`` — the label parsed but did not match any known
      enum value.
    * ``MERGE_IS_NOT_TIER_C`` — the label was ``merge-protected``. A merge
      into a protected branch is requested by opening the PR and carried by
      the merge-wait PR list, so it is not escalated (Takahito, msg-4361).
    * ``JEV_LIKELY_NOT`` — the label was admitted, but the Tier-C Decider
      (Jev, ``[decider.tierc] mode = "bounce"``) judged the turn
      ``LIKELY_NOT`` a human decision. Never emitted by
      :func:`decide_admission`; written by
      :meth:`~spirrow_mindwire.conductor.tierc_gate.TierCGate.jev_bounce` so
      the same RETRY store redeems it.
    * ``SECURITY_BASIS_MISSING`` / ``SECURITY_BASIS_MALFORMED`` — a ``security`` handoff
      without exactly one well-formed ``SECURITY-BASIS:`` line (msg-9650 §1.1). Puts a
      structural security hold on the thread/author (msg-9665 §1.1⁷).
    * ``SECURITY_TIGHTEN_IS_AGENT_CALL`` — the basis was ``tighten`` / ``harden`` /
      ``narrow``: strengthening inside the boundary is the agent's call (article 6). No hold.
    * ``SECURITY_HOLD`` — the thread/author has an open security hold and this handoff may not
      pass it (msg-9655 §1.1''). A structural hold when the gate emits it (msg-9665 §1.1⁷).
    * ``SECURITY_HOLD_NO_VERDICT`` / ``EXPOSURE_UNLOCATED`` — written by the conductor after a
      Jev question (hold relations, ``exposure_claim``), never by :func:`decide_admission`.
    """

    NO_LABEL = "no-label"
    OTHER_NOT_ADMITTED = "other-not-admitted"
    RELEASE_CROSS_REPO_NEEDS_AUTHOR_CHOICE = "release-cross-repo-needs-author-choice"
    UNKNOWN_LABEL = "unknown-label"
    MERGE_IS_NOT_TIER_C = "merge-is-not-tier-c"
    JEV_LIKELY_NOT = "jev-likely-not"
    SECURITY_BASIS_MISSING = "security-basis-missing"
    SECURITY_BASIS_MALFORMED = "security-basis-malformed"
    SECURITY_TIGHTEN_IS_AGENT_CALL = "security-tighten-is-agent-call"
    SECURITY_HOLD = "security-hold"
    SECURITY_HOLD_NO_VERDICT = "security-hold-no-verdict"
    EXPOSURE_UNLOCATED = "exposure-unlocated"


# ---------------------------------------------------------------------------
# Label sets
# ---------------------------------------------------------------------------


#: Article 6, the security trust boundary (T-tier-c-six-articles-security-boundary msg-9638).
SECURITY_LABEL: str = "security"

#: Article 5, the ruling on an AI-to-AI dispute (msg-9650 §1.2).
DISPUTE_LABEL: str = "dispute"

#: The Tier-C types of an objection's *substance* (msg-9650 §1.2, Obj-1-6). The naysayer's
#: check conditions and its "a Tier-C objection is handed back at most once" list derive from
#: this set. ``dispute`` names a process state, not a substance, so it is not here.
SUBSTANCE_LABELS: frozenset[str] = frozenset({"goal", "cost", "irreversible", SECURITY_LABEL})

#: The Tier-C intents that constitute a valid admission label (msg-3630 §2.1;
#: ``merge-protected`` removed 2026-10-03, msg-4361; ``security`` / ``dispute`` added by
#: msg-9650 §1.2). No other label reaches the human without a RETRY force-admit.
#: Article 4 (work only a person can do) has no label: it is the ``HUMAN-TASK:`` form, which
#: must carry no Tier-C label at all (msg-9647 §1).
ADMIT_LABELS: frozenset[str] = SUBSTANCE_LABELS | {DISPUTE_LABEL}

#: The admitted labels as the bounce hints spell them (sorted, derived — never re-spelled).
_LABELS_SLASH: str = " / ".join(sorted(ADMIT_LABELS))
_LABELS_COMMA: str = ", ".join(sorted(ADMIT_LABELS))

#: The ``SECURITY-BASIS:`` kinds that admit (msg-9650 §1.1).
SECURITY_BASIS_KINDS: frozenset[str] = frozenset({"widen", "exposure"})

#: The ``SECURITY-BASIS:`` kinds that name a tightening — the agent's call, not article 6.
SECURITY_TIGHTEN_KINDS: frozenset[str] = frozenset({"tighten", "harden", "narrow"})

#: The hint for a missing / malformed basis. It asks for the place, never the value: writing
#: a secret into the thread would itself be a new exposure (msg-9647 §1.1).
SECURITY_BASIS_HINT: str = (
    "a `TIER-C: security` handoff needs exactly one line `SECURITY-BASIS: widen <what> <how far>` "
    "or `SECURITY-BASIS: exposure <where: path:line / commit sha / msg-id>`; give the location, "
    "never the secret value itself"
)

#: Appended to the hint when the same bounce comes back on a RETRY (msg-9650 §1.1).
SECURITY_RETRY_SUFFIX: str = "; a security handoff does not pass on a re-send alone"

#: The hint for a tightening basis.
SECURITY_TIGHTEN_HINT: str = (
    "strengthening inside the existing boundary (a deny added, redaction adjusted, a permission "
    "narrowed) is your call: decide it and record `DECIDED: [security-tighten] ...`"
)

#: The hint for a bounce on an open security hold (msg-9653 §1.1').
SECURITY_HOLD_HINT: str = (
    "this thread holds a security request of yours that was refused for lack of a basis, and "
    "while it is open no Tier-C handoff of yours in this thread reaches the human, whatever its "
    "label and even on a re-send: only a human releases it, by writing `HOLD-RELEASE: <hold id>` "
    "(the hold id is the RETRY uuid of the refusal that opened it) in this thread. Do not "
    "escalate; record `DECIDED:` / `DEFERRED:` instead"
)

#: The former fourth label. Recognised so its bounce can say why (a merge is
#: requested by the PR itself), instead of reading as an unknown label.
MERGE_LABEL: str = "merge-protected"

#: The hint delivered on a :attr:`BounceReason.MERGE_IS_NOT_TIER_C` bounce.
MERGE_NOT_TIER_C_HINT: str = (
    "a merge into a protected branch is already requested by the open PR and listed on the "
    "merge-wait PR list; do not escalate it. Hand the PR to the gate (`NEXT: pr-review <ref>`) "
    "or, if the gate already approved it, end without asking the human"
)


def require_admitted(label: str, *, where: str) -> str:
    """Return ``label`` unchanged, raising if the gate does not admit it.

    For code outside this module that must emit a specific Tier-C label (a worked prompt
    example, the relay's merge-request line). Called at import time, it turns "the gate
    renamed or dropped a label that some producer still emits" from a silent drift into an
    import failure (PR #365 PR-gate advisory, msg-4850). ``where`` names the caller, so the
    error says which producer went stale.
    """
    if label not in ADMIT_LABELS:
        raise RuntimeError(
            f"{where}: Tier-C label {label!r} is not in "
            f"tier_c_admission_gate.ADMIT_LABELS {sorted(ADMIT_LABELS)}"
        )
    return label


#: The legacy labels the gate auto-rewrites to the new enum on the way
#: through (msg-3646 D1, msg-3710 §1 legacy_auto). ``scope`` becomes
#: ``goal``; ``billing`` becomes ``cost``. Every rewrite emits a
#: ``LABEL_MIGRATION`` entry so the 14-day audit can measure the residual
#: legacy usage without inspecting the JSONL by hand.
LEGACY_LABEL_MAP: dict[str, str] = {
    "scope": "goal",
    "billing": "cost",
}


#: The single label that is admitted without being one of the four Tier-C
#: intents (msg-3630 §2.1 "迷ったら相談してよい"; msg-3706 §1 分岐追加).
#: Recorded as :attr:`LogKind.ADMIT_UNSURE` in the decisions log so a
#: dashboard can measure how often unsure escalations arrive.
UNSURE_LABEL: str = "unsure:goal?"


#: The label whose disambiguation the gate refuses to make on behalf of
#: the author (msg-3706 §1). Every ``release-cross-repo`` intent bounces
#: with a hint listing the two acceptable substitutes.
RELEASE_CROSS_REPO_LABEL: str = "release-cross-repo"


#: The exact hint text delivered on a
#: :attr:`BounceReason.RELEASE_CROSS_REPO_NEEDS_AUTHOR_CHOICE` bounce.
#: Kept as a module constant so the tests can pin the wire text without
#: reaching into private state.
RELEASE_CROSS_REPO_HINT: str = (
    "choose irreversible (public release); a merge or a protected-branch deploy is requested by "
    "its PR and is not a Tier-C escalation"
)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


# The RETRY prefix grammar (msg-3648 v1 §2, integrated into the v8
# pseudocode's step 0). Must sit on an early standalone line. The UUID
# token is opaque — the admission gate does not validate its shape;
# it merely hands the token to the ``retry_lookup`` callable so the
# infra can decide whether the token maps to an unresolved bounce for
# the same author. This keeps the gate free of UUID format assumptions
# (v4 / v7 / bespoke) that a future infra change could invalidate.
# ``[ \t]*\r?$`` (not ``[ \t]*$``): with ``re.MULTILINE`` the ``$`` anchor
# in Python matches strictly before ``\n`` — never before ``\r``. A body
# carrying CRLF line endings (any HTTP client, any Windows author) would
# leave a stray ``\r`` between the trailing whitespace and the newline,
# so ``[ \t]*$`` would fail to match and the parser would return ``None``
# on a perfectly valid RETRY line. The explicit ``\r?`` accepts either
# line-ending convention. Fix for PR-gate objection #322-3 (CRLF).
_RETRY_LINE_RE: re.Pattern[str] = re.compile(
    r"^[ \t]*RETRY:[ \t]*(?P<uuid>\S+)[ \t]*\r?$", re.MULTILINE
)

# The TIER-C label grammar. Deliberately narrow: the label must sit on
# a standalone line, immediately following ``TIER-C:`` with optional
# horizontal whitespace. Any occurrence anywhere in the body counts —
# unlike the calibration-tag parser in ``handoff.py`` (which requires
# the label to sit exactly one line above ``NEXT: human``), the
# admission gate treats the label as the author's intent statement and
# does not scan the neighbouring lines. This is a deliberate choice:
# the admission gate is looking at bodies the write side has already
# validated as ending in a Tier-C handoff, and forcing a specific
# neighbour line would false-negative on the many valid formattings
# authors already use.
#
# ``other:<reason>`` is a distinct family: the reason text may contain
# arbitrary non-newline characters after the colon.
# ``[ \t]*\r?$`` for the same CRLF reason as ``_RETRY_LINE_RE`` above.
# The ``other:[^\r\n]*`` sub-pattern intentionally excludes ``\r`` too so
# a CRLF-terminated ``TIER-C: other: reason\r\n`` does not capture the CR
# into the reason (a stray ``\r`` in the reason string would poison the
# canonical form the ``.strip()`` step relies on).
#
# The generic-enum sub-pattern accepts BOTH kebab-case (``merge-protected``)
# and snake_case (``merge_protected``): ``[A-Za-z][A-Za-z0-9_-]*``. The
# kebab convention is the one the four Tier-C intents actually use, but
# authors typing an unrecognised label frequently reach for underscores
# — an author-facing regex that only tolerates hyphens forces a
# snake_case typo (``TIER-C: unknown_label``) all the way out of the
# grammar, and the caller then hits the ``NO_LABEL`` bounce arm ("the
# handoff carried no TIER-C: line") when the label was visibly present.
# Widening the character class routes such labels into the correct
# ``UNKNOWN_LABEL`` arm whose hint text names the four valid intents.
# Fix for PR-gate objection #322-10 (structure).
_LABEL_LINE_RE: re.Pattern[str] = re.compile(
    r"^[ \t]*TIER-C:[ \t]*"
    r"(?P<label>other:[^\r\n]*|unsure:goal\?|[A-Za-z][A-Za-z0-9_-]*)"
    r"[ \t]*\r?$",
    re.IGNORECASE | re.MULTILINE,
)


def extract_retry_uuid(body: str) -> str | None:
    """Return the RETRY UUID token in ``body``, or ``None`` if not present.

    The token is whatever non-whitespace run follows the ``RETRY:``
    keyword. Format validation lives with the caller (the ``retry_lookup``
    callable) — the admission gate is opaque to UUID shape by design.

    Only the FIRST match is returned. A body carrying more than one
    ``RETRY:`` line is malformed at the caller's level; picking the
    first match is a deterministic, testable rule that never surprises
    an author who happens to quote a prior RETRY line further down.
    """
    match = _RETRY_LINE_RE.search(body)
    return match.group("uuid") if match is not None else None


def extract_label(body: str) -> str | None:
    """Return the Tier-C label in ``body``, or ``None`` if not present.

    The label is lower-cased so ``TIER-C: Goal`` and ``tier-c: goal``
    normalise the same way. An ``other:<reason>`` label keeps its
    ``other:`` prefix and preserves the reason text (case + inner
    whitespace) so the bounce log can attribute the failure to a
    specific reason string.

    Only the FIRST match is returned. A body with multiple TIER-C
    lines is malformed at the caller's level and the deterministic
    first-match rule matches the RETRY parser above.
    """
    match = _LABEL_LINE_RE.search(body)
    if match is None:
        return None
    raw = match.group("label")
    lowered = raw.lower()
    if lowered.startswith("other:"):
        # Preserve the reason text case; strip BOTH leading whitespace
        # (between the colon and the reason) and any trailing whitespace
        # the greedy ``other:[^\r\n]*`` regex captured, so
        # ``TIER-C: other: text  `` and ``TIER-C: other: text`` aggregate
        # to the same canonical label. Fix for PR-gate objection #322-2.
        return "other:" + raw[len("other:") :].strip()
    return lowered


# Any line starting with the keyword counts toward the one-line rule, so a second, malformed
# line is caught rather than ignored (msg-9650 §1.1). CRLF tolerated as in the parsers above.
_SECURITY_BASIS_LINE_RE: re.Pattern[str] = re.compile(
    r"^[ \t]*SECURITY-BASIS:(?P<rest>[^\r\n]*)\r?$", re.IGNORECASE | re.MULTILINE
)
# The well-formed rest of the line: a kind, then (for an admitting kind) a non-empty detail.
_SECURITY_BASIS_REST_RE: re.Pattern[str] = re.compile(
    r"^[ \t]*(?P<kind>[A-Za-z-]+)(?:[ \t]+(?P<detail>\S[^\r\n]*?))?[ \t]*$"
)


@dataclass(frozen=True)
class SecurityBasisCheck:
    """The result of checking a body's ``SECURITY-BASIS:`` lines (msg-9650 §1.1).

    ``reason`` is ``None`` when the body carries exactly one admitting line. ``basis`` is the
    line's text (stripped) whenever there was exactly one line, whatever its shape — it is what
    a hold records as ``held_basis``. ``kind`` is the lower-cased kind when one parsed.
    """

    reason: BounceReason | None
    basis: str | None = None
    kind: str | None = None


def check_security_basis(body: str) -> SecurityBasisCheck:
    """Check the ``SECURITY-BASIS:`` grammar of a ``security`` handoff (msg-9650 §1.1 table).

    The gate checks shape, line count and non-emptiness only. Whether the detail is concrete
    enough is for Jev (rule_6's note) and the naysayer.
    """
    lines = list(_SECURITY_BASIS_LINE_RE.finditer(body))
    if not lines:
        return SecurityBasisCheck(BounceReason.SECURITY_BASIS_MISSING)
    if len(lines) > 1:
        return SecurityBasisCheck(BounceReason.SECURITY_BASIS_MALFORMED)
    rest = lines[0].group("rest")
    basis = ("SECURITY-BASIS:" + rest).strip()
    shape = _SECURITY_BASIS_REST_RE.match(rest)
    if shape is None:
        return SecurityBasisCheck(BounceReason.SECURITY_BASIS_MALFORMED, basis)
    kind = shape.group("kind").lower()
    if kind in SECURITY_TIGHTEN_KINDS:
        return SecurityBasisCheck(BounceReason.SECURITY_TIGHTEN_IS_AGENT_CALL, basis, kind)
    if kind not in SECURITY_BASIS_KINDS or shape.group("detail") is None:
        return SecurityBasisCheck(BounceReason.SECURITY_BASIS_MALFORMED, basis, kind)
    return SecurityBasisCheck(None, basis, kind)


def normalized_body_sha256(body: str) -> str:
    """The sha256 the structural-hold dedupe compares (msg-9665 §1.1⁷).

    Line endings are unified, each line's runs of whitespace collapse to one space, and blank
    lines and ``RETRY:`` lines drop out — a RETRY re-send of the same body is the same body.
    Deterministic and Jev-free.
    """
    kept: list[str] = []
    for raw in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw.split())
        if not line or _RETRY_LINE_RE.fullmatch(line):
            continue
        kept.append(line)
    return hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogEntry:
    """One JSONL row the caller should append to the decisions log.

    The gate returns log entries in the order they should be appended.
    Every entry carries a ``kind`` (from :class:`LogKind`) plus a
    ``payload`` dict of kind-specific fields — including every field
    the gate itself has enough information to stamp:

    * ``ts`` — from the ``now`` clock the caller injected.
    * ``author`` — from the ``author`` parameter the caller passed.
    * ``retry_uuid`` — for ``BOUNCED`` and ``RETRY_ADMIT`` entries,
      from the ``bounce_uuid`` / retry-token parameters.
    * per-kind fields (``reason``, ``hint``, ``label``, ``from``,
      ``to``, ``normalized_label``, etc.).

    Only the message-envelope columns the gate never sees —
    ``thread`` and ``msg_id`` — are added by the transport at
    :func:`~spirrow_mindwire.tier_c_decisions_log.append_log_entry`
    time. This means the caller may pass the returned entries to
    :func:`~spirrow_mindwire.tier_c_decisions_log.append_log_entries`
    without any unpack/re-instantiate step: the gate is the sole
    constructor of a well-formed :class:`LogEntry` shape. Fix for
    PR-gate objection #322-6 (dual-management complexity).

    A single ``decide_admission`` call may return zero, one, or two
    entries:

    * bounce paths return one ``BOUNCED``.
    * ordinary admit paths return zero (``goal``/``cost``/
      ``irreversible``/``merge-protected``).
    * legacy admits return one ``LABEL_MIGRATION``.
    * unsure admits return one ``ADMIT_UNSURE``.
    * RETRY admits return one ``RETRY_ADMIT``, plus a preceding
      ``LABEL_MIGRATION`` if the retry itself normalised a legacy
      label (msg-3710 §1 0a → 0b).

    Immutability
    ------------

    :class:`LogEntry` is ``frozen=True``, and its ``payload`` is
    wrapped in a :class:`types.MappingProxyType` in ``__post_init__``
    so a downstream caller cannot mutate the entry through
    ``entry.payload[key] = value`` after construction. Fix for
    PR-gate objection #322-9: a frozen dataclass whose only field
    is a mutable dict makes the freeze meaningless. Reads and
    equality comparisons still work — ``MappingProxyType`` is a
    read-only view over the underlying dict, not a re-implementation.
    """

    kind: LogKind
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        # Wrap the payload in a read-only view so ``frozen=True`` covers
        # both the field assignment (existing behaviour) AND mutation
        # through the payload's dict interface. If the caller already
        # handed us a ``MappingProxyType`` we do not double-wrap: two
        # layers would defeat the equality shortcut with the underlying
        # dict without adding any protection. Fix for #322-9.
        if isinstance(self.payload, MappingProxyType):
            return
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class AdmissionDecision:
    """The result of a single :func:`decide_admission` call.

    * ``verdict`` — :class:`AdmissionVerdict.ADMIT` or ``BOUNCE``.
    * ``rule`` — the pseudocode arm that fired, useful for metrics and
      for the audit trail (``"R0-RETRY"``, ``"R1-admit"``, etc.).
    * ``bounce_reason`` / ``bounce_hint`` — populated only when
      ``verdict is BOUNCE``. The hint may be ``None`` when the bounce
      is unambiguous (e.g. ``NO_LABEL``: the hint would repeat the
      reason).
    * ``normalized_label`` — the label the gate would treat the
      handoff as carrying (post legacy rewrite). ``None`` when there
      was no label at all. Callers use this to route the admitted
      handoff downstream (integrator / human queue) without re-parsing.
    * ``log_entries`` — the list of :class:`LogEntry` values the caller
      should append to the JSONL log, in the order returned.
    """

    verdict: AdmissionVerdict
    rule: str
    bounce_reason: BounceReason | None = None
    bounce_hint: str | None = None
    normalized_label: str | None = None
    log_entries: list[LogEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


#: The type of the RETRY store lookup. Given a ``(uuid, author)`` pair,
#: return ``True`` if ``uuid`` names an unresolved bounce that ``author``
#: was the recipient of. "Unresolved" means: a prior ``BOUNCED`` entry
#: exists in the decisions log with this UUID and this author, and no
#: subsequent ``RETRY_ADMIT`` entry has been recorded against the same
#: UUID (msg-3710 §1 "RETRY_ADMIT が append されたら uuid は resolved").
RetryLookup = Callable[[str, str], bool]


def decide_admission(
    *,
    body: str,
    author: str,
    retry_lookup: RetryLookup,
    now: datetime,
    bounce_uuid: str,
    security_hold: bool = False,
) -> AdmissionDecision:
    """Answer whether a ``NEXT: human`` handoff may reach the human.

    This is the v8 pseudocode from msg-3710 §1 — the RETRY prologue
    (step 0 with legacy-normalisation sub-step 0a) followed by the
    main label evaluation (step 1). See the module docstring for the
    full decision table.

    The function is pure: no I/O, no clock reads (``now`` is passed
    in), no mutable module state. Every observation the caller must
    lift from the world — the RETRY store, the current wall clock,
    a fresh UUID for a possible bounce — is a parameter. A test can
    drive every arm deterministically with a fake ``retry_lookup``,
    a fixed ``now``, and a fixed ``bounce_uuid``.

    Parameters
    ----------
    body :
        The full message body. The gate extracts the TIER-C label
        and any RETRY prefix from it.
    author :
        The canonical persona name of the message author. Used both
        as the second argument to ``retry_lookup`` and as the
        ``author`` field on every emitted log entry.
    retry_lookup :
        A callable ``(uuid, author) -> bool`` that returns ``True``
        exactly when the UUID names an unresolved bounce recorded
        against the same author. See :data:`RetryLookup`.
    now :
        The wall-clock timestamp to stamp on log entries. Passed as
        a parameter so tests can drive the timestamp deterministically.
    bounce_uuid :
        A freshly-generated UUID the gate stamps into the
        ``retry_uuid`` field of any ``BOUNCED`` entry it emits. The
        transport should generate one per admission call regardless
        of the expected outcome; unused UUIDs are cheap and this
        keeps the gate as the sole constructor of a well-formed
        :class:`LogEntry` (fix for PR-gate objection #322-6). If the
        arm chosen is not a bounce, the UUID is discarded.
    security_hold :
        ``True`` when the caller's decisions log shows an open security hold for this
        thread/author (msg-9655 §1.1'', msg-9665 §1.1⁷). Evaluated before the RETRY prologue:
        while held, **nothing passes** — RETRY never force-admits and no label is admitted
        (PR #595 gate Obj-1, fix_cut: until the per-hold Jev re-check of part 1b exists, a held
        head cannot be judged a supplement rather than an evasion, so it waits for a human's
        ``HOLD-RELEASE:``). Every refusal while held records its own structural hold, as before
        (msg-9665 §1.1⁷); a tightening ``security`` head keeps its own no-hold bounce.
    """

    # Common-field stamper. The gate has enough context — ``now``,
    # ``author`` — to fully populate every payload it emits, so
    # downstream code sees a complete row without any unpack /
    # re-instantiate step. Only ``thread`` / ``msg_id`` are added
    # later by the transport at append time (they live on the
    # message envelope the gate never sees).
    def _entry(kind: LogKind, extra: dict[str, Any]) -> LogEntry:
        payload = {"ts": now.isoformat(), "author": author, **extra}
        return LogEntry(kind=kind, payload=payload)

    label = extract_label(body)
    retry_uuid = extract_retry_uuid(body)

    # A security bounce. Every arm but the tighten one puts a structural hold on the
    # thread/author (msg-9653 §1.1', msg-9665 §1.1⁷): the payload carries what the caller's
    # hold store needs — the basis line as written and the normalised body hash for dedupe.
    # ``held_msg_id`` is the envelope ``msg_id`` the transport adds.
    def _security_bounce(
        reason: BounceReason, hint: str, rule: str, basis: str | None, *, retry: bool = False
    ) -> AdmissionDecision:
        hold = reason is not BounceReason.SECURITY_TIGHTEN_IS_AGENT_CALL
        if retry and hold:
            hint += SECURITY_RETRY_SUFFIX
        extra: dict[str, Any] = {
            "reason": reason.value,
            "label": label,
            "hint": hint,
            "retry_uuid": bounce_uuid,
        }
        if hold:
            extra |= {
                "security_hold": True,
                "hold_kind": "structural",
                "held_basis": basis,
                "body_sha256": normalized_body_sha256(body),
            }
        return AdmissionDecision(
            verdict=AdmissionVerdict.BOUNCE,
            rule=rule,
            bounce_reason=reason,
            bounce_hint=hint,
            normalized_label=label,
            log_entries=[_entry(LogKind.BOUNCED, extra)],
        )

    def _basis_bounce(rule: str, *, retry: bool = False) -> AdmissionDecision | None:
        check = check_security_basis(body)
        if check.reason is None:
            return None
        hint = (
            SECURITY_TIGHTEN_HINT
            if check.reason is BounceReason.SECURITY_TIGHTEN_IS_AGENT_CALL
            else SECURITY_BASIS_HINT
        )
        return _security_bounce(check.reason, hint, rule, check.basis, retry=retry)

    # -----------------------------------------------------------------
    # Step H — an open security hold (msg-9655 §1.1''). Before the RETRY prologue, so neither a
    # RETRY nor a relabel to ``other:`` / ``unsure`` force-admits past it.
    if security_hold:
        held_label = LEGACY_LABEL_MAP.get(label, label) if label is not None else None
        basis = None
        if held_label == SECURITY_LABEL:
            refused = _basis_bounce("RH-security-basis")
            if refused is not None and (
                refused.bounce_reason is BounceReason.SECURITY_TIGHTEN_IS_AGENT_CALL
            ):
                return refused
            basis = check_security_basis(body).basis
        # Every other head waits for a human release (PR #595 gate Obj-1, fix_cut): no
        # ``RH-recheck`` admission until part 1b asks Jev about each open hold.
        return _security_bounce(
            BounceReason.SECURITY_HOLD, SECURITY_HOLD_HINT, "RH-security-hold", basis
        )

    # -----------------------------------------------------------------
    # Step 0 — RETRY binding check (msg-3710 §1 step 0).
    #
    # The RETRY prologue runs FIRST, before any label evaluation. If
    # the body carries a RETRY prefix whose UUID matches an unresolved
    # bounce for the same author, the gate admits. The precise
    # ``reason`` on the RETRY_ADMIT entry depends on the label the
    # author supplied on the retry (post legacy normalisation):
    #
    #   * legal Tier-C enum or legacy alias  →  label_corrected
    #   * unsure:goal?                        →  unsure_after_retry
    #   * anything else                        →  second_time_force_admit
    #
    # The second_time_force_admit case is the deliberate infinite-loop
    # cutoff (msg-3648 v1 §2): the author has retried once and still
    # supplied a label the gate cannot pass; rather than bouncing
    # forever, we let the handoff through and rely on the
    # ``/decisions`` dashboard's high count on this reason to flag
    # that the initial bounce ``hint`` needs improvement.
    #
    # A retry prefix that does NOT match an unresolved bounce
    # (typo, wrong author, bogus UUID) falls through to the main
    # label evaluation. This preserves msg-3648 v1 §2's "``RETRY:`` の
    # 付いていない新規は最初からやり直す" rule for the case of an
    # accidental prefix.
    if retry_uuid is not None and retry_lookup(retry_uuid, author):
        entries: list[LogEntry] = []

        # 0a — legacy_auto normalisation runs before the retry-outcome
        # branch so a ``scope`` / ``billing`` retry is recognised as
        # the same success as a ``goal`` / ``cost`` retry (msg-3710
        # §1 0a fix for Einstein msg-3709 BLOCKING).
        if label is not None and label in LEGACY_LABEL_MAP:
            normalized: str | None = LEGACY_LABEL_MAP[label]
            entries.append(
                _entry(
                    LogKind.LABEL_MIGRATION,
                    {
                        "from": label,
                        "to": normalized,
                        "via": "retry",
                        "retry_uuid": retry_uuid,
                    },
                )
            )
        else:
            normalized = label

        # 0a' — a security retry still needs its basis: a missing or malformed basis is never
        # force-admitted (msg-9650 §1.1, Obj-1-3), it bounces again with a hold.
        if normalized == SECURITY_LABEL:
            refused = _basis_bounce("R0-RETRY:security-basis", retry=True)
            if refused is not None:
                return refused

        # 0b — decide the RETRY outcome on the post-normalisation label.
        reason: RetryAdmitReason
        if normalized is not None and normalized in ADMIT_LABELS:
            reason = RetryAdmitReason.LABEL_CORRECTED
        elif normalized == UNSURE_LABEL:
            reason = RetryAdmitReason.UNSURE_AFTER_RETRY
        else:
            # other:, release-cross-repo, unknown, or absent labels on
            # a valid retry: the gate admits to prevent an infinite
            # loop, but the dashboard records the fall-through so a
            # sustained trend can drive a hint-text revision.
            reason = RetryAdmitReason.SECOND_TIME_FORCE_ADMIT

        entries.append(
            _entry(
                LogKind.RETRY_ADMIT,
                {
                    "reason": reason.value,
                    "retry_uuid": retry_uuid,
                    "label": label,
                    "normalized_label": normalized,
                },
            )
        )
        return AdmissionDecision(
            verdict=AdmissionVerdict.ADMIT,
            rule=f"R0-RETRY:{reason.value}",
            normalized_label=normalized,
            log_entries=entries,
        )

    # -----------------------------------------------------------------
    # Step 1 — the main label evaluation.
    #
    # From here on the retry prefix (if any) is treated as absent —
    # either there was no prefix, or the prefix pointed at an
    # unresolved / non-existent bounce (typo, wrong author, expired).
    #
    # The order of the arms below matches the pseudocode in
    # msg-3710 §1 exactly:
    #
    #   1. no label                        → bounce(no-label)
    #   1b. label == MERGE_LABEL           → bounce(merge-is-not-tier-c) (2026-10-03)
    #   2. label in ADMIT_LABELS           → admit
    #   3. label == UNSURE_LABEL           → admit, ADMIT_UNSURE
    #   4. label starts "other:"           → bounce(other-not-admitted)
    #   5. label in LEGACY_LABEL_MAP       → auto-rewrite, admit,
    #                                        LABEL_MIGRATION
    #   6. label == RELEASE_CROSS_REPO     → bounce(release-cross-repo-
    #                                        needs-author-choice)
    #   7. else                             → bounce(unknown-label)
    if label is None:
        return AdmissionDecision(
            verdict=AdmissionVerdict.BOUNCE,
            rule="R1-no-label",
            bounce_reason=BounceReason.NO_LABEL,
            bounce_hint=(
                f"attach a `TIER-C: <label>` line above `NEXT: human` with one of: {_LABELS_COMMA}"
            ),
            log_entries=[
                _entry(
                    LogKind.BOUNCED,
                    {
                        "reason": BounceReason.NO_LABEL.value,
                        "label": None,
                        "retry_uuid": bounce_uuid,
                    },
                )
            ],
        )

    if label == MERGE_LABEL:
        return AdmissionDecision(
            verdict=AdmissionVerdict.BOUNCE,
            rule="R1-merge-not-tier-c",
            bounce_reason=BounceReason.MERGE_IS_NOT_TIER_C,
            bounce_hint=MERGE_NOT_TIER_C_HINT,
            log_entries=[
                _entry(
                    LogKind.BOUNCED,
                    {
                        "reason": BounceReason.MERGE_IS_NOT_TIER_C.value,
                        "label": label,
                        "retry_uuid": bounce_uuid,
                    },
                )
            ],
        )

    if label in ADMIT_LABELS:
        if label == SECURITY_LABEL:
            refused = _basis_bounce("R1-security-basis")
            if refused is not None:
                return refused
        return AdmissionDecision(
            verdict=AdmissionVerdict.ADMIT,
            rule="R1-admit",
            normalized_label=label,
        )

    if label == UNSURE_LABEL:
        return AdmissionDecision(
            verdict=AdmissionVerdict.ADMIT,
            rule="R1-admit-unsure",
            normalized_label=label,
            log_entries=[
                _entry(
                    LogKind.ADMIT_UNSURE,
                    {"label": label},
                )
            ],
        )

    if label.startswith("other:"):
        return AdmissionDecision(
            verdict=AdmissionVerdict.BOUNCE,
            rule="R1-other-not-admitted",
            bounce_reason=BounceReason.OTHER_NOT_ADMITTED,
            bounce_hint=(
                "`other:<reason>` is a calibration escape hatch, not an "
                f"admission ticket; pick one of {_LABELS_SLASH}, "
                "or decide it yourself"
            ),
            log_entries=[
                _entry(
                    LogKind.BOUNCED,
                    {
                        "reason": BounceReason.OTHER_NOT_ADMITTED.value,
                        "label": label,
                        "retry_uuid": bounce_uuid,
                    },
                )
            ],
        )

    if label in LEGACY_LABEL_MAP:
        normalized_main = LEGACY_LABEL_MAP[label]
        return AdmissionDecision(
            verdict=AdmissionVerdict.ADMIT,
            rule="R1-legacy-migration",
            normalized_label=normalized_main,
            log_entries=[
                _entry(
                    LogKind.LABEL_MIGRATION,
                    {
                        "from": label,
                        "to": normalized_main,
                        "via": "main",
                    },
                )
            ],
        )

    if label == RELEASE_CROSS_REPO_LABEL:
        return AdmissionDecision(
            verdict=AdmissionVerdict.BOUNCE,
            rule="R1-release-cross-repo",
            bounce_reason=BounceReason.RELEASE_CROSS_REPO_NEEDS_AUTHOR_CHOICE,
            bounce_hint=RELEASE_CROSS_REPO_HINT,
            log_entries=[
                _entry(
                    LogKind.BOUNCED,
                    {
                        "reason": (BounceReason.RELEASE_CROSS_REPO_NEEDS_AUTHOR_CHOICE.value),
                        "label": label,
                        "hint": RELEASE_CROSS_REPO_HINT,
                        "retry_uuid": bounce_uuid,
                    },
                )
            ],
        )

    return AdmissionDecision(
        verdict=AdmissionVerdict.BOUNCE,
        rule="R1-unknown-label",
        bounce_reason=BounceReason.UNKNOWN_LABEL,
        bounce_hint=(
            f"unknown Tier-C label; pick one of {_LABELS_SLASH} "
            "(or `unsure:goal?` if genuinely unsure)"
        ),
        log_entries=[
            _entry(
                LogKind.BOUNCED,
                {
                    "reason": BounceReason.UNKNOWN_LABEL.value,
                    "label": label,
                    "retry_uuid": bounce_uuid,
                },
            )
        ],
    )
