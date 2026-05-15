"""HTTP MCP transport + tool registration for the mindwire-mcp-server.

Constructs a :class:`mcp.server.fastmcp.FastMCP` bound to
``MCPServerConfig.host:port``, wraps the Starlette app it produces with
:class:`ApiKeyMiddleware`, and starts uvicorn for operator-manual
foreground lifecycle (= integrator decide T-feat3-d2-mcp-server msg-127
§1 D2-2: async + MCP SDK + operator manual startup).

**Cross-process invariant** (= integrator decide §1 D2-6, also recorded
in docs/feature-3-design.md §2.1): this server and the in-process
watcher dispatcher coordinate only via the on-disk thread directory
(``meta.yaml`` / ``messages/*`` / ``logs/threads/<ULID>.jsonl``). No
shared memory, no socket-level RPC. Both processes therefore see the
same race-acceptance contract documented in
``docs/feature-2-design.md`` §3.6 (= operator should stop the watcher
before destructive manual edits). The api-key boundary lives between
caller and this server; the watcher does not consult it.

**Tool registration**: the 3 write tools (``send_message`` /
``open_thread`` / ``resolve_thread``) live in :mod:`tools_write` and
are wired in via :func:`_register_write_tools`. Per integrator decide
§1 D2-1 (= the 3-tool minimal set, Naysayer Q4 frame inversion: an
additional ``update_awaiting_from`` tool is deferred until an observed
driver demands toggle-without-message).
"""

from __future__ import annotations

import logging

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette

from spirrow_mindwire.config import MindwireSettings, load_settings
from spirrow_mindwire.mcp_write_server.auth import ApiKeyMiddleware, read_api_key
from spirrow_mindwire.mcp_write_server.tools_write import WriteTools, register_tools

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
"""URL path where the streamable-http MCP transport is mounted.

Clients connect to ``http://<host>:<port>/mcp``. Kept as a module
constant so tests and clients can import it without depending on
FastMCP's default.
"""

SERVER_NAME = "mindwire-write"
"""Logical MCP server name advertised in the streamable-http handshake.

Distinct from the in-process ``mindwire`` server (the one the watcher
injects into claude-code) so a single caller can connect to both
without name collisions.
"""


def build_app(settings: MindwireSettings, *, api_key: str) -> Starlette:
    """Build the Starlette ASGI app for the write-only MCP server.

    Returns a fully-wired app: streamable-http MCP endpoint at
    :data:`MCP_PATH`, all routes gated by :class:`ApiKeyMiddleware`.
    Tool registration is performed via :func:`_register_write_tools` —
    a no-op stub in commit 2; commit 3 fills it.
    """
    fastmcp = FastMCP(
        name=SERVER_NAME,
        host=settings.mcp_server.host,
        port=settings.mcp_server.port,
        streamable_http_path=MCP_PATH,
    )
    _register_write_tools(fastmcp, settings)
    app = fastmcp.streamable_http_app()
    app.add_middleware(ApiKeyMiddleware, api_key=api_key)
    return app


def _register_write_tools(fastmcp: FastMCP, settings: MindwireSettings) -> None:
    """Register the 3 write tools on ``fastmcp``.

    Constructs a single :class:`WriteTools` instance bound to
    ``settings.paths.data_dir`` and shared across all incoming requests.
    The per-thread asyncio.Lock dict inside :class:`WriteTools` is what
    serialises same-thread send_message / resolve_thread calls within
    this process.
    """
    tools = WriteTools(data_dir=settings.paths.data_dir)
    register_tools(fastmcp, tools)


def main() -> None:
    """Entry point for the ``mindwire-mcp-server`` CLI.

    Loads :class:`MindwireSettings`, resolves the API key from the env
    var named in :attr:`MCPServerConfig.api_key_env`, builds the app,
    and runs uvicorn in the foreground. Operator-manual lifecycle
    (start with ``uv run mindwire-mcp-server``, stop with Ctrl-C) per
    integrator decide §1 D2-2.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()
    api_key = read_api_key(settings)
    app = build_app(settings, api_key=api_key)
    logger.info(
        "starting %s at http://%s:%d%s",
        SERVER_NAME,
        settings.mcp_server.host,
        settings.mcp_server.port,
        MCP_PATH,
    )
    uvicorn.run(
        app,
        host=settings.mcp_server.host,
        port=settings.mcp_server.port,
        log_level="info",
    )


__all__ = ["MCP_PATH", "SERVER_NAME", "build_app", "main"]
