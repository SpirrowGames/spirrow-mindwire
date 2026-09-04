"""Deliverable 6 — extract a stable ``failure_class`` from ``session_log_tail`` text.

The motivation (msg-2354 §1 M-2 and msg-2470 §4):

    * ``quarantine.json`` has ``failure_fingerprint = {head, control}``. Grouping by
      that field produces "every entry in its own bin" — the fingerprint is unique per
      failure occurrence, not per failure *type*.
    * The error class is currently buried in ``session_log_tail`` — a free-text
      capture of the conductor's stdout+stderr. Grouping on it needs an extraction
      step that runs at quarantine time and persists the result as a first-class
      field, ``failure_class``.
    * The extractor is deliberately narrow: it names shapes we have already seen in
      the log (msg-2354 §1 M-2 lists ``ClaudeCodeSdkDeliveryError`` with
      ``subtype='error_during_execution'`` as the top signature). Unknown shapes fall
      through to a well-named ``unknown`` bucket — that is the ledger asymmetry
      applied one layer down: an unrecognised failure produces a noisy row, not a
      silently-collapsed one.

The extractor is text-in / string-out and has no I/O of its own. The CLI wrapper
(``python -m spirrow_mindwire.stall_ledger``, defined in ``__main__.py``) is a
plumbing convenience for the PowerShell wrapper that writes ``quarantine.json``;
the wrapper joins the tail into a single blob, pipes it over stdin, and stores
the answer in the new ``failure_class`` field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FailureSignature:
    """One recognised failure shape.

    ``label``       — the ``failure_class`` value written to the record. Stable across
                      versions; adding a new label is fine, renaming an existing one is
                      a breaking change to the digest's group-by.
    ``pattern``     — the regex that identifies the shape in the tail. Kept narrow to
                      avoid false positives; a wider pattern would silently absorb a
                      neighbouring failure class and defeat the whole point of the
                      grouping.
    ``description`` — human-facing text emitted alongside a group in the digest. Never
                      consulted by group-by.
    """

    label: str
    pattern: re.Pattern[str]
    description: str


# The ordering matters: earlier signatures win. Put the specific *before* the general
# (e.g. "SDK is_error / error_during_execution" before "generic SDK is_error"), so a
# concrete pattern is not stolen by a broader one that appears later in the list.
#
# When a new signature is added, its pattern MUST be tight enough to avoid stealing
# lines from an existing signature — the test suite pins each signature's minimum
# example verbatim, so accidental capture is caught by the pinning tests.
#
# Cross-line matching is BANNED. Every pattern here uses default (non-DOTALL) mode so
# ``.`` cannot cross a newline; a pattern that needs to correlate two identifiers must
# find them on the SAME line, or add its own separate signature for a two-line
# variant. This is not stylistic — PR-gate #219 objection 1 caught the earlier
# ``re.DOTALL`` version of ``sdk-error-during-execution`` bridging an unrelated later
# log line into the wrong bucket, and the fix has to be structural (a pattern
# discipline the test suite pins) not local (one flag removed one time).
_SIGNATURES: tuple[FailureSignature, ...] = (
    FailureSignature(
        label="sdk-error-during-execution",
        # M-2 top signature: msg-2354 §1 records this as 25/20/5/0 occurrences on
        # 08-30/31/09-01/02. The log line the SDK adapter emits carries both the
        # exception class name AND the subtype on the SAME line — see
        # ``src/spirrow_mindwire/exceptions.py`` and the tail sample in msg-2354 §1
        # M-2 itself:
        #     ClaudeCodeSdkDeliveryError: SDK is_error; subtype='error_during_execution'
        # The pattern therefore matches on that single line only. ``[^\n]*?`` — the
        # explicit "any char except newline" quantifier — replaces the older
        # ``.*?`` + ``re.DOTALL`` combination and makes the single-line contract
        # visible in the pattern itself, so a future edit that adds DOTALL back has
        # nothing to gain from it (the char class already excludes ``\n``).
        pattern=re.compile(
            r"ClaudeCodeSdkDeliveryError[^\n]*?subtype\s*=\s*['\"]?error_during_execution['\"]?",
        ),
        description="Claude Code SDK reported is_error / error_during_execution",
    ),
    FailureSignature(
        label="sdk-is-error-generic",
        # Fallback for any other SDK is_error that did not match the specific subtype
        # above. Kept separate from ``unknown`` because the SDK-vs-loop distinction is
        # the one the operator most wants to see at a glance. No cross-line matching:
        # each alternative is a fixed token that stands on its own.
        pattern=re.compile(r"ClaudeCodeSdkDeliveryError|SDK\s+is_error", re.IGNORECASE),
        description="Claude Code SDK reported is_error (subtype not recognised)",
    ),
    FailureSignature(
        label="conductor-timeout",
        # Narrowed vs. the earlier ``conductor.*timeout`` shape: ``[^\n]{0,80}``
        # bounds both the axis (single line) and the distance (80 chars) between
        # "conductor" and "timeout" so a stray "conductor" up-log cannot bond with a
        # much later "timeout" and misclassify an unrelated failure. ``TimeoutError``
        # stays as an anchor for the raw Python exception name.
        pattern=re.compile(r"conductor[^\n]{0,80}timeout|TimeoutError", re.IGNORECASE),
        description="Conductor exceeded a per-tick or per-call timeout",
    ),
    FailureSignature(
        label="preflight-attestation-failed",
        # Named in msg-2354 §1 M-2 as the reason the one remaining quarantine entry
        # was left in place. Distinguishing this class from the SDK ones lets the
        # digest show "this failure is legitimate, not the same batch as the SDK
        # storm" without the operator having to open every log tail. Distance-bounded
        # for the same reason ``conductor-timeout`` is.
        pattern=re.compile(
            r"preflight[^\n]{0,40}attestation[^\n]{0,40}fail",
            re.IGNORECASE,
        ),
        description="Naysayer preflight attestation failed",
    ),
    FailureSignature(
        label="lease-conflict",
        # ``lease.*(conflict|held|denied)`` collapsed onto a single line with a
        # distance bound; ``LeaseHeldError`` is a fixed identifier and stays as-is.
        pattern=re.compile(
            r"lease[^\n]{0,40}(conflict|held|denied)|LeaseHeldError",
            re.IGNORECASE,
        ),
        description="Exclusive resource lease was held by another owner",
    ),
)

UNKNOWN_LABEL: str = "unknown"


def classify_failure(session_log_tail: str | None) -> str:
    """Return the ``failure_class`` label for the given ``session_log_tail``.

    The only supported input shape is the joined single-string tail. That is what
    the CLI in ``__main__.py`` hands us after reading stdin — the PowerShell
    wrapper joins ``$SessionLogTail`` with newlines and pipes the result. An
    earlier revision accepted iterables too, but the PR-gate (#219 objection 3)
    caught it as speculative YAGNI: no shipped caller passed anything but a
    string, and the iterable branch was untestable in a way that mattered to any
    shipped caller. Narrowing the contract removes a dead code path and (as
    objection 2 also pointed out) sidesteps the ``None`` filtering that was
    tested only by proxy.

    Return contract:

        * ``None``, empty, or whitespace-only → ``"unknown"``. The lack of a tail
          is not evidence of any particular class, but it IS a class the operator
          needs to see (M-2's ``last_msg`` empty case).
        * Otherwise: the label of the first matching signature in ``_SIGNATURES``
          order, or ``"unknown"`` if none match.
    """

    if session_log_tail is None or not session_log_tail.strip():
        return UNKNOWN_LABEL
    for sig in _SIGNATURES:
        if sig.pattern.search(session_log_tail):
            return sig.label
    return UNKNOWN_LABEL


def known_labels() -> tuple[str, ...]:
    """Return the tuple of labels the classifier can currently emit (plus ``unknown``).

    Consumers that want to render a per-label summary (the digest's per-signature
    daily hit count, §4) can iterate this to seed empty buckets. Adding a new
    signature therefore reflects into the digest without any digest-side change.
    """

    return (*(sig.label for sig in _SIGNATURES), UNKNOWN_LABEL)


def describe_label(label: str) -> str:
    """Return the human-facing description for a label, or the label itself."""

    for sig in _SIGNATURES:
        if sig.label == label:
            return sig.description
    if label == UNKNOWN_LABEL:
        return "Failure did not match any known signature"
    return label
