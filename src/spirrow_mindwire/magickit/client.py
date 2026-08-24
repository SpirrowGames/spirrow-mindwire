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


def envelope_error(payload: Any) -> str | None:
    """The failure described by a chatroom payload, or ``None`` if it is not one.

    conclair does not raise when a chatroom call is refused. It answers with an
    ordinary **success** response whose body is an error envelope::

        {"error_type": "ChatroomNotFoundError",
         "error": "Thread 'T-pr-review-spirrow-magickit-22' not found in project '...'",
         "details": {"project": "...", "thread_id": "..."}}

    ``isError`` is not set, so ``parse_tool_result`` used to find JSON, return
    it, and every ``except MagickitMcpError`` above the transport was simply
    never reached. ``chatroom_open_thread``'s own MCP docstring says as much --
    *"On success: {...}. On failure: conclair error envelope {...}"* -- so the
    contract every caller was written against never existed.

    Detection is by ``error_type`` -- a fact the far end states -- rather than
    by an expected key being absent, which is how "absent" once became
    "unidentifiable" in the ``PrReviewOrchestrator`` regression (#150). Living
    here in the client rather than at any one call site means the seventh
    caller in the future cannot re-open the same hole by omission.

    Public because :class:`McpToolCaller` fakes in the test suite must be able
    to mimic what :func:`parse_tool_result` does with an envelope -- otherwise
    a fake that returns a raw envelope dict to a caller papers over the very
    regression this refactor exists to close (msg-1115 §5 DoD #3 / #150's
    "27 tests green against a contract that does not exist").
    """
    if not isinstance(payload, dict):
        return None
    error_type = payload.get("error_type")
    if not isinstance(error_type, str) or not error_type:
        return None
    message = payload.get("error")
    return f"{error_type}: {message}" if isinstance(message, str) and message else error_type


def raise_if_envelope(payload: Any) -> None:
    """Raise :class:`MagickitMcpError` iff ``payload`` is a chatroom error envelope.

    The one place that turns "here is what the far end said no with" into an
    exception. :func:`parse_tool_result` calls it after decoding a live
    response; a test fake that wants to simulate a refusal calls it on its
    scripted payload before returning. Keeping both paths on this single
    function is what the DoD #3 rule "the fake is raised from the measured
    envelope shape" means in code -- there is no second, drifting definition
    of "envelope" for tests to fall out of sync with.
    """
    failure = envelope_error(payload)
    if failure is not None:
        raise MagickitMcpError(f"magickit tool returned an error envelope: {failure}")


def parse_tool_result(result: Any) -> Any:
    """Extract the JSON payload from an MCP ``CallToolResult``.

    Prefers ``structuredContent``; falls back to JSON-decoding the first
    text content block. Raises :class:`MagickitMcpError` on a tool error --
    either an explicit ``isError`` from the transport or a conclair **error
    envelope** returned inside a nominally-successful response (see
    :func:`_envelope_error`). Pure (duck-typed) so it is unit-testable without
    a live server.

    Elevating the envelope at this one boundary is what msg-1115 §4 asked for:
    the six call sites that read envelopes as successes (silent empty lists,
    silent successful dashboard writes, an all-green run reported on a critique
    that never landed) now cannot -- their transport speaks the same language
    as an ``isError`` transport failure, and the failure is visible as an
    exception at the point it was refused. Callers that want to soften a
    refusal (``LoopControlReader.read`` fails safe to ``hold``; the orchestrator
    swallows only ``already exists`` at open time) catch it explicitly; the
    default is "raise".
    """
    if getattr(result, "isError", False):
        raise MagickitMcpError(f"magickit tool reported isError: {result!r}")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        payload: Any = structured
    else:
        payload = _first_json_block(result)
        if payload is _MISSING:
            raise MagickitMcpError(f"magickit tool returned no JSON content: {result!r}")
    raise_if_envelope(payload)
    return payload


_MISSING: Any = object()


def _first_json_block(result: Any) -> Any:
    """The first JSON-decodable text content block, or :data:`_MISSING` if none.

    Split out so :func:`parse_tool_result` reads top-down as ``isError → payload
    → envelope`` without a nested loop obscuring where the envelope check runs.
    """
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue  # not JSON — try the next content block
    return _MISSING


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
    "envelope_error",
    "magickit_mcp_url",
    "parse_tool_result",
    "raise_if_envelope",
]
