"""Cross-thread reference facts, once per tick (thread-relations, msg-9729 D-2 / D-3 / D-4).

Two modes, both read-only against the chatroom (``chatroom_list_threads`` / ``chatroom_get_thread``
only, enforced by :class:`~spirrow_mindwire.magickit.read_only.ReadOnlyMcp`). Neither writes to
any thread.

Tick mode (default; run by ``deploy/run-conductor-scheduled.ps1`` once per tick)
-------------------------------------------------------------------------------

::

    uv run python scripts/thread_references.py --sweep-config <sweep.json> [--state-dir <dir>]

Refreshes ``<state-dir>/thread-references-cache.json`` (see
:mod:`spirrow_mindwire.thread_references_tick`), writes the D-2 index
``<state-dir>/thread-references-index.json`` the conductor reads at dispatch, and prints one ASCII
JSON line::

    {"projects": [{"project": ..., "listed": N, "known": N, "fetched": N, "pending": N,
                   "fetch_errors": N, "stale_threads": [...], "complete": true, "error": null}],
     "digest_lines": ["T-a の PR o/r#7 merged → 参照のある open スレッド: T-b"],
     "any_error": false}

``digest_lines`` (D-3) covers merges reported in the last ``--digest-hours`` (default 24). A
project with ``complete: false`` had threads it could not read this tick; the sweep shows that as
a warning next to the merge lines, and the D-2 notice carries the same warning (PR #593 gate
Obj-1). A project whose listing failed keeps its previous index entry WITH that entry's own
``observed_at`` — it is never restamped as observed now — and reports ``error`` (Obj-2).

Candidates mode (D-4, and the D-1 thread-opening step)
------------------------------------------------------

::

    uv run python scripts/thread_references.py --candidates --project <p> [--thread <T-id>]

Prints the project's OPEN threads (id, title, tags) and, for ``--thread``, the reference
candidates (shared ``T###`` tag, ``関連:`` / ``Related:`` lines, existing references) and whether
the thread already carries ``Related: none``. Without ``--thread`` it prints candidates for every
open thread. Output only: the owner decides and writes the declaration by hand (msg-9729 D-4).

``--thread`` is read first, ahead of the fetch budget, and the refreshed cache is saved, so a
repeated run keeps making progress on a large project instead of re-reading the same prefix
(PR #593 gate Obj-4). ``complete: false`` means some threads were not read yet, so candidates
from them can be missing: run again until it is ``true`` before concluding ``Related: none``.
The cache is shared with the tick; both write it atomically and a lost update only costs a
re-read, because every entry is re-validated against the listing's (status, last_msg_id).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from spirrow_mindwire.config import DEFAULT_DATA_DIR
from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp
from spirrow_mindwire.magickit.read_only import ReadOnlyMcp, ReadOnlyViolationError
from spirrow_mindwire.thread_references_tick import (
    CACHE_FILENAME,
    DEFAULT_MAX_FETCH,
    INDEX_FILENAME,
    READ_ONLY_TOOLS,
    ProjectRead,
    ToolCaller,
    load_json,
    refresh_project,
    write_json_atomic,
)
from spirrow_mindwire.thread_relations import (
    INDEX_SCHEMA,
    ProjectView,
    build_notice_index,
    build_project_entry,
    collect_edges,
    digest_merged_lines,
    reference_candidates,
    related_none_msg,
)
from spirrow_mindwire.unregistered_threads import UnregisteredThreadsError, load_registered


def _read_only(inner: ToolCaller) -> ReadOnlyMcp:
    return ReadOnlyMcp(inner, READ_ONLY_TOOLS, label="thread-references")


async def run_tick(
    mcp: ToolCaller,
    projects: list[str],
    state_dir: Path,
    *,
    max_fetch: int,
    digest_hours: float,
    now: datetime,
) -> dict[str, Any]:
    cache_path = state_dir / CACHE_FILENAME
    index_path = state_dir / INDEX_FILENAME
    raw_cache = load_json(cache_path)
    cache: dict[str, Any] = raw_cache if isinstance(raw_cache, dict) else {}
    reads: list[ProjectRead] = []
    for project in projects:
        reads.append(await refresh_project(mcp, project, cache, max_fetch=max_fetch))
    write_json_atomic(cache_path, cache)

    good = {r.project: r.snapshots for r in reads if r.error is None}
    old_index = load_json(index_path)
    old_projects = (
        old_index.get("projects")
        if isinstance(old_index, dict) and old_index.get("schema") == INDEX_SCHEMA
        else None
    )
    entries: dict[str, Any] = {}
    for r in reads:
        if r.error is None:
            entries[r.project] = build_project_entry(
                ProjectView(
                    snapshots=r.snapshots,
                    observed_at=now,
                    pending=r.pending,
                    fetch_errors=r.fetch_errors,
                    stale=frozenset(r.stale_threads),
                )
            )
        elif isinstance(old_projects, dict) and isinstance(old_projects.get(r.project), dict):
            # Carried over as-is: its own observed_at still says when it was actually seen.
            entries[r.project] = old_projects[r.project]
    write_json_atomic(index_path, build_notice_index(entries))

    since = now - timedelta(hours=digest_hours)
    lines: list[str] = []
    for project, snaps in good.items():
        lines.extend(
            f"{project}: {line}"
            for line in digest_merged_lines(snaps, collect_edges(snaps), since=since)
        )
    return {
        "projects": [r.as_json() for r in reads],
        "digest_lines": lines,
        "any_error": any(r.error is not None for r in reads),
    }


async def run_candidates(
    mcp: ToolCaller, project: str, thread: str | None, state_dir: Path, *, max_fetch: int
) -> dict[str, Any]:
    cache_path = state_dir / CACHE_FILENAME
    raw_cache = load_json(cache_path)
    cache: dict[str, Any] = raw_cache if isinstance(raw_cache, dict) else {}
    read = await refresh_project(mcp, project, cache, max_fetch=max_fetch, priority=thread)
    if read.error is not None:
        return {"project": project, "error": read.error}
    write_json_atomic(cache_path, cache)
    snaps = read.snapshots
    edges = collect_edges(snaps)
    by_id = {s.thread_id: s for s in snaps}
    open_items = [i for i in read.listed if i.thread_id in by_id and by_id[i.thread_id].is_open]
    targets = [thread] if thread else [i.thread_id for i in open_items]
    stale = set(read.stale_threads)
    return {
        "project": project,
        "error": None,
        "complete": read.complete,
        "pending": read.pending,
        "fetch_errors": read.fetch_errors,
        "stale_threads": list(read.stale_threads),
        "open_threads": [
            {"thread_id": i.thread_id, "title": i.title, "tags": list(i.tags)} for i in open_items
        ],
        "candidates": [
            {
                "thread_id": t,
                # False: this thread itself is unread or only known from an older snapshot.
                "read": t in by_id and t not in stale,
                "related_none_msg": related_none_msg(by_id[t]) if t in by_id else None,
                "candidates": [
                    {"thread_id": c.other, "reasons": list(c.reasons), "referenced": c.referenced}
                    for c in reference_candidates(snaps, edges, t)
                ],
            }
            for t in targets
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep-config", default=None)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--url", default=None, help="magickit MCP URL (default: env/in-code)")
    parser.add_argument("--max-fetch", type=int, default=DEFAULT_MAX_FETCH)
    parser.add_argument("--digest-hours", type=float, default=24.0)
    parser.add_argument("--candidates", action="store_true")
    parser.add_argument("--project", default=None)
    parser.add_argument("--thread", default=None)
    args = parser.parse_args()

    state_dir = Path(args.state_dir) if args.state_dir else DEFAULT_DATA_DIR / "state"
    mcp = _read_only(StreamableHttpChatroomMcp(args.url))
    try:
        if args.candidates:
            if not args.project:
                print("thread_references: --candidates needs --project", file=sys.stderr)
                return 2
            payload = asyncio.run(
                run_candidates(mcp, args.project, args.thread, state_dir, max_fetch=args.max_fetch)
            )
        else:
            sweep = (
                Path(args.sweep_config)
                if args.sweep_config
                else DEFAULT_DATA_DIR / "config" / "sweep.json"
            )
            try:
                projects = list(load_registered(sweep).projects)
            except UnregisteredThreadsError as exc:
                print(f"thread_references: {exc}", file=sys.stderr)
                return 2
            payload = asyncio.run(
                run_tick(
                    mcp,
                    projects,
                    state_dir,
                    max_fetch=args.max_fetch,
                    digest_hours=args.digest_hours,
                    now=datetime.now(UTC),
                )
            )
    except ReadOnlyViolationError as exc:
        print(f"thread_references: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defence in depth
        print(f"thread_references: run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
