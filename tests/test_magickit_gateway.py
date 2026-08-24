"""Tests for MagickitChatroomGateway + parse_tool_result (ADR-06 §3.3, T12 PR-F)."""

from __future__ import annotations

from typing import Any

import pytest

from spirrow_mindwire.dispatcher.gateway import ChatroomGateway
from spirrow_mindwire.magickit.client import MagickitMcpError, parse_tool_result
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
