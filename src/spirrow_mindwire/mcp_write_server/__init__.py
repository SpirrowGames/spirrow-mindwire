"""mindwire-mcp-server: write-only HTTP MCP API for claude.ai-side callers.

Phase 1 Feature 3-A sub-PR 2 (= integrator decide T-feat3-d2-mcp-server
msg-127). Distinct from:

- :mod:`spirrow_mindwire.mcp_server` — the read-only stub at the
  ``mindwire-mcp`` entry point, kept as status quo per the 3-layer
  separation decision (D2-3 = A, see docs/feature-3-design.md §2.1).
- :mod:`spirrow_mindwire.claude_code.tools.mindwire_server` — the
  in-process MCP server the watcher injects into claude-code during
  dispatch.

This subpackage runs as a *separate process* (= operator manual startup,
``uv run mindwire-mcp-server``) and coordinates with the watcher only
via the on-disk thread directory.
"""

from spirrow_mindwire.mcp_write_server.http import build_app, main

__all__ = ["build_app", "main"]
