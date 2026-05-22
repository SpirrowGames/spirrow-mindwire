"""ChatroomGateway — ADR-2026-05-21-06 §3.3 (T12, dispatcher-internal).

The dispatcher's bridge to the ChatRoom for *posting* completed replies.
Per ADR-06 §3.3 this is intentionally **not** a Port (Phase 1 fixes one
ChatRoom implementation = magickit chatroom); it is an internal
abstraction so the dispatcher core stays testable (a fake gateway in
tests) while the concrete magickit-MCP-backed implementation is injected
at runtime (watcher / runtime wiring, T14 / Step 3).
"""

from __future__ import annotations

from typing import Protocol

from ..value_objects import Role, ThreadRef


class ChatroomGateway(Protocol):
    """Posts dispatcher-completed replies to the ChatRoom (ADR-06 §3.3)."""

    async def post_reply(
        self,
        thread_ref: ThreadRef,
        *,
        author: Role,
        body: str,
        reply_to_msg_id: str | None,
        idempotency_key: str,
    ) -> str:
        """Post a reply and return the ChatRoom-assigned message id.

        ``author`` is the session role (ADR-06 §2.4 dispatcher-completed
        field; I3 author = role). ``idempotency_key`` is the I5 dedup key
        (``f"{session_id}:{reply_seq}"``) for ChatRoom-side dedup.
        """
        ...


__all__ = ["ChatroomGateway"]
