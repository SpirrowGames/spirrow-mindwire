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

import httpx
from mcp import ClientSession, McpError
from mcp.client.streamable_http import StreamableHTTPError, streamablehttp_client

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


def is_envelope(payload: Any) -> bool:
    """``True`` iff ``payload`` looks like a chatroom **error envelope** (§3 detection rule).

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

    **The detection is deliberately loose** — any top-level ``error_type: <str>``
    is treated as an envelope. This is msg-1685 §1's decision, not accident. Two
    error directions are possible:

    - *False positive* (a legitimate success payload happens to include a top-level
      string ``error_type``): raises :class:`MagickitMcpError` at the call site;
      the exception carries the payload's top-level key list (see
      :func:`raise_if_envelope`) so the schema collision is diagnosable from a
      single log line.
    - *False negative* (a real envelope whose shape drifts away from
      ``error_type``, or a stricter detector "helpfully" rejecting a well-formed
      envelope): flows through as a success, silently — the exact failure mode
      msg-1115 §2 called worst (empty thread lists / silent zero-id posts / a
      stale dashboard, none leaving a trace).

    Between diagnosable-crash and silent-drop the choice is diagnosable-crash,
    so the detector stays loose. Narrowing it (requiring ``error`` too, or an
    absent success key, etc.) trades a rare false positive for a class of silent
    failure this refactor exists to close — do not do it.

    **Switch point for a transport-level `isError`**: if conclair ever grows a
    genuine transport ``isError`` flag on the envelope (parity with the MCP
    ``CallToolResult.isError`` bit :func:`parse_tool_result` already honours),
    that flag becomes the primary detector and this function's body reduces to
    "trust the flag". Change happens in one place — here — precisely because the
    detection has always been centralised at the client boundary.
    """
    if not isinstance(payload, dict):
        return False
    error_type = payload.get("error_type")
    return isinstance(error_type, str) and bool(error_type)


# §3 constants. Names, not magic numbers, so a future edit that thinks it is
# "just relaxing the cap" reads what it is undoing (values must not leak
# unbounded into an exception message — Einstein's msg-1687 concern).
_ELEVATION_VALUE_LIMIT = 200
"""Max characters of ``error_type`` / ``error`` retained in the elevation
exception message. §3.4."""

_ELEVATION_TRUNCATION_MARKER = "…[truncated]"
"""Appended after a truncated value so a reader knows the log line is not the
whole story. §3.4."""


def _elevation_snippet(payload: dict[str, Any], key: str) -> str | None:
    """The truncated string value of ``payload[key]``, or ``None`` if not a string.

    Per §3.2-3.4: only string values of ``error_type`` and ``error`` are ever
    admitted to the elevation message; anything non-string produces ``None`` and
    the value is omitted entirely (its key still appears in the key list —
    diagnostics come from *shape*, not from stringifying arbitrary payload
    values). This is the single formatting site referenced by §3's "the
    exception's constructor decides what may be shown" — the ``report_observed``
    warning stringifies the exception, it does not re-derive values from the
    payload, so this cap governs every log line downstream.
    """
    raw = payload.get(key)
    if not isinstance(raw, str):
        return None
    if len(raw) <= _ELEVATION_VALUE_LIMIT:
        return raw
    return raw[:_ELEVATION_VALUE_LIMIT] + _ELEVATION_TRUNCATION_MARKER


def _elevation_message(payload: Any) -> str:
    """Compose the :class:`MagickitMcpError` message for an elevated envelope.

    The message is bounded by construction (§3 rules): a sorted list of the
    payload's top-level key names (bare names, no recursion, no values), plus
    the truncated string values of ``error_type`` and ``error`` when — and only
    when — they are strings. **No other key's value is ever included** — this
    is what msg-1688 §5's "no ``repr(payload)`` / ``json.dumps(payload)``"
    forbids in one place, so no second formatter can grow that habit anywhere
    downstream.

    Callers pass raw ``payload`` (not ``dict``) so that a mis-shaped input still
    yields *some* diagnostic message rather than an ``AttributeError`` — the
    only path through :func:`raise_if_envelope` goes through :func:`is_envelope`
    which has already narrowed to ``dict`` in practice, but the fallback keeps
    an accidental direct call from raising an unrelated exception type.
    """
    if not isinstance(payload, dict):
        return f"magickit tool returned an error envelope: {type(payload).__name__} payload"
    keys = sorted(payload.keys())
    parts = [f"keys={keys}"]
    type_snippet = _elevation_snippet(payload, "error_type")
    if type_snippet is not None:
        parts.append(f"error_type={type_snippet!r}")
    err_snippet = _elevation_snippet(payload, "error")
    if err_snippet is not None:
        parts.append(f"error={err_snippet!r}")
    return "magickit tool returned an error envelope: " + " ".join(parts)


def envelope_error(payload: Any) -> str | None:
    """Back-compat shim: the elevation message iff ``payload`` is an envelope, else ``None``.

    Kept because several tests introspect the string form of the elevation.
    New code should call :func:`raise_if_envelope` or read
    :func:`_elevation_message` directly.
    """
    if not is_envelope(payload):
        return None
    return _elevation_message(payload)


def raise_if_envelope(payload: Any) -> None:
    """Raise :class:`MagickitMcpError` iff ``payload`` is a chatroom error envelope.

    The one place that turns "here is what the far end said no with" into an
    exception. :func:`parse_tool_result` calls it after decoding a live
    response; a test fake that wants to simulate a refusal calls it on its
    scripted payload before returning. Keeping both paths on this single
    function is what the DoD #3 rule "the fake is raised from the measured
    envelope shape" means in code -- there is no second, drifting definition
    of "envelope" for tests to fall out of sync with.

    The exception message is constructed by :func:`_elevation_message` under
    msg-1688 §3's rules (top-level key names, plus ``error_type`` / ``error``
    string values truncated at :data:`_ELEVATION_VALUE_LIMIT`, and nothing
    else). Formatting lives on the exception-construction side so downstream
    log sites — notably ``LoopControlReader.report_observed`` — stringify the
    exception rather than re-format the payload, keeping the value-safety
    rule in one place.
    """
    if is_envelope(payload):
        raise MagickitMcpError(_elevation_message(payload))


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


# msg-1685 §2: the responsibility for wrapping a transport failure into
# :class:`MagickitMcpError` sits with :meth:`StreamableHttpChatroomMcp.call_tool`
# (not the ``report_observed`` catch, which stays a plain ``except
# MagickitMcpError``). The list is name-catched — deliberately not ``except
# Exception`` — so a programming bug (``TypeError`` from a mis-shaped call,
# ``KeyError`` from a typo, …) propagates rather than being silently converted
# to an "MCP failure". If a genuine transport error class leaks through this
# tuple in production, the fix is here — extend the tuple — not to widen the
# call-site catch. The order constraint (msg-1685 §2 last bullet) is that any
# tuple extension lands with the failing test in the same PR.
#
# Coverage of the enumerated classes:
#   - ``httpx.HTTPError``      → connect / read / status / timeout / DNS
#     (parent of ``ConnectError``, ``ReadError``, ``TimeoutException``,
#     ``HTTPStatusError``, ``NetworkError``, …).
#   - ``OSError``              → socket-level / filesystem-adjacent transport
#     faults that surface below httpx (e.g. broken pipe).
#   - ``TimeoutError``         → the Py 3.11 builtin (``asyncio.TimeoutError``
#     aliases it); anyio's cancellation-based timeouts land here.
#   - ``json.JSONDecodeError`` → non-JSON body that slips past the mcp
#     transport into user code (``parse_tool_result`` also handles bad JSON in
#     content blocks, but a top-level decode failure at the transport is a
#     different edge and worth being explicit).
#   - ``McpError``             → mcp protocol-level error (server returned a
#     JSON-RPC error object).
#   - ``StreamableHTTPError``  → mcp transport-level error (streamable-http
#     specific).
_TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    OSError,
    TimeoutError,
    json.JSONDecodeError,
    McpError,
    StreamableHTTPError,
)


def _wrap_transport_error(name: str, exc: BaseException) -> MagickitMcpError:
    """Uniform :class:`MagickitMcpError` from a transport-class ``exc``.

    Includes the exception class name so a log reader can tell "timeout" apart
    from "connection refused" without opening the traceback. ``from exc``
    preserves the chain for tracebacks; the string form stays short.
    """
    return MagickitMcpError(f"magickit MCP call {name!r} failed: {type(exc).__name__}: {exc}")


def _first_leaf(exc: BaseException) -> BaseException:
    """The first non-group exception reached by peeling any nested groups.

    ``ExceptionGroup.split`` returns a matched sub-group whose leaves can
    themselves be groups (mcp / anyio nest a task-group's inner failure in a
    per-connection outer group). Peeling to a leaf gives the wrap a concrete
    class name — ``ConnectError`` rather than ``ExceptionGroup``.
    """
    while isinstance(exc, BaseExceptionGroup):
        exc = next(iter(exc.exceptions))
    return exc


class StreamableHttpChatroomMcp:
    """:class:`McpToolCaller` over the ``mcp`` Streamable HTTP transport.

    Connects per call (Phase 1; low volume). A persistent session is a
    Phase 2 optimization. **Real network I/O — covered by the ``-m manual``
    smoke test, not CI.**

    Overriding :meth:`_call_mcp` in a subclass is the seam the DoD #3 transport
    contract tests use to inject each named exception class; production
    :meth:`call_tool` is what verifies each of those is wrapped into
    :class:`MagickitMcpError`.
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url if url is not None else magickit_mcp_url()

    async def _call_mcp(self, name: str, arguments: dict[str, Any]) -> Any:
        """Open a transport, initialise a session, invoke the tool, return the raw result.

        Extracted from :meth:`call_tool` so the tuple of caught exception classes
        in :meth:`call_tool` is the *only* place transport-vs-programming errors
        are distinguished. Tests inject failures by overriding this method
        rather than by monkey-patching the ``mcp`` package.
        """
        async with (
            streamablehttp_client(self._url) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await session.call_tool(name, arguments)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        # Two catch clauses — deliberately not ``except*`` — because we must
        # hand the caller (``LoopControlReader.report_observed``, whose narrow
        # ``except MagickitMcpError`` is the whole design of msg-1685 §2) a
        # **bare** :class:`MagickitMcpError`, not one bundled inside an
        # :class:`ExceptionGroup`. PR-gate on #178 flagged this: raising from
        # inside an ``except*`` clause can bundle the newly raised exception
        # back into a group alongside any unmatched siblings, defeating the
        # bare-catch contract at the call site. A regular ``except`` block
        # does not have that behaviour, so the ``raise`` here is guaranteed
        # bare — pinned by ``test_call_tool_wraps_transport_error_as_a_bare_magickit_mcp_error``.
        #
        # * Bare transport exception  → ``except _TRANSPORT_EXCEPTIONS`` → bare wrap.
        # * ``ExceptionGroup`` containing only transport exceptions (the mcp
        #   / anyio task-group case; the mcp streamable-http transport wraps
        #   internal failures in ``anyio.create_task_group``) → the group
        #   handler splits on ``_TRANSPORT_EXCEPTIONS``, peels to a leaf, and
        #   re-raises a bare wrap.
        # * ``ExceptionGroup`` mixing transport + non-transport → the group
        #   handler surfaces both: the wrap AND the non-transport remainder,
        #   as a new group. Programming errors stay visible (not silently
        #   converted to an "MCP failure" — the invariant msg-1685 §2 asked
        #   for) while the transport failure is still recognisable.
        # * ``ExceptionGroup`` with no transport exceptions → re-raised as-is
        #   (pure programming errors escape).
        # * Bare programming error → uncaught, propagates as-is.
        try:
            result = await self._call_mcp(name, arguments)
        except _TRANSPORT_EXCEPTIONS as exc:
            raise _wrap_transport_error(name, exc) from exc
        except BaseExceptionGroup as group:
            transport_part, remainder = group.split(_TRANSPORT_EXCEPTIONS)
            if transport_part is None:
                raise  # no transport exceptions in the group — pure programming errors
            first = _first_leaf(transport_part)
            wrapped = _wrap_transport_error(name, first)
            if remainder is None:
                raise wrapped from first  # bare MagickitMcpError to the caller
            # Transport + non-transport siblings: keep the wrap visible next
            # to the programming-error remainder so neither disappears. Two
            # subtleties (PR-gate on #178 flagged both):
            #
            # 1. ``wrapped.__cause__ = first`` **must be set explicitly**.
            #    ``_wrap_transport_error`` just constructs the exception, so
            #    without this the wrap has no cause chain — the traceback link
            #    to the underlying transport failure would be severed. The
            #    single-transport branch above gets this via ``raise wrapped
            #    from first``; here the wrap goes into a list rather than
            #    being raised, so the same chain must be set by hand.
            # 2. The group is raised ``from group`` (the *caught* group), not
            #    ``from first``. Chaining to ``first`` would say "the whole
            #    new group was caused by the transport error", which is false
            #    — the programming-bug remainder is a sibling task failure,
            #    not a downstream effect. Chaining to ``group`` (the original
            #    caught container) correctly says "this new group is a
            #    repackaging of the one we caught". The ``from`` form is
            #    required by ruff B904; ``from None`` would suppress the
            #    caught context, which we want to keep.
            wrapped.__cause__ = first
            raise BaseExceptionGroup(
                "magickit call: transport failure alongside non-transport error",
                [wrapped, remainder],
            ) from group
        return parse_tool_result(result)


__all__ = [
    "MagickitMcpError",
    "McpToolCaller",
    "StreamableHttpChatroomMcp",
    "envelope_error",
    "is_envelope",
    "magickit_mcp_url",
    "parse_tool_result",
    "raise_if_envelope",
]
