"""CLI: ``python -m spirrow_mindwire.park_wake --project <p> [--sweep-config <path>]``.

Prints ONE JSON object on stdout, ASCII-only so the PowerShell wrapper's ``ConvertFrom-Json``
reads it under any console codepage (the rule ``pr_event_advance`` follows).

Exit codes: ``0`` the tick ran (threads may still have been skipped — see ``errors`` / ``held``);
``1`` the tick could not run at all (the roster did not load, the thread listing failed, or a
crash). Nothing is written in the ``1`` case. The wrapper is fail-open on this probe: a broken
park-wake tick must not stop the main sweep.

The roster is required, unlike 1b's: without it every wake would read as an unknown name, so
every ``blocked-on`` line would classify as unclassified and no park could wake. A tick that
cannot load it refuses to run rather than report that.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from ..config import load_settings
from ..github.client import GitHubClient
from ..magickit.client import StreamableHttpChatroomMcp
from ..pr_event_advance.__main__ import registered_threads
from .runner import run_tick

_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(errors="backslashreplace")


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    roster = dict(load_settings().conductor.roster)
    if not roster:
        raise RuntimeError("conductor.roster is empty: no wake persona could resolve")
    async with GitHubClient() as gh:
        report = await run_tick(
            mcp=StreamableHttpChatroomMcp(args.url),
            gh=gh,
            project=args.project,
            roster=roster,
            registered=registered_threads(args.sweep_config, args.project),
        )
    return report.as_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--url", default=None, help="magickit MCP URL (default: in-code/env)")
    parser.add_argument("--sweep-config", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        out = asyncio.run(_main(args))
    except Exception as exc:  # top-level guard — the sweep must not hang on us
        print(json.dumps({"project": args.project, "error": str(exc)}, ensure_ascii=True))
        print(f"park_wake: tick failed: {exc!r}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
