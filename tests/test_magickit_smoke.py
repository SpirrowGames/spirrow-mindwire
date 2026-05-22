"""Manual smoke test for the live magickit MCP path (ADR-06 §8, PR-G).

Marked ``manual`` → excluded from CI (``addopts -m "not manual"``); run with
``uv run pytest -m manual``. Requires the local no-auth magickit MCP reachable
at ``MINDWIRE_MAGICKIT_MCP_URL`` (default ``http://100.79.84.62:8117/mcp``,
Tailscale — chatroom thread msg-193).

Scope (flagged for ADR-verify): the automated case is a **read-only
connectivity check** (``chatroom_list_threads``). The full §8 round-trip —
spawn a real ``ClaudeCodeSdkAdapter`` (real Claude Agent SDK subprocess →
Claude API cost) + post into a live thread + assert an ``author=proposer``
reply — is a **documented dogfood procedure** rather than an automated test,
because it incurs API cost and posts to a live chatroom (side effects) and
cannot be validated in this dev environment. Full-round-trip procedure:

  1. Start the watcher against a throwaway test thread + a human message::

         mcp = StreamableHttpChatroomMcp()
         registry = InMemoryAdapterRegistry()
         registry.register(ClaudeCodeSdkAdapter(cwd=Path.cwd()))
         dispatcher = Dispatcher(registry=registry, gateway=MagickitChatroomGateway(mcp))
         watcher = ChatroomWatcher(mcp, dispatcher, [WatchSpec(thread_ref, Role.PROPOSER)])
         await watcher.start(); await watcher.poll_once()

  2. Verify an ``author=proposer`` message appears in the thread.
"""

from __future__ import annotations

import pytest

from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp


@pytest.mark.manual
@pytest.mark.anyio
async def test_magickit_connectivity_list_threads() -> None:
    mcp = StreamableHttpChatroomMcp()  # MINDWIRE_MAGICKIT_MCP_URL or default
    result = await mcp.call_tool("chatroom_list_threads", {"project": "spirrow-mindwire"})
    assert isinstance(result, dict)
    assert "items" in result
