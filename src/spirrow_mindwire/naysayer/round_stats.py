"""Offline tallies for ``T-naysayer-exhaustive-first-then-scoped`` (D5 / msg-7474 Obj-2-2).

The runtime ``naysayer.round`` log line is the quick reading. This module recomputes from the posts
themselves, which also survive a lost log, and it is the only place the author's
``Obj-<round>-<n>: 採用=...`` lines meet the naysayer's arrays: those lines do not exist yet at the
moment a naysayer posts. Pure functions; ``scripts/naysayer_round_stats.py`` does the reading.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .review_rounds import count_round, facts_from_element, read_objection_block

_OBJ_LINE_RE = re.compile(r"^\s*Obj-(\d+)-(\d+):\s*採用=(min|cut|own|as-proposed)\b", re.MULTILINE)
_BACKEND_RE = re.compile(r"<!-- attest:.*?\bbackend=([\w.-]+)")
_LOG_FIELD_RE = re.compile(r"\b([a-z_]+)=(\S+)")
_LOG_COUNT_FIELDS = ("late", "late_advisory", "fix_missing", "objections_unparsed")


@dataclass(frozen=True)
class ThreadStats:
    thread: str
    rounds: int
    blocking: int
    advisory: int
    late: int
    late_advisory: int
    fix_missing: int
    objections_unparsed: int
    late_by_backend: Mapping[str, int]
    dispositions: Mapping[str, int]
    unanswered: int


def tally_thread(
    thread: str,
    posts: Sequence[tuple[str, str]],
    *,
    naysayers: frozenset[str],
    vocabulary_blocks: Mapping[str, bool],
    extra_author_text: Iterable[str] = (),
) -> ThreadStats:
    """Tally one thread's ``(author, body)`` posts, oldest first.

    An unreadable block adds to ``objections_unparsed`` and to no other count: it has an unknown
    number of objections, so it can be neither zero nor ``unanswered``. ``extra_author_text`` is
    PR body text, where a pull request's ``Obj-`` lines live (msg-7467 Obj-1). A line naming no
    objection that exists (a wrong round or position) answers nothing.
    """
    folded = {n.casefold() for n in naysayers}
    expected: set[tuple[int, int]] = set()
    answered: dict[tuple[int, int], str] = {}
    totals: Counter[str] = Counter()
    late_by_backend: Counter[str] = Counter()
    round_number = 0
    author_text: list[str] = list(extra_author_text)

    for author, body in posts:
        if author.casefold() not in folded:
            author_text.append(body)
            continue
        round_number += 1
        block = read_objection_block(body)
        if block.elements is None:
            totals["objections_unparsed"] += 1
            continue
        counts = count_round(
            (facts_from_element(e, vocabulary_blocks) for e in block.elements),
            round_number=round_number,
        )
        totals.update(
            blocking=counts.blocking,
            advisory=counts.advisory,
            late=counts.late,
            late_advisory=counts.late_advisory,
            fix_missing=counts.fix_missing,
        )
        backend = _BACKEND_RE.search(body)
        if counts.late:
            late_by_backend[backend.group(1) if backend else "unknown"] += counts.late
        expected.update((round_number, i + 1) for i in range(len(block.elements)))

    for text in author_text:
        for found in _OBJ_LINE_RE.finditer(text):
            key = (int(found.group(1)), int(found.group(2)))
            if key in expected:
                answered[key] = found.group(3)

    return ThreadStats(
        thread=thread,
        rounds=round_number,
        blocking=totals["blocking"],
        advisory=totals["advisory"],
        late=totals["late"],
        late_advisory=totals["late_advisory"],
        fix_missing=totals["fix_missing"],
        objections_unparsed=totals["objections_unparsed"],
        late_by_backend=dict(late_by_backend),
        dispositions=dict(Counter(answered.values())),
        unanswered=len(expected) - len(answered),
    )


def median_rounds(stats: Iterable[ThreadStats]) -> float | None:
    """The before/after comparison metric named by msg-7405 D5."""
    rounds = [s.rounds for s in stats]
    return statistics.median(rounds) if rounds else None


def tally_log_lines(lines: Iterable[str]) -> dict[tuple[str, str], Counter[str]]:
    """Sum ``naysayer.round`` lines by ``(thread-or-pr, backend)``.

    Output tokens are only in the log, so this is where the second comparison metric comes from.
    ``-`` is "not counted" and adds nothing; ``scope=full-fallback`` is counted because msg-7467
    Obj-2 revisits the rebase decision on that number.
    """
    tally: dict[tuple[str, str], Counter[str]] = {}
    for line in lines:
        if "naysayer.round " not in line:
            continue
        fields = dict(_LOG_FIELD_RE.findall(line.split("naysayer.round ", 1)[1]))
        target = fields.get("thread", "-")
        if target == "-":
            target = fields.get("pr", "-")
        counter = tally.setdefault((target, fields.get("backend", "-")), Counter())
        counter["posts"] += 1
        if fields.get("scope") == "full-fallback":
            counter["full_fallback"] += 1
        for name in (*_LOG_COUNT_FIELDS, "out_tokens"):
            value = fields.get(name, "-")
            if value.isdigit():
                counter[name] += int(value)
    return tally


__all__ = ["ThreadStats", "median_rounds", "tally_log_lines", "tally_thread"]
