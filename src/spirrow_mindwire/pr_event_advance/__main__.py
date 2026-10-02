"""CLI: ``python -m spirrow_mindwire.pr_event_advance --project <p> [--sweep-config <path>]``.

Prints ONE JSON object on stdout (ASCII-only, so the PowerShell wrapper's ``ConvertFrom-Json``
reads it under any console codepage — the same rule as ``scripts/gate_bootstrap_tick.py``).

Exit codes: ``0`` the tick ran (individual threads may still have been skipped — see
``outcomes``); ``1`` the tick could not run at all (thread listing failed, or a crash). The
wrapper is fail-open on this probe: a broken 1b tick must not stop the main sweep.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from ..conductor.roster import RoleResolutionError, derive_identity_by_role
from ..config import load_settings
from ..github.client import GitHubClient
from ..magickit.client import StreamableHttpChatroomMcp
from ..value_objects import Role
from .runner import run_tick

_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(errors="backslashreplace")


def resolve_proposer() -> tuple[str | None, str | None]:
    """The roster's single proposer persona, or ``(None, why)``. Never raises."""
    try:
        roster = load_settings().conductor.roster
        return derive_identity_by_role(roster, Role.PROPOSER), None
    except RoleResolutionError as exc:
        return None, str(exc)
    except Exception as exc:  # a config that cannot load is "roster unresolved", reported
        return None, f"settings unreadable: {exc}"


def registered_threads(path: Path | None, project: str) -> frozenset[str] | None:
    """Thread ids the sweep list holds for ``project``; ``None`` when not known."""
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(candidates, list):
        return None
    return frozenset(
        c["thread_id"]
        for c in candidates
        if isinstance(c, dict)
        and c.get("project") == project
        and isinstance(c.get("thread_id"), str)
    )


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    proposer, roster_error = resolve_proposer()
    async with GitHubClient() as gh:
        report = await run_tick(
            mcp=StreamableHttpChatroomMcp(args.url),
            gh=gh,
            project=args.project,
            proposer=proposer,
            registered=registered_threads(args.sweep_config, args.project),
        )
    out = report.as_dict()
    if roster_error is not None:
        out["roster_error"] = roster_error
    return out


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
        print(f"pr_event_advance: tick failed: {exc!r}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
