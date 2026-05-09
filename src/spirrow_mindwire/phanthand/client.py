"""Async HTTP client for the Phanthand read-only file API.

Watcher → Phanthand integration per ``docs/architecture.md`` §6.5.
The client is a thin wrapper around ``httpx.AsyncClient``: it sends
typed requests, validates the standard ``ApiResponse`` envelope, and
turns ``success=False`` and HTTP-layer failures into typed exceptions.

Error policy (per ``feedback_trust_llm_for_tool_errors``):
- This layer raises clean, typed exceptions.
- The MCP-tool wrapper layer (built in a later sub-PR) catches them
  and converts to ``{"is_error": true, ...}`` tool results so the LLM
  can react. We do **not** translate to tool-results here — keeping
  this layer purely transport-level.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter

from .models import (
    ApiResponse,
    FileExistsData,
    FileInfoData,
    FileListData,
    FileReadData,
    FileSearchData,
    FileTreeData,
    HealthData,
)

T = TypeVar("T", bound=BaseModel)


class PhanthandError(Exception):
    """Base class for all Phanthand client errors."""


class PhanthandHTTPError(PhanthandError):
    """HTTP-layer failure (network error, non-2xx status, malformed body)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PhanthandAPIError(PhanthandError):
    """Phanthand returned ``success=False`` with an application error.

    The original ``error`` string from the response body is preserved so
    the caller can surface it verbatim to the LLM.
    """

    def __init__(self, message: str, *, endpoint: str) -> None:
        super().__init__(message)
        self.endpoint = endpoint


class PhanthandClient:
    """Thin async client wrapping the Phanthand HTTP API.

    Usage::

        async with PhanthandClient(endpoint, api_key) as client:
            data = await client.read_file("/path/to/file")

    Or with explicit lifecycle::

        client = PhanthandClient(endpoint, api_key)
        try:
            ...
        finally:
            await client.aclose()
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str | None,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=endpoint.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> PhanthandClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── System ────────────────────────────────────────────────────

    async def health(self) -> HealthData:
        """``GET /health``. Auth not required."""
        try:
            resp = await self._client.get("/health")
        except httpx.HTTPError as e:
            raise PhanthandHTTPError(f"GET /health: {e}") from e
        return self._unwrap("/health", resp, HealthData)

    # ── File operations (all require auth) ───────────────────────

    async def read_file(self, path: str, encoding: str = "utf-8") -> FileReadData:
        return await self._post("/files/read", {"path": path, "encoding": encoding}, FileReadData)

    async def list_directory(
        self,
        path: str,
        pattern: str = "*",
        *,
        recursive: bool = False,
    ) -> FileListData:
        return await self._post(
            "/files/list",
            {"path": path, "pattern": pattern, "recursive": recursive},
            FileListData,
        )

    async def file_exists(self, path: str) -> FileExistsData:
        return await self._post("/files/exists", {"path": path}, FileExistsData)

    async def file_info(self, path: str) -> FileInfoData:
        return await self._post("/files/info", {"path": path}, FileInfoData)

    async def file_tree(
        self,
        path: str,
        max_depth: int = 3,
        exclude_patterns: list[str] | None = None,
    ) -> FileTreeData:
        body: dict[str, Any] = {"path": path, "max_depth": max_depth}
        if exclude_patterns is not None:
            body["exclude_patterns"] = exclude_patterns
        return await self._post("/files/tree", body, FileTreeData)

    async def file_search(
        self,
        path: str,
        pattern: str,
        max_results: int = 100,
    ) -> FileSearchData:
        return await self._post(
            "/files/search",
            {"path": path, "pattern": pattern, "max_results": max_results},
            FileSearchData,
        )

    # ── internals ────────────────────────────────────────────────

    async def _post(
        self,
        endpoint: str,
        body: dict[str, Any],
        model: type[T],
    ) -> T:
        try:
            resp = await self._client.post(endpoint, json=body)
        except httpx.HTTPError as e:
            raise PhanthandHTTPError(f"POST {endpoint}: {e}") from e
        return self._unwrap(endpoint, resp, model)

    @staticmethod
    def _unwrap(endpoint: str, resp: httpx.Response, model: type[T]) -> T:
        if resp.status_code >= 500:
            raise PhanthandHTTPError(
                f"{endpoint} returned {resp.status_code}",
                status_code=resp.status_code,
            )
        if resp.status_code in (401, 403):
            raise PhanthandHTTPError(
                f"{endpoint} returned {resp.status_code} (auth)",
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            raise PhanthandHTTPError(
                f"{endpoint} returned {resp.status_code}",
                status_code=resp.status_code,
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise PhanthandHTTPError(f"{endpoint}: malformed JSON: {e}") from e

        adapter: TypeAdapter[ApiResponse[T]] = TypeAdapter(ApiResponse[model])  # type: ignore[valid-type]
        envelope = adapter.validate_python(payload)
        if not envelope.success:
            raise PhanthandAPIError(envelope.error or "(no error message)", endpoint=endpoint)
        if envelope.data is None:
            raise PhanthandHTTPError(f"{endpoint}: success=True but data is null")
        return envelope.data


__all__ = [
    "PhanthandAPIError",
    "PhanthandClient",
    "PhanthandError",
    "PhanthandHTTPError",
]
