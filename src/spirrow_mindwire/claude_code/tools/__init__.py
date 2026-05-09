"""In-process MindWire custom MCP tools.

The 5 tools (``write_reply`` + 4 Phanthand-backed read tools) form the
sole tool surface claude-code sees in Phase 0; SDK built-ins are
disabled (architecture.md §6.2).
"""

from __future__ import annotations

from .mindwire_server import build_mindwire_mcp_server, build_mindwire_tools

__all__ = ["build_mindwire_mcp_server", "build_mindwire_tools"]
