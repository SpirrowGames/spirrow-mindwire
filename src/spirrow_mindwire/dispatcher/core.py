"""Dispatcher core — ADR-2026-05-21-06 §4 (T13 Step 2 live dispatch loop).

Wires the registry, adapters, and ChatroomGateway into the live path:
spawn a role's session, route ChatroomEvents to it (dedup I4, per-session
FIFO I9), and complete + post the adapter's replies (I5 idempotency_key).
The dispatcher provides each session's :class:`SpawnContext` callbacks —
``on_reply`` (reply completion → gateway) and ``on_event_log``
(observational, I7-isolated).

Out of scope here (Step 3 / Phase 1 dogfood): retry / dead-letter on
gateway failure (a failed post propagates and fails the delivery,
fail-loud), §8 smoke test, ChatroomWatcher (T14) intake.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..ports import AdapterRegistry, RoleAdapter, SpawnContext
from ..value_objects import ChatroomEvent, Event, ReplyDraft, Role, SessionHandle, ThreadRef
from .dedup import DEFAULT_DEDUP_SET_SIZE, EventDedup
from .event_log import delivery_failed_event, reply_sent_event
from .gateway import ChatroomGateway
from .session_fsm import SessionStateMachine

logger = logging.getLogger(__name__)

EventSink = Callable[[Event], Awaitable[None]]
"""Async sink for observational Event-log entries (flat JSONL writer at runtime)."""


class NoQualifiedAdapterError(LookupError):
    """Raised by :meth:`Dispatcher.spawn_instance` when no adapter qualifies for a role."""


class UnknownSessionError(KeyError):
    """Raised for a SessionHandle the dispatcher did not spawn."""

    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        self.session_id = session_id

    def __str__(self) -> str:
        return f"dispatcher has no session {self.session_id!r}"


@dataclass
class _DispatchSession:
    adapter: RoleAdapter
    fsm: SessionStateMachine
    lock: asyncio.Lock
    handle: SessionHandle | None = None
    reply_seq: int = 0


class Dispatcher:
    """Routes ChatroomEvents to adapter sessions and posts their replies."""

    def __init__(
        self,
        *,
        registry: AdapterRegistry,
        gateway: ChatroomGateway,
        event_sink: EventSink | None = None,
        dedup_size: int = DEFAULT_DEDUP_SET_SIZE,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._event_sink = event_sink
        self._dedup = EventDedup(max_size=dedup_size)
        self._sessions: dict[SessionHandle, _DispatchSession] = {}

    async def spawn_instance(
        self, thread_ref: ThreadRef, role: Role, instance_id: str
    ) -> SessionHandle:
        """Spawn a qualified adapter session as instance ``instance_id``.

        ``role`` is retained (Instance attribute) for adapter selection;
        ``instance_id`` is the stable per-instance label (ADR-2026-05-24-08
        §2.2) handed to the adapter via :attr:`SpawnContext.own_instance_id`
        so it lands on the returned :class:`SessionHandle`. Phase 1 callers
        mint it with :func:`~spirrow_mindwire.value_objects.mint_instance_id`
        (``"{role}-1"``); the dispatcher does not mint here (parallel
        allocation is a caller/Phase 2 concern, the seam is in ``mint_*``).

        Phase 1 policy = first qualified adapter (registry lookup only;
        ADR-06 §3.2 ``qualified_for`` policy-isation is Phase 2). Raises
        :class:`NoQualifiedAdapterError` if none qualify (e.g. no
        ``NAYSAYER_QUALIFIED`` adapter for the naysayer slot — ADR-05 §5).
        Raises :class:`ValueError` on an empty/blank ``instance_id``: it becomes
        the chatroom reply author (I3 v2.2), so a blank label would post an
        author-less reply. Phase 1's only caller (``WatchSpec``) always mints a
        non-empty id, but this Port is the Phase 2 public surface — fail loud.
        """
        if not instance_id.strip():
            raise ValueError("spawn_instance requires a non-empty instance_id")
        candidates = self._registry.qualified_for(role)
        if not candidates:
            raise NoQualifiedAdapterError(f"no adapter qualified for role {role.value!r}")
        adapter = candidates[0]
        session = _DispatchSession(adapter=adapter, fsm=SessionStateMachine(), lock=asyncio.Lock())

        async def on_reply(draft: ReplyDraft) -> None:
            await self._handle_reply(session, draft)

        ctx = SpawnContext(
            on_reply=on_reply,
            on_event_log=self._on_event_log,
            own_role=role,
            own_instance_id=instance_id,
        )
        handle = await adapter.spawn(thread_ref, role, ctx)
        session.handle = handle
        self._sessions[handle] = session
        return handle

    async def dispatch(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        """Route one event to its session (I4 dedup + I9 per-session FIFO)."""
        session = self._sessions.get(handle)
        if session is None:
            raise UnknownSessionError(handle.session_id)
        if self._dedup.seen(event.event_id):
            return  # I4: this ULID event_id was already processed
        # Mark before delivery — required for correct dedup under concurrency
        # (marking after the await would let two concurrent same-event_id
        # dispatches both pass seen()). A failed delivery is therefore not
        # retried under the same event_id: Phase 1 is fail-loud; retry /
        # dead-letter (and any dedup redesign it needs) is out of scope.
        self._dedup.mark(event.event_id)
        # I9: serialize deliver_event per SessionHandle (FIFO by call order; the
        # caller delivers in occurred_at order — ChatRoom msg-id monotonic).
        async with session.lock:
            try:
                await session.adapter.deliver_event(handle, event)
            except asyncio.CancelledError:
                raise  # let cancellation propagate immediately (no FAILED noise)
            except Exception as exc:
                # §8: record a FAILED event before re-raising (fail-loud).
                # _on_event_log isolates sink errors (I7).
                await self._on_event_log(delivery_failed_event(handle, event, exc))
                raise

    async def halt(self, handle: SessionHandle) -> None:
        """Halt a spawned session via its adapter (idempotent per I8)."""
        session = self._sessions.get(handle)
        if session is None:
            raise UnknownSessionError(handle.session_id)
        await session.adapter.halt(handle)

    async def aclose(self) -> None:
        """Halt every spawned session (best-effort), symmetric with ``ChatroomWatcher.stop()``.

        The watcher records its handles and halts them in ``stop()``; the conductor instead spawns
        sessions directly through this dispatcher (no watcher records the handles), so the conductor
        daemon teardown calls this to halt the adapter sessions — e.g. the Claude SDK subprocess —
        so they disconnect cleanly and don't leak transports at interpreter exit. Halt is idempotent
        (I8); a failing halt is logged and the rest still run. Safe to call when no session is open.
        """
        for handle in list(self._sessions):
            try:
                await self.halt(handle)
            except Exception:
                logger.exception("halt failed during dispatcher aclose; continuing")
        self._sessions.clear()

    async def _handle_reply(self, session: _DispatchSession, draft: ReplyDraft) -> None:
        # Runs inside the adapter's deliver_event (under the session lock) per
        # the SpawnContext.on_reply contract, so reply_seq increments are
        # serialized → I5 monotonic per session.
        if session.handle is None:  # pragma: no cover - handle set before any delivery
            raise RuntimeError("reply emitted before the session handle was assigned")
        handle = session.handle
        session.reply_seq += 1
        idempotency_key = f"{handle.session_id}:{session.reply_seq}"  # I5
        posted_msg_id = await self._gateway.post_reply(
            handle.thread_ref,
            author=handle.instance_id,  # I3 v2.2 (ADR-06 amendment): author = instance_id
            body=draft.body,
            reply_to_msg_id=draft.reply_to_msg_id,
            idempotency_key=idempotency_key,
        )
        await self._on_event_log(
            reply_sent_event(
                handle, draft, posted_msg_id=posted_msg_id, idempotency_key=idempotency_key
            )
        )

    async def _on_event_log(self, event: Event) -> None:
        # I7: observational channel — a raising sink must NOT break the main
        # flow; failures are isolated to an internal log.
        if self._event_sink is None:
            return
        try:
            await self._event_sink(event)
        except Exception:
            logger.warning("event-log sink raised; isolated per I7", exc_info=True)


__all__ = [
    "Dispatcher",
    "EventSink",
    "NoQualifiedAdapterError",
    "UnknownSessionError",
]
