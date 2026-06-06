"""Async HTTP client for the Lexora model gateway (OpenAI-compatible).

Stage 2 (ADR-06 dogfood roadmap) wiring: the independent-naysayer
RoleAdapter (:class:`~spirrow_mindwire.adapters.naysayer_lexora.NaysayerLexoraAdapter`)
reaches the naysayer model through Lexora's ``model="naysayer"`` tier.
Lexora is an OpenAI-compatible gateway (``POST /v1/chat/completions``)
fronting several backends; the *tier name* goes in the ``model`` field.

The ``naysayer`` tier routes to **Gemini** (``gemini-3.1-pro-preview``) since
the T15 pivot — SOT per ADR-2026-06-03-17 N-4 (the earlier "DeepSeek V4-Flash"
was retired; the independent-model identity is pinned in
:data:`spirrow_mindwire.naysayer.principles.NAYSAYER_UPSTREAM_MODEL`).

Runtime target (chatroom ``T-phase2-stage2-naysayer-adapter`` msg-215):
- Default endpoint is **``http://localhost:8110``** — Lexora binds
  ``0.0.0.0`` with **no caller auth**, so on the production host (mindwire
  co-resident with Lexora on sg-ai-server-01) the loopback address is the
  one that does not widen the unauthenticated surface onto the LAN /
  Tailscale net. Override via ``MINDWIRE_LEXORA_URL`` (e.g. a dev box
  reaches the server over Tailscale at ``http://100.79.84.62:8110``); the
  default must stay loopback.
- No auth header is sent (the gateway requires none from callers; its
  configured api_keys are for Lexora → upstream providers, not for us).

Error policy (per ``feedback_trust_llm_for_tool_errors`` precedent in
``phanthand/client.py``): this layer raises clean, typed exceptions and
is **fail-loud** — any non-2xx (e.g. an unknown tier → ``502`` with the
upstream's ``404 model does not exist``) becomes a
:class:`LexoraHTTPError`, never a silent empty result. The adapter maps
these onto the §3.4 Port exception catalog.

:class:`LexoraClient` does real network I/O and is exercised only by the
``-m manual`` smoke test; the adapter *logic* is unit-tested against the
:class:`LexoraChatClient` Protocol with a fake (the ``client_factory``
pattern from ``claude_code_sdk.py``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Protocol

import httpx

_DEFAULT_LEXORA_URL = "http://localhost:8110"
"""Default Lexora gateway endpoint. Loopback **by design** (no-auth surface) —
env-overridable, but the default must not be a LAN/Tailscale IP."""

# Lexora's gateway timeout is 900s (long reasoning generations). The client default is set BY THE
# CALLER (e.g. the naysayer PR-review driver picks backend + margin so the client always outlives
# the backend — see ``naysayer/pr_review.py``). This module-level value is only the bare default
# for ad-hoc ``LexoraClient()`` use; it intentionally equals the backend timeout, so a caller that
# needs the "never time out before the backend" guarantee must pass an explicit ``timeout_seconds``
# margin rather than rely on this default.
_DEFAULT_TIMEOUT_SECONDS = 900.0


def lexora_url() -> str:
    """Resolve the Lexora URL from ``MINDWIRE_LEXORA_URL`` (env) or the default.

    Unset *or empty* falls back to :data:`_DEFAULT_LEXORA_URL` (an empty
    base URL would otherwise fail confusingly at request time).
    """
    return os.environ.get("MINDWIRE_LEXORA_URL") or _DEFAULT_LEXORA_URL


class LexoraError(Exception):
    """Base class for all Lexora client errors."""


class LexoraHTTPError(LexoraError):
    """HTTP-layer failure: network error, non-2xx status, or malformed body.

    Non-2xx is **fail-loud** (ADR-06 Stage 2 main order #1): an unknown /
    unreachable tier surfaces here rather than silently routing elsewhere.
    The upstream ``detail`` string (FastAPI error shape) is preserved in
    the message when present.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LexoraTimeoutError(LexoraHTTPError):
    """The request did not complete within the client timeout (``httpx.TimeoutException``).

    A **subclass** of :class:`LexoraHTTPError`, so existing ``except LexoraHTTPError`` handlers
    stay backward-compatible (a timeout is still an HTTP-layer failure). It is broken out so a
    caller that wants to treat a timeout differently — e.g. the naysayer PR-review driver, which
    degrades a timed-out review to a fail-closed REQUEST_CHANGES instead of crashing the pipeline —
    can catch it *specifically* (``except LexoraTimeoutError``) ahead of the generic handler. Other
    transport failures (connect / read errors, etc.) remain plain :class:`LexoraHTTPError`.
    """


class LexoraAPIError(LexoraError):
    """The gateway returned 2xx but a structurally-unusable completion.

    Typical: no ``choices`` / missing ``message`` — i.e. a well-formed
    HTTP response that does not carry an assistant turn.
    """


@dataclass(frozen=True)
class ChatMessage:
    """One OpenAI-style chat message (``role`` + ``content``)."""

    role: str  # "system" / "user" / "assistant"
    content: str


@dataclass(frozen=True)
class ChatCompletion:
    """Parsed ``/v1/chat/completions`` result (the fields the adapter uses).

    ``reasoning_content`` is the reasoning model's *deliberation* — a
    **sibling** of ``content`` under ``choices[0].message`` (confirmed
    against the live gateway, msg-215 open point). It is captured for
    observability but the **reply is always ``content``**; the adapter
    never posts ``reasoning_content``.
    """

    content: str
    reasoning_content: str | None
    finish_reason: str | None
    model: str | None
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class LexoraChatClient(Protocol):
    """Structural view of the Lexora methods the adapter drives.

    Satisfied by :class:`LexoraClient`; the adapter's unit tests inject a
    fake with the same shape (``client_factory`` pattern).
    """

    async def chat_completion(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        max_tokens: int,
    ) -> ChatCompletion: ...

    async def health(self) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


def _parse_completion(payload: dict[str, Any]) -> ChatCompletion:
    """Extract a :class:`ChatCompletion` from a raw response body.

    Raises :class:`LexoraAPIError` if the 2xx body lacks an assistant turn.
    """
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LexoraAPIError(f"completion response had no choices: {payload!r}")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise LexoraAPIError(f"completion choice had no message: {choice!r}")
    content = message.get("content")
    usage = payload.get("usage")
    return ChatCompletion(
        # content may legitimately be "" / None when the budget was spent on
        # reasoning_content (finish_reason="length"); the adapter treats an
        # empty reply as a fail-loud DeliveryError, not this layer.
        content=content if isinstance(content, str) else "",
        reasoning_content=message.get("reasoning_content"),
        finish_reason=choice.get("finish_reason"),
        model=payload.get("model"),
        usage=usage if isinstance(usage, dict) else {},
        raw=payload,
    )


class LexoraClient:
    """Thin async client over the Lexora OpenAI-compatible gateway.

    Usage::

        async with LexoraClient() as client:           # MINDWIRE_LEXORA_URL or default
            result = await client.chat_completion(
                model="naysayer", messages=[...], max_tokens=4096,
            )

    A single instance is **shared** across naysayer sessions (one httpx
    connection pool) and closed once at adapter teardown via
    :meth:`aclose`.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # ``url`` must include the scheme; without one httpx treats it as a
        # relative URL and requests fail opaquely. No Authorization header is
        # set — the gateway requires none from callers (msg-215).
        self._client = httpx.AsyncClient(
            base_url=(url if url is not None else lexora_url()).rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> LexoraClient:
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

    async def health(self) -> dict[str, Any]:
        """``GET /health``. Returns the gateway's per-backend status dict."""
        try:
            resp = await self._client.get("/health")
        except httpx.TimeoutException as e:
            # TimeoutException is a subclass of RequestError, so it must be caught FIRST to wrap
            # it as the (sub)typed LexoraTimeoutError rather than the generic LexoraHTTPError.
            raise LexoraTimeoutError(f"GET /health timed out: {e}") from e
        except httpx.RequestError as e:
            raise LexoraHTTPError(f"GET /health: {e}") from e
        if resp.status_code >= 400:
            raise LexoraHTTPError(
                f"/health returned {resp.status_code}: {_error_detail(resp)}",
                status_code=resp.status_code,
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise LexoraHTTPError(f"/health: malformed JSON: {e}") from e
        if not isinstance(body, dict):
            raise LexoraHTTPError(f"/health: expected a JSON object, got {type(body).__name__}")
        return body

    async def chat_completion(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        max_tokens: int,
    ) -> ChatCompletion:
        """``POST /v1/chat/completions`` for ``model`` (a Lexora tier name).

        Fail-loud: a non-2xx status raises :class:`LexoraHTTPError` with
        the upstream ``detail`` (so an unknown tier never degrades into a
        silent fallback). Raises :class:`LexoraAPIError` if a 2xx body has
        no assistant turn.
        """
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        try:
            resp = await self._client.post("/v1/chat/completions", json=body)
        except httpx.TimeoutException as e:
            # TimeoutException is a subclass of RequestError, so it must be caught FIRST to wrap
            # it as the (sub)typed LexoraTimeoutError. The naysayer driver catches this specifically
            # to degrade a timed-out review to a fail-closed REQUEST_CHANGES instead of crashing.
            raise LexoraTimeoutError(f"POST /v1/chat/completions ({model}) timed out: {e}") from e
        except httpx.RequestError as e:
            raise LexoraHTTPError(f"POST /v1/chat/completions ({model}): {e}") from e
        if resp.status_code >= 400:
            detail = _error_detail(resp)
            raise LexoraHTTPError(
                f"/v1/chat/completions ({model}) returned {resp.status_code}: {detail}",
                status_code=resp.status_code,
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise LexoraHTTPError(f"/v1/chat/completions ({model}): malformed JSON: {e}") from e
        if not isinstance(payload, dict):
            kind = type(payload).__name__
            raise LexoraHTTPError(
                f"/v1/chat/completions ({model}): expected a JSON object, got {kind}"
            )
        return _parse_completion(payload)


def _error_detail(resp: httpx.Response) -> str:
    """Best-effort extraction of a FastAPI ``{"detail": ...}`` error string."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)[:500]


__all__ = [
    "ChatCompletion",
    "ChatMessage",
    "LexoraAPIError",
    "LexoraChatClient",
    "LexoraClient",
    "LexoraError",
    "LexoraHTTPError",
    "LexoraTimeoutError",
    "lexora_url",
]
