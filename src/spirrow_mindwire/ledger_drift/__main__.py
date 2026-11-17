"""CLI: ``python -m spirrow_mindwire.ledger_drift`` (also ``mindwire ledger-drift``). Read only.

Modes (design v0.3, msg-9524 / msg-9526):

* ``--dry-run [--since YYYY-MM-DD] [--projects a,b] [--format json|table]`` — read and print;
  writes no file. ``--since`` is the one-off backfill (msg-9524 Obj-1-2): the window then starts
  there instead of ``window_days`` ago.
* ``--tick`` — the sweep's once-per-tick call. When ``sweep.json``'s ``ledger_drift.enabled`` is
  not ``true`` it prints ``{"enabled": false}`` and returns before any client exists. Enabled, it
  writes ``<logs_dir>/ledger_drift/<project>.json`` per project, atomically.
* ``--show`` — print what the digest would print for each project's saved result (the freshness
  contract, :func:`~.result.judge`); reads no network.

Exit ``0`` the pass ran (a partial read is ``0`` with the gap in the file: it is a result);
``1`` it could not run, or a result file could not be saved (msg-9526 Obj-2-2: a save failure
must be loud, and the scheduled log keeps the stderr line).

Who reads the output: the operator, through the digest's 台帳のずれ section or this CLI. Nothing
here posts to a chatroom thread, so there is no resolved-thread fallback to declare.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ..config import load_settings
from ..github.client import github_token
from ..magickit.client import StreamableHttpChatroomMcp
from ..magickit.read_only import ReadOnlyMcp
from ..repo_projects import RepoProjectMap, load_repo_project_map
from ..settled_close.config import sweep_projects
from .config import LedgerDriftConfig, parse_ledger_drift_config
from .reader import GITHUB_API, READ_TOOLS, read_inputs
from .result import build, digest_lines, judge, result_path, write_atomic

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


def _since(args: argparse.Namespace, config: LedgerDriftConfig, now: datetime) -> datetime:
    if args.since:
        value = datetime.fromisoformat(args.since)
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return now - timedelta(days=config.window_days)


async def _pass(
    args: argparse.Namespace,
    config: LedgerDriftConfig,
    projects: list[str],
    repo_map: RepoProjectMap,
    now: datetime,
) -> dict[str, dict[str, Any]]:
    since = _since(args, config, now)
    mcp = ReadOnlyMcp(StreamableHttpChatroomMcp(args.url), READ_TOOLS, label="ledger_drift")
    token = github_token()
    http = (
        httpx.AsyncClient(
            base_url=GITHUB_API,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=30.0,
        )
        if token
        else None
    )
    out: dict[str, dict[str, Any]] = {}
    try:
        for project in projects:
            inputs = await read_inputs(
                mcp,
                http,
                project=project,
                targets=[t for t in repo_map.targets if t.project == project],
                repo_map=repo_map,
                bases=config.bases,
                since=since,
                until=now,
            )
            out[project] = build(
                project=project,
                generated_at=now,
                since=since,
                until=now,
                sections=inputs.sections(),
            )
    finally:
        if http is not None:
            await http.aclose()
    return out


def _logs_dir(args: argparse.Namespace) -> Path:
    return Path(args.logs_dir) if args.logs_dir else load_settings().paths.logs_dir


def run(args: argparse.Namespace) -> int:
    try:
        sweep = _load_sweep(args.sweep_config)
        config = parse_ledger_drift_config(sweep)
        projects = _projects(args, sweep)
        if args.show:
            now = datetime.now(UTC)
            stale = timedelta(minutes=config.stale_after_minutes)
            for project in projects:
                judged = judge(result_path(_logs_dir(args), project), now=now, stale_after=stale)
                sys.stdout.buffer.write(
                    ("\n".join([f"[{project}]", *digest_lines(judged)]) + "\n").encode("utf-8")
                )
            return 0
        if args.tick and not config.enabled:
            print(json.dumps({"phase": "ledger_drift", "enabled": False}))
            return 0
        if not projects:
            raise ValueError("no project to read: pass --projects or a sweep.json naming some")
        repo_map = load_repo_project_map(args.sweep_config)
        now = datetime.now(UTC)
        results = asyncio.run(_pass(args, config, projects, repo_map, now))
    except Exception as exc:  # top-level guard: the sweep must not hang on us
        print(json.dumps({"phase": "ledger_drift", "error": str(exc)}, ensure_ascii=True))
        print(f"ledger_drift: pass failed: {exc!r}", file=sys.stderr)
        return 1
    if args.tick:
        failed = False
        for project, payload in results.items():
            try:
                write_atomic(result_path(_logs_dir(args), project), payload)
            except (OSError, ValueError) as exc:
                failed = True
                print(f"ledger_drift: save failed for {project}: {exc!r}", file=sys.stderr)
        print(json.dumps({"phase": "ledger_drift", "enabled": True, "projects": sorted(results)}))
        return 1 if failed else 0
    if args.format == "table":
        sys.stdout.buffer.write(render_table(results).encode("utf-8"))
    else:
        print(json.dumps(results, ensure_ascii=True, sort_keys=True))
    return 0


def render_table(results: dict[str, dict[str, Any]]) -> str:
    lines: list[str] = []
    for project, payload in sorted(results.items()):
        sections = payload["sections"]
        lines.append(
            f"[{project}] window {payload['window']['since']} .. {payload['window']['until']}"
        )
        for name, body in sections.items():
            if not body["complete"]:
                lines.append(f"  {name}: INCOMPLETE: {'; '.join(body['incomplete_reasons'])}")
                continue
            lines.append(f"  {name}: {body['count']}")
            for row in body["rows"]:
                lines.append(f"    {json.dumps(row, ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines) + "\n"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="read and print; write nothing")
    mode.add_argument("--tick", action="store_true", help="the sweep's call: writes result files")
    mode.add_argument("--show", action="store_true", help="print the saved results as the digest")
    parser.add_argument("--since", default="", help="window start (ISO date), the backfill run")
    parser.add_argument("--projects", default="", help="comma-separated (default: sweep.json's)")
    parser.add_argument("--sweep-config", type=Path, default=None, help="sweep.json")
    parser.add_argument("--logs-dir", default="", help="default: mindwire.toml's logs_dir")
    parser.add_argument("--format", choices=("json", "table"), default="json")
    parser.add_argument("--url", default=None, help="magickit MCP URL (default: in-code/env)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
