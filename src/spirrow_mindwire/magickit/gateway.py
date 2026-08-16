"""MagickitChatroomGateway — concrete ChatroomGateway (ADR-06 §3.3, T12).

Implements :class:`spirrow_mindwire.dispatcher.gateway.ChatroomGateway` by
posting dispatcher-completed replies through the magickit
``chatroom_post_message`` MCP tool. Dispatcher-internal (not a Port, §3.3);
the transport is an injected :class:`~spirrow_mindwire.magickit.client.McpToolCaller`.

Two integration-semantics choices flagged for ADR-verify:

1. **msg_type** — a role's chatroom reply needs a `chatroom_post_message`
   ``msg_type`` (propose/question/answer/decide/report/handoff/ack). The
   ReplyDraft does not carry one, so the gateway uses a configurable default
   (``reply_msg_type``, default ``"report"``).
2. **idempotency_key (I5)** — magickit ``chatroom_post_message`` exposes no
   idempotency parameter, so the dispatcher-computed key cannot be passed to
   the ChatRoom. The key is **accepted for contract compatibility but
   currently unused** (there is no magickit field to carry it). ChatRoom-side
   dedup stays Phase 2 (the original ADR-06 I5 note: "ChatRoom 側 dedup
   サポートが未実装なら Phase 2 対応") and is wired in when magickit adds an
   idempotency field.
"""

from __future__ import annotations

from typing import Any

from ..value_objects import Role, ThreadRef
from .client import MagickitMcpError, McpToolCaller


def _extract_msg_id(result: Any) -> str:
    msg = result.get("msg") if isinstance(result, dict) else None
    msg_id = msg.get("msg_id") if isinstance(msg, dict) else None
    if isinstance(msg_id, str):
        return msg_id
    raise MagickitMcpError(f"chatroom_post_message returned no msg_id: {result!r}")


class MagickitChatroomGateway:
    """Posts replies to the magickit chatroom via ``chatroom_post_message``.

    Conforms to :class:`~spirrow_mindwire.dispatcher.gateway.ChatroomGateway`.
    """

    def __init__(self, mcp: McpToolCaller, *, reply_msg_type: str = "report") -> None:
        self._mcp = mcp
        self._reply_msg_type = reply_msg_type

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
        # idempotency_key is computed by the dispatcher (I5) but magickit
        # chatroom_post_message has no idempotency field, so it is accepted for
        # contract compatibility but currently unused → ChatRoom-side dedup is
        # Phase 2 (original ADR-06 I5 note).
        _ = idempotency_key
        arguments: dict[str, Any] = {
            "project": thread_ref.project_id,
            "thread_id": thread_ref.thread_id,
            "msg_type": self._reply_msg_type,
            "author": author,  # I3 v2.2 (ADR-06 amendment): author = instance_id
            "content": body,
        }
        if reply_to_msg_id is not None:
            arguments["reply_to"] = reply_to_msg_id
        # D-1 (T-dispatched-turn-gets-one-message). Sent, never silently dropped:
        # a ``RoleNotAllowed`` from the far end is the gate WORKING (that identity
        # may not claim that role) and must propagate, not be retried without the
        # role — retrying without it is disarming the check, not recovering from it.
        #
        # One thing this cannot do from here: the chatroom drops an unverified role
        # for an author with no registered identity, posting the message anyway with
        # ``role: null``. So the value lands only for authors registered in magickit.
        # The conductor authors under the roster persona (``Einstein`` …), which is
        # registered — non-null roles from manual turns by those same names are the
        # evidence — but a harness-only author would silently record null.
        if role is not None:
            arguments["role"] = role.value
        result = await self._mcp.call_tool("chatroom_post_message", arguments)
        return _extract_msg_id(result)


__all__ = ["MagickitChatroomGateway"]
