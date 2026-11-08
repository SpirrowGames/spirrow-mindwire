"""Bounded implementer self-continuation — the ``CONTINUE:`` line (T-implementer-self-continuation).

Design: Bohr msg-7879 (D-1'), revised msg-7881 (D-1''), msg-7883 (D-1'''), msg-7885 (D-1''''),
msg-7887 (D-1'''''); naysayer go from Einstein (the reply to msg-7887, empty objections array,
handed to the implementer).

An implementer that cannot finish inside one turn budget ends its report with::

    CONTINUE: <what the next turn does, one line>
    NEXT: <its own persona>

A self-handoff is otherwise a ``SELF_HANDOFF`` stop (the adapter's self-filter drops a delivery
whose author is the recipient). The continuation does NOT re-dispatch the implementer's own
report: the conductor posts a relay under :data:`~.core.CONDUCTOR_RELAY_AUTHOR`::

    [continue <n>/<cap>] <CONTINUE body>
    NEXT: <implementer persona>

and dispatches the implementer on that relay.

``n`` is never read from the report. The conductor derives it from the thread (D-1'' / Obj-1-2):
the number of implementer CONTINUE reports since the most recent handoff into the implementer
from an entry author (human, naysayer, PR-gate relay). Any other message in between — a proposer,
naysayer or human speaking, a non-CONTINUE implementer report, any other relay — breaks the chain
and the self-handoff falls back to the ordinary ``SELF_HANDOFF`` stop. That is the interrupt.

Everything here is pure: no I/O, no roster ownership. The caller classifies each message into a
:class:`Link` and supplies the clone check.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ..routing import ContinueVerdict, continuation_admissible
from ..source_marker import (
    ATTESTATION_MARKER_PREFIX,
    ATTESTATION_MARKER_SUFFIX,
    SOURCE_MARKER_PREFIX,
    SOURCE_MARKER_SUFFIX,
)

__all__ = [
    "DEFAULT_CONTINUE_CAP",
    "EMPTY_BODY_PLACEHOLDER",
    "ContinuePlan",
    "ContinueVerdict",
    "Link",
    "chain_count",
    "continuation_admissible",
    "find_continue_declaration",
    "is_continue_relay_body",
    "relay_chain_count",
    "render_relay",
]

#: Default number of continuations per chain (msg-7879 §2-2: 4 turns x 1800 s = 2 h).
DEFAULT_CONTINUE_CAP = 3

#: Written into the relay when the CONTINUE body is empty (msg-7883: an empty body still continues).
EMPTY_BODY_PLACEHOLDER = "（理由なし）"  # noqa: RUF001 (fullwidth parentheses intentional)

_CONTINUE_RE = re.compile(r"\ACONTINUE:(?P<body>.*)\Z")
# The known control lines the trailing block may contain (msg-7883 / msg-7885).
_CONTROL_RE = re.compile(r"\A(?:TIER-C-CHECK|TIER-C|ASK):")
# ``NEXT:`` as the existing handoff parser reads it: the token is the line's own head.
_NEXT_RE = re.compile(r"\A(?:\*\*|`|_)*NEXT(?:\*\*|`|_)*[:：]")  # noqa: RUF001

# A Markdown code-fence line: a run of 3+ backticks or 3+ tildes, then an optional info string.
# A backtick fence's info string may not contain a backtick (CommonMark), so ```` ``x`` ```` is
# not a fence line.
_FENCE_RE = re.compile(r"\A(?P<run>`{3,}(?=[^`]*\Z)|~{3,})(?P<info>.*)\Z")

_RELAY_RE = re.compile(r"\A\[continue (?P<n>\d+)/(?P<cap>\d+)\]")


def find_continue_declaration(body: str) -> str | None:
    """The ``CONTINUE:`` body declared in ``body``'s trailing block, or ``None`` if none is.

    The trailing block (msg-7885, start position fixed by msg-7887):

    * ``i_next`` is the last line that is neither blank nor a harness marker, and it must be a
      ``NEXT:`` line. Trailing blank lines after it are ignored, as the handoff parser ignores
      them. So are the ``<!-- source: … -->`` / ``<!-- attest: … -->`` lines the dispatcher
      appends after every reply (:func:`~spirrow_mindwire.source_marker.append_markers`): the
      stored body never ends on the author's ``NEXT:``, and without this skip no production
      report could declare a continuation (spirrow-mindwire msg-8109 / msg-8151, both stopped
      as SELF_HANDOFF with no ``conductor continue`` log line).
    * The walk starts at ``i_next - 1``. The ``NEXT:`` line itself is neither in the block nor a
      reason to stop the walk.
    * Each line, leading whitespace stripped, is in the block if it is blank, a known control line
      (``TIER-C-CHECK:`` / ``TIER-C:`` / ``ASK:``) or starts with ``CONTINUE:``. The first line
      that is none of these ends the walk and is not in the block (body prose, a closing code
      fence). Reaching the top of the report also ends it.
    * If the block holds one or more ``CONTINUE:`` lines the one nearest ``NEXT:`` wins; its body
      is returned stripped (possibly ``""``). A ``CONTINUE:`` outside the block — in prose or
      inside a code fence — is not a declaration.
    * If a code fence is still open at ``NEXT:`` (see :func:`_fence_open_before`), the trailing
      lines are fenced example text and nothing is declared.
    """
    lines = body.splitlines()
    i_next = len(lines) - 1
    while i_next >= 0 and (not lines[i_next].strip() or _is_harness_marker(lines[i_next])):
        i_next -= 1
    if i_next < 0 or not _NEXT_RE.match(lines[i_next].strip()):
        return None
    # PR #519 gate round 3 Obj-2 / round 4 Obj-1: a fence still open above ``NEXT:`` means the
    # ``NEXT:`` and anything in the trailing block are example text, not a declaration.
    if _fence_open_before(lines, i_next):
        return None
    for i in range(i_next - 1, -1, -1):
        line = lines[i].strip()
        if not line or _CONTROL_RE.match(line):
            continue
        m = _CONTINUE_RE.match(line)
        if m is None:
            return None
        # Walking upward, the first CONTINUE: met is the one nearest NEXT:.
        return m.group("body").strip()
    return None


def _is_harness_marker(line: str) -> bool:
    """Is ``line`` one of the marker lines the dispatcher appends after a reply's handoff?"""
    stripped = line.strip()
    return (
        stripped.startswith(SOURCE_MARKER_PREFIX) and stripped.endswith(SOURCE_MARKER_SUFFIX)
    ) or (
        stripped.startswith(ATTESTATION_MARKER_PREFIX)
        and stripped.endswith(ATTESTATION_MARKER_SUFFIX)
    )


def _fence_open_before(lines: list[str], end: int) -> bool:
    """Is a Markdown code fence still open after ``lines[:end]``?

    Tracks the opening fence as CommonMark does (PR #519 gate round 4 Obj-1), instead of counting
    fence lines: a fence opens on a run of 3+ backticks or 3+ tildes and closes only on a line that
    is a run of the *same* character, at least as long as the opening run, with nothing after it
    but whitespace. Inside a backtick fence a ``~~~`` line, or a shorter backtick run, is content.
    Leading whitespace is stripped, as everywhere else in this parser.
    """
    open_char = ""
    open_len = 0
    for raw in lines[:end]:
        m = _FENCE_RE.match(raw.strip())
        if m is None:
            continue
        run = m.group("run")
        if not open_char:
            open_char, open_len = run[0], len(run)
        elif run[0] == open_char and len(run) >= open_len and not m.group("info").strip():
            open_char, open_len = "", 0
    return bool(open_char)


def is_continue_relay_body(body: str) -> bool:
    """Does ``body`` open with the ``[continue n/cap]`` relay tag this module renders?"""
    return _RELAY_RE.match(body.lstrip()) is not None


def render_relay(*, n: int, cap: int, continue_body: str, implementer: str) -> str:
    """The relay the conductor posts; its last line routes the turn to the implementer (Obj-1-1)."""
    text = continue_body.strip() or EMPTY_BODY_PLACEHOLDER
    return f"[continue {n}/{cap}] {text}\nNEXT: {implementer}"


class Link(StrEnum):
    """How one thread message takes part in a continuation chain (caller-classified)."""

    #: An implementer report whose trailing block declares ``CONTINUE:`` and hands to itself.
    CONTINUE_REPORT = "continue_report"
    #: A conductor relay carrying the ``[continue n/cap]`` tag.
    CONTINUE_RELAY = "continue_relay"
    #: A handoff into the implementer from an author guard (i) lets through (human, naysayer,
    #: PR-gate relay). The implementer only ran on it if guard (i) honoured it.
    ENTRY = "entry"
    #: Anything else. Breaks the chain.
    OTHER = "other"


def chain_count(links: Sequence[Link]) -> int | None:
    """``n`` for a head that is a CONTINUE report, or ``None`` when there is no valid chain.

    ``links`` is the thread oldest-first; the head is ``links[-1]``. Walking back from the head,
    the chain must alternate report, relay, report, relay, ... and end on an :attr:`Link.ENTRY`
    directly above the oldest report. ``n`` is the number of reports in the chain, the head's
    included — the first continuation is ``n = 1``.
    """
    if not links or links[-1] is not Link.CONTINUE_REPORT:
        return None
    n = 0
    expect = Link.CONTINUE_REPORT
    for link in reversed(links):
        if link is expect:
            if link is Link.CONTINUE_REPORT:
                n += 1
                expect = Link.CONTINUE_RELAY
            else:
                expect = Link.CONTINUE_REPORT
            continue
        if link is Link.ENTRY and expect is Link.CONTINUE_RELAY:
            return n
        return None
    return None


def relay_chain_count(links: Sequence[Link]) -> int | None:
    """``n`` already spent for a head that is a CONTINUE relay (carve-out ④), or ``None``.

    The relay is admissible as a guard-(i) carve-out only if the message above it is a report
    whose own chain is valid; the relay then carries that report's ``n``.
    """
    if len(links) < 2 or links[-1] is not Link.CONTINUE_RELAY:
        return None
    return chain_count(links[:-1])


@dataclass(frozen=True)
class ContinuePlan:
    """An admitted continuation: what the conductor posts and whom it dispatches."""

    n: int
    cap: int
    continue_body: str
    implementer: str

    def relay(self) -> str:
        return render_relay(
            n=self.n, cap=self.cap, continue_body=self.continue_body, implementer=self.implementer
        )
