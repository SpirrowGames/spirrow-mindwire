"""Self-negation detector for ``NEXT: human`` escalations.

Thread: T-tierc-overpass-self-contradicting-escalations.

Spec: Bohr msg-6664 §2 (measurement A), revised by msg-6666 Objection 1 (whole body, no Markdown
interpretation), summarised in msg-6668; approved by Einstein msg-6667 / msg-6669 and the
hand-off to the implementer.

**What it looks for.** An escalation whose own text says that the escalation is unnecessary
("This escalation is premature", 「人の判断は不要」) while it still ends in ``NEXT: human``.

**What it reads (msg-6666 Objection 1).** The **whole body**. The only lines skipped are the ones
decided line by line, without parsing Markdown:

* a line that starts (column 0) with ``NEXT:``, ``TIER-C:``, ``RETRY:`` or ``TIER-C-CHECK:``;
* a line holding only an HTML comment (``<!-- … -->``, surrounding whitespace allowed).

Quotes (``>``), code spans and fenced / indented code are **not** removed. A hit inside them is a
false positive that the measurement counts; the adoption rule (msg-6666 §4: zero hits on genuine
rows) then rejects the detector. The detector is not made smarter to avoid them.

**The patterns are fixed here (msg-6664 §2) and are not extended after looking at the corpus.**
Implementer's choices, made before any measurement run and recorded in ``report.md``: matching
is case-insensitive (``re.IGNORECASE``; it changes nothing for the Japanese patterns), and each
pattern is applied to one line at a time, so a phrase broken across two lines is not matched.

:func:`looks_quoted_or_fenced` is **display only** (msg-6666 Objection 1, last bullet): it lets a
human reading the hit table see whether a hit sits in a quote or in a code block. No decision
reads it.

**Not wired into the conductor.** This module is used only by ``scripts/tierc_casebook.py``.
Whether a log-only check enters the conductor is decided by ``report.md`` (msg-6666 §4) and would
be a separate PR.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

SELFNEG_PATTERNS: tuple[tuple[str, str], ...] = (
    ("en_escalation_is_premature", r"escalation is premature"),
    ("en_premature_escalation", r"premature escalation"),
    ("en_not_need_human", r"(do|does) not need (a )?(human|Takahito)"),
    ("en_no_human_decision_needed", r"no human (decision|judg(e)?ment) (is )?(needed|required)"),
    ("en_not_tier_c", r"not (a )?Tier-C"),
    ("ja_human_judgement_unneeded", r"(人|Takahito)の判断は(不要|要らない|いらない)"),
    ("ja_escalation_unneeded", r"エスカレーションは(不要|時期尚早)"),
    ("ja_not_tier_c", r"Tier-C (ではない|に当たらない)"),
)
"""msg-6664 §2, in that order. ``(id, regex)``; the id is what the hit table shows."""

_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (pid, re.compile(rx, re.IGNORECASE)) for pid, rx in SELFNEG_PATTERNS
)

EXCLUDED_LINE_PREFIXES: tuple[str, ...] = ("NEXT:", "TIER-C:", "RETRY:", "TIER-C-CHECK:")
"""msg-6666 Objection 1: lines starting with these (column 0) are not searched."""

_COMMENT_ONLY = re.compile(r"^\s*(?:<!--(?:(?!-->).)*-->\s*)+$")
"""A line made only of one or more complete HTML comments. Each comment stops at its own
``-->``, so text between two comments (``<!-- a --> text <!-- b -->``) keeps the line searched."""


@dataclass(frozen=True)
class SelfNegationHit:
    """One pattern match. ``line_no`` is 1-based in the body as given."""

    pattern: str
    line_no: int
    match: str
    line: str


def is_excluded_line(line: str) -> bool:
    """True for a handoff / label line (column 0) or a line that is only an HTML comment."""
    return line.startswith(EXCLUDED_LINE_PREFIXES) or bool(_COMMENT_ONLY.match(line))


def detect_self_negation(body: str) -> list[SelfNegationHit]:
    """Every pattern match in ``body`` outside the excluded lines, in line then pattern order.

    One hit per (line, pattern): a line matching two patterns gives two hits; the same pattern
    twice on one line gives one. Pure: no I/O, no state."""
    hits: list[SelfNegationHit] = []
    for i, line in enumerate(body.splitlines(), start=1):
        if is_excluded_line(line):
            continue
        for pid, rx in _COMPILED:
            m = rx.search(line)
            if m is not None:
                hits.append(SelfNegationHit(pattern=pid, line_no=i, match=m.group(0), line=line))
    return hits


_FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def looks_quoted_or_fenced(lines: Sequence[str], line_no: int) -> str:
    """**Display only.** A rough guess for the human reading the hit table:
    ``"quote"`` (the line starts with ``>``), ``"fence"`` (an odd number of fence lines above
    it), ``"indent"`` (4+ leading spaces), else ``""``. Never used by any decision."""
    line = lines[line_no - 1]
    if line.lstrip().startswith(">"):
        return "quote"
    fences = sum(1 for prev in lines[: line_no - 1] if _FENCE.match(prev))
    if fences % 2 == 1:
        return "fence"
    if line.startswith("    "):
        return "indent"
    return ""


__all__ = [
    "EXCLUDED_LINE_PREFIXES",
    "SELFNEG_PATTERNS",
    "SelfNegationHit",
    "detect_self_negation",
    "is_excluded_line",
    "looks_quoted_or_fenced",
]
