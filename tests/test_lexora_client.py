"""Tests for :class:`spirrow_mindwire.lexora.client.LexoraClient`.

httpx ``MockTransport`` exercises the full request/response cycle without
a live Lexora gateway. The response bodies mirror the **real** gateway
shape captured against ``model="naysayer"`` (DeepSeek V4-Flash): the reply
is ``choices[0].message.content`` and the deliberation is the sibling
``choices[0].message.reasoning_content``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from spirrow_mindwire.lexora.client import (
    _DEFAULT_LEXORA_URL,
    ChatMessage,
    LexoraAPIError,
    LexoraClient,
    LexoraHTTPError,
    lexora_url,
)

URL = "http://lexora.local:8110"


def _completion(
    *,
    content: str | None = "ok",
    reasoning: str | None = "thinking...",
    finish_reason: str = "stop",
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return httpx.Response(
        200,
        json={
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
            "model": "DeepSeek-V4-Flash.gguf",
            "object": "chat.completion",
            "usage": {"completion_tokens": 12, "prompt_tokens": 34, "total_tokens": 46},
            "id": "chatcmpl-xyz",
        },
    )


def _client(handler: Any) -> LexoraClient:
    return LexoraClient(URL, transport=httpx.MockTransport(handler))


def _msgs() -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="be terse"),
        ChatMessage(role="user", content="critique X"),
    ]


# ---------- URL resolution ----------------------------------------------


def test_lexora_url_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDWIRE_LEXORA_URL", raising=False)
    assert lexora_url() == _DEFAULT_LEXORA_URL == "http://localhost:8110"


def test_lexora_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWIRE_LEXORA_URL", "http://100.79.84.62:8110")
    assert lexora_url() == "http://100.79.84.62:8110"


def test_lexora_url_empty_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWIRE_LEXORA_URL", "")
    assert lexora_url() == _DEFAULT_LEXORA_URL


# ---------- chat_completion happy path -----------------------------------


@pytest.mark.anyio
async def test_chat_completion_parses_content_and_reasoning() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/chat/completions"
        assert req.method == "POST"
        body = req.read()
        assert b'"naysayer"' in body
        assert b'"max_tokens"' in body
        return _completion(content="the reply", reasoning="the deliberation")

    async with _client(handler) as client:
        result = await client.chat_completion(model="naysayer", messages=_msgs(), max_tokens=4096)
    assert result.content == "the reply"
    assert result.reasoning_content == "the deliberation"  # separate sibling field
    assert result.finish_reason == "stop"
    assert result.model == "DeepSeek-V4-Flash.gguf"
    assert result.usage["completion_tokens"] == 12


@pytest.mark.anyio
async def test_chat_completion_no_auth_header_sent() -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["auth"] = req.headers.get("authorization", "")
        return _completion()

    async with _client(handler) as client:
        await client.chat_completion(model="naysayer", messages=_msgs(), max_tokens=100)
    assert captured["auth"] == ""  # gateway requires no caller auth (msg-215)


@pytest.mark.anyio
async def test_chat_completion_empty_content_is_returned_not_raised() -> None:
    # An empty reply (budget spent on reasoning) is a valid transport result;
    # the *adapter* decides it is a fail-loud condition, not this layer.
    def handler(_req: httpx.Request) -> httpx.Response:
        return _completion(content="", reasoning="all budget here", finish_reason="length")

    async with _client(handler) as client:
        result = await client.chat_completion(model="naysayer", messages=_msgs(), max_tokens=10)
    assert result.content == ""
    assert result.finish_reason == "length"


# ---------- fail-loud (non-2xx) -----------------------------------------


@pytest.mark.anyio
async def test_unknown_tier_raises_http_error_with_detail() -> None:
    # Real gateway behaviour: unknown tier → 502 with the upstream detail;
    # must surface loudly, never silently fall back (main order #1).
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "vLLM error (404): model does not exist"})

    async with _client(handler) as client:
        with pytest.raises(LexoraHTTPError) as ei:
            await client.chat_completion(model="nope", messages=_msgs(), max_tokens=10)
    assert ei.value.status_code == 502
    assert "does not exist" in str(ei.value)


@pytest.mark.anyio
async def test_network_error_raises_http_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _client(handler) as client:
        with pytest.raises(LexoraHTTPError):
            await client.chat_completion(model="naysayer", messages=_msgs(), max_tokens=10)


@pytest.mark.anyio
async def test_no_choices_raises_api_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [], "model": "m"})

    async with _client(handler) as client:
        with pytest.raises(LexoraAPIError):
            await client.chat_completion(model="naysayer", messages=_msgs(), max_tokens=10)


@pytest.mark.anyio
async def test_malformed_json_raises_http_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    async with _client(handler) as client:
        with pytest.raises(LexoraHTTPError):
            await client.chat_completion(model="naysayer", messages=_msgs(), max_tokens=10)


# ---------- health -------------------------------------------------------


@pytest.mark.anyio
async def test_health_returns_status_dict() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/health"
        assert req.method == "GET"
        return httpx.Response(200, json={"status": "degraded", "backends": {"naysayer": "healthy"}})

    async with _client(handler) as client:
        h = await client.health()
    assert h["status"] == "degraded"
    assert h["backends"]["naysayer"] == "healthy"


@pytest.mark.anyio
async def test_health_non_2xx_raises() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    async with _client(handler) as client:
        with pytest.raises(LexoraHTTPError):
            await client.health()
