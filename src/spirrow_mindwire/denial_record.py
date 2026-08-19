"""Structured record for an allow-list denial — what was attempted, not just which rule fired.

Spec: ``spec/design/T-denial-detail-and-overdeny.md`` (PR-1). Background: on
2026-08-11 six implementer sessions halted on ``allow-list denied fs.delete`` (an
operation retired from the classifier on 2026-08-19 —
T-drop-branch-prediction-from-allowlist §3, msg-1272 §1) and **no record anywhere
said what the session had tried to do**. The classifier holds the raw command in
:attr:`~spirrow_mindwire.allowlist.ClassifiedAction.detail`, but the error message
is built from ``decision.reason`` alone — the static Tier-C string out of the
allow-list YAML. The denial was loud about the *rule* and silent about the *act*,
which made the halts undiagnosable. The record design here survives the
``fs.delete`` retirement unchanged, because the diagnostic question ("which act
tripped this rule") is common to every deny — it is not tied to any one operation.

Two layers, deliberately split by what can fail:

* **Layer A** (:func:`layer_a`) is unconditional and carries **no input-derived data**
  — only counts, offsets, booleans and the operation/rule identity. It cannot leak a
  secret because it never contains a substring of the command. It is what answers
  "which verdict fired, and was the match inside a heredoc body".
* **Layer B** (:func:`layer_b`) is best-effort and *does* quote input: a ±100 char
  window around the match, redacted. If redaction raises, the window is dropped
  (``<redact-failed>``) rather than emitted unredacted — fail-closed, because the
  point of the window is convenience and the point of redaction is safety.

The layers are separate so that a redaction bug degrades the record instead of
destroying it: layer A still answers the diagnostic question with layer B empty.
"""

from __future__ import annotations

import re
from typing import Any

from .allowlist import AllowlistDecision, ClassifiedAction

# Characters of context kept either side of the match in layer B (spec B1).
_CONTEXT_RADIUS = 100

# Known secret shapes. Deliberately a *shape* list, not an exhaustive one: an
# unlisted secret shape survives into layer B. That residue is named in the spec
# rather than papered over — layer A is the part that is safe by construction.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bghp_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bgho_[A-Za-z0-9]{16,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    # JWT: three dot-separated base64url runs.
    re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
)

# `--token X` / `--password=X` / `Authorization: Bearer X` / `FOO_SECRET=X`.
_LABELLED_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(--(?:token|password|passwd|secret|api[-_]?key)[=\s]+)(\S+)", re.IGNORECASE),
    re.compile(r"(Authorization\s*:\s*(?:Bearer|Basic)\s+)(\S+)", re.IGNORECASE),
    re.compile(r"(\b[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|APIKEY|API_KEY)[A-Z0-9_]*\s*=\s*)(\S+)"),
)

# URL userinfo: scheme://user:pass@host
_URL_USERINFO = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)(@)")

# A long unbroken high-entropy run (mixed case + digits) that no named rule caught.
_HIGH_ENTROPY = re.compile(
    r"\b(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{32,}\b"
)

_REDACTED = "<redacted>"

# Heredoc start: `<<EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"`.
_HEREDOC_START = re.compile(r"<<-?\s*[\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?")

_CONTROL_CHARS = {ord(c): f"\\x{ord(c):02x}" for c in map(chr, range(0x20)) if c not in "\n\t"}


def redact(text: str) -> str:
    """Replace known secret shapes in ``text``.

    Order matters: labelled values and URL userinfo run first so that the value they
    guard is masked even when it does not match a known token shape, then the token
    shapes, then the high-entropy catch-all. Escapes control characters and newlines
    last so the result is a single log-safe line.
    """
    out = text
    for pattern in _LABELLED_VALUE_PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)}{_REDACTED}", out)
    out = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{m.group(2)}:{_REDACTED}{m.group(4)}", out)
    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    out = _HIGH_ENTROPY.sub(_REDACTED, out)
    out = out.translate(_CONTROL_CHARS)
    return out.replace("\n", "\\n").replace("\t", "\\t")


def layer_a(decision: AllowlistDecision, action: ClassifiedAction) -> dict[str, Any]:
    """The unconditional layer: identity, provenance and shape — never input text.

    Every value here is an enum name, a boolean, or a count/offset derived from the
    command. No field is a substring of the command, so this layer cannot carry a
    secret. Test ``test_layer_a_carries_no_input_derived_data`` enforces that.
    """
    detail = action.detail or ""
    offset = action.match_offset
    return {
        "operation": decision.operation.value,
        "reason": decision.reason,
        "rule_id": action.rule_id or "unknown",
        "corroborated": action.corroborated or "unknown",
        "match_offset": offset,
        "match_line": (detail.count("\n", 0, offset) + 1) if offset >= 0 else -1,
        "line_count": detail.count("\n") + 1 if detail else 0,
        "detail_len": len(detail),
        "has_heredoc": bool(_HEREDOC_START.search(detail)),
        "indirection_gate": action.indirection_gate,
    }


def layer_b(action: ClassifiedAction) -> dict[str, str]:
    """The best-effort layer: a redacted ±100 char window around the match.

    Never the whole command. ``<no-match>`` when the verdict carries no offset (the
    structural classifier does not report one), ``<redact-failed>`` when redaction
    itself raises — the window is dropped rather than emitted raw.
    """
    detail = action.detail or ""
    offset = action.match_offset
    if offset < 0 or not detail:
        return {"context_window": "<no-match>"}
    start = max(0, offset - _CONTEXT_RADIUS)
    end = min(len(detail), offset + _CONTEXT_RADIUS)
    try:
        window = redact(detail[start:end])
    except Exception:
        return {"context_window": "<redact-failed>"}
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(detail) else ""
    return {"context_window": f"{prefix}{window}{suffix}"}


def build_denial_record(decision: AllowlistDecision, action: ClassifiedAction) -> dict[str, Any]:
    """Layer A plus layer B for one denial. The only place the two are combined."""
    return {**layer_a(decision, action), **layer_b(action)}


def render_denial(record: dict[str, Any]) -> str:
    """One-line rendering of a denial record for the log sink.

    The single formatting site (spec S3) — callers pass a record, never assemble
    their own string, so the shape cannot drift between the exception path and the
    event-log path.
    """
    return " ".join(f"{k}={v!r}" for k, v in record.items())


__all__ = ["build_denial_record", "layer_a", "layer_b", "redact", "render_denial"]
