"""Tier-C admission gate — is a ``NEXT: human`` handoff eligible for the human?

This is the label-based entry gate the Fermi/Bohr/Einstein design in
T-tier-c-admission-gate converged on: only handoffs that name one of four
Tier-C intents (goal / cost / irreversible / merge-protected) may reach the
human. Every other label class is either bounced back to the author for
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
the author must choose between ``merge-protected`` (protected-branch
deploy) and ``irreversible`` (public release) themselves.

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

    * ``DECIDED`` — implementer chose to fix an advisory in the same PR
      (source: workspace LLM via conductor extraction).
    * ``DEFERRED`` — implementer chose to skip an advisory (source:
      workspace LLM via conductor extraction).
    * ``BOUNCED`` — admission gate rejected a handoff (source: infra;
      this module's :func:`decide_admission`).
    * ``LABEL_MIGRATION`` — legacy label auto-rewritten to the new enum
      (source: infra; this module's :func:`decide_admission`).
    * ``ADMIT_UNSURE`` — admission gate admitted a ``unsure:goal?``
      handoff (source: infra; this module's :func:`decide_admission`).
    * ``RETRY_ADMIT`` — admission gate admitted via the RETRY prologue
      (source: infra; this module's :func:`decide_admission`).
    """

    DECIDED = "DECIDED"
    DEFERRED = "DEFERRED"
    BOUNCED = "BOUNCED"
    LABEL_MIGRATION = "LABEL_MIGRATION"
    ADMIT_UNSURE = "ADMIT_UNSURE"
    RETRY_ADMIT = "RETRY_ADMIT"


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
      is ``merge-protected`` or ``irreversible``; the author must
      choose (msg-3646 D1, echoed in msg-3706 §1).
    * ``UNKNOWN_LABEL`` — the label parsed but did not match any known
      enum value.
    """

    NO_LABEL = "no-label"
    OTHER_NOT_ADMITTED = "other-not-admitted"
    RELEASE_CROSS_REPO_NEEDS_AUTHOR_CHOICE = "release-cross-repo-needs-author-choice"
    UNKNOWN_LABEL = "unknown-label"


# ---------------------------------------------------------------------------
# Label sets
# ---------------------------------------------------------------------------


#: The four Tier-C intents that constitute a valid admission label
#: (msg-3630 §2.1). No other label reaches the human without a RETRY
#: force-admit.
ADMIT_LABELS: frozenset[str] = frozenset({"goal", "cost", "irreversible", "merge-protected"})


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
    "choose merge-protected (protected-branch deploy) or irreversible (public release)"
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
_LABEL_LINE_RE: re.Pattern[str] = re.compile(
    r"^[ \t]*TIER-C:[ \t]*"
    r"(?P<label>other:[^\r\n]*|unsure:goal\?|[A-Za-z][A-Za-z0-9-]*)"
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
                "attach a `TIER-C: <label>` line above `NEXT: human` "
                "with one of: goal, cost, irreversible, merge-protected"
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

    if label in ADMIT_LABELS:
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
                "admission ticket; pick one of goal / cost / irreversible / "
                "merge-protected, or decide it yourself"
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
            "unknown Tier-C label; pick one of goal / cost / irreversible / "
            "merge-protected (or `unsure:goal?` if genuinely unsure)"
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
