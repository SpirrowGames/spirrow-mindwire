"""Phase 2 of the PR-review sweep — close PR-review ledgers whose PR has ended.

**Off by default.** Without ``--enable`` this script makes no write of any kind: it runs
Phase 1's read-only sort, then prints, in order, the ledgers it *would* try this tick.
No close-capable MCP client is even built, and the state file is not written:

    uv run python scripts/pr_review_sweep_phase2.py \
        --config ~/spirrow-mindwire-data/config/pr_review_sweep.json

With ``--enable`` it closes ledgers for real, which cannot be undone. Turning it on is a
Tier-C decision taken after this script is merged (msg-6014 §5), not a default.

The rules are in :mod:`spirrow_mindwire.pr_review_sweep.phase2`. This file only wires
them up:

* The candidate rows come from Phase 1's own ``sweep_project``, loaded by path and called
  through Phase 1's read-only client, so Phase 2 sorts ledgers exactly as Phase 1 does.
* Closing goes through a second client whose allowlist is exactly
  ``chatroom_close_thread`` + ``chatroom_get_thread``
  (:data:`~spirrow_mindwire.pr_review_sweep.phase2.CLOSE_TOOLS`). It is built only when
  ``--enable`` is given.
* Attempt state lives in ``<state-dir>/<project>.json``, one file per project, written
  atomically after an enabled tick. Deleting it is harmless (every ledger reads as
  never tried).

Output is one JSON object on stdout, ASCII-only. Errors go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from spirrow_mindwire.config import DEFAULT_DATA_DIR
from spirrow_mindwire.filesystem.atomic import atomic_write_text
from spirrow_mindwire.github.client import GitHubClient
from spirrow_mindwire.magickit.client import MagickitMcpError, StreamableHttpChatroomMcp
from spirrow_mindwire.magickit.read_only import ReadOnlyMcp, ReadOnlyViolationError
from spirrow_mindwire.pr_review_sweep.config import (
    SweepConfig,
    SweepConfigError,
    load_sweep_config,
)
from spirrow_mindwire.pr_review_sweep.phase0 import Excluded
from spirrow_mindwire.pr_review_sweep.phase1 import LedgerRow
from spirrow_mindwire.pr_review_sweep.phase2 import (
    CLOSE_TOOLS,
    DEFAULT_AUTHOR,
    DEFAULT_BACKOFF_CAP_UNITS,
    DEFAULT_BACKOFF_UNIT_SECONDS,
    DEFAULT_BREAKER,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_NEEDS_HUMAN_AFTER,
    Phase2Settings,
    State,
    ToolCaller,
    dump_state,
    load_state,
    report_to_json,
    run_tick,
)

_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(errors="backslashreplace")


def _load_phase1_cli() -> ModuleType:
    """Phase 1's CLI, loaded by path: ``scripts/`` is not an importable package."""
    path = Path(__file__).resolve().parent / "pr_review_sweep_phase1.py"
    spec = importlib.util.spec_from_file_location("pr_review_sweep_phase1_cli", path)
    if spec is None or spec.loader is None:  # pragma: no cover - the file ships with this one
        raise ImportError(f"cannot load {path}")
    module = sys.modules.get(spec.name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


_PHASE1 = _load_phase1_cli()


def default_state_dir() -> Path:
    return DEFAULT_DATA_DIR / "state" / "pr_review_sweep"


def state_path(state_dir: Path, project: str) -> Path:
    return state_dir / f"{project}.json"


def read_state(state_dir: Path, projects: list[str]) -> tuple[State, list[str]]:
    """Every project's state file merged into one map. Fail-open, like :func:`load_state`."""
    state: State = {}
    warnings: list[str] = []
    for project in projects:
        path = state_path(state_dir, project)
        try:
            text: str | None = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = None
        except OSError as exc:
            warnings.append(f"{project}: cannot read state file, treated as empty: {exc}")
            text = None
        project_state, warning = load_state(project, text)
        state.update(project_state)
        if warning:
            warnings.append(warning)
    return state, warnings


def write_state(state_dir: Path, projects: list[str], state: State) -> None:
    for project in projects:
        atomic_write_text(state_path(state_dir, project), dump_state(project, state) + "\n")


async def collect_rows(
    read_mcp: Any, github: Any, config: SweepConfig
) -> tuple[list[LedgerRow], list[Excluded]]:
    rows: list[LedgerRow] = []
    excluded: list[Excluded] = []
    for entry in config.entries:
        project_rows, project_excluded = await _PHASE1.sweep_project(read_mcp, github, entry)
        rows.extend(project_rows)
        excluded.extend(project_excluded)
    return rows, excluded


async def _run(
    config: SweepConfig, url: str | None, settings: Phase2Settings, state_dir: Path
) -> dict[str, Any]:
    inner = StreamableHttpChatroomMcp(url)
    read_mcp = _PHASE1.ReadOnlyMcp(inner)
    close_mcp: ToolCaller | None = (
        ReadOnlyMcp(inner, CLOSE_TOOLS, label="Phase 2") if settings.enabled else None
    )
    projects = [e.project for e in config.entries]
    async with GitHubClient() as github:
        rows, excluded = await collect_rows(read_mcp, github, config)
    state, warnings = read_state(state_dir, projects)
    report = await run_tick(close_mcp, rows, state, datetime.now(UTC), settings)
    report.warnings.extend(warnings)
    if settings.enabled:
        write_state(state_dir, projects, report.state)
    payload = report_to_json(report)
    payload["projects"] = projects
    payload["state_dir"] = str(state_dir)
    payload["out_of_scope_count"] = len(excluded)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None, help="sweep config path")
    parser.add_argument("--url", default=None, help="magickit MCP URL (default: env/in-code)")
    parser.add_argument(
        "--state-dir", default=None, help=f"attempt state dir (default: {default_state_dir()})"
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="actually close ledgers (irreversible). Without it nothing is written.",
    )
    parser.add_argument("--author", default=DEFAULT_AUTHOR)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS, help="N")
    parser.add_argument("--breaker", type=int, default=DEFAULT_BREAKER, help="K")
    parser.add_argument(
        "--needs-human-after", type=int, default=DEFAULT_NEEDS_HUMAN_AFTER, help="R"
    )
    parser.add_argument("--backoff-cap-units", type=int, default=DEFAULT_BACKOFF_CAP_UNITS)
    parser.add_argument(
        "--backoff-unit-seconds",
        type=int,
        default=DEFAULT_BACKOFF_UNIT_SECONDS,
        help="wall-clock length of one backoff unit (msg-6017 advisory)",
    )
    return parser


def settings_from_args(args: argparse.Namespace) -> Phase2Settings:
    return Phase2Settings(
        enabled=bool(args.enable),
        max_attempts=args.max_attempts,
        breaker=args.breaker,
        needs_human_after=args.needs_human_after,
        backoff_cap_units=args.backoff_cap_units,
        backoff_unit_seconds=args.backoff_unit_seconds,
        author=args.author,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = settings_from_args(args)
    except ValueError as exc:
        print(f"pr_review_sweep_phase2: {exc}", file=sys.stderr)
        return 2
    path = Path(args.config) if args.config else _PHASE1._PHASE0.default_config_path()
    try:
        config = load_sweep_config(path)
    except SweepConfigError as exc:
        print(f"pr_review_sweep_phase2: {exc}", file=sys.stderr)
        return 2
    state_dir = Path(args.state_dir) if args.state_dir else default_state_dir()

    try:
        payload = asyncio.run(_run(config, args.url, settings, state_dir))
    except (MagickitMcpError, ReadOnlyViolationError) as exc:
        print(f"pr_review_sweep_phase2: sweep failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
