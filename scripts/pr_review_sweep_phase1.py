"""Phase 1 of the PR-review sweep — say which PR-review ledgers Phase 2 would close.

Read-only. This script closes nothing, posts nothing, and calls no ledger. It sorts every
in-scope ``T-pr-review-*`` ledger by its PR's state and prints the result:

    uv run python scripts/pr_review_sweep_phase1.py \
        --config ~/spirrow-mindwire-data/config/pr_review_sweep.json

The classification is in :mod:`spirrow_mindwire.pr_review_sweep.phase1`. This file only
collects the facts, and it collects them through the same readers Phase 0 uses:

* chatroom reads go through Phase 0's :class:`ReadOnlyMcp`, re-bound here to Phase 1's own
  two-tool allowlist. A write tool raises at the call instead of reaching the network.
* PR state comes from mindwire's own :class:`GitHubClient`, not from magickit. The
  chatroom listing still goes through the magickit MCP endpoint, as in Phase 0; what
  msg-4753 found broken on ``:8117`` is magickit's own GitHub check inside a close,
  which Phase 1 never asks for.

The S-pre intake filter runs before any GitHub call, exactly as in Phase 0. An
``UNPARSEABLE`` id costs no GitHub call either: there is no PR to ask about.

For a terminal PR the script runs one windowed event query (``since = closed_at``) to fill
the ``post_terminal_messages`` column. That column is reported only; Phase 1 does not
branch on it (msg-5808 §2 removed Phase 0's S1 as a gate).

Output is one JSON object on stdout, ASCII-only. Errors go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from spirrow_mindwire.github.client import GitHubClient, PrRef, PrResolution, PrState
from spirrow_mindwire.magickit.client import MagickitMcpError, StreamableHttpChatroomMcp
from spirrow_mindwire.magickit.read_only import ReadOnlyMcp as _SharedReadOnlyMcp
from spirrow_mindwire.magickit.read_only import ReadOnlyViolationError
from spirrow_mindwire.pr_review_sweep.config import (
    ProjectEntry,
    SweepConfig,
    SweepConfigError,
    load_sweep_config,
)
from spirrow_mindwire.pr_review_sweep.phase0 import Excluded, intake_exclusion_reason
from spirrow_mindwire.pr_review_sweep.phase1 import (
    LEDGER_ID_PREFIX,
    LedgerRow,
    build_report,
    classify_ledger,
    report_to_json,
    unparseable_row,
)

_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(errors="backslashreplace")


def _load_phase0_cli() -> ModuleType:
    """Phase 0's CLI, loaded by path: ``scripts/`` is not an importable package.

    Loaded so its paged readers are shared, not copied. Two copies of a paging loop is
    how the two ``ReadOnlyMcp`` copies once came about (``magickit/read_only.py``).
    """
    path = Path(__file__).resolve().parent / "pr_review_sweep_phase0.py"
    spec = importlib.util.spec_from_file_location("pr_review_sweep_phase0_cli", path)
    if spec is None or spec.loader is None:  # pragma: no cover - the file ships with this one
        raise ImportError(f"cannot load {path}")
    module = sys.modules.get(spec.name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


_PHASE0 = _load_phase0_cli()

#: Every chatroom tool Phase 1 may call. Both are reads. Phase 0's third tool
#: (``chatroom_get_thread``) is not needed: S1 (ii) is not a gate here.
READ_ONLY_TOOLS = frozenset({"chatroom_list_threads", "chatroom_list_events"})

Phase1WriteAttemptedError = ReadOnlyViolationError


class ToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class PrStateReader(Protocol):
    async def fetch_pr_state(self, pr: PrRef) -> PrState: ...


class ReadOnlyMcp(_SharedReadOnlyMcp):
    """The shared allowlist wrapper, bound to Phase 1's allowlist and label."""

    def __init__(self, inner: ToolCaller) -> None:
        super().__init__(inner, READ_ONLY_TOOLS, label="Phase 1")


async def sweep_project(
    mcp: ReadOnlyMcp, github: PrStateReader, entry: ProjectEntry
) -> tuple[list[LedgerRow], list[Excluded]]:
    """Classify every ``T-pr-review-*`` thread in one project."""
    rows: list[LedgerRow] = []
    excluded: list[Excluded] = []
    for thread in await _PHASE0._list_threads(mcp, entry.project):
        thread_id = str(thread.get("thread_id") or "")
        if not thread_id.startswith(LEDGER_ID_PREFIX):
            continue
        status = str(thread.get("status") or "")

        # S-pre first, so a finished thread costs nothing and is never listed as leftover.
        out_of_scope = intake_exclusion_reason(status)
        if out_of_scope is not None:
            excluded.append(Excluded(thread_id, status, out_of_scope))
            continue

        number = entry.pr_number_for_thread(thread_id)
        if number is None:
            rows.append(unparseable_row(thread_id, entry.project, status))
            continue

        pr = await github.fetch_pr_state(PrRef(entry.owner, entry.repo, number))
        post_terminal: int | None = None
        if pr.resolution is PrResolution.CLOSED and pr.closed_at is not None:
            stamps = await _PHASE0._post_message_times(mcp, entry.project, thread_id, pr.closed_at)
            post_terminal = len(stamps)
        rows.append(
            classify_ledger(
                thread_id, entry.project, status, pr, post_terminal_messages=post_terminal
            )
        )
    return rows, excluded


async def _run(config: SweepConfig, url: str | None) -> dict[str, Any]:
    mcp = ReadOnlyMcp(StreamableHttpChatroomMcp(url))
    rows: list[LedgerRow] = []
    excluded: list[Excluded] = []
    async with GitHubClient() as github:
        for entry in config.entries:
            project_rows, project_excluded = await sweep_project(mcp, github, entry)
            rows.extend(project_rows)
            excluded.extend(project_excluded)
    payload = report_to_json(build_report(rows, out_of_scope=excluded))
    payload["projects"] = [e.project for e in config.entries]
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=None,
        help=f"sweep config path (default: {_PHASE0.default_config_path()})",
    )
    parser.add_argument("--url", default=None, help="magickit MCP URL (default: env/in-code)")
    args = parser.parse_args()

    path = Path(args.config) if args.config else _PHASE0.default_config_path()
    try:
        config = load_sweep_config(path)
    except SweepConfigError as exc:
        print(f"pr_review_sweep_phase1: {exc}", file=sys.stderr)
        return 2

    try:
        payload = asyncio.run(_run(config, args.url))
    except (MagickitMcpError, Phase1WriteAttemptedError) as exc:
        print(f"pr_review_sweep_phase1: sweep failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
