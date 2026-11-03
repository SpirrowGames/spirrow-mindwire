"""Read-only tally of naysayer review rounds (T-naysayer-exhaustive-first-then-scoped D5).

Counts per thread, and ``LATE`` by backend, recomputed from the posts: rounds, blocking / advisory,
``late``, ``late_advisory``, ``fix_missing``, ``objections_unparsed``, and the ``採用=``
distribution with ``unanswered``. ``--log`` also sums the runtime ``naysayer.round`` lines, the
only source of output tokens. Run it over the design threads before the change to get the
baseline, with the same command.

Usage::

    uv run python scripts/naysayer_round_stats.py --project spirrow-mindwire \\
        --thread T-spawn-via-claude-bg --naysayer Einstein

    # A PR: the gate's ledger thread, plus the PR body that carries the Obj- lines.
    uv run python scripts/naysayer_round_stats.py --project spirrow-mindwire \\
        --thread T-pr-review-spirrow-mindwire-123 --naysayer pr-gate \\
        --pr SpirrowGames/spirrow-mindwire#123

    uv run python scripts/naysayer_round_stats.py --log path/to/conductor.log

``--naysayer`` is the post author to count as the naysayer (repeatable). ``--pr`` requires the
``gh`` CLI. It changes nothing: it calls ``chatroom_get_thread`` and ``gh pr view`` only.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
from pathlib import Path

from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp
from spirrow_mindwire.naysayer.principles import objection_classes
from spirrow_mindwire.naysayer.round_stats import median_rounds, tally_log_lines, tally_thread


def _pr_body(ref: str) -> str:
    repo, number = ref.split("#", 1)
    return subprocess.run(
        ["gh", "pr", "view", number, "--repo", repo, "--json", "body", "--jq", ".body"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


async def _threads(args: argparse.Namespace) -> None:
    mcp = StreamableHttpChatroomMcp()
    vocabulary = {name: entry.blocks for name, entry in objection_classes().items()}
    extra = [_pr_body(ref) for ref in args.pr]
    stats = []
    for thread in args.thread:
        result = await mcp.call_tool(
            "chatroom_get_thread",
            {"project": args.project, "thread_id": thread, "mode": "full"},
        )
        posts = [(str(m.get("author", "")), str(m.get("content", ""))) for m in result["messages"]]
        stat = tally_thread(
            thread,
            posts,
            naysayers=frozenset(args.naysayer),
            vocabulary_blocks=vocabulary,
            extra_author_text=extra,
        )
        stats.append(stat)
        print(stat)
    print(f"median rounds: {median_rounds(stats)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project")
    parser.add_argument("--thread", action="append", default=[])
    parser.add_argument("--naysayer", action="append", default=[])
    parser.add_argument("--pr", action="append", default=[], help="owner/repo#n")
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()
    if args.thread:
        if not args.project or not args.naysayer:
            parser.error("--thread needs --project and --naysayer")
        asyncio.run(_threads(args))
    if args.log:
        with args.log.open(encoding="utf-8", errors="replace") as lines:
            for (target, backend), counts in sorted(tally_log_lines(lines).items()):
                print(target, backend, dict(counts))
    if not args.thread and not args.log:
        parser.error("give --thread or --log")


if __name__ == "__main__":
    main()
