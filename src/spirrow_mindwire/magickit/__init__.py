"""Magickit chatroom integration — MCP client + ChatroomGateway (T12) + Watcher (T14).

Runtime wiring to the local no-auth magickit chatroom MCP (ADR-06 §3.3 / §7):
the gateway posts replies, the watcher reads new messages and feeds the
dispatcher.
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
from .watcher import ChatroomWatcher, WatchSpec

__all__ = [
    "ChatroomWatcher",
    "MagickitChatroomGateway",
    "MagickitMcpError",
    "McpToolCaller",
    "StreamableHttpChatroomMcp",
    "WatchSpec",
    "magickit_mcp_url",
    "parse_tool_result",
]
