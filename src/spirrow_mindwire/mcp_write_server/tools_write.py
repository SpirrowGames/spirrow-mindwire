"""The 3 write tools exposed by the mindwire-mcp-server.

Per integrator decide ``T-feat3-d2-mcp-server`` msg-127 §1 D2-1
(= the 3-tool minimal set):

- :meth:`WriteTools.send_message` — append a claude.ai message to an
  existing thread and toggle ``meta.awaiting_from`` so the watcher
  invokes claude-code on the next turn.
- :meth:`WriteTools.open_thread` — create a fresh thread directory and
  seed it with the first message (= staging-rename pattern, same as
  watcher-side thread creation, so the watcher never observes a
  half-built directory).
- :meth:`WriteTools.resolve_thread` — transition the thread to
  ``status="resolved"`` so the watcher stops responding to new messages.

A fourth potential tool ``update_awaiting_from`` (toggle without an
accompanying message) is intentionally deferred — Naysayer Q4 frame
inversion: until an observed driver demands toggle-without-message,
adding it would be speculation. Future drivers can append the tool
incrementally without changing this module's contract.

**Per-thread serialisation**: :meth:`send_message` and
:meth:`resolve_thread` on the same thread are serialised through an
``asyncio.Lock`` dict keyed by ``thread_id`` — same shape as
:class:`spirrow_mindwire.watcher.dispatcher.ThreadDispatcher`. Two
concurrent calls on the same thread never race on ``next_seq``
computation or on the atomic meta write.

**Cross-process race scope**: this lock only serialises in-process
callers (= multiple MCP clients hitting the same server). Coordination
with the watcher dispatcher is via the on-disk thread directory only
(msg-127 §1 D2-6 acceptance). Watcher and MCP-server racing on the
same thread within a few ms can produce both processes computing the
same ``next_seq``; the watcher's `mcp__mindwire__write_reply` then
silently overwrites the MCP-server's message file, or vice versa. The
Phase 1 MVP accepts this; sub-PR 4 is the planned 2-phase-commit
re-design.

**Error model** ([[feedback_trust_llm_for_tool_errors]]): user-actionable
failures (= bad thread_id, missing thread, terminal state, turn-discipline
violation) raise :class:`ToolError` with a verbatim message. FastMCP
converts those to MCP ``CallToolResult`` with ``isError=True`` and the
LLM decides how to react. Internal filesystem faults (= e.g.
``atomic_write_text`` ``OSError``) propagate as 500-level errors so
operators are alerted.

**#39 carry N2 disposition** (external caller idempotency for
``set_awaiting_from``): the shared
:func:`spirrow_mindwire.awaiting_from_toggle.toggle_awaiting_from`
helper already short-circuits when ``awaiting_from`` is at the target.
The dispatcher relied on this defensively; this module now exercises
the same path from a second caller, so the idempotent skip is
production-tested rather than purely speculative. No additional
function-level guard added — the existing semantics are sufficient.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from ulid import ULID

from spirrow_mindwire._seq import zero_padded_seq
from spirrow_mindwire._time import iso_z
from spirrow_mindwire.awaiting_from_toggle import toggle_awaiting_from
from spirrow_mindwire.filesystem import (
    EventLogWriter,
    ThreadDirLayout,
    atomic_write_text,
)
from spirrow_mindwire.lifecycle import (
    TERMINAL_STATES,
    InvalidTransitionError,
    transition_state,
)
from spirrow_mindwire.schema import (
    MessageReceived,
    Participant,
    ThreadCreated,
    ThreadMeta,
    ThreadResolved,
    ThreadStatusChanged,
)
from spirrow_mindwire.ulid_util import new_ulid
from spirrow_mindwire.watcher.loader import load_messages, load_thread_meta

logger = logging.getLogger(__name__)

# Phase 0 / Phase 1 invariant: only 2 participants. Every external write
# through this server originates from claude.ai-side, so the sender /
# recipient pair is hard-coded. The Phase 2+ N-party extension would
# accept these as arguments instead of constants.
_EXTERNAL_SENDER: Participant = "claude.ai"
_RECIPIENT: Participant = "claude-code"


class WriteTools:
    """Bundle of the 3 write tool handlers + their per-thread lock state.

    Constructed once at server startup with the ``data_dir`` from
    :class:`MindwireSettings.paths`, then shared across all incoming
    MCP requests. The same instance serves every request, so the
    per-thread lock dict accumulates one entry per thread touched by
    the process lifetime (= same growth contract as
    :class:`spirrow_mindwire.watcher.dispatcher.ThreadDispatcher._thread_locks`).
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._locks_mutex = asyncio.Lock()

    async def _get_thread_lock(self, thread_id: str) -> asyncio.Lock:
        """Return (and lazily allocate) the asyncio.Lock for ``thread_id``."""
        async with self._locks_mutex:
            return self._thread_locks.setdefault(thread_id, asyncio.Lock())

    def _layout(self, thread_id: str) -> ThreadDirLayout:
        """Build a :class:`ThreadDirLayout`, surfacing bad IDs as :class:`ToolError`."""
        try:
            return ThreadDirLayout(base_dir=self._data_dir, thread_id=thread_id)
        except ValueError as e:
            raise ToolError(str(e)) from e

    # ------------------------------------------------------------------
    # send_message
    # ------------------------------------------------------------------

    async def send_message(self, thread_id: str, body: str) -> dict[str, Any]:
        """Append a claude.ai message and toggle ``awaiting_from``.

        Requires:
        - ``thread_id`` is a valid ULID and the thread directory exists.
        - ``meta.status`` is non-terminal.
        - ``meta.awaiting_from == "claude.ai"`` (turn discipline — the
          watcher dispatcher toggles to ``claude.ai`` only after
          claude-code finishes its reply; sending before that would
          overlap with an in-flight invoke).

        On success, writes ``messages/NNN-from-cai.md`` atomically,
        appends a :class:`MessageReceived` event, and toggles
        ``awaiting_from`` to ``claude-code`` (= the watcher will pick
        up the next turn).
        """
        layout = self._layout(thread_id)
        if not layout.thread_dir.is_dir():
            raise ToolError(f"thread {thread_id!r} does not exist under {self._data_dir}")

        lock = await self._get_thread_lock(thread_id)
        async with lock:
            return self._send_message_locked(layout, body)

    def _send_message_locked(self, layout: ThreadDirLayout, body: str) -> dict[str, Any]:
        meta = load_thread_meta(layout)
        if meta.status in TERMINAL_STATES:
            raise ToolError(
                f"thread {layout.thread_id!r} is in terminal state {meta.status!r}; "
                "send_message requires status active or retrying"
            )
        if meta.awaiting_from != _EXTERNAL_SENDER:
            raise ToolError(
                f"thread {layout.thread_id!r} has awaiting_from={meta.awaiting_from!r}; "
                f"send_message requires awaiting_from={_EXTERNAL_SENDER!r} "
                "(claude-code's turn — wait for the dispatcher to toggle)"
            )

        existing = load_messages(layout)
        next_seq = (existing[-1].seq + 1) if existing else 1
        ts = datetime.now(UTC)
        msg_id = f"{layout.thread_id}/{zero_padded_seq(next_seq)}"
        content = _format_message(msg_id=msg_id, seq=next_seq, ts=ts, body=body)
        target = layout.message_path(next_seq, _EXTERNAL_SENDER)
        atomic_write_text(target, content)

        size_bytes = len(content.encode("utf-8"))
        log = EventLogWriter(layout.event_log_path)
        log.append(_message_received_event(layout.thread_id, ts, next_seq, size_bytes))
        toggle_awaiting_from(layout=layout, log=log, from_participant=_EXTERNAL_SENDER)

        return {
            "thread_id": layout.thread_id,
            "seq": next_seq,
            "msg_id": msg_id,
            "awaiting_from": _RECIPIENT,
        }

    # ------------------------------------------------------------------
    # open_thread
    # ------------------------------------------------------------------

    async def open_thread(
        self,
        initial_message: str,
        title: str = "",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new thread seeded with one claude.ai-authored message.

        Generates a fresh ULID, stages ``meta.yaml`` + the first message
        under ``.staging-<ULID>/``, then ``os.replace``s the staging
        dir into the canonical thread path (= same atomic-thread-creation
        pattern as the watcher). The watcher therefore observes the
        thread for the first time only after both files are durable.
        """
        thread_id = str(ULID())
        layout = self._layout(thread_id)
        return self._open_thread(layout, initial_message, title, tuple(tags or ()))

    def _open_thread(
        self,
        layout: ThreadDirLayout,
        initial_message: str,
        title: str,
        tags: tuple[str, ...],
    ) -> dict[str, Any]:
        ts = datetime.now(UTC)
        meta = ThreadMeta(
            schema_version=2,
            thread_id=layout.thread_id,
            title=title,
            status="active",
            awaiting_from=_RECIPIENT,
            participants=(_EXTERNAL_SENDER, _RECIPIENT),
            created_at=ts,
            updated_at=ts,
            tags=tags,
        )
        msg_id = f"{layout.thread_id}/{zero_padded_seq(1)}"
        message_content = _format_message(msg_id=msg_id, seq=1, ts=ts, body=initial_message)
        meta_yaml = yaml.safe_dump(
            meta.model_dump(mode="json"),
            default_flow_style=False,
            sort_keys=False,
        )

        # Stage under .staging-<ULID> so the watcher never sees a
        # half-built thread directory (architecture.md §3.2).
        atomic_write_text(layout.staging_meta_path, meta_yaml)
        atomic_write_text(layout.staging_message_path(1, _EXTERNAL_SENDER), message_content)
        # Single ``os.replace`` flips staging → canonical atomically.
        os.replace(layout.staging_dir, layout.thread_dir)

        log = EventLogWriter(layout.event_log_path)
        log.append(
            ThreadCreated(
                schema_version=1,
                event_id=new_ulid(),
                ts=ts,
                thread_id=layout.thread_id,
                title=title,
            )
        )
        log.append(
            _message_received_event(layout.thread_id, ts, 1, len(message_content.encode("utf-8")))
        )
        return {
            "thread_id": layout.thread_id,
            "msg_id": msg_id,
            "awaiting_from": _RECIPIENT,
        }

    # ------------------------------------------------------------------
    # resolve_thread
    # ------------------------------------------------------------------

    async def resolve_thread(self, thread_id: str) -> dict[str, Any]:
        """Transition the thread to ``status="resolved"``.

        Idempotent: a thread already in ``resolved`` returns ``noop=True``
        without writing meta or events.
        """
        layout = self._layout(thread_id)
        if not layout.thread_dir.is_dir():
            raise ToolError(f"thread {thread_id!r} does not exist under {self._data_dir}")
        lock = await self._get_thread_lock(thread_id)
        async with lock:
            return self._resolve_thread_locked(layout)

    def _resolve_thread_locked(self, layout: ThreadDirLayout) -> dict[str, Any]:
        meta = load_thread_meta(layout)
        if meta.status == "resolved":
            return {
                "thread_id": layout.thread_id,
                "status": "resolved",
                "noop": True,
            }
        try:
            new_meta = transition_state(layout, "resolved", awaiting_from=None)
        except (InvalidTransitionError, ValueError) as e:
            # Non-allowed source status (e.g., archived → resolved) or
            # field constraint violation. Both are caller-actionable.
            raise ToolError(f"cannot resolve thread {layout.thread_id!r}: {e}") from e

        log = EventLogWriter(layout.event_log_path)
        ts = datetime.now(UTC)
        log.append(
            ThreadStatusChanged(
                schema_version=1,
                event_id=new_ulid(),
                ts=ts,
                thread_id=layout.thread_id,
                from_status=meta.status,
                to_status="resolved",
                retry_count=new_meta.retry_count,
            )
        )
        log.append(
            ThreadResolved(
                schema_version=1,
                event_id=new_ulid(),
                ts=ts,
                thread_id=layout.thread_id,
            )
        )
        return {"thread_id": layout.thread_id, "status": "resolved"}


def _message_received_event(
    thread_id: str, ts: datetime, seq: int, size_bytes: int
) -> MessageReceived:
    """Build a :class:`MessageReceived` event with the ``from`` alias.

    ``MessageReceived.from_`` is bound to the YAML alias ``from``; pydantic
    accepts the alias key in ``model_validate`` but not in the generated
    ``__init__`` typing (``from`` is a Python keyword), so this helper
    funnels every construction through ``model_validate``.
    """
    return MessageReceived.model_validate(
        {
            "schema_version": 1,
            "event_id": new_ulid(),
            "ts": ts,
            "thread_id": thread_id,
            "seq": seq,
            "from": _EXTERNAL_SENDER,
            "size_bytes": size_bytes,
        }
    )


def _format_message(*, msg_id: str, seq: int, ts: datetime, body: str) -> str:
    """Render a Message file (frontmatter + body) — mirrors write_reply."""
    frontmatter = {
        "schema_version": 1,
        "msg_id": msg_id,
        "seq": seq,
        "from": _EXTERNAL_SENDER,
        "to": _RECIPIENT,
        "created_at": iso_z(ts),
    }
    yaml_block = yaml.safe_dump(
        frontmatter,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    yaml_block = yaml_block.rstrip("\n") + "\n"
    return f"---\n{yaml_block}---\n\n{body}\n"


def register_tools(fastmcp: FastMCP, tools: WriteTools) -> None:
    """Register the 3 write tools on the FastMCP server."""

    @fastmcp.tool(
        name="send_message",
        description=(
            "Append a claude.ai-authored message to an existing thread. "
            "Pass `thread_id` (a ULID) and `body` (markdown). After the "
            "message is written, `meta.awaiting_from` toggles to "
            "`claude-code` so the watcher picks up the next turn. "
            "Errors: nonexistent thread, terminal state, or turn "
            "discipline violation (must be claude.ai's turn)."
        ),
    )
    async def send_message(thread_id: str, body: str) -> dict[str, Any]:
        return await tools.send_message(thread_id, body)

    @fastmcp.tool(
        name="open_thread",
        description=(
            "Create a new thread seeded with one claude.ai message. "
            "Returns the new `thread_id` (ULID). `meta.awaiting_from` "
            "is set to `claude-code`, so the watcher will pick it up "
            "for the first reply turn."
        ),
    )
    async def open_thread(
        initial_message: str,
        title: str = "",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return await tools.open_thread(initial_message, title=title, tags=tags)

    @fastmcp.tool(
        name="resolve_thread",
        description=(
            "Transition the thread to `status=resolved`. Idempotent: a "
            "thread already in resolved returns `noop=True`. Errors if "
            "the current status doesn't permit a resolved transition "
            "(e.g., archived)."
        ),
    )
    async def resolve_thread(thread_id: str) -> dict[str, Any]:
        return await tools.resolve_thread(thread_id)


__all__ = ["WriteTools", "register_tools"]
