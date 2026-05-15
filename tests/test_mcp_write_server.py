"""Tests for ``spirrow_mindwire.mcp_write_server`` (Feature 3-A sub-PR 2).

Commit 2 scope: bootstrap + api-key middleware. The 3 write tools
(``send_message`` / ``open_thread`` / ``resolve_thread``) and their
in-server unit tests (= C1 reuse, C4 concurrency) are added in commit 3.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from spirrow_mindwire.config import MindwireSettings
from spirrow_mindwire.mcp_write_server.auth import (
    ApiKeyMiddleware,
    MissingApiKeyError,
    read_api_key,
)
from spirrow_mindwire.mcp_write_server.http import MCP_PATH, build_app

# ---------------------------------------------------------------------------
# read_api_key
# ---------------------------------------------------------------------------


def test_read_api_key_returns_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWIRE_MCP_API_KEY", "s3cret")
    settings = MindwireSettings()
    assert read_api_key(settings) == "s3cret"


def test_read_api_key_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDWIRE_MCP_API_KEY", raising=False)
    settings = MindwireSettings()
    with pytest.raises(MissingApiKeyError, match="MINDWIRE_MCP_API_KEY"):
        read_api_key(settings)


def test_read_api_key_empty_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWIRE_MCP_API_KEY", "")
    settings = MindwireSettings()
    with pytest.raises(MissingApiKeyError):
        read_api_key(settings)


def test_read_api_key_honours_custom_env_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_KEY_ENV", "alt-secret")
    monkeypatch.delenv("MINDWIRE_MCP_API_KEY", raising=False)
    settings = MindwireSettings(mcp_server={"api_key_env": "CUSTOM_KEY_ENV"})  # type: ignore[arg-type]
    assert read_api_key(settings) == "alt-secret"


# ---------------------------------------------------------------------------
# ApiKeyMiddleware (unit-level, isolated from FastMCP)
# ---------------------------------------------------------------------------


def _make_stub_app(api_key: str) -> Starlette:
    """Build a minimal Starlette app guarded by :class:`ApiKeyMiddleware`."""

    async def ok(_request: object) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", ok)])
    app.add_middleware(ApiKeyMiddleware, api_key=api_key)
    return app


def test_middleware_accepts_valid_bearer_token() -> None:
    client = TestClient(_make_stub_app("s3cret"))
    response = client.get("/", headers={"Authorization": "Bearer s3cret"})
    assert response.status_code == 200
    assert response.text == "ok"


def test_middleware_accepts_lowercase_bearer_scheme() -> None:
    """The HTTP scheme token is case-insensitive (RFC 7235)."""
    client = TestClient(_make_stub_app("s3cret"))
    response = client.get("/", headers={"Authorization": "bearer s3cret"})
    assert response.status_code == 200


def test_middleware_rejects_missing_header() -> None:
    client = TestClient(_make_stub_app("s3cret"))
    response = client.get("/")
    assert response.status_code == 401
    assert response.json()["detail"] == "missing or malformed Authorization header"


def test_middleware_rejects_wrong_scheme() -> None:
    client = TestClient(_make_stub_app("s3cret"))
    response = client.get("/", headers={"Authorization": "Basic s3cret"})
    assert response.status_code == 401
    assert response.json()["detail"] == "missing or malformed Authorization header"


def test_middleware_rejects_empty_bearer_token() -> None:
    client = TestClient(_make_stub_app("s3cret"))
    response = client.get("/", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


def test_middleware_rejects_wrong_token() -> None:
    client = TestClient(_make_stub_app("s3cret"))
    response = client.get("/", headers={"Authorization": "Bearer not-the-key"})
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid API key"


# ---------------------------------------------------------------------------
# build_app integration
# ---------------------------------------------------------------------------


def test_build_app_returns_starlette() -> None:
    """``build_app`` returns a Starlette ASGI app."""
    settings = MindwireSettings()
    app = build_app(settings, api_key="s3cret")
    assert isinstance(app, Starlette)


def test_build_app_mcp_endpoint_requires_auth() -> None:
    """The MCP endpoint is gated by :class:`ApiKeyMiddleware`."""
    settings = MindwireSettings()
    app = build_app(settings, api_key="s3cret")
    with TestClient(app) as client:
        response = client.post(MCP_PATH, json={})
        assert response.status_code == 401
