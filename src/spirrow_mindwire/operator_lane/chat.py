"""The lane's two chatroom operations: read a thread, post an operator report.

Producer declaration (OBL-CHATROOM-PRODUCER-READER-SURFACE) for every post made through
:meth:`Chat.post` — the runner's report, the conductor's re-post and the quarantine post:

- Intended reader: the persona who wrote the ``OPERATOR-TASK`` (the report's ``NEXT:`` hands back
  to them); for a quarantine or needs-hands stop, the person who takes operator work by hand.
- Fallback surface when the target thread is gone: disposition **(1) an alternative durable
  surface**. A :class:`ThreadResolvedError` is not retried. The operator pass turns it into a
  ``notify`` event, which the conductor wrapper writes to its log and sends as a Discord
  notification, and the report itself stays on disk under ``operator_reports\\done\\``. Nothing
  is ever posted into another thread.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from ..magickit.client import McpToolCaller, StreamableHttpChatroomMcp


class Chat(Protocol):
    def messages(self, project: str, thread_id: str) -> list[dict[str, Any]]: ...

    def post(self, project: str, thread_id: str, content: str) -> str:
        """Post ``content`` as the operator; return the new ``msg_id`` (raise on failure)."""


class McpChat:
    def __init__(
        self,
        author: str,
        caller_factory: Callable[[], McpToolCaller] = StreamableHttpChatroomMcp,
    ) -> None:
        self._author = author
        self._factory = caller_factory

    def messages(self, project: str, thread_id: str) -> list[dict[str, Any]]:
        async def _get() -> Any:
            return await self._factory().call_tool(
                "chatroom_get_thread", {"project": project, "thread_id": thread_id, "mode": "full"}
            )

        result = asyncio.run(_get())
        msgs = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(msgs, list):
            raise RuntimeError(f"chatroom_get_thread returned no messages for {thread_id}")
        return [m for m in msgs if isinstance(m, dict)]

    def post(self, project: str, thread_id: str, content: str) -> str:
        async def _post() -> Any:
            return await self._factory().call_tool(
                "chatroom_post_message",
                {
                    "project": project,
                    "thread_id": thread_id,
                    "msg_type": "report",
                    "author": self._author,
                    "content": content,
                },
            )

        result = asyncio.run(_post())
        msg = result.get("msg") if isinstance(result, dict) else None
        msg_id = msg.get("msg_id") if isinstance(msg, dict) else None
        if not isinstance(msg_id, str) or not msg_id:
            raise RuntimeError(f"chatroom_post_message returned no msg_id: {result!r}")
        return msg_id


__all__ = ["Chat", "McpChat"]
