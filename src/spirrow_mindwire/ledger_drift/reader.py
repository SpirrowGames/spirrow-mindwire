"""The read layer: merged PRs from GitHub, threads and tasks from magickit. Reads only.

Design v0.3 (msg-9524 / msg-9526). Every read here fails *into* the report, never past it: a
listing that breaks part-way records which input is incomplete and why (T42), and
:func:`~.detect.detect` then blanks exactly the sections that needed it.

Magickit tools are fixed to :data:`READ_TOOLS` (the ``settled_close`` pattern): the CLI wraps the
client in :class:`~spirrow_mindwire.magickit.read_only.ReadOnlyMcp`, so no write can be sent.
GitHub is read with ``GET /repos/{o}/{r}/pulls?state=closed&base=<base>`` sorted by ``updated``
descending, every page, until a page's last ``updated_at`` falls before the window: a PR merged
inside the window was updated no earlier than its merge, so nothing older can be in it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

import httpx

from ..magickit.client import MagickitMcpError
from ..pr_event_advance.runner import list_open_threads
from ..repo_projects import RepoProjectMap, RepoTarget
from ..settled_close.classify import is_human_name
from .detect import (
    COMPLETED,
    MergedPr,
    PrLink,
    Section,
    SectionResult,
    TaskInfo,
    ThreadInfo,
    detect,
    link_pr,
    thread_task_ids,
)

#: Every magickit tool ledger_drift may call (msg-9524: these four reads and nothing else).
READ_TOOLS = frozenset({"list_tasks", "get_task", "chatroom_list_threads", "chatroom_get_thread"})
GITHUB_API = "https://api.github.com"
PAGE_SIZE = 100
#: A runaway guard on the PR listing, far above any 30-day window; hitting it is incomplete.
MAX_PR_PAGES = 50


class ToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass
class Inputs:
    """What one pass read, and which inputs are partial (name -> why)."""

    prs: list[PrLink] = field(default_factory=list)
    threads: dict[tuple[str, str], ThreadInfo | None] = field(default_factory=dict)
    open_threads: list[ThreadInfo] = field(default_factory=list)
    tasks: dict[tuple[str, str], TaskInfo] = field(default_factory=dict)
    incomplete: dict[str, str] = field(default_factory=dict)

    def gap(self, name: str, why: str) -> None:
        self.incomplete[name] = (
            f"{self.incomplete[name]}; {why}" if name in self.incomplete else why
        )

    def sections(self) -> dict[Section, SectionResult]:
        return detect(
            prs=self.prs,
            threads=self.threads,
            open_threads=self.open_threads,
            tasks=self.tasks,
            incomplete=self.incomplete,
        )


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


async def list_merged_prs(
    http: httpx.AsyncClient,
    target: RepoTarget,
    *,
    base: str,
    since: datetime,
    until: datetime,
    repo_map: RepoProjectMap,
) -> tuple[list[MergedPr], str | None]:
    """Every PR merged into ``base`` of ``target`` with ``since <= merged_at <= until``.

    Returns ``(prs, None)``, or what was read so far with the reason the listing is partial.
    """
    out: list[MergedPr] = []
    for page in range(1, MAX_PR_PAGES + 1):
        try:
            resp = await http.get(
                f"/repos/{target.owner}/{target.repo}/pulls",
                params={
                    "state": "closed",
                    "base": base,
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": PAGE_SIZE,
                    "page": page,
                },
            )
        except httpx.HTTPError as exc:
            return out, f"{target.slug} {base}: page {page} failed: {exc!r}"
        if resp.status_code != 200:
            return out, f"{target.slug} {base}: page {page} failed: HTTP {resp.status_code}"
        rows = resp.json()
        if not isinstance(rows, list):
            return out, f"{target.slug} {base}: page {page} failed: not a list"
        oldest_updated: datetime | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            updated = _parse_ts(row.get("updated_at"))
            if updated is not None:
                oldest_updated = updated
            merged = _parse_ts(row.get("merged_at"))
            if merged is None or not (since <= merged <= until):
                continue
            body = row.get("body")
            out.append(
                MergedPr(
                    repo=target.slug,
                    number=int(row.get("number") or 0),
                    merged_at=merged.isoformat(),
                    base=base,
                    body=body if isinstance(body, str) else "",
                    project=repo_map.resolve(target.owner, target.repo).project,
                )
            )
        if len(rows) < PAGE_SIZE or (oldest_updated is not None and oldest_updated < since):
            return out, None
    return out, f"{target.slug} {base}: more than {MAX_PR_PAGES} pages"


async def read_thread(mcp: ToolCaller, project: str, thread_id: str) -> ThreadInfo | None:
    """One thread's metadata (``summary``). Raises on a transport failure; ``None``: not found."""
    payload = await mcp.call_tool(
        "chatroom_get_thread", {"project": project, "thread_id": thread_id, "mode": "summary"}
    )
    thread = payload.get("thread") if isinstance(payload, dict) else None
    if not isinstance(thread, dict):
        return None
    return _with_owner(ThreadInfo.from_payload(project, thread | {"thread_id": thread_id}))


def _with_owner(info: ThreadInfo) -> ThreadInfo:
    # settled_close's name check; the registry lookup (get_identity) is not on READ_TOOLS.
    kind = "human" if is_human_name(info.owner) else ("agent" if info.owner else "missing")
    return replace(info, owner_kind=kind)


async def settled_close_target(mcp: ToolCaller, info: ThreadInfo) -> bool:
    """Agent-owned with a proposer ``done`` head: settled_close's own target (a mark only)."""
    if info.owner_kind != "agent":
        return False
    try:
        payload = await mcp.call_tool(
            "chatroom_get_thread",
            {"project": info.project, "thread_id": info.thread_id, "mode": "full"},
        )
    except MagickitMcpError:
        return False
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict):
        return False
    head = messages[-1]
    disposition = head.get("disposition")
    return (
        isinstance(disposition, dict)
        and disposition.get("kind") == "done"
        and head.get("role") == "proposer"
    )


async def list_tasks(mcp: ToolCaller, project: str) -> list[TaskInfo]:
    """Every task of ``project``. Raises :class:`MagickitMcpError` on an unreadable answer."""
    payload = await mcp.call_tool("list_tasks", {"project": project})
    rows = None
    if isinstance(payload, dict):
        for key in ("tasks", "items"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        if payload.get("success") is False:
            rows = None
    if rows is None:
        raise MagickitMcpError(f"list_tasks returned no task list: {str(payload)[:200]}")
    total = payload.get("total") if isinstance(payload, dict) else None
    if isinstance(total, int) and total > len(rows):
        raise MagickitMcpError(f"list_tasks returned {len(rows)} of {total} tasks")
    tasks = [TaskInfo.from_payload(project, r) for r in rows if isinstance(r, dict)]
    return [t for t in tasks if t.task_id]


async def read_inputs(
    mcp: ToolCaller,
    http: httpx.AsyncClient | None,
    *,
    project: str,
    targets: Iterable[RepoTarget],
    repo_map: RepoProjectMap,
    bases: Iterable[str],
    since: datetime,
    until: datetime,
) -> Inputs:
    """Read everything one project's report needs. Never raises for a failed read."""
    inputs = Inputs()
    task_projects: set[str] = set()

    # Tasks of the project itself: (c) and (d) read the whole ledger.
    await _load_tasks(mcp, inputs, project, task_projects)

    # Open threads: (c) reads every one, not bounded by the window.
    try:
        ids = await list_open_threads(mcp, project)
    except MagickitMcpError as exc:
        inputs.gap("open_threads", f"chatroom_list_threads failed: {exc}")
        ids = []
    for thread_id in ids:
        try:
            info = await read_thread(mcp, project, thread_id)
        except MagickitMcpError as exc:
            inputs.gap("open_threads", f"{thread_id}: {exc}")
            continue
        if info is not None:
            inputs.open_threads.append(info)

    # PRs merged in the window, and the threads they link.
    repo_targets = list(targets)
    if http is None:
        inputs.gap("prs", "no GitHub token")
    elif not repo_targets:
        inputs.gap("prs", f"no GitHub repo for project {project} in sweep.json clones")
    else:
        for target in repo_targets:
            for base in bases:
                prs, why = await list_merged_prs(
                    http, target, base=base, since=since, until=until, repo_map=repo_map
                )
                inputs.prs.extend(link_pr(pr) for pr in prs)
                if why:
                    inputs.gap("prs", why)
    for link in inputs.prs:
        key = link.thread_key
        if key is None or key in inputs.threads:
            continue
        try:
            inputs.threads[key] = await read_thread(mcp, *key)
        except MagickitMcpError as exc:
            inputs.gap("linked_threads", f"{key[0]}/{key[1]}: {exc}")
            continue
        # A linked thread in another project resolves its tasks in that project.
        await _load_tasks(mcp, inputs, key[0], task_projects)

    # Mark the (c) candidates settled_close already handles (one full read each, few rows).
    marked: list[ThreadInfo] = []
    for info in inputs.open_threads:
        if _all_completed(info, inputs.tasks):
            info = _mark(info, await settled_close_target(mcp, info))
        marked.append(info)
    inputs.open_threads = marked
    return inputs


async def _load_tasks(mcp: ToolCaller, inputs: Inputs, project: str, done: set[str]) -> None:
    if project in done:
        return
    done.add(project)
    try:
        for task in await list_tasks(mcp, project):
            inputs.tasks[(task.project, task.task_id)] = task
    except MagickitMcpError as exc:
        inputs.gap("tasks", f"{project}: {exc}")


def _all_completed(info: ThreadInfo, tasks: Mapping[tuple[str, str], TaskInfo]) -> bool:
    ids = thread_task_ids(info)
    return bool(ids) and all(
        (t := tasks.get((info.project, i))) is not None and t.status == COMPLETED for i in ids
    )


def _mark(info: ThreadInfo, target: bool) -> ThreadInfo:
    return replace(info, settled_close_target=target)
