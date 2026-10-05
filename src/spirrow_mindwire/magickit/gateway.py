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

``disposition`` (T-next-line-carries-who-not-why Slice 3, Bohr msg-5175 §3 /
msg-5179 §3): when the reply ends in ``NEXT: none`` with a valid ``STOP:`` line
directly above it, the parsed disposition is sent with the post
(:func:`~spirrow_mindwire.conductor.disposition.body_disposition`). An ``ABSENT``
or ``MALFORMED`` line sends nothing, and magickit records the post exactly as
before. A disposition magickit refuses never blocks the post: it is re-sent without the
field (see :data:`DISPOSITION_VALUE_ERRORS`). The wake is resolved on the
conductor roster, so the gateway needs one; a gateway built without a roster
(the watcher loop) never sends a disposition.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..conductor.disposition import body_disposition
from ..value_objects import Role, ThreadRef
from .client import MagickitMcpError, McpToolCaller

logger = logging.getLogger(__name__)

#: magickit #98's refusal when the identity registry could not answer the disposition checks. The
#: one disposition refusal that is not about the value.
DISPOSITION_UNAVAILABLE_ERROR = "DispositionValidationUnavailableError"

#: magickit #98's refusals of the disposition VALUE. The value comes from the agent's own ``STOP:``
#: line, not from configuration, so a refusal here is the agent's mistake, and it is the same
#: mistake every time the same context is replayed. Propagating it would drop the whole post,
#: leave the head where it was, and relaunch the agent into the same mistake on every tick: a
#: permanent stall (PR #455 naysayer, REQUEST_CHANGES). So the post is re-sent WITHOUT the field
#: and the refusal is logged at ERROR. The thread moves, magickit records a null disposition (no
#: false claim is stored), and the malformed line stays in the body for the digest to see.
DISPOSITION_VALUE_ERRORS = frozenset(
    {
        "DispositionInvalidError",
        "DispositionHumanNotAllowedError",
        "DispositionWakeUnknownError",
    }
)


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

    def __init__(
        self,
        mcp: McpToolCaller,
        *,
        reply_msg_type: str = "report",
        roster: Mapping[str, Role] | None = None,
    ) -> None:
        self._mcp = mcp
        self._reply_msg_type = reply_msg_type
        self._roster = roster

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
        disposition = body_disposition(body, self._roster) if self._roster is not None else None
        if disposition is None:
            result = await self._mcp.call_tool("chatroom_post_message", arguments)
            return _extract_msg_id(result)
        try:
            result = await self._mcp.call_tool(
                "chatroom_post_message", {**arguments, "disposition": disposition}
            )
        except MagickitMcpError as exc:
            # A refusal of the DISPOSITION is answered by posting without it. The body still
            # carries the ``STOP:`` line, which is what the park-wake tick and the digest classify
            # from (magickit msg-1015 v11 §1), so nothing is lost. Any OTHER refusal (``role``,
            # an unknown author, a resolved thread...) propagates exactly as it did before
            # Slice 3: those checks concern configuration, and retrying past them would disarm
            # them. The disposition is different: an agent wrote it (DISPOSITION_VALUE_ERRORS).
            if exc.error_type == DISPOSITION_UNAVAILABLE_ERROR:
                logger.warning(
                    "disposition not sent (identity registry unavailable): thread=%s author=%s "
                    "disposition=%s — posting without it; the STOP: line stays in the body",
                    thread_ref.thread_id,
                    author,
                    disposition,
                )
            elif exc.error_type in DISPOSITION_VALUE_ERRORS:
                logger.error(
                    "disposition refused (%s): thread=%s author=%s disposition=%s — posting "
                    "without it so the head moves; magickit records a null disposition",
                    exc.error_type,
                    thread_ref.thread_id,
                    author,
                    disposition,
                )
            else:
                raise
            result = await self._mcp.call_tool("chatroom_post_message", arguments)
        return _extract_msg_id(result)


__all__ = ["DISPOSITION_UNAVAILABLE_ERROR", "DISPOSITION_VALUE_ERRORS", "MagickitChatroomGateway"]
