"""Magickit MCP client — Streamable HTTP transport to the local chatroom.

Thin client over the ``mcp`` package's Streamable HTTP transport, used by
:class:`~spirrow_mindwire.magickit.gateway.MagickitChatroomGateway` (T12)
and (Step 3 PR-G) the ChatroomWatcher (T14) to reach the magickit chatroom
MCP tools.

Runtime target (chatroom thread ``T-phase1-impl-t11-t13`` msg-193): the
**local no-auth** magickit MCP instance on sg-ai-server-01's Tailscale IP,
default ``http://100.79.84.62:8117/mcp``, overridable via
``MINDWIRE_MAGICKIT_MCP_URL`` (the IP is environment-dependent — do not
hardcode it elsewhere). No auth (the Tailscale boundary gates access).

:class:`StreamableHttpChatroomMcp` does real network I/O and is therefore
exercised only by the ``-m manual`` smoke test (PR-G), not CI; the pure
result-parsing helper and all gateway/watcher *logic* are unit-tested
against the :class:`McpToolCaller` Protocol with fakes.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

_DEFAULT_MAGICKIT_MCP_URL = "http://100.79.84.62:8117/mcp"
"""Default local no-auth magickit MCP endpoint (Tailscale). Env-overridable; not to be hardcoded."""


def magickit_mcp_url() -> str:
    """Resolve the magickit MCP URL from ``MINDWIRE_MAGICKIT_MCP_URL`` (env) or the default."""
    # Treat unset *or empty* as "use default" (an empty URL would fail confusingly).
    return os.environ.get("MINDWIRE_MAGICKIT_MCP_URL") or _DEFAULT_MAGICKIT_MCP_URL


class MagickitMcpError(RuntimeError):
    """An MCP tool call to magickit failed or returned an unusable result."""


class McpToolCaller(Protocol):
    """Calls one magickit MCP tool and returns its parsed JSON result.

    Satisfied by :class:`StreamableHttpChatroomMcp`; gateway/watcher tests
    inject a fake.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


def parse_tool_result(result: Any) -> Any:
    """Extract the JSON payload from an MCP ``CallToolResult``.

    Prefers ``structuredContent``; falls back to JSON-decoding the first
    text content block. Raises :class:`MagickitMcpError` on a tool error.
    Pure (duck-typed) so it is unit-testable without a live server.
    """
    if getattr(result, "isError", False):
        raise MagickitMcpError(f"magickit tool reported isError: {result!r}")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue  # not JSON — try the next content block
    raise MagickitMcpError(f"magickit tool returned no JSON content: {result!r}")


class StreamableHttpChatroomMcp:
    """:class:`McpToolCaller` over the ``mcp`` Streamable HTTP transport.

    Connects per call (Phase 1; low volume). A persistent session is a
    Phase 2 optimization. **Real network I/O — covered by the ``-m manual``
    smoke test, not CI.**
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url if url is not None else magickit_mcp_url()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            async with (
                streamablehttp_client(self._url) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.call_tool(name, arguments)
        except MagickitMcpError:
            raise
        except Exception as exc:
            raise MagickitMcpError(f"magickit MCP call {name!r} failed: {exc}") from exc
        return parse_tool_result(result)


__all__ = [
    "MagickitMcpError",
    "McpToolCaller",
    "StreamableHttpChatroomMcp",
    "magickit_mcp_url",
    "parse_tool_result",
]
