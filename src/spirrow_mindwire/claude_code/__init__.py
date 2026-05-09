"""Claude Code session integration (watcher → Claude Agent SDK).

Phase 0 surface (built incrementally as Feature 1 sub-PRs land):

- :func:`build_thread_prompt` — render a thread's state as the XML
  payload the SDK consumes (architecture.md §6.4)
- :func:`invoke_claude_code` + :data:`SYSTEM_PROMPT` — orchestrate one
  SDK session (architecture.md §6.1-§6.2)
- custom tools (write_reply / read_file / list_dir / search /
  file_info) land in a subsequent sub-PR
"""

from __future__ import annotations

from .prompt import build_thread_prompt
from .session import InvokeResult, invoke_claude_code
from .system_prompt import SYSTEM_PROMPT

__all__ = [
    "SYSTEM_PROMPT",
    "InvokeResult",
    "build_thread_prompt",
    "invoke_claude_code",
]
