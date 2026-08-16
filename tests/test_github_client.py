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
    CiState,
    GitHubClient,
    GitHubHTTPError,
    PrRef,
    ReviewEvent,
    _derive_ci_state,
    _required_workflows_from_env,
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


def test_parse_pr_ref_ends_at_markdown_emphasis_including_the_underscore() -> None:
    # Every producer feeding this function writes Markdown, so a marker against the number is
    # punctuation, not part of the ref. `_` used to be the exception -- `\b` is defined by `\w`
    # and `\w` counts `_` as a word character -- so an emphasised ref parsed as no ref at all
    # while every other marker parsed fine (T-handoff-parser-markdown-tolerance msg-1163).
    for text in (
        "spirrowgames/spirrow-mindwire#42_",
        "spirrowgames/spirrow-mindwire#42__",
        "spirrowgames/spirrow-mindwire#42*",
        "spirrowgames/spirrow-mindwire#42.",
        "spirrowgames/spirrow-mindwire#42_ and then some prose",
    ):
        assert parse_pr_ref(text) == _PR, text


def test_parse_pr_ref_still_refuses_a_number_that_runs_on() -> None:
    # The end of a ref moved for `_` only. Letters and digits still continue it, so a ref is not
    # invented out of a longer token (the divergence that retired this grammar's second copy).
    assert parse_pr_ref("spirrowgames/spirrow-mindwire#42abc") is None


def test_parse_pr_ref_refuses_an_underscore_in_the_owner() -> None:
    # A GitHub login is alphanumerics and hyphens, so `_acme` was never an owner -- but the class
    # accepted one, and an italicised ref parsed with a repository that does not exist. Callers
    # already fail safe on None, which is where this now goes. (A repo name may still contain `_`.)
    assert parse_pr_ref("_spirrowgames/spirrow-mindwire#42") is None
    assert parse_pr_ref("_spirrowgames/spirrow-mindwire#42_") is None
    repo_underscore = parse_pr_ref("acme/my_repo#7")
    assert repo_underscore is not None and repo_underscore.repo == "my_repo"


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


# ---------- fetch_ci_status (ADR-16 L1, Actions API) ---------------------- #


def _ci_handler(
    *,
    head_sha: str = "sha1",
    runs: list[dict[str, Any]] | None = None,
    pulls_status: int = 200,
    runs_status: int = 200,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/pulls/{_PR.number}"):
            if pulls_status != 200:
                return httpx.Response(pulls_status, json={"message": "pulls error"})
            return httpx.Response(200, json={"head": {"sha": head_sha}})
        if path.endswith("/actions/runs"):
            if runs_status != 200:
                return httpx.Response(runs_status, json={"message": "runs error"})
            return httpx.Response(200, json={"workflow_runs": runs if runs is not None else []})
        return httpx.Response(500, json={"message": f"unexpected {path}"})

    return handler


def _run(**kw: Any) -> dict[str, Any]:
    base = {
        "workflow_id": 1,
        "run_number": 1,
        "name": "test",
        "status": "completed",
        "conclusion": "success",
    }
    base.update(kw)
    return base


@pytest.mark.anyio
async def test_fetch_ci_status_success() -> None:
    async with _client(_ci_handler(head_sha="abc", runs=[_run()])) as client:
        st = await client.fetch_ci_status(_PR)
    assert st.state is CiState.SUCCESS
    assert st.head_sha == "abc"
    assert st.failing == []


@pytest.mark.anyio
async def test_fetch_ci_status_failure_names_failing_check() -> None:
    runs = [_run(name="test", conclusion="failure")]
    async with _client(_ci_handler(runs=runs)) as client:
        st = await client.fetch_ci_status(_PR)
    assert st.state is CiState.FAILURE
    assert st.failing == ["test"]


@pytest.mark.anyio
async def test_fetch_ci_status_pending_on_incomplete_run() -> None:
    runs = [_run(status="in_progress", conclusion=None)]
    async with _client(_ci_handler(runs=runs)) as client:
        st = await client.fetch_ci_status(_PR)
    assert st.state is CiState.PENDING


@pytest.mark.anyio
async def test_fetch_ci_status_no_runs_is_unknown() -> None:
    # No CI runs for the head SHA → can't confirm green → fail-closed UNKNOWN.
    async with _client(_ci_handler(head_sha="abc", runs=[])) as client:
        st = await client.fetch_ci_status(_PR)
    assert st.state is CiState.UNKNOWN
    assert st.head_sha == "abc"


@pytest.mark.anyio
async def test_fetch_ci_status_latest_run_per_workflow_wins() -> None:
    # An older failed run + a newer success run for the same workflow → SUCCESS
    # (a re-run / superseded run must not false-fail).
    runs = [
        _run(run_number=4, conclusion="failure"),
        _run(run_number=5, conclusion="success"),
    ]
    async with _client(_ci_handler(runs=runs)) as client:
        st = await client.fetch_ci_status(_PR)
    assert st.state is CiState.SUCCESS


# ---------- CI gate scoping (MINDWIRE_NAYSAYER_REQUIRED_WORKFLOWS) -------- #


def test_derive_ci_state_required_workflows_ignores_advisory_pending() -> None:
    # PR #14 case: the gating "voxel-gate" succeeded; an advisory "voxel-stats"
    # is stuck pending (no self-hosted runner). Scoped to voxel-gate → SUCCESS.
    runs = [
        _run(workflow_id=1, name="voxel-gate", status="completed", conclusion="success"),
        _run(workflow_id=2, name="voxel-stats", status="pending", conclusion=None),
    ]
    st = _derive_ci_state(runs, "abc", required_workflows=frozenset({"voxel-gate"}))
    assert st.state is CiState.SUCCESS


def test_derive_ci_state_required_workflow_no_run_yet_is_pending() -> None:
    # naysayer PR #111 (round 2): with a checklist, a required gate that has no run
    # yet is the SAME wait-state as partial coverage → PENDING, not UNKNOWN (UNKNOWN
    # would misreport a token/permissions problem). Only the advisory ran here, so
    # the required "voxel-gate" is filtered out and no considered runs remain.
    runs = [_run(workflow_id=2, name="voxel-stats", status="pending", conclusion=None)]
    st = _derive_ci_state(runs, "abc", required_workflows=frozenset({"voxel-gate"}))
    assert st.state is CiState.PENDING


def test_derive_ci_state_no_required_no_runs_is_unknown() -> None:
    # Without a checklist, zero runs for the SHA is the genuine "is there any CI?"
    # fail-closed UNKNOWN — the one case UNKNOWN is reserved for (+ read failures).
    assert _derive_ci_state([], "abc").state is CiState.UNKNOWN


def test_derive_ci_state_required_workflow_failure_still_fails() -> None:
    runs = [
        _run(workflow_id=1, name="voxel-gate", conclusion="failure"),
        _run(workflow_id=2, name="voxel-stats", status="pending", conclusion=None),
    ]
    st = _derive_ci_state(runs, "abc", required_workflows=frozenset({"voxel-gate"}))
    assert st.state is CiState.FAILURE
    assert st.failing == ["voxel-gate"]


def test_derive_ci_state_required_workflow_missing_run_is_pending() -> None:
    # naysayer PR #111: with MULTIPLE required workflows, a required gate that has
    # no run for this SHA yet (GitHub Actions hasn't scheduled it) must NOT let the
    # subset that did run open the gate. backend-gate succeeded but frontend-gate is
    # absent → PENDING (fail-closed), not SUCCESS.
    runs = [_run(workflow_id=1, name="backend-gate", status="completed", conclusion="success")]
    st = _derive_ci_state(
        runs, "abc", required_workflows=frozenset({"backend-gate", "frontend-gate"})
    )
    assert st.state is CiState.PENDING
    assert st.failing == []


def test_derive_ci_state_required_workflows_full_coverage_is_success() -> None:
    # Both required gates produced a successful run → coverage complete → SUCCESS.
    runs = [
        _run(workflow_id=1, name="backend-gate", status="completed", conclusion="success"),
        _run(workflow_id=2, name="frontend-gate", status="completed", conclusion="success"),
    ]
    st = _derive_ci_state(
        runs, "abc", required_workflows=frozenset({"backend-gate", "frontend-gate"})
    )
    assert st.state is CiState.SUCCESS


def test_derive_ci_state_default_considers_all_workflows() -> None:
    # Unset (None) preserves prior behavior: an advisory pending still gates.
    runs = [
        _run(workflow_id=1, name="voxel-gate", status="completed", conclusion="success"),
        _run(workflow_id=2, name="voxel-stats", status="pending", conclusion=None),
    ]
    assert _derive_ci_state(runs, "abc").state is CiState.PENDING


def test_required_workflows_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDWIRE_NAYSAYER_REQUIRED_WORKFLOWS", raising=False)
    assert _required_workflows_from_env() is None
    monkeypatch.setenv("MINDWIRE_NAYSAYER_REQUIRED_WORKFLOWS", " voxel-gate , ")
    assert _required_workflows_from_env() == frozenset({"voxel-gate"})
    monkeypatch.setenv("MINDWIRE_NAYSAYER_REQUIRED_WORKFLOWS", "   ")
    assert _required_workflows_from_env() is None


@pytest.mark.anyio
async def test_fetch_ci_status_scoped_to_required_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINDWIRE_NAYSAYER_REQUIRED_WORKFLOWS", "voxel-gate")
    runs = [
        _run(workflow_id=1, name="voxel-gate", status="completed", conclusion="success"),
        _run(workflow_id=2, name="voxel-stats", status="pending", conclusion=None),
    ]
    async with _client(_ci_handler(head_sha="abc", runs=runs)) as client:
        st = await client.fetch_ci_status(_PR)
    assert st.state is CiState.SUCCESS


@pytest.mark.anyio
async def test_fetch_ci_status_runs_403_fail_closed() -> None:
    # 403 on the runs read (e.g. token lacks Actions:read) → fail-closed UNKNOWN,
    # NOT a raise (a CI read failure must withhold APPROVE, not crash the review).
    async with _client(_ci_handler(head_sha="abc", runs_status=403)) as client:
        st = await client.fetch_ci_status(_PR)
    assert st.state is CiState.UNKNOWN
    assert st.head_sha == "abc"


@pytest.mark.anyio
async def test_fetch_ci_status_pulls_404_fail_closed() -> None:
    async with _client(_ci_handler(pulls_status=404)) as client:
        st = await client.fetch_ci_status(_PR)
    assert st.state is CiState.UNKNOWN
    assert st.head_sha is None


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
