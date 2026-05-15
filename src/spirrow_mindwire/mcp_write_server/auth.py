"""API-key authentication for the mindwire-mcp-server.

Bearer-token gate on every HTTP request — the API key is loaded once at
startup from the env var named in :attr:`MCPServerConfig.api_key_env`
(name in TOML, value in the environment so secrets never land on disk).
Missing / mismatched headers return 401 with a verbatim short detail
message; the caller (= a claude.ai-side MCP client) decides how to react
to auth failures per [[feedback_trust_llm_for_tool_errors]].

The constant-time comparison (:func:`hmac.compare_digest`) defends
against timing side-channels — a meaningful safeguard for a long-lived
bearer token even though the server binds to ``127.0.0.1`` by default.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from spirrow_mindwire.config import MindwireSettings


class MissingApiKeyError(RuntimeError):
    """Raised when ``MCPServerConfig.api_key_env`` resolves to an empty value.

    Surfaced at server startup (in :func:`read_api_key`) rather than at
    first request so the operator gets a fail-fast.
    """


def read_api_key(settings: MindwireSettings) -> str:
    """Resolve the API key from ``MCPServerConfig.api_key_env``.

    Raises :class:`MissingApiKeyError` if the env var is unset or empty.
    """
    env_name = settings.mcp_server.api_key_env
    value = os.environ.get(env_name, "")
    if not value:
        raise MissingApiKeyError(
            f"environment variable {env_name!r} (= MCPServerConfig.api_key_env) "
            "must be set to a non-empty API key for the mindwire-mcp-server"
        )
    return value


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Require ``Authorization: Bearer <api_key>`` on every request.

    A mismatch or missing header returns 401 with ``{"detail": "..."}``.
    Token comparison uses :func:`hmac.compare_digest` (= constant-time)
    so probing for the secret via timing differences is not viable.
    """

    def __init__(self, app: ASGIApp, *, api_key: str) -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return JSONResponse(
                {"detail": "missing or malformed Authorization header"},
                status_code=401,
            )
        if not hmac.compare_digest(token, self._api_key):
            return JSONResponse({"detail": "invalid API key"}, status_code=401)
        return await call_next(request)


__all__ = ["ApiKeyMiddleware", "MissingApiKeyError", "read_api_key"]
