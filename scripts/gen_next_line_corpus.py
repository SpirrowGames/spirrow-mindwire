#!/usr/bin/env python
"""Rebuild ``tests/data/next_line_corpus.tsv`` from the LIVE magickit chatrooms.

Why this exists (``T-handoff-parser-markdown-tolerance`` msg-1129 §4): two rounds of this parser
shipped with green tests and the real failing shape still broken, because the shapes under test
were *imagined*. Imagined shapes only close imagined holes. So the regression corpus is harvested
from actual traffic instead: every line in every message of the named chatroom projects that
carries the ``NEXT:`` keyword, with the resolution the parser must produce for it.

Run it (needs network reach to the magickit MCP endpoint, so it is NOT part of the gate)::

    uv run python scripts/gen_next_line_corpus.py

Two reductions keep the fixture reviewable — both mechanical, neither a judgement about which
shapes matter:

1. Each line is cut to its **decision head**: the verbatim text up to and including the first word
   of the token, plus the next few characters (where the trailing decoration that broke this parser
   twice lives). The gloss after that is what the parser is *supposed* to ignore. The cut is not
   assumed to be harmless — every line is resolved both whole and cut, and any line where the two
   disagree is kept whole (and reported, because such a line would be evidence against the design).
2. Lines are de-duplicated. Handoff-shaped lines — the ones the parser must get RIGHT — are kept
   per distinct decision head, i.e. exhaustively over the shapes the chatrooms have produced.
   Prose lines are all rejected for one reason (the parser's line rule refuses them), so they are
   kept per distinct *short* token head: enough witnesses that the rule cannot be relaxed
   unnoticed, without spending a reviewer's budget on a thousand restatements of one fact. Which
   bucket a line lands in is decided by asking the parser (:func:`is_prose`), not by a second
   spelling of its rule — a second spelling is how a real handoff could be filed as prose and
   deduplicated away, i.e. how this harvester could bury the evidence it exists to produce.

The counts of what each surviving record stands for are written into the file header, so a reader
can see the corpus is a projection of ~3.5k real lines rather than a hand-picked list.

What this harvest structurally CANNOT see: a shape the parser used to handle and no longer does.
Real traffic records what people wrote, never what the code could do, so a capability removed by a
rewrite leaves no trace here — which is exactly how seven shapes were lost under a green corpus
(msg-1150 §2). ``tests/test_conductor_handoff_migration.py`` is the mechanism for that half.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from spirrow_mindwire.conductor.handoff import (  # noqa: E402
    _NEXT_LINE_RE,
    PR_REVIEW_TOKEN,
    HandoffKind,
    resolve_handoff,
)
from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp  # noqa: E402
from spirrow_mindwire.value_objects import Role  # noqa: E402

PROJECTS = ("spirrow-mindwire", "spirrow-voxelworld")
OUT_PATH = REPO_ROOT / "tests" / "data" / "next_line_corpus.tsv"

# The roster the corpus is resolved against. It is the shipped example roster
# (deploy/mindwire.toml.example) — the personas these two chatrooms actually run on. Recorded in the
# fixture header so the expectations are reproducible.
ROSTER = {"Bohr": Role.PROPOSER, "Heisenberg": Role.IMPLEMENTER, "Einstein": Role.NAYSAYER}

_KEYWORD_RE = re.compile(r"NEXT\s*[:：]", re.IGNORECASE)  # noqa: RUF001 (fullwidth colon)
_FIRST_WORD_RE = re.compile(r"^[^\w]*[\w][\w/#.:_-]*")
_PREFIX_CHARS = 12
_PROSE_KEY_CHARS = 10
_TAIL_CHARS = 6


def decision_head(line: str) -> str:
    """The line cut to the part the parser's decision can depend on (see module docstring)."""
    keyword = _KEYWORD_RE.search(line)
    if keyword is None:  # pragma: no cover - callers filter on the keyword first
        return line
    token = line[keyword.end() :]
    word = _FIRST_WORD_RE.match(token)
    cut = word.end() if word is not None else 0
    if word is not None and word.group(0).strip(" *_`").casefold() == PR_REVIEW_TOKEN:
        # the target of a pr-review handoff is the *second* word (the ref), so keep that one too
        rest = token[cut:]
        operand = _FIRST_WORD_RE.match(rest.lstrip())
        cut += len(rest) - len(rest.lstrip()) + (operand.end() if operand is not None else 0)
    start = max(0, keyword.start() - _PREFIX_CHARS)
    return line[start : keyword.end()] + token[: cut + _TAIL_CHARS]


def expectation(line: str) -> tuple[str, str]:
    """``(kind, token)`` the parser must produce for ``line``; token is "" when it carries none."""
    handoff = resolve_handoff(line, ROSTER)
    token = handoff.token if handoff.kind is not HandoffKind.ABSENT else None
    return handoff.kind.value, token or ""


def is_prose(line: str) -> bool:
    """True when the parser refuses the line outright — a sentence mentioning the keyword.

    This asks the parser's own line rule instead of restating it. The restatement it replaces
    (``re.search(r"\\w", before_the_keyword)``) had drifted from the rule in the same place the
    rule itself was wrong: Python's ``\\w`` matches ``_``, so a real ``_NEXT: human_`` handoff
    would have been read as prose, keyed as one of a thousand interchangeable rejections, and
    dropped in favour of a shorter sibling — the harvester burying the very evidence it exists to
    surface (msg-1148 §5-3). A second spelling of a rule is a second place for it to be wrong, so
    there is now only one.
    """
    return _KEYWORD_RE.search(line) is not None and _NEXT_LINE_RE.search(line) is None


async def harvest() -> tuple[list[str], Counter[str]]:
    """Every ``NEXT:``-bearing line in every message of ``PROJECTS``, plus per-project counts."""
    mcp = StreamableHttpChatroomMcp()
    lines: list[str] = []
    stats: Counter[str] = Counter()
    for project in PROJECTS:
        threads: list[dict[str, object]] = []
        offset = 0
        while True:
            page = await mcp.call_tool(
                "chatroom_list_threads", {"project": project, "limit": 200, "offset": offset}
            )
            items = page.get("items") or []
            threads.extend(items)
            offset += len(items)
            if not items or offset >= page.get("total", len(threads)):
                break
        stats[f"{project}: threads"] = len(threads)
        for thread in threads:
            body = await mcp.call_tool(
                "chatroom_get_thread",
                {"project": project, "thread_id": thread["thread_id"], "mode": "full"},
            )
            for message in body.get("messages") or []:
                stats[f"{project}: messages"] += 1
                for line in (message.get("content") or "").splitlines():
                    if _KEYWORD_RE.search(line):
                        # Tabs are the column separator; a line carrying one is stored with the tab
                        # rendered as spaces (no real handoff line has ever had one — the count is
                        # in the header so a future one is visible rather than silently mangled).
                        if "\t" in line:
                            stats[f"{project}: lines containing a tab"] += 1
                        lines.append(line.replace("\t", "    ").strip())
                        stats[f"{project}: NEXT-bearing lines"] += 1
    return lines, stats


def build(lines: list[str]) -> tuple[list[tuple[str, str, str]], Counter[str], list[str]]:
    """Reduce raw lines to corpus records. Returns (records, counts, lines-that-resist-the-cut)."""
    counts: Counter[str] = Counter()
    resisted: list[str] = []
    chosen: dict[tuple[str, str], str] = {}
    for line in lines:
        head = decision_head(line)
        if expectation(head) != expectation(line):
            head = line  # the cut changed the answer: keep the whole line and report it
            resisted.append(line)
        # handoff-shaped lines are kept per distinct head; prose is one rejection reason, so it is
        # kept per distinct short token head (the prefix only has to contain *a* word character).
        keyword = _KEYWORD_RE.search(head)
        token_head = head[keyword.end() :] if keyword is not None else head
        key = ("prose", token_head[:_PROSE_KEY_CHARS]) if is_prose(line) else ("shape", head)
        counts[f"{key[0]} lines"] += 1
        if key not in chosen or len(head) < len(chosen[key]):
            chosen[key] = head
    records = sorted((*expectation(head), head) for head in set(chosen.values()))
    counts["records"] = len(records)
    return records, counts, resisted


def render(records: list[tuple[str, str, str]], counts: Counter[str], stats: Counter[str]) -> str:
    header = [
        "# Real-traffic corpus for the conductor's NEXT-handoff parser."
        " GENERATED — do not hand-edit.",
        "# Regenerate with: uv run python scripts/gen_next_line_corpus.py",
        "#",
        "# Every NEXT:-bearing line of every message in the live chatrooms below, cut to its",
        "# decision head and de-duplicated (see scripts/gen_next_line_corpus.py for both rules).",
        "# Roster: " + ", ".join(f"{k}={v.value}" for k, v in ROSTER.items()),
        "#",
        *(f"# source  {key}: {value}" for key, value in sorted(stats.items())),
        *(f"# reduced {key}: {value}" for key, value in sorted(counts.items())),
        "#",
        "# columns: kind<TAB>token<TAB>line",
    ]
    body = [f"{kind}\t{token}\t{line}" for kind, token, line in records]
    return "\n".join([*header, *body]) + "\n"


def main() -> int:
    lines, stats = asyncio.run(harvest())
    records, counts, resisted = build(lines)
    if resisted:
        note = f"{len(resisted)} line(s) resolve differently when cut — kept whole:"
        print(note, file=sys.stderr)
        for line in resisted[:20]:
            print(f"  {line}", file=sys.stderr)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(records, counts, stats), encoding="utf-8", newline="\n")
    print(f"wrote {len(records)} records from {len(lines)} real lines -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
