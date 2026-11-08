"""CLI: ``python -m spirrow_mindwire.pr_event_advance.auto_gate``.

One invocation per sweep tick, naming every project whose HOLD check passed, so "at most 3 fires
per tick" counts the tick and not each project.

Prints ONE ASCII JSON object on stdout. Exit ``0``: the pass ran, or stopped at the cap. Exit
``1``: it could not run (bad configuration, a crash). The wrapper is fail-open on this probe.

``sweep.json`` key ``auto_gate`` (all optional)::

    {"mode": "dry-run" | "on", "auto_gate_daily_cap": 30, "gate_bases": ["main", "develop"]}

Absent means dry-run (msg-7483 D4). Repos come from ``clones``: each clone's ``origin`` remote.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ...config import load_settings
from ...github.client import GitHubClient
from ...magickit.client import StreamableHttpChatroomMcp
from ...repo_projects import RepoProjectMap, build_repo_project_map
from .runner import (
    DEFAULT_DAILY_CAP,
    DEFAULT_GATE_BASES,
    AutoGateConfig,
    DailyCounterStore,
    PassReport,
    RepoTarget,
    jst_date,
    run_pass,
)


class AutoGateConfigError(ValueError):
    """``sweep.json`` cannot be used. Reported; nothing is posted."""


def parse_auto_gate_config(sweep: dict[str, Any]) -> AutoGateConfig:
    raw = sweep.get("auto_gate")
    if raw is None:
        return AutoGateConfig()
    if not isinstance(raw, dict):
        raise AutoGateConfigError("auto_gate must be an object")
    mode = raw.get("mode", "dry-run")
    if mode not in ("dry-run", "on"):
        raise AutoGateConfigError(f"auto_gate.mode must be 'dry-run' or 'on', got {mode!r}")
    cap = raw.get("auto_gate_daily_cap", DEFAULT_DAILY_CAP)
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
        raise AutoGateConfigError(f"auto_gate_daily_cap must be an integer >= 0, got {cap!r}")
    bases = raw.get("gate_bases", list(DEFAULT_GATE_BASES))
    if (
        not isinstance(bases, list)
        or not bases
        or not all(isinstance(base, str) and base for base in bases)
    ):
        raise AutoGateConfigError(f"gate_bases must be a non-empty string list, got {bases!r}")
    return AutoGateConfig(live=mode == "on", daily_cap=cap, gate_bases=tuple(bases))


def resolve_targets(
    repo_map: RepoProjectMap, projects: list[str]
) -> tuple[list[RepoTarget], list[dict[str, str]]]:
    """Each requested project's repo, from the shared ``clones`` table (msg-8032 §1).

    One bad clone costs only that project here; for bare-id resolution it makes the whole table
    incomplete (:meth:`RepoProjectMap.resolve`).
    """
    by_project = {target.project: target for target in repo_map.targets}
    targets: list[RepoTarget] = []
    errors: list[dict[str, str]] = []
    for project in projects:
        target = by_project.get(project)
        if target is None:
            errors.append(
                {"project": project, "error": repo_map.errors.get(project, "no clones entry")}
            )
            continue
        targets.append(target)
    return targets, errors


def known_projects(sweep: dict[str, Any]) -> frozenset[str]:
    """Every project ``clones`` names, held or not: what a qualified ``Thread:`` may point at."""
    clones = sweep.get("clones")
    return (
        frozenset(k for k in clones if isinstance(k, str))
        if isinstance(clones, dict)
        else frozenset()
    )


def cap_digest_line(report: PassReport, now: datetime) -> str | None:
    """The digest's line for a capped day: today's or yesterday's, as the digest is daily."""
    if report.capped_on is None:
        return None
    today = date.fromisoformat(jst_date(now))
    if report.capped_on not in (today.isoformat(), (today - timedelta(days=1)).isoformat()):
        return None
    return f"auto-gate: {report.capped_on} に日次上限へ到達（{report.cap} 件）"  # noqa: RUF001


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    sweep = json.loads(args.sweep_config.read_text(encoding="utf-8-sig"))
    if not isinstance(sweep, dict):
        raise AutoGateConfigError("sweep.json must be an object")
    config = parse_auto_gate_config(sweep)
    repo_map = build_repo_project_map(sweep.get("clones"))
    targets, target_errors = resolve_targets(
        repo_map, sorted({p for p in args.projects.split(",") if p})
    )
    now = datetime.now(UTC)
    settings = load_settings()
    # The same roster park-wake reads (park_wake/__main__.py). An empty one is passed on, not
    # raised: 1c then holds every tail in an accepted terminal form (roster-unavailable) and
    # still judges the rest (msg-8218 §2b-2).
    roster = dict(settings.conductor.roster)
    async with GitHubClient() as gh:
        report = await run_pass(
            mcp=StreamableHttpChatroomMcp(args.url),
            gh=gh,
            targets=targets,
            config=config,
            store=DailyCounterStore(args.state_file),
            review_login=settings.naysayer_gating.review_login,
            now=now,
            known_projects=known_projects(sweep),
            repo_map=repo_map,
            roster=roster,
        )
    out = report.as_dict()
    if not repo_map.complete:
        out["repo_map_unresolved"] = sorted(repo_map.unresolved)
    if target_errors:
        out["target_errors"] = target_errors
    line = cap_digest_line(report, now)
    if line is not None:
        out["cap_line"] = line
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects", required=True, help="comma-separated")
    parser.add_argument("--sweep-config", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--url", default=None, help="magickit MCP URL (default: in-code/env)")
    args = parser.parse_args(argv)
    try:
        out = asyncio.run(_main(args))
    except Exception as exc:  # top-level guard: the sweep must not hang on us
        print(json.dumps({"error": str(exc)}, ensure_ascii=True))
        print(f"auto_gate: pass failed: {exc!r}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
