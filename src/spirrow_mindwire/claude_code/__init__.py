"""Claude Code session integration (watcher → Claude Agent SDK).

Phase 0 surface (built incrementally as Feature 1 sub-PRs land):

- :func:`build_thread_prompt` — render a thread's state as the XML
  payload the SDK consumes (architecture.md §6.4)
- session / system_prompt / custom tools land in subsequent sub-PRs
"""

from __future__ import annotations

from .prompt import build_thread_prompt

__all__ = ["build_thread_prompt"]
