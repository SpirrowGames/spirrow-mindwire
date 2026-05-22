"""ChatroomWatcher (T14) — ADR-2026-05-21-06 §7 (Step 3 PR-G).

Polls the magickit chatroom for new messages on configured ``(thread, role)``
watches and feeds them to the :class:`~spirrow_mindwire.dispatcher.core.Dispatcher`
as :class:`ChatroomEvent`\\s, **in occurred_at order** — msg-190 note 1: the
caller (this watcher) owns occurred_at ordering for I9; ``chatroom_get_thread``
returns messages in numeric msg-id order, which is chronological.

Phase 1 uses **explicit** :class:`WatchSpec`\\s (dynamic role assignment from
thread metadata is Phase 2, ADR-06 §5). The §8 smoke is a single watch
``(thread, proposer)``.

``event_id = f"{thread_id}:{msg_id}"`` — chatroom-derived, thread-namespaced
(chatroom thread msg-198 fork 3): a *stable* identifier so re-polls and a
fresh watcher dedup correctly via the dispatcher's I4 set. ADR-06 §4 I4
interpretation note (added to the Drive doc, audit root msg-197/198):
``event_id`` need only be a stable identifier, not a ULID type — using a
chatroom-derived stable id serves I4's restart-safe-dedup purpose.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..dispatcher.core import Dispatcher
from ..value_objects import (
    ChatroomEvent,
    EventType,
    NewMessagePayload,
    Role,
    SessionHandle,
    ThreadRef,
)
from .client import McpToolCaller

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchSpec:
    """One ``(thread, role)`` the watcher runs (Phase 1 explicit assignment)."""

    thread_ref: ThreadRef
    role: Role


def _parse_occurred_at(value: Any) -> datetime:
    """Parse a chatroom ISO-8601 timestamp; fall back to now() if absent/bad."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)  # 3.11 handles the trailing 'Z'
        except ValueError:
            pass
    return datetime.now(UTC)


class ChatroomWatcher:
    """Polls magickit chatroom watches → ChatroomEvents → dispatcher (T14)."""

    def __init__(
        self,
        mcp: McpToolCaller,
        dispatcher: Dispatcher,
        watches: Sequence[WatchSpec],
    ) -> None:
        self._mcp = mcp
        self._dispatcher = dispatcher
        self._watches = list(watches)
        self._handles: dict[WatchSpec, SessionHandle] = {}
        self._seen_msg_ids: set[str] = set()

    async def start(self) -> None:
        """Spawn one adapter session per watch (call once before polling)."""
        for watch in self._watches:
            self._handles[watch] = await self._dispatcher.spawn_role(
                watch.thread_ref, watch.role
            )

    async def poll_once(self) -> int:
        """Poll every watch once, dispatching new messages; return # dispatched."""
        dispatched = 0
        for watch in self._watches:
            handle = self._handles.get(watch)
            if handle is None:
                continue  # start() not called for this watch
            dispatched += await self._poll_watch(watch, handle)
        return dispatched

    async def _poll_watch(self, watch: WatchSpec, handle: SessionHandle) -> int:
        result = await self._mcp.call_tool(
            "chatroom_get_thread",
            {
                "project": watch.thread_ref.project_id,
                "thread_id": watch.thread_ref.thread_id,
                "mode": "full",
            },
        )
        messages = result.get("messages", []) if isinstance(result, dict) else []
        count = 0
        # numeric msg-id order = chronological = occurred_at order (msg-190 note 1).
        for msg in messages:
            msg_id = msg.get("msg_id") if isinstance(msg, dict) else None
            if not isinstance(msg_id, str) or msg_id in self._seen_msg_ids:
                continue
            self._seen_msg_ids.add(msg_id)
            await self._dispatcher.dispatch(handle, self._to_event(watch.thread_ref, msg))
            count += 1
        return count

    def _to_event(self, thread_ref: ThreadRef, msg: dict[str, Any]) -> ChatroomEvent:
        msg_id = str(msg["msg_id"])
        return ChatroomEvent(
            # fork 3 (msg-198): thread-namespaced stable id for restart-safe I4 dedup.
            event_id=f"{thread_ref.thread_id}:{msg_id}",
            event_type=EventType.NEW_MESSAGE,  # Phase 1: every new msg is a NEW_MESSAGE
            thread_ref=thread_ref,
            occurred_at=_parse_occurred_at(msg.get("timestamp")),
            payload=NewMessagePayload(
                msg_id=msg_id,
                author=str(msg.get("author", "")),
                body=str(msg.get("content", "")),
                parent_msg_id=msg.get("reply_to") or None,
            ),
        )

    async def run(self, *, poll_interval_seconds: float = 5.0) -> None:
        """Poll loop until cancelled (daemon entry; call :meth:`start` first).

        A poll failure is logged and the loop continues (the watcher must not
        die on a transient chatroom read error).
        """
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("chatroom poll failed; continuing")
            await asyncio.sleep(poll_interval_seconds)


__all__ = ["ChatroomWatcher", "WatchSpec"]
