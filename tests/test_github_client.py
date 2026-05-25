"""Tests for :class:`spirrow_mindwire.github.client.GitHubClient` + PR-ref parsing.

httpx ``MockTransport`` exercises the request/response cycle (diff fetch +
review submit) without a live GitHub.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from spirrow_mindwire.github.client import (
    GitHubClient,
    GitHubHTTPError,
    PrRef,
    ReviewEvent,
    github_token,
    naysayer_github_token,
    parse_pr_ref,
)

_PR = PrRef(owner="spirrowgames", repo="spirrow-mindwire", number=42)


def _client(handler: Any, *, token: str | None = "tok") -> GitHubClient:
    return GitHubClient(token, transport=httpx.MockTransport(handler))


# ---------- PR-ref parsing ----------------------------------------------- #


def test_parse_pr_ref_short_form() -> None:
    pr = parse_pr_ref("please review spirrowgames/spirrow-mindwire#42 now")
    assert pr == _PR


def test_parse_pr_ref_url() -> None:
    pr = parse_pr_ref("https://github.com/spirrowgames/spirrow-mindwire/pull/42 has the diff")
    assert pr == _PR


def test_parse_pr_ref_none() -> None:
    assert parse_pr_ref("no pull request here, just chatter") is None


def test_pr_ref_slug() -> None:
    assert _PR.slug == "spirrowgames/spirrow-mindwire#42"


# ---------- token resolution --------------------------------------------- #


def test_github_token_prefers_mindwire_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWIRE_GITHUB_TOKEN", "mw")
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    assert github_token() == "mw"


def test_github_token_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDWIRE_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    assert github_token() == "gh"


def test_github_token_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDWIRE_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert github_token() is None


def test_naysayer_token_separate_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # T22: the naysayer resolves its own var; github_token() (the proposer/
    # implementer author identity) must NOT pick it up — the two GitHub
    # identities stay separate so the naysayer's review is author != approver.
    monkeypatch.setenv("MINDWIRE_NAYSAYER_GITHUB_TOKEN", "nay")
    monkeypatch.setenv("MINDWIRE_GITHUB_TOKEN", "author")
    assert naysayer_github_token() == "nay"
    assert github_token() == "author"


def test_naysayer_token_falls_back_to_shared_until_provisioned(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # T22: before the distinct token is provisioned, the naysayer falls back to
    # the shared author token (the same-identity 422 → COMMENT path then applies),
    # and warns so the fail-open (author == approver) is visible, not silent.
    monkeypatch.delenv("MINDWIRE_NAYSAYER_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("MINDWIRE_GITHUB_TOKEN", "author")
    with caplog.at_level(logging.WARNING):
        assert naysayer_github_token() == "author"
    assert any("self-approval" in r.message for r in caplog.records)


# ---------- fetch_pr_diff ------------------------------------------------- #


@pytest.mark.anyio
async def test_fetch_pr_diff_returns_text() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["accept"] = request.headers.get("accept")
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, text="diff --git a/x b/x\n+added")

    async with _client(handler) as client:
        diff = await client.fetch_pr_diff(_PR)
    assert "diff --git" in diff
    assert seen["path"] == "/repos/spirrowgames/spirrow-mindwire/pulls/42"
    assert seen["accept"] == "application/vnd.github.v3.diff"
    assert seen["auth"] == "Bearer tok"


@pytest.mark.anyio
async def test_fetch_pr_diff_non_2xx_fail_loud() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    async with _client(handler) as client:
        with pytest.raises(GitHubHTTPError) as exc:
            await client.fetch_pr_diff(_PR)
    assert exc.value.status_code == 404
    assert "Not Found" in str(exc.value)


# ---------- submit_review ------------------------------------------------- #


@pytest.mark.anyio
async def test_submit_review_posts_event_and_body() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 7, "state": "CHANGES_REQUESTED"})

    async with _client(handler) as client:
        result = await client.submit_review(
            _PR, event=ReviewEvent.REQUEST_CHANGES, body="needs work"
        )
    assert result["id"] == 7
    assert seen["method"] == "POST"
    assert seen["path"] == "/repos/spirrowgames/spirrow-mindwire/pulls/42/reviews"
    assert seen["body"] == {"event": "REQUEST_CHANGES", "body": "needs work"}


@pytest.mark.anyio
async def test_submit_review_non_2xx_fail_loud() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Unprocessable"})

    async with _client(handler) as client:
        with pytest.raises(GitHubHTTPError) as exc:
            await client.submit_review(_PR, event=ReviewEvent.APPROVE, body="ok")
    assert exc.value.status_code == 422


@pytest.mark.anyio
async def test_no_token_omits_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDWIRE_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, text="diff")

    async with _client(handler, token=None) as client:
        await client.fetch_pr_diff(_PR)
    assert seen["auth"] is None
