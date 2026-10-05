"""Ledger notes — the two Magickit task operations a role session may perform.

Design: thread ``T-silent-stops-need-a-generic-watchdog-and-a-loud-stand-down``, Bohr msg-5296
(scope), msg-5298 (concurrency strategy order), msg-5300 (strategy 3 fixed), Einstein msg-5297 /
msg-5299 / msg-5301 (review). The human authorised the capability in msg-5089 / msg-5194.

Why this exists instead of attaching the Magickit MCP server: that server exposes 87 tools,
including ``delete_project``, ``delete_task`` and ``loop_control_set``, and attaching a server
exposes every tool it has. ``allowed_tools`` only decides which of them run without a prompt. So
the session gets this proxy, which can do two things and nothing else:

* :meth:`LedgerNotes.get_task` — read one task (read-only).
* :meth:`LedgerNotes.append_note` — **append** to that task's notes. There is no replace: a replace
  can erase notes, which is close to irreversible. A correction is a further appended note.

Both are pinned to the project the thread was resolved in (``ThreadRef.project_id``). Neither takes
a project argument, so a session cannot write another project's ledger.

**Concurrency — strategy 3, and what it does not do.** Magickit was checked first (2026-10-02,
``list_tools`` read-only) for the two better options msg-5298 ranks above this one:

1. an atomic append tool: there is none. The only other tool that writes notes is
   ``complete_task(notes=...)``, which also marks the task completed, so it is not an append.
2. a versioned write: ``update_task`` takes no ``updated_at`` / version / ``If-Match`` argument.

So the append is read-modify-write through ``update_task(description=...)`` ("New description
(stored in notes)" in Magickit's own docstring). What this module does about that:

* (a) appends to the same task from this process are serialised by a per-(project, task) lock;
* (d) every append first writes a **recovery record** — the full notes it read, their sha256, and
  the text it is about to append — to its own file, and only then writes Magickit. An append by
  another mindwire process that this write overwrote can be rebuilt from that process's record.

**The recovery record is the journal, not the event_log.** The loop's event sink logs one line per
event and drops its payload (thread msg-5884), so ``ledger.note_appended`` alone recovers nothing in
production. :class:`LedgerJournal` writes one JSON file per append,
``<root>/<project_id>/<task_id>/<event_id>.json``, with ``atomic_write_text(..., fsync=True)``
(temp file, fsync, rename): a record is either whole or absent, two processes never share a file,
and so no file lock is needed (Bohr msg-5886, Einstein msg-5887). ``event_id`` is a ULID issued
monotonically within this process, and issued while the per-task lock is held, so sorting one
task's file names gives the order this process wrote them in; across processes the order is only
as good as the millisecond clock.

**The journal write is an exception to I7.** It is not observation: it is the only recovery path
strategy 3 has. If it cannot be written, the append is not made — ``ledger.note_failed`` with
``reason=journal_unavailable`` — because appending without it is exactly the silent loss T42 exists
to prevent. A record whose Magickit write then failed stays on disk; the ``ledger.note_failed``
event names it in ``journal_event_id`` so a reader can tell an attempt from an applied append.
Records are never deleted by this module: they are the recovery data (Einstein msg-5887 noted the
unbounded growth as a non-blocking advisory).

**Strategy 3 does not detect collisions. Recovery from an overlap relies on the journal.** A
write by another process, or by a person in the Magickit UI, that lands between this module's read
and its write is overwritten, and nothing here notices. A person's write leaves no record, so it
cannot be recovered from here at all. The permanent fix is an append operation on the Magickit side
(another repository); there is deliberately no post-write check here, because reading back what
you just wrote cannot show that you overwrote someone else (Einstein msg-5299).

**Nothing fails silently.** Every error this module returns to the session is accompanied by a
``ledger.note_failed`` event carrying a :class:`LedgerFailure` reason — the T42 rule applied here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ulid import ULID

from .._time import iso_z
from ..filesystem.atomic import atomic_write_text
from ..value_objects import Event
from .client import MagickitMcpError, McpToolCaller

EVENT_KIND_LEDGER_NOTE_APPENDED = "ledger.note_appended"
EVENT_KIND_LEDGER_NOTE_FAILED = "ledger.note_failed"

# Magickit tool names. Only these two are ever called; nothing else on that server is reachable
# through this module.
_TOOL_GET_TASK = "get_task"
_TOOL_UPDATE_TASK = "update_task"

# ``phase=""`` asks Magickit to resolve the phase from the task id (its own documented behaviour
# when the id is unique across phases). The phase Magickit answers with is then used for the write.
_AUTO_PHASE = ""

# Path components of a journal record. Anything else (``..``, separators, drive letters, spaces)
# is refused before a path is built, so a task id cannot place a file outside the journal root.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")

JOURNAL_DIR_NAME = "ledger"
"""Under ``settings.paths.logs_dir``; see :func:`spirrow_mindwire.loop_runner._build_dispatcher`."""

logger = logging.getLogger(__name__)


class LedgerOperation(StrEnum):
    GET = "get"
    APPEND = "append"


class LedgerFailure(StrEnum):
    """Why a ledger call returned an error. Closed set; every value is emitted in an event."""

    INVALID_ARGUMENT = "invalid_argument"
    """``task_id`` or ``text`` was empty."""
    MAGICKIT_UNREACHABLE = "magickit_unreachable"
    """The Magickit call raised (transport failure or an error envelope)."""
    TASK_NOT_FOUND = "task_not_found"
    """``get_task`` answered ``success: false`` / no task."""
    WRITE_REJECTED = "write_rejected"
    """``update_task`` answered without ``success: true``."""
    INVALID_IDENTIFIER = "invalid_identifier"
    """``project_id`` or ``task_id`` is not ``[A-Za-z0-9_-]+``, so it cannot name a journal path."""
    JOURNAL_UNAVAILABLE = "journal_unavailable"
    """The recovery record could not be written, so the append was not made (I7 exception)."""
    UNEXPECTED_ERROR = "unexpected_error"
    """Anything else. Still reported; never swallowed."""


class LedgerError(Exception):
    """A ledger call failed; the event for it has already been emitted."""

    def __init__(self, reason: LedgerFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class TaskLocks:
    """Per-(project, task) asyncio locks — strategy 3 (a).

    One instance per process (:data:`PROCESS_TASK_LOCKS`) so every implementer session in the
    daemon shares it. It does nothing across processes; see the module docstring.

    Entries are reference-counted and evicted when the last holder or waiter leaves, so a
    long-running daemon does not accumulate one lock per task it ever touched (PR #409 gate
    advisory). The count is changed only in synchronous code on the event loop, so taking and
    releasing it cannot interleave with another coroutine.
    """

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], tuple[asyncio.Lock, int]] = {}

    def __len__(self) -> int:
        return len(self._locks)

    @asynccontextmanager
    async def hold(self, project_id: str, task_id: str) -> AsyncIterator[None]:
        key = (project_id, task_id)
        lock, users = self._locks.get(key, (None, 0))
        if lock is None:
            lock = asyncio.Lock()
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, remaining = self._locks[key]
            if remaining <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, remaining - 1)


PROCESS_TASK_LOCKS = TaskLocks()


class MonotonicUlids:
    """ULIDs that strictly increase within this process.

    Two random ULIDs from the same millisecond are not ordered, so plain ``new_ulid`` would not
    make "sort the file names" mean "the order they were written". When the clock has not moved
    past the last id, the next id is the last one plus one.
    """

    def __init__(self) -> None:
        self._last = 0
        self._guard = threading.Lock()

    def __call__(self) -> str:
        with self._guard:
            candidate = int(ULID())
            if candidate <= self._last:
                candidate = self._last + 1
            self._last = candidate
            return str(ULID.from_int(candidate))


PROCESS_ULIDS = MonotonicUlids()


@dataclass(frozen=True)
class LedgerJournal:
    """One recovery record per append, one file per record (Bohr msg-5886).

    ``root`` is ``<logs_dir>/ledger`` in production. :meth:`write` is ``atomic_write_text`` with
    fsync; any exception from it propagates, and :class:`LedgerNotes` turns it into
    ``journal_unavailable`` before Magickit is touched.
    """

    root: Path

    @staticmethod
    def check_identifiers(project_id: str, task_id: str) -> None:
        """Raise ``invalid_identifier`` unless both ids are safe as one path segment each."""
        for name, value in (("project_id", project_id), ("task_id", task_id)):
            if not _IDENTIFIER.fullmatch(value):
                raise LedgerError(
                    LedgerFailure.INVALID_IDENTIFIER,
                    f"{name} {value!r} must match {_IDENTIFIER.pattern}",
                )

    def path_for(self, project_id: str, task_id: str, event_id: str) -> Path:
        self.check_identifiers(project_id, task_id)
        return self.root / project_id / task_id / f"{event_id}.json"

    def write(self, record: dict[str, Any]) -> Path:
        path = self.path_for(record["project_id"], record["task_id"], record["event_id"])
        text = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
        atomic_write_text(path, text, fsync=True)
        return path


@dataclass
class LedgerHead:
    """The chatroom message the session is currently answering, for the provenance header.

    Mutable on purpose: the server is built once at spawn, and the adapter updates
    ``msg_id`` each time it delivers a message to the session.
    """

    msg_id: str = ""


@dataclass
class LedgerNotes:
    """Get / append-only notes on one project's Magickit tasks, for one thread's session."""

    mcp: McpToolCaller
    project_id: str
    thread_id: str
    on_event: Callable[[Event], Awaitable[None]]
    journal: LedgerJournal
    head: LedgerHead = field(default_factory=LedgerHead)
    locks: TaskLocks = field(default_factory=lambda: PROCESS_TASK_LOCKS)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    new_id: Callable[[], str] = field(default=lambda: PROCESS_ULIDS())

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Return the task (with its notes and phase). Errors raise :class:`LedgerError`."""
        try:
            task_id = _require(task_id, "task_id")
            task, phase = await self._read(task_id)
            return {"task": task, "phase": phase, "project": self.project_id}
        except Exception as exc:
            raise await self._fail(LedgerOperation.GET, task_id, exc) from exc

    async def append_note(self, task_id: str, text: str) -> dict[str, Any]:
        """Append ``text`` under a provenance header to the task's notes.

        Order: read the notes, write the recovery record (:class:`LedgerJournal`), then write
        Magickit. Returns the header, byte count and event id. Errors raise :class:`LedgerError`
        after emitting ``ledger.note_failed``.
        """
        event_id = ""
        journaled = False
        try:
            task_id = _require(task_id, "task_id")
            text = _require(text, "text")
            # Refuse a path-unsafe id before locking or reading anything.
            self.journal.check_identifiers(self.project_id, task_id)
            async with self.locks.hold(self.project_id, task_id):
                # Issued under the per-task lock, never before it: the id orders the record
                # files, so it must follow the order the read-modify-write cycles run in. An id
                # taken before the lock lets a later holder carry an earlier id (PR #439 gate).
                event_id = self.new_id()
                task, phase = await self._read(task_id)
                prior = task.get("notes")
                prior_notes = prior if isinstance(prior, str) else ""
                prior_sha256 = hashlib.sha256(prior_notes.encode("utf-8")).hexdigest()
                header = self._header()
                addition = f"{header}\n{text}"
                new_notes = f"{prior_notes}\n\n{addition}" if prior_notes else addition
                record = {
                    "event_id": event_id,
                    "project_id": self.project_id,
                    "thread_id": self.thread_id,
                    "task_id": task_id,
                    "header": header,
                    "appended_text": text,
                    "prior_notes": prior_notes,
                    "prior_notes_sha256": prior_sha256,
                }
                try:
                    # Off the event loop: fsync latency is unbounded (PR #439 gate advisory).
                    # Still inside the per-task lock, so the record lands before Magickit.
                    await asyncio.to_thread(self.journal.write, record)
                except Exception as exc:
                    raise LedgerError(
                        LedgerFailure.JOURNAL_UNAVAILABLE,
                        f"recovery record for {task_id} could not be written, so nothing was "
                        f"appended: {type(exc).__name__}: {exc}",
                    ) from exc
                journaled = True
                result = await self.mcp.call_tool(
                    _TOOL_UPDATE_TASK,
                    {
                        "task_id": task_id,
                        "phase": phase,
                        "description": new_notes,
                        "project": self.project_id,
                    },
                )
                if not (isinstance(result, dict) and result.get("success") is True):
                    raise LedgerError(
                        LedgerFailure.WRITE_REJECTED,
                        f"update_task did not report success for {task_id}: {_short(result)}",
                    )
        except Exception as exc:
            raise await self._fail(
                LedgerOperation.APPEND,
                task_id,
                exc,
                journal_event_id=event_id if journaled else None,
            ) from exc

        appended_bytes = len(addition.encode("utf-8"))
        await self._emit(
            EVENT_KIND_LEDGER_NOTE_APPENDED,
            {
                "project_id": self.project_id,
                "thread_id": self.thread_id,
                "task_id": task_id,
                "header": header,
                "appended_text": text,
                "appended_bytes": appended_bytes,
                "prior_notes": prior_notes,
                "prior_notes_sha256": prior_sha256,
            },
            event_id=event_id,
        )
        return {
            "task_id": task_id,
            "header": header,
            "appended_bytes": appended_bytes,
            "event_id": event_id,
        }

    async def _read(self, task_id: str) -> tuple[dict[str, Any], str]:
        result = await self.mcp.call_tool(
            _TOOL_GET_TASK,
            {
                "task_id": task_id,
                "phase": _AUTO_PHASE,
                "project": self.project_id,
                "include_linked_docs": False,
            },
        )
        task = result.get("task") if isinstance(result, dict) else None
        if not (
            isinstance(result, dict) and result.get("success") is True and isinstance(task, dict)
        ):
            raise LedgerError(
                LedgerFailure.TASK_NOT_FOUND,
                f"task {task_id} not found in project {self.project_id}: {_short(result)}",
            )
        phase = result.get("phase")
        return task, phase if isinstance(phase, str) else _AUTO_PHASE

    def _header(self) -> str:
        head = self.head.msg_id or "no-head"
        return f"[mindwire {self.thread_id} {head} {iso_z(self.now())}]"

    async def _fail(
        self,
        op: LedgerOperation,
        task_id: object,
        exc: Exception,
        *,
        journal_event_id: str | None = None,
    ) -> LedgerError:
        if isinstance(exc, LedgerError):
            error = exc
        elif isinstance(exc, MagickitMcpError):
            error = LedgerError(LedgerFailure.MAGICKIT_UNREACHABLE, f"magickit call failed: {exc}")
        else:
            error = LedgerError(LedgerFailure.UNEXPECTED_ERROR, f"{type(exc).__name__}: {exc}")
        fields: dict[str, Any] = {
            "project_id": self.project_id,
            "thread_id": self.thread_id,
            "task_id": str(task_id),
            "operation": op.value,
            "reason": error.reason.value,
            "error": str(error),
        }
        if journal_event_id is not None:
            # A recovery record was written, but the Magickit write after it did not land.
            fields["journal_event_id"] = journal_event_id
        await self._emit(EVENT_KIND_LEDGER_NOTE_FAILED, fields)
        return error

    async def _emit(
        self, kind: str, fields: dict[str, Any], *, event_id: str | None = None
    ) -> None:
        # Observational (I7): a raising sink must not turn a ledger result into a different one.
        # The journal write is the I7 exception, and it happens before this, never here.
        event = Event(
            event_id=event_id if event_id is not None else self.new_id(),
            occurred_at=self.now(),
            kind=kind,
            fields=fields,
        )
        try:
            await self.on_event(event)
        except Exception:
            logger.warning("on_event_log raised for %s; isolated per I7", kind, exc_info=True)


def _require(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(LedgerFailure.INVALID_ARGUMENT, f"{name} must be a non-empty string")
    return value.strip() if name == "task_id" else value


def _short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 300 else text[:300] + "…"


__all__ = [
    "EVENT_KIND_LEDGER_NOTE_APPENDED",
    "EVENT_KIND_LEDGER_NOTE_FAILED",
    "JOURNAL_DIR_NAME",
    "PROCESS_TASK_LOCKS",
    "PROCESS_ULIDS",
    "LedgerError",
    "LedgerFailure",
    "LedgerHead",
    "LedgerJournal",
    "LedgerNotes",
    "LedgerOperation",
    "MonotonicUlids",
    "TaskLocks",
]
