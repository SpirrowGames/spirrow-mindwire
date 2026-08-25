"""Tests for MagickitChatroomGateway + parse_tool_result (ADR-06 §3.3, T12 PR-F)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mcp import ErrorData, McpError
from mcp.client.streamable_http import StreamableHTTPError

from spirrow_mindwire.dispatcher.gateway import ChatroomGateway
from spirrow_mindwire.magickit.client import (
    MagickitMcpError,
    StreamableHttpChatroomMcp,
    parse_tool_result,
)
from spirrow_mindwire.magickit.gateway import MagickitChatroomGateway
from spirrow_mindwire.value_objects import Role, ThreadRef

_TR = ThreadRef(project_id="spirrow-mindwire", thread_id="T-x", chatroom_uri="mc://t")


class _FakeCaller:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return self.result


class _FakeToolResult:
    def __init__(
        self,
        *,
        structured: Any = None,
        content: list[Any] | None = None,
        is_error: bool = False,
    ) -> None:
        self.structuredContent = structured
        self.content = content
        self.isError = is_error


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


@pytest.mark.anyio
async def test_post_reply_calls_chatroom_post_message() -> None:
    caller = _FakeCaller({"msg": {"msg_id": "msg-200"}})
    gw = MagickitChatroomGateway(caller)
    out = await gw.post_reply(
        _TR, author=Role.PROPOSER, body="hi", reply_to_msg_id="m1", idempotency_key="s:1"
    )
    assert out == "msg-200"
    name, args = caller.calls[0]
    assert name == "chatroom_post_message"
    assert args["project"] == "spirrow-mindwire"
    assert args["thread_id"] == "T-x"
    assert args["author"] == "proposer"  # I3: author = role name
    assert args["content"] == "hi"
    assert args["reply_to"] == "m1"
    assert args["msg_type"] == "report"


@pytest.mark.anyio
async def test_post_reply_omits_reply_to_when_none() -> None:
    caller = _FakeCaller({"msg": {"msg_id": "m"}})
    gw = MagickitChatroomGateway(caller)
    await gw.post_reply(
        _TR, author=Role.NAYSAYER, body="x", reply_to_msg_id=None, idempotency_key="s:1"
    )
    _, args = caller.calls[0]
    assert "reply_to" not in args


@pytest.mark.anyio
async def test_custom_reply_msg_type() -> None:
    caller = _FakeCaller({"msg": {"msg_id": "m"}})
    gw = MagickitChatroomGateway(caller, reply_msg_type="answer")
    await gw.post_reply(
        _TR, author=Role.PROPOSER, body="x", reply_to_msg_id=None, idempotency_key="s:1"
    )
    assert caller.calls[0][1]["msg_type"] == "answer"


@pytest.mark.anyio
async def test_post_reply_missing_msg_id_raises() -> None:
    gw = MagickitChatroomGateway(_FakeCaller({"oops": True}))
    with pytest.raises(MagickitMcpError):
        await gw.post_reply(
            _TR, author=Role.PROPOSER, body="x", reply_to_msg_id=None, idempotency_key="s:1"
        )


def test_conforms_to_chatroom_gateway_protocol() -> None:
    gw: ChatroomGateway = MagickitChatroomGateway(_FakeCaller({"msg": {"msg_id": "m"}}))
    assert gw is not None


def test_parse_tool_result_prefers_structured() -> None:
    assert parse_tool_result(_FakeToolResult(structured={"a": 1})) == {"a": 1}


def test_parse_tool_result_text_json_fallback() -> None:
    assert parse_tool_result(_FakeToolResult(content=[_FakeTextBlock('{"b": 2}')])) == {"b": 2}


def test_parse_tool_result_is_error_raises() -> None:
    with pytest.raises(MagickitMcpError):
        parse_tool_result(_FakeToolResult(is_error=True, content=[_FakeTextBlock("{}")]))


def test_parse_tool_result_no_content_raises() -> None:
    with pytest.raises(MagickitMcpError):
        parse_tool_result(_FakeToolResult(content=[]))


def test_parse_tool_result_skips_invalid_json_block() -> None:
    # An invalid-JSON text block is skipped; a later valid block wins.
    result = _FakeToolResult(content=[_FakeTextBlock("not json"), _FakeTextBlock('{"ok": 1}')])
    assert parse_tool_result(result) == {"ok": 1}


def test_parse_tool_result_all_invalid_json_raises_magickit_error() -> None:
    # Invalid JSON surfaces as MagickitMcpError, not a raw JSONDecodeError.
    with pytest.raises(MagickitMcpError):
        parse_tool_result(_FakeToolResult(content=[_FakeTextBlock("not json")]))


# --------------------------------------------------------------------------- #
# T-error-envelope-read-as-data DoD #1: parse_tool_result elevates a conclair
# error envelope to MagickitMcpError at the client boundary, so no caller has
# to remember to check for one (msg-1115 §4).
# --------------------------------------------------------------------------- #

# The envelopes below are verbatim from the live server on 2026-08-16 (see
# tests/test_orchestrator.py::_error_envelope). Elevating them here is the
# single source of truth the msg-1496 refactor pinned; a change to the
# envelope shape will break these tests before it silently breaks callers.

_LIVE_NOT_FOUND: dict[str, Any] = {
    "error_type": "ChatroomNotFoundError",
    "error": "Thread 'T-pr-review-spirrow-magickit-22' not found in project 'spirrow-mindwire'",
    "details": {
        "project": "spirrow-mindwire",
        "thread_id": "T-pr-review-spirrow-magickit-22",
    },
}


def test_parse_tool_result_elevates_envelope_from_structured_content() -> None:
    with pytest.raises(MagickitMcpError, match="ChatroomNotFoundError"):
        parse_tool_result(_FakeToolResult(structured=_LIVE_NOT_FOUND))


def test_parse_tool_result_elevates_envelope_from_text_block() -> None:
    import json

    with pytest.raises(MagickitMcpError, match="ChatroomNotFoundError"):
        parse_tool_result(_FakeToolResult(content=[_FakeTextBlock(json.dumps(_LIVE_NOT_FOUND))]))


def test_parse_tool_result_error_message_includes_the_far_end_reason() -> None:
    # DoD #4.4 (msg-1496): the warning at the report_observed call site must be
    # able to show ``error_type`` and the message; that requires the elevation
    # to carry both, not just the class name.
    with pytest.raises(MagickitMcpError) as excinfo:
        parse_tool_result(_FakeToolResult(structured=_LIVE_NOT_FOUND))
    text = str(excinfo.value)
    assert "ChatroomNotFoundError" in text
    assert "T-pr-review-spirrow-magickit-22" in text


def test_parse_tool_result_lets_a_payload_with_no_error_type_pass_through() -> None:
    # Only ``error_type`` sets the envelope apart from an ordinary payload —
    # a domain field that merely mentions the word "error" is not one.
    payload = {"thread": {"title": "x"}, "messages": []}
    assert parse_tool_result(_FakeToolResult(structured=payload)) == payload


# --------------------------------------------------------------------------- #
# T-error-envelope-followup DoD #2 (msg-1688 §3-4): the elevation exception's
# message is bounded by construction — top-level key names + ``error_type`` /
# ``error`` string values only, each truncated at 200 chars — and no payload
# value ever appears on any other path. The negative assertion (b) is the
# load-bearing one: a future "helpful" edit that adds ``repr(payload)`` to
# the message string must fail this test.
# --------------------------------------------------------------------------- #

_SENTINEL_NOT_TO_LEAK = "SENSITIVE_VALUE_MUST_NOT_APPEAR_IN_MESSAGE_c47f2c8b"
_SENTINEL_NESTED = "NESTED_VALUE_MUST_NOT_APPEAR_78a1"


def _elevate_payload(payload: dict[str, Any]) -> MagickitMcpError:
    """Trigger the envelope elevation and return the raised exception for introspection."""
    with pytest.raises(MagickitMcpError) as excinfo:
        parse_tool_result(_FakeToolResult(structured=payload))
    return excinfo.value


def test_dod2a_elevation_message_lists_every_top_level_key_by_name() -> None:
    """msg-1688 §4 (a): the top-level key list is what proves a false-positive elevation.

    Any legitimate payload that trips the detector shows its shape here — a
    reader can tell "success payload with a spurious ``error_type``" apart
    from "conclair error envelope" by reading the key set.
    """
    payload = {
        "error_type": "SomeError",
        "error": "why",
        "details": {"x": _SENTINEL_NESTED},
        "extra_domain_key": _SENTINEL_NOT_TO_LEAK,
        "another_key": [1, 2, 3],
    }
    exc = _elevate_payload(payload)
    text = str(exc)
    for key in payload:
        assert key in text, f"top-level key {key!r} missing from elevation message: {text!r}"


def test_dod2b_elevation_message_never_dumps_values_from_non_string_or_extra_keys() -> None:
    """msg-1688 §4 (b) — the negative pin.

    This is the test msg-1688 called "the real one" — the constraint's job is
    to reject a diff that "helpfully" adds ``repr(payload)`` or an extra key's
    value to the message. Positive asserts would let that diff pass silently.

    A payload's non-envelope keys carry sentinel strings; the elevation must
    surface **none** of them. ``error_type`` / ``error`` values ARE allowed
    (§3.2-3.3), so those are omitted from the sentinel set.
    """
    payload = {
        "error_type": "SomeError",
        "error": "why",
        "details": {"secret": _SENTINEL_NESTED, "actor": "internal-service-key"},
        "extra_domain_key": _SENTINEL_NOT_TO_LEAK,
    }
    exc = _elevate_payload(payload)
    text = str(exc)
    # Non-string values (dicts, lists, ...) must not appear via ``repr()``.
    assert _SENTINEL_NOT_TO_LEAK not in text, (
        f"extra key's value leaked into elevation message: {text!r}"
    )
    assert _SENTINEL_NESTED not in text, f"nested value leaked into elevation message: {text!r}"
    assert "internal-service-key" not in text, (
        f"nested actor value leaked into elevation message: {text!r}"
    )


def test_dod2b_elevation_message_omits_non_string_error_type_and_error_values() -> None:
    """§3.2-3.3 corollary: when ``error_type`` / ``error`` are not strings, the
    values are omitted entirely (the keys still appear in the top-level list).
    """
    payload = {
        "error_type": "RealError",  # keep the detector tripped
        "error": {"nested": _SENTINEL_NESTED},  # non-string → value omitted
        "extras": [_SENTINEL_NOT_TO_LEAK],
    }
    exc = _elevate_payload(payload)
    text = str(exc)
    assert _SENTINEL_NESTED not in text
    assert _SENTINEL_NOT_TO_LEAK not in text
    # Keys still present, so the schema mismatch remains diagnosable.
    assert "error" in text
    assert "extras" in text


def test_dod2c_elevation_message_truncates_error_and_error_type_at_200_chars() -> None:
    """msg-1688 §3.4: ``error_type`` / ``error`` values are capped at
    :data:`_ELEVATION_VALUE_LIMIT` (200 chars); a truncation marker follows.
    A conclair error message that happens to embed a full chatroom thread body
    must not blow the log line up unbounded.
    """
    big = "X" * 500
    payload = {
        "error_type": big,
        "error": big,
    }
    exc = _elevate_payload(payload)
    text = str(exc)
    # The full 500-char string must not appear intact.
    assert big not in text, "value was not truncated"
    # A prefix of the value + the truncation marker must appear (per key).
    assert "…[truncated]" in text
    # The message must remain of a bounded length that is a small multiple of
    # the cap (two capped values + short surrounding text) — hard-coding an
    # exact length would be brittle, but proving "far shorter than 500x2 raw"
    # keeps a future edit that removes truncation red.
    assert len(text) < 700, f"elevation message unexpectedly long: len={len(text)}"


def test_dod2_report_observed_warning_stringifies_the_exception_not_the_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """msg-1688 §3 last bullet + §4 last paragraph: the ``report_observed``
    warning has no second formatter — it emits ``str(exc)``, so the value-safety
    rule applied at :func:`raise_if_envelope` governs the log line too.

    Verified against the actual code path (:class:`LoopControlReader` catching
    an envelope-elevated ``MagickitMcpError``); the sentinel from a non-envelope
    key must not surface in the warning either.
    """
    import logging

    from spirrow_mindwire.conductor.control import ControlState, LoopControlReader
    from spirrow_mindwire.magickit.client import raise_if_envelope

    class _EnvelopeMcp:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            raise_if_envelope(
                {
                    "error_type": "ChatroomWriteFailed",
                    "error": "storage backend refused the write",
                    "details": {"secret_actor_key": _SENTINEL_NOT_TO_LEAK},
                    "extras": _SENTINEL_NESTED,
                }
            )

    async def _run() -> None:
        reader = LoopControlReader(_EnvelopeMcp(), project="p")
        await reader.report_observed(ControlState.HOLD)

    with caplog.at_level(logging.WARNING, logger="spirrow_mindwire.conductor.control"):
        import anyio

        anyio.run(_run)
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "ChatroomWriteFailed" in log_text  # the diagnostic info survives
    assert _SENTINEL_NOT_TO_LEAK not in log_text
    assert _SENTINEL_NESTED not in log_text


# --------------------------------------------------------------------------- #
# T-error-envelope-followup DoD #3 (msg-1685 §2 + §4.3): the responsibility for
# converting a transport failure into :class:`MagickitMcpError` sits with
# :meth:`StreamableHttpChatroomMcp.call_tool`, not the ``report_observed``
# call-site catch. The tests below inject each named exception class through
# the ``_call_mcp`` seam and assert the wrap; a leak means the fix goes into
# the client's ``_TRANSPORT_EXCEPTIONS`` tuple, not into a call site.
# --------------------------------------------------------------------------- #


class _RaisingMcp(StreamableHttpChatroomMcp):
    """Test seam: raise a scripted exception from ``_call_mcp``.

    Injecting through the same method the production transport ends up calling
    keeps the test close to the boundary msg-1685 §2 named — the tuple in
    ``call_tool`` — rather than exercising a private helper.
    """

    def __init__(self, exc: BaseException) -> None:
        super().__init__(url="http://test.invalid/mcp")
        self._exc = exc

    async def _call_mcp(self, name: str, arguments: dict[str, Any]) -> Any:
        raise self._exc


@pytest.mark.anyio
async def test_call_tool_wraps_httpx_connect_error() -> None:
    # connect error — one of the enumerated classes.
    with pytest.raises(MagickitMcpError, match="ConnectError"):
        await _RaisingMcp(httpx.ConnectError("cannot connect")).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_httpx_read_timeout() -> None:
    # read timeout — httpx.TimeoutException is a subclass of httpx.HTTPError.
    with pytest.raises(MagickitMcpError, match="ReadTimeout"):
        await _RaisingMcp(httpx.ReadTimeout("read timed out")).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_httpx_non_2xx_status() -> None:
    # non-2xx — HTTPStatusError. Constructed with the request/response the
    # library expects; the important part is that the type is in the tuple.
    request = httpx.Request("POST", "http://test.invalid/mcp")
    response = httpx.Response(503, request=request)
    with pytest.raises(MagickitMcpError, match="HTTPStatusError"):
        await _RaisingMcp(
            httpx.HTTPStatusError("503", request=request, response=response)
        ).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_json_decode_error() -> None:
    # non-JSON body reaching this level — direct JSONDecodeError.
    with pytest.raises(MagickitMcpError, match="JSONDecodeError"):
        await _RaisingMcp(json.JSONDecodeError("expecting value", "not json", 0)).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_mcp_error() -> None:
    # mcp protocol error — server returned a JSON-RPC error object.
    err = McpError(ErrorData(code=-32000, message="server refused"))
    with pytest.raises(MagickitMcpError, match="McpError"):
        await _RaisingMcp(err).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_streamable_http_error() -> None:
    # mcp streamable-http transport error.
    with pytest.raises(MagickitMcpError, match="StreamableHTTPError"):
        await _RaisingMcp(StreamableHTTPError("resumption token missing")).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_os_error() -> None:
    # socket-level / broken pipe class of failure that surfaces below httpx.
    with pytest.raises(MagickitMcpError, match=r"OSError|ConnectionResetError"):
        await _RaisingMcp(ConnectionResetError("peer reset")).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_builtin_timeout_error() -> None:
    # asyncio.TimeoutError == TimeoutError (Py 3.11+); anyio's timeouts land here.
    with pytest.raises(MagickitMcpError, match="TimeoutError"):
        await _RaisingMcp(TimeoutError("op timed out")).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_unwraps_transport_error_inside_exception_group() -> None:
    """anyio task groups (used by the mcp streamable-http transport) may wrap a
    transport failure in an :class:`ExceptionGroup`. The ``except*`` in
    ``call_tool`` unwraps it so callers see the same :class:`MagickitMcpError`
    they would for a bare raise.
    """
    inner = httpx.ConnectError("cannot connect")
    group = ExceptionGroup("mcp transport", [inner])
    with pytest.raises(MagickitMcpError, match="ConnectError"):
        await _RaisingMcp(group).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_lets_programming_errors_escape() -> None:
    """The load-bearing counterpart to the wrap tests.

    msg-1685 §2 forbids ``except Exception`` in ``call_tool`` because it would
    silently convert a programming bug (``TypeError`` from a mis-shaped call,
    ``KeyError`` from a typo) into an "MCP failure" — the same silent-success
    hole the whole T-error-envelope-read-as-data thread exists to close.

    A bare ``TypeError`` from ``_call_mcp`` must propagate; the sibling test
    ``test_report_observed_lets_programming_errors_propagate`` in
    ``test_conductor_control.py`` pins the same rule at the call-site catch.
    """
    with pytest.raises(TypeError):
        await _RaisingMcp(TypeError("mis-shaped call")).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_lets_key_error_escape_too() -> None:
    """Second programming-error class, same rule. A tuple that grew to catch
    ``KeyError`` (say, "network layer sometimes raises it") would fail here."""
    with pytest.raises(KeyError):
        await _RaisingMcp(KeyError("missing dict key")).call_tool("x", {})
