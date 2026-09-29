"""D-16ab foundation: the open-PR listing / review-body / additive fields in
``github.client`` and the shared ``ReadOnlyMcp`` (T-stalled-pr-has-no-detector
msg-4685 §1, §7-7, §7-8)."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from spirrow_mindwire.github.client import (
    GitHubClient,
    GitHubHTTPError,
    OpenPrListingError,
    PrRef,
)
from spirrow_mindwire.magickit.read_only import ReadOnlyMcp, ReadOnlyViolationError
from spirrow_mindwire.stall_ledger.heartbeat import FetchOutcome, build_open_pr_query


def _gh(handler: Callable[[httpx.Request], httpx.Response]) -> GitHubClient:
    return GitHubClient("tok", transport=httpx.MockTransport(handler))


def _pr_row(n: int, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "number": n,
        "draft": False,
        "head": {"sha": f"sha{n}"},
        "created_at": "2026-09-01T00:00:00Z",
    }
    row.update(over)
    return row


# ── §7-7 snapshot: the listing sends build_open_pr_query exactly ──────────────────────


def test_open_pr_listing_sends_the_pinned_query_and_paginates() -> None:
    seen: list[httpx.Request] = []
    pinned = build_open_pr_query("o", "r")

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        page = int(req.url.params["page"])
        rows = [_pr_row(i) for i in range(100)] if page == 1 else [_pr_row(100), {"x": 1}]
        return httpx.Response(200, json=rows)

    listing = asyncio.run(_gh(handler).list_open_prs("o", "r"))
    assert [r.url.path for r in seen] == [pinned["path"], pinned["path"]]
    for i, req in enumerate(seen, start=1):
        params = dict(req.url.params)
        assert params.pop("page") == str(i)
        assert params == {k: str(v) for k, v in pinned["params"].items()}
    assert listing.examined == 102
    assert listing.unrecognized == 1
    assert len(listing.prs) == 101


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(401, "auth_failure"), (403, "auth_failure"), (500, "http_error")],
)
def test_open_pr_listing_failure_is_loud(status: int, outcome: str) -> None:
    client = _gh(lambda req: httpx.Response(status, json={"message": "no"}))
    with pytest.raises(OpenPrListingError) as info:
        asyncio.run(client.list_open_prs("o", "r"))
    assert info.value.outcome == outcome
    FetchOutcome(info.value.outcome)  # every outcome is a FetchOutcome value


def test_review_body_404_is_none_and_errors_raise() -> None:
    ref = PrRef("o", "r", 1)
    assert asyncio.run(_gh(lambda r: httpx.Response(404)).fetch_review_body(ref, "9")) is None
    body = asyncio.run(
        _gh(lambda r: httpx.Response(200, json={"body": "hi"})).fetch_review_body(ref, "9")
    )
    assert body == "hi"
    with pytest.raises(GitHubHTTPError):
        asyncio.run(_gh(lambda r: httpx.Response(502)).fetch_review_body(ref, "9"))


def test_review_id_and_mergeable_state_are_read() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/reviews"):
            return httpx.Response(
                200, json=[{"id": 77, "state": "APPROVED", "submitted_at": "2026-09-01T00:00:00Z"}]
            )
        return httpx.Response(
            200, json={"state": "open", "head": {"sha": "s"}, "mergeable_state": "dirty"}
        )

    client = _gh(handler)
    ref = PrRef("o", "r", 1)
    assert asyncio.run(client.fetch_pr_reviews_strict(ref))[0].review_id == "77"
    assert asyncio.run(client.fetch_pr_state(ref)).mergeable_state == "dirty"


class _ListOnlyMcp:
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return {"items": [], "total": 0}


# ── §7-8: ReadOnlyMcp lives in one place ──────────────────────────────────────────────


def _load_script(name: str) -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_d16ab_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("script", ["unregistered_threads", "pr_review_sweep_phase0"])
def test_both_scripts_use_the_shared_read_only_mcp(script: str) -> None:
    module = _load_script(script)
    assert issubclass(module.ReadOnlyMcp, ReadOnlyMcp)
    assert module.Phase0WriteAttemptedError is ReadOnlyViolationError


def test_shared_read_only_mcp_refuses_and_passes() -> None:
    mcp = ReadOnlyMcp(_ListOnlyMcp(), {"chatroom_list_threads"}, label="X")
    with pytest.raises(ReadOnlyViolationError, match="X is read-only"):
        asyncio.run(mcp.call_tool("chatroom_post_message", {}))
    assert asyncio.run(mcp.call_tool("chatroom_list_threads", {"offset": 0}))["total"] == 0
