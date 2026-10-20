"""Guard (i) G1 — the Tier-C segment scan, and the recorder's ``TIER-C-RESOLVED:`` line.

Thread ``T-guard-i-latches-on-discharged-tierc-line`` (operator msg-7182, Bohr msg-7202 /
msg-7204, Einstein msg-7203 / its endorsement of msg-7204). The production fault: spirrow-lexora
``T-streaming-ledger-row`` msg-880 — an attested naysayer proceed under ``run`` — was redirected by
guard (i) because G1 was still latched by ``TIER-C: irreversible`` lines (msg-751 / msg-754) whose
decision the human had already given OUTSIDE the chatroom (recorded by the operator in msg-874).
Before this module the human's own post was the only way to unlatch G1, so an out-of-chat decision
recorded by anyone else could never discharge a declaration.

What this module adds, and nothing more:

* **The scan** (:func:`scan_tier_c_segment`) — the G1 rule the conductor has always applied
  (newest → oldest, stop at the most recent human-authored message, every ``TIER-C: <label>``
  line in between latches), now returning WHICH declarations latch instead of a bare bool, so
  the guard-(i) relay can name them (msg-7204 B1: report every open declaration, not the first).
* **The resolution line** — ``TIER-C-RESOLVED: <msg-id> decided-by=<human identity>
  on=YYYY-MM-DD``, one id per line (msg-7204 A1), honoured ONLY when the posting author is in
  ``[conductor] tier_c_recorders``. A resolution discharges exactly the declaration it names, and
  only if it was posted in a message NEWER than that declaration (the scan runs newest → oldest
  and checks a message's declarations before collecting its own resolutions, so a message cannot
  resolve itself). A new ``TIER-C:`` line after the resolution latches G1 again.

Forgery (msg-7202 §2 (a)): an agent persona — proposer, naysayer, implementer — writing the line
discharges nothing, however it is worded, because its author is not a recorder (the conductor also
refuses, at construction, a recorder list that names a roster persona). This is the same
author-string environment trust as :func:`~.human_identity.is_human_identity` (D-3): noise
reduction, not authentication; the authoritative Tier-C guard stays the human's manual ``main``
merge. The default recorder list is empty, which is byte-for-byte the pre-change behaviour.

Fail direction: everything that does not parse closes nothing it should not. A recorder line that
starts with the keyword but does not match the whole grammar is ``malformed`` and discharges
nothing; a resolution whose declaration is not in the segment (beyond the human boundary, or a
typo'd id) is ``unmatched`` and discharges nothing (msg-7204 B2) — both are reported so the
operator can see why the latch did not lift. No path here raises on chatroom text.

The function is pure (messages in, :class:`SegmentScan` out) so the read-only census
(``scripts/guard_i_latch_census.py``, msg-7202 §2 (c)) imports this exact rule instead of
re-spelling it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from .handoff import declares_tier_c

__all__ = [
    "TIER_C_RESOLVED_KEYWORD",
    "SegmentScan",
    "TierCResolution",
    "is_recorder",
    "parse_resolution_lines",
    "scan_tier_c_segment",
]

TIER_C_RESOLVED_KEYWORD = "TIER-C-RESOLVED"
"""The recorder's discharge keyword (msg-7202 §2 (a), one id per line per msg-7204 A1)."""

# A line that *tries* to be a resolution: keyword (any case) then a colon. Anything that matches
# this but not :data:`_RESOLVED_RE` is malformed — reported, never honoured.
_RESOLVED_ATTEMPT_RE = re.compile(r"\A\s*TIER-C-RESOLVED\s*:", re.IGNORECASE)
# The whole-line grammar. Strict on purpose: this is the side that OPENS the gate.
_RESOLVED_RE = re.compile(
    r"\A\s*TIER-C-RESOLVED: (?P<msg_id>msg-\d+) decided-by=(?P<by>\S+) "
    r"on=(?P<on>\d{4}-\d{2}-\d{2})\s*\Z"
)


@dataclass(frozen=True)
class TierCResolution:
    """One well-formed ``TIER-C-RESOLVED:`` line."""

    target_msg_id: str
    decided_by: str
    on: date


@dataclass(frozen=True)
class SegmentScan:
    """What G1 saw in the segment since the human last spoke.

    * ``open_declarations`` — msg ids whose ``TIER-C:`` line still latches G1, oldest first.
      G1 is closed iff this is non-empty.
    * ``resolved_declarations`` — declarations discharged by a recorder's resolution line.
    * ``boundary_msg_id`` — the human message the scan stopped at (``None``: no human message, the
      whole thread was scanned).
    * ``unmatched`` — resolution targets that named no declaration inside the segment (beyond the
      human boundary, a typo, a message with no ``TIER-C:`` line). Discharge nothing.
    * ``malformed`` — msg ids of recorder posts carrying a keyword line that failed the grammar
      (including ``decided-by`` not being the human identity, or an impossible date).
    * ``ignored_non_recorder`` — msg ids of posts by NON-recorders carrying a keyword line. Shown so
      a misconfigured ``tier_c_recorders`` (or an agent trying) is visible, not silent.
    """

    open_declarations: tuple[str, ...]
    resolved_declarations: tuple[str, ...]
    boundary_msg_id: str | None
    unmatched: tuple[str, ...]
    malformed: tuple[str, ...]
    ignored_non_recorder: tuple[str, ...]

    @property
    def declares_tier_c(self) -> bool:
        """G1 closed?"""
        return bool(self.open_declarations)


def is_recorder(author: str, recorders: Collection[str]) -> bool:
    """Is ``author`` a configured Tier-C recorder? Case-insensitive, like the human test."""
    folded = author.casefold()
    return bool(folded) and any(folded == r.casefold() for r in recorders if r)


def parse_resolution_lines(
    body: str, *, is_human: Callable[[str], bool]
) -> tuple[list[TierCResolution], bool]:
    """``(resolutions, any_malformed)`` for every keyword line in ``body``.

    A line is a resolution only if it matches the whole grammar, its date is a real calendar date,
    and ``decided-by`` is the human identity (``is_human``). Anything else that starts with the
    keyword sets ``any_malformed`` and contributes nothing.
    """
    found: list[TierCResolution] = []
    malformed = False
    for line in body.splitlines():
        if not _RESOLVED_ATTEMPT_RE.match(line):
            continue
        match = _RESOLVED_RE.match(line)
        if match is None or not is_human(match.group("by")):
            malformed = True
            continue
        try:
            on = date.fromisoformat(match.group("on"))
        except ValueError:
            malformed = True
            continue
        found.append(TierCResolution(match.group("msg_id"), match.group("by"), on))
    return found, malformed


def _has_resolution_attempt(body: str) -> bool:
    return any(_RESOLVED_ATTEMPT_RE.match(line) for line in body.splitlines())


def scan_tier_c_segment(
    messages: Sequence[dict[str, Any]],
    *,
    is_human: Callable[[str], bool],
    recorders: Collection[str] = (),
) -> SegmentScan:
    """Run G1 over ``messages`` (oldest first, as the chatroom returns them).

    Walk newest → oldest. At the first human-authored message, stop: that is the only reset
    boundary (msg-4858 §2), unchanged. For every other message, first test its ``TIER-C:`` lines
    against the resolutions collected from NEWER messages, then collect its own resolutions (if
    its author is a recorder). With ``recorders`` empty this is exactly the previous rule.
    """
    pending: dict[str, None] = {}  # resolution targets seen so far (newer posts), insertion-ordered
    matched: set[str] = set()
    open_newest_first: list[str] = []
    resolved_newest_first: list[str] = []
    malformed: list[str] = []
    ignored: list[str] = []
    boundary: str | None = None
    for msg in reversed(messages):
        author = str(msg.get("author") or "")
        msg_id = str(msg.get("msg_id") or "")
        body = str(msg.get("content") or "")
        if is_human(author):
            boundary = msg_id or None
            break
        if declares_tier_c(body):
            if msg_id and msg_id in pending:
                matched.add(msg_id)
                resolved_newest_first.append(msg_id)
            else:
                open_newest_first.append(msg_id)
        if not _has_resolution_attempt(body):
            continue
        if not is_recorder(author, recorders):
            ignored.append(msg_id)
            continue
        resolutions, bad = parse_resolution_lines(body, is_human=is_human)
        if bad:
            malformed.append(msg_id)
        for res in resolutions:
            pending.setdefault(res.target_msg_id, None)
    return SegmentScan(
        open_declarations=tuple(reversed(open_newest_first)),
        resolved_declarations=tuple(reversed(resolved_newest_first)),
        boundary_msg_id=boundary,
        unmatched=tuple(t for t in pending if t not in matched),
        malformed=tuple(reversed(malformed)),
        ignored_non_recorder=tuple(reversed(ignored)),
    )
