"""Async GitHub REST client — Stage 3 naysayer PR review (WIRING_ALLOWLIST_SPEC §A.3).

The PR-review naysayer adapter
(:class:`~spirrow_mindwire.adapters.naysayer_pr_review.NaysayerPrReviewAdapter`)
uses this to (1) fetch a PR's unified diff and (2) submit a PR review
(``APPROVE`` / ``REQUEST_CHANGES``) on the develop→main PR. Auth is a
**repo-scoped fine-grained token** (env spec §4: Contents R/W + PR R/W).
The proposer/implementer token is resolved from ``MINDWIRE_GITHUB_TOKEN`` /
``GITHUB_TOKEN`` (:func:`github_token`); the **naysayer** uses a *separate*
identity (``MINDWIRE_NAYSAYER_GITHUB_TOKEN``, :func:`naysayer_github_token`)
so its review is author≠approver — GitHub rejects approving your own PR (T22).

Error policy mirrors :mod:`spirrow_mindwire.lexora.client` / ``phanthand``:
clean typed exceptions, **fail-loud** — any non-2xx becomes a
:class:`GitHubHTTPError`, never a silent empty result. The adapter maps an
unreachable GitHub onto a fail-closed halt (ADR-07 §2.6).

:class:`GitHubClient` does real network I/O and is exercised only by the
``-m manual`` smoke test; the adapter logic is unit-tested against the
:class:`GitHubReviewClient` Protocol (httpx ``MockTransport``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol

import httpx

_DEFAULT_API_BASE = "https://api.github.com"
_DEFAULT_TIMEOUT_SECONDS = 60.0

_PR_URL_RE = re.compile(r"github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/(\d+)")
_PR_SHORT_RE = re.compile(r"\b([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#(\d+)\b")


def github_token() -> str | None:
    """Resolve the proposer/implementer GitHub token from env.

    ``MINDWIRE_GITHUB_TOKEN`` first, then ``GITHUB_TOKEN``. This is the shared
    author identity (``takayan0908``); it deliberately does **not** read the
    naysayer var, so the two identities stay separate (T22).
    """
    return os.environ.get("MINDWIRE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or None


def naysayer_github_token() -> str | None:
    """Resolve the naysayer's GitHub token — a *separate identity* (T22).

    GitHub forbids approving your own PR, so the naysayer's review must come from
    a different account (``takahito-spirrowgames``) than the proposer/implementer
    author. Resolves ``MINDWIRE_NAYSAYER_GITHUB_TOKEN`` first; if that is not
    provisioned yet it falls back to the shared :func:`github_token` so the
    adapter still functions (the same-identity 422 is then handled by the
    naysayer adapter's COMMENT fallback) — once the distinct token is set, the
    formal APPROVE / REQUEST_CHANGES goes through. The token's scope is the
    caller's concern (env spec: repo-scoped, least privilege); this only resolves
    which identity to authenticate as.
    """
    return os.environ.get("MINDWIRE_NAYSAYER_GITHUB_TOKEN") or github_token()


class ReviewEvent(StrEnum):
    """A GitHub PR review event (the naysayer's verdict, §A.3)."""

    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    COMMENT = "COMMENT"


@dataclass(frozen=True)
class PrRef:
    """A pull-request reference (``owner/repo#number``)."""

    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


def parse_pr_ref(text: str) -> PrRef | None:
    """Extract a :class:`PrRef` from free text (PR URL or ``owner/repo#n``).

    Returns ``None`` if no PR reference is present (the caller decides whether
    that is a no-op or an error).
    """
    m = _PR_URL_RE.search(text)
    if m is None:
        m = _PR_SHORT_RE.search(text)
    if m is None:
        return None
    return PrRef(owner=m.group(1), repo=m.group(2), number=int(m.group(3)))


class GitHubError(Exception):
    """Base class for all GitHub client errors."""


class GitHubHTTPError(GitHubError):
    """HTTP-layer failure: network error, non-2xx status, or malformed body.

    Fail-loud (ADR-07 §2.6): a failed fetch/submit surfaces here rather than
    degrading silently, so the naysayer adapter can fail-closed halt.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubReviewClient(Protocol):
    """Structural view of the GitHub methods the naysayer adapter drives."""

    async def fetch_pr_diff(self, pr: PrRef) -> str: ...

    async def submit_review(
        self, pr: PrRef, *, event: ReviewEvent, body: str
    ) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class GitHubClient:
    """Thin async client over the GitHub REST API (diff fetch + review submit).

    A single instance is **shared** across naysayer sessions (one httpx pool)
    and closed once at adapter teardown via :meth:`aclose`.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = _DEFAULT_API_BASE,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token if token is not None else github_token()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers=headers,
            transport=transport,
        )

    async def __aenter__(self) -> GitHubClient:
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

    async def fetch_pr_diff(self, pr: PrRef) -> str:
        """``GET /repos/{owner}/{repo}/pulls/{n}`` as a unified diff."""
        path = f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}"
        try:
            resp = await self._client.get(
                path, headers={"Accept": "application/vnd.github.v3.diff"}
            )
        except httpx.RequestError as exc:
            raise GitHubHTTPError(f"GET {path} (diff): {exc}") from exc
        if resp.status_code >= 400:
            raise GitHubHTTPError(
                f"GET {path} (diff) returned {resp.status_code}: {_error_detail(resp)}",
                status_code=resp.status_code,
            )
        return resp.text

    async def submit_review(self, pr: PrRef, *, event: ReviewEvent, body: str) -> dict[str, Any]:
        """``POST /repos/{owner}/{repo}/pulls/{n}/reviews`` with a verdict event."""
        path = f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews"
        try:
            resp = await self._client.post(path, json={"event": event.value, "body": body})
        except httpx.RequestError as exc:
            raise GitHubHTTPError(f"POST {path} (review): {exc}") from exc
        if resp.status_code >= 400:
            raise GitHubHTTPError(
                f"POST {path} (review) returned {resp.status_code}: {_error_detail(resp)}",
                status_code=resp.status_code,
            )
        try:
            body_json = resp.json()
        except ValueError as exc:
            raise GitHubHTTPError(f"POST {path} (review): malformed JSON: {exc}") from exc
        return body_json if isinstance(body_json, dict) else {"raw": body_json}


def _error_detail(resp: httpx.Response) -> str:
    """Best-effort extraction of a GitHub ``{"message": ...}`` error string."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict) and "message" in body:
        return str(body["message"])
    return str(body)[:500]


__all__ = [
    "GitHubClient",
    "GitHubError",
    "GitHubHTTPError",
    "GitHubReviewClient",
    "PrRef",
    "ReviewEvent",
    "github_token",
    "naysayer_github_token",
    "parse_pr_ref",
]
