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
        author: str,
        body: str,
        reply_to_msg_id: str | None,
        idempotency_key: str,
        role: Role | None = None,
    ) -> str:
        """Post a reply and return the ChatRoom-assigned message id.

        ``author`` is the session's stable ``instance_id`` (e.g.
        ``"proposer-1"``) — ADR-06 §2.4 dispatcher-completed field, I3 v2.2
        (ADR-06 amendment): author = instance_id, not the bare role.
        ``idempotency_key`` is the I5 dedup key
        (``f"{session_id}:{reply_seq}"``) for ChatRoom-side dedup.

        ``role`` is the role the session was spawned for
        (T-dispatched-turn-gets-one-message D-1). The chatroom validates it
        against the author identity's ``allowed_roles`` *before writing
        anything*, so a recorded non-null role means "this identity was verified
        able to claim this role" — the I-6 invariant the loop's gates are named
        after. It was receivable from the day it shipped and never once supplied
        from here: measured on the live corpus 2026-08-16, 38/38 harness-attested
        naysayer posts recorded ``role: null``, i.e. the gate role was unarmed in
        every autonomous post it had ever made. ``None`` omits the parameter
        entirely rather than sending an empty string, because an empty string is
        a claim the server would try to validate.
        """
        ...


__all__ = ["ChatroomGateway"]
