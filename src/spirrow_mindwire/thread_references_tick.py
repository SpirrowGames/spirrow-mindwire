"""The once-per-tick read behind :mod:`spirrow_mindwire.thread_relations` (msg-9729).

Thread ``T-thread-relations-unused-so-parallel-rule-cannot-be-checked``. Reading every thread of a
project, closed ones included, is what makes the reverse direction visible (msg-9544 §1), and it
is the expensive part, so it happens here, once per sweep tick, and every dispatch of that tick
reads the result from a file (``scripts/thread_references.py`` writes it, the conductor reads it).

Cost is bounded two ways:

* **Cache keyed on (status, last_msg_id).** The listing carries both, so a thread whose key is
  unchanged since the last tick is not fetched again. Only threads that moved are read.
* **``max_fetch`` per tick.** The first run on a host with hundreds of threads would otherwise
  read them all at once. A thread not yet fetched is left out of this tick's index and picked up
  on a later tick; the report says how many are still pending, so a partial view is never shown
  as complete.

Read-only: :data:`READ_ONLY_TOOLS` is the whole allowlist, enforced by
:class:`~spirrow_mindwire.magickit.read_only.ReadOnlyMcp` in the CLI. Nothing here writes to any
thread (msg-9544 §3).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .chatroom.status import FINISHED_STATUSES
from .magickit.client import MagickitMcpError
from .thread_relations import ThreadSnapshot, snapshot_from_json, snapshot_of, snapshot_to_json
from .unregistered_threads import LIVE_STATUSES

#: The only chatroom tools this read may call.
READ_ONLY_TOOLS = frozenset({"chatroom_list_threads", "chatroom_get_thread"})

#: Every status, so closed threads are read too (msg-9544 §1: closed included).
ALL_STATUSES: tuple[str, ...] = tuple(sorted(set(LIVE_STATUSES) | {"parked"} | FINISHED_STATUSES))

#: Default per-tick fetch bound. See the module docstring.
DEFAULT_MAX_FETCH = 150

_PAGE = 200

CACHE_FILENAME = "thread-references-cache.json"
INDEX_FILENAME = "thread-references-index.json"


class ToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class ListedItem:
    thread_id: str
    status: str
    last_msg_id: str | None
    title: str
    tags: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.status}|{self.last_msg_id or ''}"


@dataclass
class ProjectRead:
    """One project's result for this tick. ``snapshots`` excludes threads not fetched yet."""

    project: str
    snapshots: list[ThreadSnapshot] = field(default_factory=list)
    listed: list[ListedItem] = field(default_factory=list)
    fetched: int = 0
    pending: int = 0
    fetch_errors: int = 0
    error: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "listed": len(self.listed) if self.error is None else None,
            "known": len(self.snapshots),
            "fetched": self.fetched,
            "pending": self.pending,
            "fetch_errors": self.fetch_errors,
            "error": self.error,
        }


def _tags(value: Any) -> tuple[str, ...]:
    return tuple(t for t in value if isinstance(t, str)) if isinstance(value, list) else ()


async def list_threads(mcp: ToolCaller, project: str) -> list[ListedItem]:
    """Every thread of every status in ``project``, paged on the server's own cursor."""
    out: list[ListedItem] = []
    offset = 0
    while True:
        page = await mcp.call_tool(
            "chatroom_list_threads",
            {
                "project": project,
                "status_filter": list(ALL_STATUSES),
                "limit": _PAGE,
                "offset": offset,
            },
        )
        items = page.get("items") if isinstance(page, dict) else None
        if not isinstance(items, list) or not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("thread_id") or "").strip()
            if not tid:
                continue
            last = item.get("last_msg_id")
            out.append(
                ListedItem(
                    thread_id=tid,
                    status=str(item.get("status") or "").strip(),
                    last_msg_id=last if isinstance(last, str) and last else None,
                    title=str(item.get("title") or ""),
                    tags=_tags(item.get("tags")),
                )
            )
        offset += len(items)
        total = page.get("total") if isinstance(page, dict) else None
        if isinstance(total, int) and offset >= total:
            break
    return out


async def refresh_project(
    mcp: ToolCaller,
    project: str,
    cache: dict[str, Any],
    *,
    max_fetch: int = DEFAULT_MAX_FETCH,
) -> ProjectRead:
    """Bring ``cache[project]`` up to date with at most ``max_fetch`` reads; return the view.

    ``cache[project]`` maps thread id → ``{"key": ..., "snapshot": ...}``. Threads that are no
    longer listed are dropped. A failed per-thread read keeps the previous snapshot (if any) and is
    counted; a failed listing leaves the cache untouched and sets ``error``.
    """
    read = ProjectRead(project=project)
    try:
        read.listed = await list_threads(mcp, project)
    except MagickitMcpError as exc:
        read.error = f"chatroom_list_threads failed: {exc}"
        return read
    old = cache.get(project)
    old_entries: dict[str, Any] = old if isinstance(old, dict) else {}
    new_entries: dict[str, Any] = {}
    budget = max_fetch
    for item in read.listed:
        entry = old_entries.get(item.thread_id)
        cached = snapshot_from_json(entry.get("snapshot")) if isinstance(entry, dict) else None
        fresh = isinstance(entry, dict) and entry.get("key") == item.key and cached is not None
        if not fresh and budget > 0:
            budget -= 1
            try:
                result = await mcp.call_tool(
                    "chatroom_get_thread",
                    {"project": project, "thread_id": item.thread_id, "mode": "full"},
                )
            except MagickitMcpError:
                result = None
            snap = snapshot_of(result)
            if snap is not None:
                # The listing's tags win: a full read may omit them.
                snap = ThreadSnapshot(
                    thread_id=snap.thread_id,
                    status=snap.status or item.status,
                    messages=snap.messages,
                    affects_threads=snap.affects_threads,
                    tags=item.tags or snap.tags,
                    last_msg_id=item.last_msg_id,
                )
                read.fetched += 1
                new_entries[item.thread_id] = {"key": item.key, "snapshot": snapshot_to_json(snap)}
                read.snapshots.append(snapshot_from_json(snapshot_to_json(snap)) or snap)
                continue
            read.fetch_errors += 1
        elif not fresh:
            read.pending += 1
        if cached is not None:
            new_entries[item.thread_id] = entry
            read.snapshots.append(cached)
    cache[project] = new_entries
    return read


def load_json(path: Path) -> Any:
    """The parsed file, or ``None`` when it is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write via a sibling temp file and ``os.replace``, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


__all__ = [
    "ALL_STATUSES",
    "CACHE_FILENAME",
    "DEFAULT_MAX_FETCH",
    "INDEX_FILENAME",
    "READ_ONLY_TOOLS",
    "ListedItem",
    "ProjectRead",
    "list_threads",
    "load_json",
    "refresh_project",
    "write_json_atomic",
]
