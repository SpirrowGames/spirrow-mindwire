"""Magickit chatroom integration — MCP client + concrete ChatroomGateway (T12).

Runtime wiring to the local no-auth magickit chatroom MCP (ADR-06 §3.3).
The ChatroomWatcher (T14) read side lands in a follow-up (PR-G).
"""

from __future__ import annotations

from .client import (
    MagickitMcpError,
    McpToolCaller,
    StreamableHttpChatroomMcp,
    magickit_mcp_url,
    parse_tool_result,
)
from .gateway import MagickitChatroomGateway

__all__ = [
    "MagickitChatroomGateway",
    "MagickitMcpError",
    "McpToolCaller",
    "StreamableHttpChatroomMcp",
    "magickit_mcp_url",
    "parse_tool_result",
]
