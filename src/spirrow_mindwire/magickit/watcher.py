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
    mint_instance_id,
)
from .client import McpToolCaller

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchSpec:
    """One ``(thread, role)`` instance the watcher runs (Phase 1 explicit assignment).

    ``instance_id`` is the stable per-instance label (ADR-2026-05-24-08 §2.2);
    it defaults to ``mint_instance_id(role)`` (``"{role}-1"``, the Phase 1
    1-role-1-instance case) so existing single-instance call sites need not
    pass it, while parallel instances (Phase 2) pass distinct ids. It is part
    of the dataclass identity, so ``_handles`` keyed by ``WatchSpec`` separates
    two instances of the same ``(thread, role)`` — the v2.1 dict-key collision
    (``add_watch`` no-op'ing a same-``(thread, role)`` second instance) is
    resolved structurally (ADR-08 §2.2).
    """

    thread_ref: ThreadRef
    role: Role
    instance_id: str = ""

    def __post_init__(self) -> None:
        if not self.instance_id:
            object.__setattr__(self, "instance_id", mint_instance_id(self.role))


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
        # Seen keys are thread-namespaced (== event_id) so the same msg_id in
        # two watched threads is not cross-deduped.
        self._seen: set[str] = set()

    async def start(self, *, baseline: bool = True) -> None:
        """Spawn one adapter session per watch (call once before polling).

        With ``baseline=True`` (default, production-safe) each watch's current
        messages are marked seen *without dispatching*, so the watcher acts only
        on messages that arrive after start — it does not reply to the whole
        thread history. Pass ``baseline=False`` to also dispatch the existing
        messages (e.g. the dogfood driver answering a pre-posted question).
        """
        for watch in self._watches:
            self._handles[watch] = await self._dispatcher.spawn_instance(
                watch.thread_ref, watch.role, watch.instance_id
            )
            if baseline:
                await self._baseline(watch)

    async def add_watch(self, watch: WatchSpec, *, baseline: bool = True) -> None:
        """Register a new ``(thread, role)`` after :meth:`start` — spawns its session.

        Used by the PR-review orchestrator (T20) to wire a freshly-opened
        ``T-pr-review-<n>`` thread to the naysayer at runtime. With
        ``baseline=False`` the watch's existing messages (e.g. the orchestrator's
        review-request) ARE dispatched on the next poll, so the naysayer acts on
        the request immediately rather than ignoring it as backlog. A no-op if
        the watch is already registered.
        """
        if watch in self._handles:
            return
        self._handles[watch] = await self._dispatcher.spawn_instance(
            watch.thread_ref, watch.role, watch.instance_id
        )
        # Register for polling too — poll_once() iterates _watches, so a watch
        # added only to _handles would spawn a session that never receives events
        # (the orchestrator wiring would be inert).
        self._watches.append(watch)
        if baseline:
            await self._baseline(watch)

    async def poll_once(self) -> int:
        """Poll every watch once, dispatching new messages; return # dispatched."""
        dispatched = 0
        # Snapshot: add_watch() may append to _watches across an await below.
        for watch in list(self._watches):
            handle = self._handles.get(watch)
            if handle is None:
                continue  # start() not called for this watch
            dispatched += await self._poll_watch(watch, handle)
        return dispatched

    async def _fetch_messages(self, watch: WatchSpec) -> list[Any]:
        result = await self._mcp.call_tool(
            "chatroom_get_thread",
            {
                "project": watch.thread_ref.project_id,
                "thread_id": watch.thread_ref.thread_id,
                "mode": "full",
            },
        )
        return result.get("messages", []) if isinstance(result, dict) else []

    async def _baseline(self, watch: WatchSpec) -> int:
        """Mark a watch's current messages seen *without dispatching* them.

        So a watcher starting on a thread with history acts only on messages
        arriving after start (no backlog reply). The seen-set is in-memory, so
        a restart re-baselines: down-time messages are treated as outside the
        watcher's responsibility window (durable last-seen persistence is
        Stage 2, ``WatcherStateStore``).
        """
        count = 0
        for msg in await self._fetch_messages(watch):
            msg_id = msg.get("msg_id") if isinstance(msg, dict) else None
            if isinstance(msg_id, str):
                self._seen.add(f"{watch.thread_ref.thread_id}:{msg_id}")
                count += 1
        return count

    async def _poll_watch(self, watch: WatchSpec, handle: SessionHandle) -> int:
        count = 0
        # numeric msg-id order = chronological = occurred_at order (msg-190 note 1).
        for msg in await self._fetch_messages(watch):
            msg_id = msg.get("msg_id") if isinstance(msg, dict) else None
            if not isinstance(msg_id, str):
                continue
            seen_key = f"{watch.thread_ref.thread_id}:{msg_id}"  # == event_id
            if seen_key in self._seen:
                continue
            self._seen.add(seen_key)
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

    async def stop(self) -> None:
        """Halt all spawned sessions (best-effort graceful shutdown).

        Call in a ``finally`` after :meth:`poll_once` (or after cancelling
        :meth:`run`) so adapter sessions — e.g. the Claude SDK subprocess —
        disconnect cleanly and don't leak transports at interpreter exit. Halt
        is idempotent (I8); a failing halt is logged and the rest still run.
        """
        for handle in self._handles.values():
            try:
                await self._dispatcher.halt(handle)
            except Exception:
                logger.exception("halt failed during stop; continuing")
        self._handles.clear()


__all__ = ["ChatroomWatcher", "WatchSpec"]
