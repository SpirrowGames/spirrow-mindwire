"""Pure detection: merged PRs + threads + tasks in, drift sections out. No I/O.

Design v0.3 (msg-9524 / msg-9526). The sections and what bounds each of them:

* ``not_started`` — (a) a task linked to a PR merged into develop inside the window, whose
  ledger status is still ``not_started``.
* ``pr_record`` — (a') a task linked to such a PR whose notes never mention the PR number.
  This is a *record check* candidate only; it says nothing about whether the task is complete
  (msg-9526 Obj-2-3).
* ``unlinked_prs`` — a PR in the window whose thread / task could not be resolved, with why.
* ``close_candidates`` — (c) an open thread with one or more linked tasks, every one of them
  ``completed``. Over every open thread, not bounded by the window (Obj-2-1). A completed task
  does not prove the thread's job is done, so this is a candidate list, never a close.
* ``stale_blocked_by`` — (d) a ``blocked_by`` entry that points at a ``completed`` task. Over the
  whole ledger, not bounded by the window (Obj-2-1). Detection only: removing it is magickit's.

Linking: PR -> thread uses :func:`resolve_thread_link` (the PR body's ``Thread:`` /
``Closes-thread:`` lines, de-duplicated per ``(project, id)``), so one PR yields at most one
thread. Thread -> task uses ``related_tasks`` when non-empty, else the tags matching
``^T\\d+$`` exactly, resolved inside the thread's own project. The thread id is never parsed
for a task number (``T-t232-phase-b`` was about T243).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..pr_event_advance.auto_gate.decide import resolve_thread_link

TASK_TAG_RE = re.compile(r"^T\d+$")
COMPLETED = "completed"
NOT_STARTED = "not_started"


class Section(StrEnum):
    NOT_STARTED = "not_started"
    PR_RECORD = "pr_record"
    UNLINKED_PRS = "unlinked_prs"
    CLOSE_CANDIDATES = "close_candidates"
    STALE_BLOCKED_BY = "stale_blocked_by"


#: The sections bounded by the PR window (msg-9526 Obj-2-1); the others read everything.
WINDOWED_SECTIONS = frozenset({Section.NOT_STARTED, Section.PR_RECORD, Section.UNLINKED_PRS})

#: Which inputs each section needs to have been read completely.
NEEDS: Mapping[Section, frozenset[str]] = {
    Section.NOT_STARTED: frozenset({"prs", "linked_threads", "tasks"}),
    Section.PR_RECORD: frozenset({"prs", "linked_threads", "tasks"}),
    Section.UNLINKED_PRS: frozenset({"prs", "linked_threads"}),
    Section.CLOSE_CANDIDATES: frozenset({"open_threads", "tasks"}),
    Section.STALE_BLOCKED_BY: frozenset({"tasks"}),
}


class UnlinkedReason(StrEnum):
    # The three resolve_thread_link outcomes (msg-9524 Obj-1-1), spelled as it spells them.
    NO_THREAD = "no-thread"
    AMBIGUOUS = "ambiguous"
    THREAD_LINE_UNPARSED = "thread-line-unparsed"
    #: The project of an unqualified ``Thread:`` id could not be told (repo maps to none/many).
    PROJECT_UNKNOWN = "project-unknown"
    #: The linked thread could not be found in the chatroom.
    THREAD_NOT_FOUND = "thread-not-found"
    #: The linked thread names no task (empty ``related_tasks`` and no ``T<n>`` tag).
    THREAD_HAS_NO_TASK = "thread-has-no-task"
    #: The linked thread names a task id the ledger does not hold.
    TASK_NOT_IN_LEDGER = "task-not-in-ledger"


@dataclass(frozen=True)
class MergedPr:
    repo: str  # owner/repo
    number: int
    merged_at: str
    base: str
    body: str
    #: The project the PR's repo belongs to (``None`` when not provably unique).
    project: str | None

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass(frozen=True)
class ThreadInfo:
    project: str
    thread_id: str
    status: str
    owner: str = ""
    #: ``human`` / ``agent`` / ``unknown`` (settled_close's owner check, by the I/O layer).
    owner_kind: str = "unknown"
    #: settled_close already closes it (agent owner, proposer ``STOP: done`` head).
    settled_close_target: bool = False
    related_tasks: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, project: str, thread: Mapping[str, Any]) -> ThreadInfo:
        return cls(
            project=project,
            thread_id=str(thread.get("thread_id") or thread.get("id") or ""),
            status=str(thread.get("status") or ""),
            owner=str(thread.get("owner") or ""),
            related_tasks=_str_tuple(thread.get("related_tasks")),
            tags=_str_tuple(thread.get("tags")),
        )


@dataclass(frozen=True)
class TaskInfo:
    project: str
    task_id: str
    status: str
    notes: str = ""
    blocked_by: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, project: str, task: Mapping[str, Any]) -> TaskInfo:
        notes = task.get("notes")
        if not isinstance(notes, str):
            # Magickit keeps notes in the description (ledger_notes.py appends there).
            notes = task.get("description")
        return cls(
            project=project,
            task_id=str(task.get("task_id") or task.get("id") or ""),
            status=str(task.get("status") or ""),
            notes=notes if isinstance(notes, str) else "",
            blocked_by=_str_tuple(task.get("blocked_by")),
        )


def _str_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, list | tuple):
        return tuple(str(v) for v in value if isinstance(v, str | int) and str(v).strip())
    return ()


def thread_task_ids(thread: ThreadInfo) -> tuple[str, ...]:
    """``related_tasks`` when non-empty, else the ``^T\\d+$`` tags. Never the thread id."""
    if thread.related_tasks:
        return tuple(dict.fromkeys(thread.related_tasks))
    return tuple(dict.fromkeys(t for t in thread.tags if TASK_TAG_RE.match(t)))


def pr_mentioned(notes: str, number: int) -> bool:
    """Does ``notes`` mention PR ``number`` as ``#<n>`` (not as a prefix of a longer number)?"""
    return re.search(rf"#{number}(?!\d)", notes) is not None


@dataclass(frozen=True)
class PrLink:
    """Where one PR lands: a thread key, or why it does not."""

    pr: MergedPr
    thread_key: tuple[str, str] | None = None
    reason: UnlinkedReason | None = None
    detail: tuple[str, ...] = ()


def link_pr(pr: MergedPr) -> PrLink:
    """PR -> ``(project, thread_id)`` through :func:`resolve_thread_link`, or the reason why not."""
    link = resolve_thread_link(pr.body, pr.project)
    if link.noop is not None or link.thread_id is None:
        reason = UnlinkedReason(link.noop.value) if link.noop else UnlinkedReason.NO_THREAD
        return PrLink(pr=pr, reason=reason, detail=link.ids)
    project = link.thread_project or pr.project
    if project is None:
        return PrLink(pr=pr, reason=UnlinkedReason.PROJECT_UNKNOWN, detail=link.ids)
    return PrLink(pr=pr, thread_key=(project, link.thread_id))


@dataclass
class SectionResult:
    complete: bool = True
    reasons: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"complete": self.complete}
        if self.complete:
            out["count"] = len(self.rows)
            out["rows"] = self.rows
        else:
            # An incomplete section carries no count (msg-9526 Obj-2-2): a partial number read
            # as a whole one is the silent loss T42 forbids.
            out["incomplete_reasons"] = self.reasons
        return out


def detect(
    *,
    prs: Iterable[PrLink],
    threads: Mapping[tuple[str, str], ThreadInfo | None],
    open_threads: Iterable[ThreadInfo],
    tasks: Mapping[tuple[str, str], TaskInfo],
    incomplete: Mapping[str, str] | None = None,
) -> dict[Section, SectionResult]:
    """Every section. ``incomplete`` maps an input name (see :data:`NEEDS`) to why it is partial.

    ``threads`` holds every PR-linked thread that was looked up (``None``: looked up, not found).
    ``tasks`` is keyed ``(project, task_id)``.
    """
    out = {s: SectionResult() for s in Section}

    # PR-window sections: (a), (a'), unlinked.
    by_task: dict[tuple[str, str], dict[str, MergedPr]] = {}
    multi: set[tuple[str, str]] = set()
    for link in prs:
        if link.thread_key is None:
            out[Section.UNLINKED_PRS].rows.append(_unlinked(link.pr, link.reason, link.detail))
            continue
        thread = threads.get(link.thread_key)
        label = ("/".join(link.thread_key),)
        if thread is None:
            out[Section.UNLINKED_PRS].rows.append(
                _unlinked(link.pr, UnlinkedReason.THREAD_NOT_FOUND, label)
            )
            continue
        ids = thread_task_ids(thread)
        if not ids:
            out[Section.UNLINKED_PRS].rows.append(
                _unlinked(link.pr, UnlinkedReason.THREAD_HAS_NO_TASK, label)
            )
            continue
        for task_id in ids:
            key = (thread.project, task_id)
            by_task.setdefault(key, {})[link.pr.ref] = link.pr
            if len(ids) > 1:
                multi.add(key)

    for key in sorted(by_task):
        prs_here = [by_task[key][ref] for ref in sorted(by_task[key])]
        task = tasks.get(key)
        if task is None:
            for pr in prs_here:
                out[Section.UNLINKED_PRS].rows.append(
                    _unlinked(pr, UnlinkedReason.TASK_NOT_IN_LEDGER, ("/".join(key),))
                )
            continue
        base = {
            "project": key[0],
            "task_id": key[1],
            "status": task.status,
            # The thread linked several tasks: listed in full, flagged (msg-9524 紐づけ).
            "multiple_candidates": key in multi,
        }
        if task.status == NOT_STARTED:
            out[Section.NOT_STARTED].rows.append(base | {"prs": [p.ref for p in prs_here]})
        missing = [p.ref for p in prs_here if not pr_mentioned(task.notes, p.number)]
        if missing:
            out[Section.PR_RECORD].rows.append(base | {"prs_not_in_notes": missing})

    # (c) over every open thread, (d) over every task: neither is bounded by the window.
    for thread in sorted(open_threads, key=lambda t: (t.project, t.thread_id)):
        ids = thread_task_ids(thread)
        if not ids:
            continue  # zero linked tasks is never a close candidate (msg-9524 advisory)
        linked = [tasks.get((thread.project, t)) for t in ids]
        if all(t is not None and t.status == COMPLETED for t in linked):
            out[Section.CLOSE_CANDIDATES].rows.append(
                {
                    "project": thread.project,
                    "thread_id": thread.thread_id,
                    "status": thread.status,
                    "owner": thread.owner,
                    "owner_kind": thread.owner_kind,
                    "settled_close_target": thread.settled_close_target,
                    "tasks": list(ids),
                }
            )
    for key in sorted(tasks):
        task = tasks[key]
        stale = [
            b
            for b in task.blocked_by
            if (dep := tasks.get((task.project, b))) is not None and dep.status == COMPLETED
        ]
        if stale:
            out[Section.STALE_BLOCKED_BY].rows.append(
                {
                    "project": task.project,
                    "task_id": task.task_id,
                    "status": task.status,
                    "completed_blockers": stale,
                }
            )

    gaps = dict(incomplete or {})
    for section, needs in NEEDS.items():
        reasons = [f"{name}: {gaps[name]}" for name in sorted(needs) if name in gaps]
        if reasons:
            out[section] = SectionResult(complete=False, reasons=reasons)
    return out


def _unlinked(
    pr: MergedPr, reason: UnlinkedReason | None, detail: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "pr": pr.ref,
        "merged_at": pr.merged_at,
        "reason": (reason or UnlinkedReason.NO_THREAD).value,
        "detail": list(detail),
    }
