"""Claude Code session integration (watcher → Claude Agent SDK).

Phase 0 surface:

- :func:`build_thread_prompt` — render a thread's state as the XML
  payload the SDK consumes (architecture.md §6.4)
- :func:`invoke_claude_code` + :data:`SYSTEM_PROMPT` — orchestrate one
  SDK session (architecture.md §6.1-§6.2)
- :func:`build_mindwire_mcp_server` / :func:`build_mindwire_tools` —
  in-process MCP server with the 5 custom tools (architecture.md §6.3)
"""

from __future__ import annotations

from .prompt import build_thread_prompt
from .session import InvokeResult, InvokeTimeoutError, TimeoutKind, invoke_claude_code
from .system_prompt import SYSTEM_PROMPT
from .tools import build_mindwire_mcp_server, build_mindwire_tools

__all__ = [
    "SYSTEM_PROMPT",
    "InvokeResult",
    "InvokeTimeoutError",
    "TimeoutKind",
    "build_mindwire_mcp_server",
    "build_mindwire_tools",
    "build_thread_prompt",
    "invoke_claude_code",
]
