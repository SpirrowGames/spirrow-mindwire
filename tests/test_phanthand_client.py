"""Tests for :class:`spirrow_mindwire.phanthand.PhanthandClient`.

httpx ``MockTransport`` lets us exercise the full request/response
cycle without spinning up a real Phanthand server. Each test handler
asserts the request shape and returns a synthetic response.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from spirrow_mindwire.phanthand import (
    PhanthandAPIError,
    PhanthandClient,
    PhanthandHTTPError,
)

ENDPOINT = "http://phanthand.local:7300"
API_KEY = "test-key-123"


def _ok(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data, "error": None})


def _err(message: str) -> httpx.Response:
    return httpx.Response(200, json={"success": False, "data": None, "error": message})


def _client(handler: Any, api_key: str | None = API_KEY) -> PhanthandClient:
    transport = httpx.MockTransport(handler)
    return PhanthandClient(ENDPOINT, api_key, transport=transport)


# ---------- Health ------------------------------------------------------


@pytest.mark.anyio
async def test_health_returns_data() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/health"
        assert req.method == "GET"
        return _ok(
            {
                "status": "ok",
                "version": "0.1.0",
                "hostname": "host-a",
                "uptime_seconds": 12.5,
            }
        )

    async with _client(handler) as client:
        h = await client.health()
    assert h.status == "ok"
    assert h.uptime_seconds == 12.5


# ---------- Auth header -------------------------------------------------


@pytest.mark.anyio
async def test_auth_header_set_when_api_key_provided() -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["auth"] = req.headers.get("authorization", "")
        return _ok({"path": "/x", "content": "", "size": 0, "encoding": "utf-8"})

    async with _client(handler, api_key="secret") as client:
        await client.read_file("/x")
    assert captured["auth"] == "Bearer secret"


@pytest.mark.anyio
async def test_auth_header_absent_when_api_key_none() -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["auth"] = req.headers.get("authorization", "")
        return _ok({"path": "/x", "content": "", "size": 0, "encoding": "utf-8"})

    async with _client(handler, api_key=None) as client:
        await client.read_file("/x")
    assert captured["auth"] == ""


# ---------- File operations ---------------------------------------------


@pytest.mark.anyio
async def test_read_file_success() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/files/read"
        assert req.method == "POST"
        body = req.read()
        assert b'"path"' in body
        assert b'"encoding"' in body
        return _ok(
            {
                "path": "/D/file.py",
                "content": "print(1)",
                "size": 8,
                "encoding": "utf-8",
            }
        )

    async with _client(handler) as client:
        d = await client.read_file("/D/file.py")
    assert d.content == "print(1)"
    assert d.size == 8


@pytest.mark.anyio
async def test_list_directory_success() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/files/list"
        return _ok(
            {
                "path": "/D",
                "entries": [
                    {"name": "a.py", "path": "/D/a.py", "is_dir": False, "size": 10},
                    {"name": "sub", "path": "/D/sub", "is_dir": True},
                ],
                "count": 2,
            }
        )

    async with _client(handler) as client:
        d = await client.list_directory("/D")
    assert d.count == 2
    assert d.entries[0].is_dir is False
    assert d.entries[1].size is None


@pytest.mark.anyio
async def test_file_exists_returns_flags() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok({"path": "/D/x", "exists": True, "is_file": True, "is_dir": False})

    async with _client(handler) as client:
        d = await client.file_exists("/D/x")
    assert d.exists is True
    assert d.is_file is True


@pytest.mark.anyio
async def test_file_info_passes_optional_dates() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok(
            {
                "path": "/D/x",
                "name": "x",
                "size": 100,
                "created": None,
                "modified": "2026-05-07T08:43:07Z",
                "is_file": True,
                "is_dir": False,
                "readonly": False,
            }
        )

    async with _client(handler) as client:
        d = await client.file_info("/D/x")
    assert d.size == 100
    assert d.created is None
    assert d.modified is not None


@pytest.mark.anyio
async def test_file_tree_with_default_excludes() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.read()
        # exclude_patterns is omitted when caller doesn't pass it
        assert b"exclude_patterns" not in body
        return _ok(
            {
                "path": "/D",
                "tree": {"name": "D", "path": "/D", "is_dir": True, "children": []},
            }
        )

    async with _client(handler) as client:
        d = await client.file_tree("/D")
    assert d.tree.is_dir is True


@pytest.mark.anyio
async def test_file_search_propagates_truncated_flag() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok(
            {
                "path": "/D",
                "pattern": "*.py",
                "matches": ["/D/a.py", "/D/b.py"],
                "count": 2,
                "truncated": True,
            }
        )

    async with _client(handler) as client:
        d = await client.file_search("/D", "*.py")
    assert d.truncated is True
    assert d.matches == ["/D/a.py", "/D/b.py"]


# ---------- Error paths -------------------------------------------------


@pytest.mark.anyio
async def test_application_error_raises_api_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _err("Path not allowed: /etc/passwd")

    async with _client(handler) as client:
        with pytest.raises(PhanthandAPIError, match="Path not allowed"):
            await client.read_file("/etc/passwd")


@pytest.mark.anyio
async def test_http_500_raises_http_error_with_status() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with _client(handler) as client:
        with pytest.raises(PhanthandHTTPError) as exc:
            await client.read_file("/x")
    assert exc.value.status_code == 500


@pytest.mark.anyio
async def test_http_401_raises_http_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="no auth")

    async with _client(handler) as client:
        with pytest.raises(PhanthandHTTPError) as exc:
            await client.read_file("/x")
    assert exc.value.status_code == 401


@pytest.mark.anyio
async def test_malformed_json_raises_http_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    async with _client(handler) as client:
        with pytest.raises(PhanthandHTTPError, match="malformed JSON"):
            await client.read_file("/x")


@pytest.mark.anyio
async def test_success_true_but_null_data_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": None, "error": None})

    async with _client(handler) as client:
        with pytest.raises(PhanthandHTTPError, match="data is null"):
            await client.read_file("/x")


@pytest.mark.anyio
async def test_network_error_wrapped_as_http_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _client(handler) as client:
        with pytest.raises(PhanthandHTTPError, match="connection refused"):
            await client.read_file("/x")
