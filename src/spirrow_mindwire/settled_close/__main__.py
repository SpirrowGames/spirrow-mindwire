"""CLI: ``python -m spirrow_mindwire.settled_close`` (also ``mindwire settled-close``).

Two modes (T-done-disposition-auto-close msg-8455 §2 6-2 / 6-3):

* ``--dry-run [--include-legacy] [--projects a,b] [--format json|table]`` — read only, whatever
  the configuration says. The MCP client is wrapped in a read-only allowlist
  (:data:`~.runner.READ_TOOLS`), so a close cannot be sent from this mode even by mistake.
* ``--tick --projects a,b`` — the sweep's once-per-tick call. When ``sweep.json``'s
  ``settled_close.enabled`` is not ``true`` it prints ``{"enabled": false, ...}`` and returns
  **before any MCP client exists**: a disabled tick makes no call at all. Enabled, it closes
  through an allowlist of :data:`~.runner.CLOSE_TOOLS`.

Projects default to every project ``sweep.json`` names (:func:`~.config.sweep_projects`). That is
not the whole chatroom; a full inventory passes ``--projects`` with the dashboard's set.

Output is ASCII on stdout. Exit ``0`` the pass ran; ``1`` it could not run (bad configuration,
empty project list, a crash); nothing is closed in the ``1`` case.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import load_settings
from ..magickit.client import StreamableHttpChatroomMcp
from ..magickit.read_only import ReadOnlyMcp
from .config import SettledCloseConfig, parse_settled_close_config, sweep_projects
from .runner import CLOSE_TOOLS, READ_TOOLS, PassReport, render_table, run_pass

_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(errors="backslashreplace")


def _load_sweep(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    sweep = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(sweep, dict):
        raise ValueError("sweep.json must be an object")
    return sweep


def _projects(args: argparse.Namespace, sweep: dict[str, Any]) -> list[str]:
    if args.projects:
        return sorted({p.strip() for p in args.projects.split(",") if p.strip()})
    return sweep_projects(sweep)


async def _pass(
    args: argparse.Namespace, config: SettledCloseConfig, projects: list[str], enabled: bool
) -> PassReport:
    inner = StreamableHttpChatroomMcp(args.url)
    mcp = ReadOnlyMcp(inner, CLOSE_TOOLS if enabled else READ_TOOLS, label="settled_close")
    return await run_pass(
        mcp,
        projects=projects,
        config=config,
        enabled=enabled,
        include_legacy=args.include_legacy,
        roster=dict(load_settings().conductor.roster),
        now=datetime.now(UTC),
    )


def run(args: argparse.Namespace) -> int:
    try:
        sweep = _load_sweep(args.sweep_config)
        config = parse_settled_close_config(sweep)
        if args.tick and not config.enabled:
            # Returns before any MCP client is built: a disabled tick calls nothing.
            print(json.dumps({"phase": "settled_close", "enabled": False, "close_calls": 0}))
            return 0
        projects = _projects(args, sweep)
        if not projects:
            raise ValueError("no project to read: pass --projects or a sweep.json naming some")
        report = asyncio.run(_pass(args, config, projects, enabled=bool(args.tick)))
    except Exception as exc:  # top-level guard: the sweep must not hang on us
        print(json.dumps({"phase": "settled_close", "error": str(exc)}, ensure_ascii=True))
        print(f"settled_close: pass failed: {exc!r}", file=sys.stderr)
        return 1
    if args.format == "table":
        sys.stdout.buffer.write(render_table(report).encode("utf-8"))
    else:
        print(json.dumps(report.as_dict(), ensure_ascii=True, sort_keys=True))
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="read only, whatever the config says")
    mode.add_argument(
        "--tick", action="store_true", help="the sweep's call: nothing at all unless enabled"
    )
    parser.add_argument("--include-legacy", action="store_true", help="sort null-disposition heads")
    parser.add_argument("--projects", default="", help="comma-separated (default: sweep.json's)")
    parser.add_argument("--sweep-config", type=Path, default=None, help="sweep.json")
    parser.add_argument("--format", choices=("json", "table"), default="json")
    parser.add_argument("--url", default=None, help="magickit MCP URL (default: in-code/env)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
