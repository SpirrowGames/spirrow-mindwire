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
* (d) every successful append records, in ``ledger.note_appended``, the full notes it read, their
  sha256, and the text it appended — so an append by another mindwire process that this write
  overwrote can be rebuilt from that process's own event.

**Strategy 3 does not detect collisions. Recovery from an overlap relies on the event_log.** A
write by another process, or by a person in the Magickit UI, that lands between this module's read
and its write is overwritten, and nothing here notices. A person's write leaves no event, so it
cannot be recovered from here at all. The permanent fix is an append operation on the Magickit side
(another repository); there is deliberately no post-write check here, because reading back what
you just wrote cannot show that you overwrote someone else (Einstein msg-5299).

**Nothing fails silently.** Every error this module returns to the session is accompanied by a
``ledger.note_failed`` event carrying a :class:`LedgerFailure` reason — the T42 rule applied here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .._time import iso_z
from ..ulid_util import new_ulid
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
    """

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def lock(self, project_id: str, task_id: str) -> asyncio.Lock:
        key = (project_id, task_id)
        existing = self._locks.get(key)
        if existing is None:
            existing = self._locks[key] = asyncio.Lock()
        return existing


PROCESS_TASK_LOCKS = TaskLocks()


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
    head: LedgerHead = field(default_factory=LedgerHead)
    locks: TaskLocks = field(default_factory=lambda: PROCESS_TASK_LOCKS)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

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

        Returns the header and byte count. Errors raise :class:`LedgerError` after emitting
        ``ledger.note_failed``.
        """
        try:
            task_id = _require(task_id, "task_id")
            text = _require(text, "text")
            async with self.locks.lock(self.project_id, task_id):
                task, phase = await self._read(task_id)
                prior = task.get("notes")
                prior_notes = prior if isinstance(prior, str) else ""
                header = self._header()
                addition = f"{header}\n{text}"
                new_notes = f"{prior_notes}\n\n{addition}" if prior_notes else addition
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
            raise await self._fail(LedgerOperation.APPEND, task_id, exc) from exc

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
                "prior_notes_sha256": hashlib.sha256(prior_notes.encode("utf-8")).hexdigest(),
            },
        )
        return {"task_id": task_id, "header": header, "appended_bytes": appended_bytes}

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

    async def _fail(self, op: LedgerOperation, task_id: object, exc: Exception) -> LedgerError:
        if isinstance(exc, LedgerError):
            error = exc
        elif isinstance(exc, MagickitMcpError):
            error = LedgerError(LedgerFailure.MAGICKIT_UNREACHABLE, f"magickit call failed: {exc}")
        else:
            error = LedgerError(LedgerFailure.UNEXPECTED_ERROR, f"{type(exc).__name__}: {exc}")
        await self._emit(
            EVENT_KIND_LEDGER_NOTE_FAILED,
            {
                "project_id": self.project_id,
                "thread_id": self.thread_id,
                "task_id": str(task_id),
                "operation": op.value,
                "reason": error.reason.value,
                "error": str(error),
            },
        )
        return error

    async def _emit(self, kind: str, fields: dict[str, Any]) -> None:
        # Observational (I7): a raising sink must not turn a ledger result into a different one.
        event = Event(event_id=new_ulid(), occurred_at=self.now(), kind=kind, fields=fields)
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
    "PROCESS_TASK_LOCKS",
    "LedgerError",
    "LedgerFailure",
    "LedgerHead",
    "LedgerNotes",
    "LedgerOperation",
    "TaskLocks",
]
