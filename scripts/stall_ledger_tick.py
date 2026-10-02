"""D-16ab stall-ledger tick CLI -- log-only evaluation of stalled units.

Spec: T-stalled-pr-has-no-detector msg-4685 §1 (``--data-dir``), msg-4697 §2 /
msg-4699 §2 (``--list-quarantined`` / ``--clear-quarantined``).

Three modes::

    # one heartbeat (scheduled by deploy/run-stall-ledger-tick.ps1, D-16c)
    uv run python scripts/stall_ledger_tick.py --data-dir ~/spirrow-mindwire-data \\
        --repo SpirrowGames/spirrow-mindwire --project spirrow-mindwire

    # show quarantined ledger records, pending clear requests and the last lock holder
    uv run python scripts/stall_ledger_tick.py --data-dir ... --list-quarantined

    # ask the next tick to clear one quarantined ledger record
    uv run python scripts/stall_ledger_tick.py --data-dir ... \\
        --clear-quarantined pr:SpirrowGames/spirrow-mindwire#206

The store path is always ``<data-dir>/state/stall-ledger.json``; nothing in Python
hard-codes it.

**Single writer.** Only a tick holding the OS lock on ``stall-ledger.lock`` writes the
store. ``--clear-quarantined`` never touches the store: it drops a request file that the
next tick applies (msg-4699 §2). ``--list-quarantined`` only reads.

**What this never touches.** It writes nothing to the digest, Discord, the chatroom or
GitHub (msg-4685 §8). It reads ``quarantine.json`` and never writes it, and it never
reads ``sweep.json``. Chatroom access goes through ``ReadOnlyMcp`` with a two-tool read
allowlist.

Output: JSON lines on stdout, ASCII-only (the repo's D-33 rule for machine-read stdout).
Exit code 0 whenever a heartbeat line was written -- including ``evaluated=false`` --
because the line itself carries the verdict; 2 on a usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from spirrow_mindwire.config import DEFAULT_DATA_DIR
from spirrow_mindwire.stall_ledger.adapters import (
    DEFAULT_FETCH_TIMEOUT,
    Adapter,
    ChatroomThreadAdapter,
    GitHubOpenPrAdapter,
    QuarantineFileAdapter,
)
from spirrow_mindwire.stall_ledger.driver import DEFAULT_T_TICK_MAX, TickPaths, run_tick
from spirrow_mindwire.stall_ledger.lock import read_payload
from spirrow_mindwire.stall_ledger.store import (
    load_store_readonly,
    read_clear_requests,
    write_clear_request,
)
from spirrow_mindwire.stall_ledger.timing import check_timing


def _parse_repo(text: str) -> tuple[str, str]:
    owner, sep, repo = text.partition("/")
    if not sep or not owner or not repo or "/" in repo:
        raise argparse.ArgumentTypeError(f"--repo expects owner/name, got {text!r}")
    return owner, repo


def _print(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")


def list_quarantined(paths: TickPaths) -> int:
    """Read-only: quarantine entries, pending requests, and the last lock holder."""
    entries, detail = load_store_readonly(paths.store)
    requests, invalid = read_clear_requests(paths.requests)
    _print(
        {
            "kind": "quarantined_list",
            "store": detail,
            "quarantined_records": entries,
            "pending_clear_requests": [
                {
                    "unit_key": r.unit_key,
                    "requested_by": r.requested_by,
                    "requested_at": r.requested_at,
                    "file": r.path.name,
                }
                for r in requests
            ],
            "invalid_clear_requests": [{"file": b.path.name, "detail": b.detail} for b in invalid],
            "lock_last_holder": read_payload(paths.lock),
        }
    )
    return 0


def clear_quarantined(paths: TickPaths, unit_key: str, requested_by: str) -> int:
    target = write_clear_request(paths.requests, unit_key, requested_by, datetime.now(UTC))
    _print(
        {
            "kind": "clear_requested",
            "key": unit_key,
            "requested_by": requested_by,
            "request_file": target.name,
            "note": "applied by the next tick; see --list-quarantined",
        }
    )
    return 0


async def _tick(args: argparse.Namespace, paths: TickPaths) -> int:
    fetch_timeout = timedelta(seconds=args.fetch_timeout_seconds)
    adapters: list[Adapter] = []
    github = None
    if args.repo:
        from spirrow_mindwire.github.client import GitHubClient

        github = GitHubClient(timeout_seconds=args.fetch_timeout_seconds)
        for owner, repo in args.repo:
            adapters.append(GitHubOpenPrAdapter(github, owner, repo, fetch_timeout=fetch_timeout))
    if args.project:
        from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp

        mcp = StreamableHttpChatroomMcp(args.url)
        for project in args.project:
            adapters.append(ChatroomThreadAdapter(mcp, project, fetch_timeout=fetch_timeout))
    adapters.append(QuarantineFileAdapter(paths.quarantine_json))
    try:
        await run_tick(
            paths=paths,
            adapters=adapters,
            t_tick_max=timedelta(seconds=args.t_tick_max_seconds),
            fetch_timeout=fetch_timeout,
        )
    finally:
        if github is not None:
            await github.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"data root; store = <data-dir>/state/stall-ledger.json (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument("--repo", action="append", type=_parse_repo, default=[])
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--url", default=None, help="magickit MCP URL (default: env/in-code)")
    parser.add_argument(
        "--t-tick-max-seconds",
        type=float,
        default=DEFAULT_T_TICK_MAX.total_seconds(),
        help="whole-tick deadline (default: stall_ledger.timing.T_TICK_MAX)",
    )
    parser.add_argument(
        "--fetch-timeout-seconds",
        type=float,
        default=DEFAULT_FETCH_TIMEOUT.total_seconds(),
        help="per-fetch timeout (default: stall_ledger.timing.FETCH_TIMEOUT)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list-quarantined", action="store_true")
    mode.add_argument("--clear-quarantined", metavar="UNIT_KEY")
    parser.add_argument("--requested-by", default=None)
    args = parser.parse_args(argv)
    # An override that leaves no room for one body fetch would stop owed fetches from
    # ever draining (msg-5747 advisory); refuse it rather than run a tick that can't
    # make progress.
    problem = check_timing(
        t_tick_max=timedelta(seconds=args.t_tick_max_seconds),
        fetch_timeout=timedelta(seconds=args.fetch_timeout_seconds),
    )
    if problem is not None:
        parser.error(problem)

    paths = TickPaths(state_dir=args.data_dir / "state")
    if args.list_quarantined:
        return list_quarantined(paths)
    if args.clear_quarantined:
        requested_by = args.requested_by or getpass.getuser()
        return clear_quarantined(paths, args.clear_quarantined, requested_by)
    return asyncio.run(_tick(args, paths))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
