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
    CrossPrApproveCoverage,
    GitHubClient,
    GitHubHTTPError,
    PrRef,
    ReviewEvent,
    ReviewInfo,
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
#
# The diff comes from a three-dot `compare/{base_ref}...{head_sha}` -- NOT from
# `pulls/{n}` in diff form. That old endpoint diffs against the base SHA
# snapshotted at PR creation time, which for stacked PRs / PRs that have pulled
# base in bleeds already-merged code into the gate's input (measured on
# spirrow-lexora#10: 53% excess = PR #9 code the same gate had already APPROVED).
# See spec/design/T-gate-reads-stale-base-diff.md.


def _pr_meta_handler(
    *,
    base_ref: str = "main",
    head_sha: str = "c4c107b",
    diff_text: str = "diff --git a/x b/x\n+added",
    diff_status: int = 200,
    diff_headers: dict[str, str] | None = None,
    calls: list[tuple[str, str, str | None]] | None = None,
) -> Any:
    """Serve pulls/{n} (JSON meta) and compare/{base}...{head} (diff)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        accept = request.headers.get("accept")
        auth = request.headers.get("authorization")
        if calls is not None:
            calls.append((request.method, path, accept))
        if path.endswith(f"/pulls/{_PR.number}"):
            return httpx.Response(
                200,
                json={"base": {"ref": base_ref}, "head": {"sha": head_sha}},
                headers={"x-auth-echo": auth or ""},
            )
        if "/compare/" in path:
            resp_headers = {"x-auth-echo": auth or ""}
            if diff_headers:
                resp_headers.update(diff_headers)
            if diff_status != 200:
                return httpx.Response(
                    diff_status, json={"message": "compare error"}, headers=resp_headers
                )
            return httpx.Response(diff_status, text=diff_text, headers=resp_headers)
        return httpx.Response(500, json={"message": f"unexpected {path}"})

    return handler


@pytest.mark.anyio
async def test_fetch_pr_diff_uses_three_dot_compare() -> None:
    # AC-1 / AC-2: the diff request is `compare/{base_ref}...{head_sha}` with
    # the diff Accept, and both segments come from the meta read (change the
    # meta payload -> the compare URL follows).
    calls: list[tuple[str, str, str | None]] = []
    handler = _pr_meta_handler(base_ref="develop", head_sha="c4c107b", calls=calls)
    async with _client(handler) as client:
        diff = await client.fetch_pr_diff(_PR)
    assert "diff --git" in diff
    # Two requests, in the meta-first-then-compare order.
    assert len(calls) == 2
    assert calls[0][1] == "/repos/spirrowgames/spirrow-mindwire/pulls/42"
    assert calls[0][0] == "GET"
    compare_method, compare_path, compare_accept = calls[1]
    assert compare_method == "GET"
    assert compare_path == "/repos/spirrowgames/spirrow-mindwire/compare/develop...c4c107b"
    assert compare_accept == "application/vnd.github.v3.diff"


@pytest.mark.anyio
async def test_fetch_pr_diff_base_ref_follows_meta() -> None:
    # AC-2 (second half): if the meta reports a different base branch, the
    # compare URL tracks it -- there is no pinned base sha in this code path.
    calls: list[tuple[str, str, str | None]] = []
    handler = _pr_meta_handler(base_ref="main", head_sha="deadbeef1234", calls=calls)
    async with _client(handler) as client:
        await client.fetch_pr_diff(_PR)
    assert calls[1][1] == "/repos/spirrowgames/spirrow-mindwire/compare/main...deadbeef1234"


@pytest.mark.anyio
async def test_fetch_pr_diff_url_encodes_base_ref_with_slash() -> None:
    # Advisory from Einstein: a `feature/stacked` branch name must not route
    # to a different endpoint. Slash is encoded as %2F. httpx decodes the .path
    # attribute back to `/`, so we assert on .raw_path (bytes) which keeps the
    # wire form — that is what a real GitHub sees and dispatches on.
    raw_paths: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw_paths.append(request.url.raw_path)
        if request.url.path.endswith(f"/pulls/{_PR.number}"):
            return httpx.Response(
                200, json={"base": {"ref": "feature/stacked"}, "head": {"sha": "abc123"}}
            )
        return httpx.Response(200, text="diff")

    async with _client(handler) as client:
        await client.fetch_pr_diff(_PR)
    assert (
        raw_paths[1] == b"/repos/spirrowgames/spirrow-mindwire/compare/feature%2Fstacked...abc123"
    )


@pytest.mark.anyio
async def test_fetch_pr_diff_meta_404_fails_loud_without_compare() -> None:
    # AC-3: meta non-2xx -> GitHubHTTPError, and no compare request is made
    # (nothing to compare against).
    calls: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("accept")))
        if request.url.path.endswith(f"/pulls/{_PR.number}"):
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(500, text="should not be called")

    async with _client(handler) as client:
        with pytest.raises(GitHubHTTPError) as exc:
            await client.fetch_pr_diff(_PR)
    assert exc.value.status_code == 404
    assert "Not Found" in str(exc.value)
    assert len(calls) == 1
    assert "/compare/" not in calls[0][1]


@pytest.mark.anyio
async def test_fetch_pr_diff_meta_missing_base_ref_fails_loud() -> None:
    # AC-3: meta shape is wrong (base.ref absent) -> raise; do not fall back.
    calls: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("accept")))
        # `head.sha` present but `base` empty -> base.ref lookup fails.
        return httpx.Response(200, json={"base": {}, "head": {"sha": "abc"}})

    async with _client(handler) as client:
        with pytest.raises(GitHubHTTPError):
            await client.fetch_pr_diff(_PR)
    assert len(calls) == 1
    assert "/compare/" not in calls[0][1]


@pytest.mark.anyio
async def test_fetch_pr_diff_meta_missing_head_sha_fails_loud() -> None:
    # AC-3: meta shape is wrong (head.sha absent) -> raise; do not fall back.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"base": {"ref": "main"}, "head": {}})

    async with _client(handler) as client:
        with pytest.raises(GitHubHTTPError):
            await client.fetch_pr_diff(_PR)


@pytest.mark.anyio
async def test_fetch_pr_diff_compare_non_2xx_fails_loud() -> None:
    # AC-4: compare non-2xx (e.g. 404 base branch deleted, 406 diff too large)
    # -> GitHubHTTPError.
    handler = _pr_meta_handler(diff_status=404)
    async with _client(handler) as client:
        with pytest.raises(GitHubHTTPError) as exc:
            await client.fetch_pr_diff(_PR)
    assert exc.value.status_code == 404


@pytest.mark.anyio
async def test_fetch_pr_diff_never_reads_pulls_diff_endpoint() -> None:
    # AC-4 regression guard for D-4 (no fallback): the old endpoint --
    # GET /pulls/{n} with the diff Accept -- must never be requested. If the
    # implementation ever regresses to the stale-base path, the diff Accept
    # will appear on the pulls/{n} URL and this test catches it.
    calls: list[tuple[str, str, str | None]] = []
    handler = _pr_meta_handler(calls=calls)
    async with _client(handler) as client:
        await client.fetch_pr_diff(_PR)
    for method, path, accept in calls:
        pulls_diff = (
            path.endswith(f"/pulls/{_PR.number}") and accept == "application/vnd.github.v3.diff"
        )
        assert not pulls_diff, (
            f"regressed to the pre-fix endpoint: {method} {path} with diff Accept "
            "-- the stale-base diff is exactly what T-gate-reads-stale-base-diff retires"
        )


@pytest.mark.anyio
async def test_fetch_pr_diff_sends_auth_and_diff_accept_on_compare() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/pulls/{_PR.number}"):
            return httpx.Response(200, json={"base": {"ref": "main"}, "head": {"sha": "abc"}})
        if "/compare/" in path:
            seen["accept"] = request.headers.get("accept")
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, text="diff --git a/x b/x\n+added")
        return httpx.Response(500)

    async with _client(handler) as client:
        await client.fetch_pr_diff(_PR)
    assert seen["accept"] == "application/vnd.github.v3.diff"
    assert seen["auth"] == "Bearer tok"


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
async def test_same_identity_422_carries_the_discriminating_error_text() -> None:
    """★ The ``errors`` array must survive into the exception text (measured, PR #194).

    This is the half of the same-identity fallback that was missing. The driver's COMMENT
    backstop branches on ``"own pull request"``, but GitHub puts that phrase in ``errors``
    while ``message`` is the generic ``"Unprocessable Entity"`` — so before this test the
    backstop could never fire on the real transport, and its own unit test stayed green only
    because it fabricated the exception message.

    The payload below is copied verbatim from the live 422 on
    ``POST /repos/SpirrowGames/spirrow-mindwire/pulls/194/reviews`` (2026-08-29), so the
    string this asserts on is the one GitHub actually sends, not one we invented.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "message": "Unprocessable Entity",
                "errors": ["Review Can not request changes on your own pull request"],
                "status": "422",
            },
        )

    async with _client(handler) as client:
        with pytest.raises(GitHubHTTPError) as exc:
            await client.submit_review(_PR, event=ReviewEvent.REQUEST_CHANGES, body="nope")
    assert exc.value.status_code == 422
    # The exact predicate NaysayerPrReviewDriver._submit_review evaluates.
    assert "own pull request" in str(exc.value).lower()


@pytest.mark.anyio
async def test_error_detail_reads_object_shaped_errors_entries() -> None:
    """GitHub's ``errors`` entries are sometimes objects, not strings — both must render."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "message": "Validation Failed",
                "errors": [
                    {"resource": "PullRequest", "code": "custom", "message": "No commits between"},
                    "plain string entry",
                    {"resource": "PullRequest", "code": "invalid"},
                    {"unrecognised": "shape"},
                ],
            },
        )

    async with _client(handler) as client:
        with pytest.raises(GitHubHTTPError) as exc:
            await client.submit_review(_PR, event=ReviewEvent.APPROVE, body="ok")
    detail = str(exc.value)
    assert "No commits between" in detail  # object with a message
    assert "plain string entry" in detail  # bare string
    assert "invalid" in detail  # object falling back to code
    assert "Validation Failed" in detail  # the generic message is still there


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
    seen: dict[str, Any] = {"auths": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auths"].append(request.headers.get("authorization"))
        if request.url.path.endswith(f"/pulls/{_PR.number}"):
            return httpx.Response(200, json={"base": {"ref": "main"}, "head": {"sha": "abc"}})
        return httpx.Response(200, text="diff")

    async with _client(handler, token=None) as client:
        await client.fetch_pr_diff(_PR)
    # Both the meta read and the compare read must go out unauthenticated.
    assert seen["auths"] == [None, None]


# ---------- fetch_ci_status: GraphQL fallback ----------------------------- #
#
# Measured 2026-08-17 on a private repo: `GET /actions/runs` is 403 for the
# review PAT and 404 for a classic `repo`-scope token, while `GET
# /actions/workflows` (the same `actions=read`) and `GET /contents/{path}` are
# 200 for both. Two tokens with different grants failing on the same endpoint
# set is not a permission gap, so the driver needs a second way to ask.


def _ci_handler_with_graphql(
    *,
    calls: list[str],
    head_sha: str = "sha1",
    runs: list[dict[str, Any]] | None = None,
    runs_status: int = 200,
    rollup_state: str | None = "SUCCESS",
    graphql_status: int = 200,
    graphql_head: str | None = "gsha",
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/graphql"):
            if graphql_status != 200:
                return httpx.Response(graphql_status, json={"message": "graphql error"})
            rollup = None if rollup_state is None else {"state": rollup_state}
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "headRefOid": graphql_head,
                                "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]},
                            }
                        }
                    }
                },
            )
        if path.endswith(f"/pulls/{_PR.number}"):
            return httpx.Response(200, json={"head": {"sha": head_sha}})
        if path.endswith("/actions/runs"):
            if runs_status != 200:
                return httpx.Response(runs_status, json={"message": "runs error"})
            return httpx.Response(200, json={"workflow_runs": runs if runs is not None else []})
        return httpx.Response(500, json={"message": f"unexpected {path}"})

    return handler


@pytest.mark.anyio
async def test_graphql_fallback_answers_when_rest_is_forbidden() -> None:
    calls: list[str] = []
    async with _client(
        _ci_handler_with_graphql(calls=calls, runs_status=403, rollup_state="SUCCESS")
    ) as client:
        st = await client.fetch_ci_status(_PR)
    assert st.state is CiState.SUCCESS
    assert st.head_sha == "gsha"
    # The fallback cannot name checks; that cost is accepted, not hidden.
    assert st.failing == []
    assert any(p.endswith("/graphql") for p in calls)


@pytest.mark.anyio
async def test_rest_success_does_not_consult_graphql() -> None:
    # REST stays primary: it is the only path that can name failing runs.
    calls: list[str] = []
    async with _client(
        _ci_handler_with_graphql(calls=calls, runs=[_run()], rollup_state="FAILURE")
    ) as client:
        st = await client.fetch_ci_status(_PR)
    assert st.state is CiState.SUCCESS
    assert not any(p.endswith("/graphql") for p in calls)


@pytest.mark.anyio
async def test_rest_failure_is_kept_with_its_check_names() -> None:
    calls: list[str] = []
    async with _client(
        _ci_handler_with_graphql(
            calls=calls, runs=[_run(name="test", conclusion="failure")], rollup_state="SUCCESS"
        )
    ) as client:
        st = await client.fetch_ci_status(_PR)
    assert st.state is CiState.FAILURE
    assert st.failing == ["test"]
    # A REST answer is never overridden by the fallback — least of all a red one
    # by a green one.
    assert not any(p.endswith("/graphql") for p in calls)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("rollup", "expected"),
    [
        ("SUCCESS", CiState.SUCCESS),
        ("FAILURE", CiState.FAILURE),
        ("ERROR", CiState.FAILURE),
        ("PENDING", CiState.PENDING),
        ("EXPECTED", CiState.PENDING),
        # Unknown / future enum member → fail-closed, never green.
        ("SOMETHING_NEW", CiState.UNKNOWN),
        (None, CiState.UNKNOWN),  # null rollup = no checks at all
    ],
)
async def test_graphql_rollup_state_mapping(rollup: str | None, expected: CiState) -> None:
    calls: list[str] = []
    async with _client(
        _ci_handler_with_graphql(calls=calls, runs_status=403, rollup_state=rollup)
    ) as client:
        st = await client.fetch_ci_status(_PR)
    assert st.state is expected


@pytest.mark.anyio
async def test_both_paths_unusable_stays_unknown_and_keeps_rest_head_sha() -> None:
    calls: list[str] = []
    async with _client(
        _ci_handler_with_graphql(calls=calls, head_sha="abc", runs_status=403, graphql_status=403)
    ) as client:
        st = await client.fetch_ci_status(_PR)
    assert st.state is CiState.UNKNOWN
    # REST reached the PR (so it knows the head) even though it could not read runs.
    assert st.head_sha == "abc"


# ---------- fetch_check_rollup (pre-gate admission input) ----------------- #


def _rollup_payload(
    *,
    head: str = "abc123",
    committed: str | None = "2026-09-08T21:27:42Z",
    pushed: str | None = None,
    updated: str | None = "2026-09-08T21:33:42Z",
    contexts: list[dict[str, Any]] | None = None,
    has_next_page: bool = False,
) -> dict[str, Any]:
    """A GraphQL response shaped exactly like the one measured against #236 on 2026-09-09."""
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "headRefOid": head,
                    "updatedAt": updated,
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "committedDate": committed,
                                    "pushedDate": pushed,
                                    "statusCheckRollup": {
                                        "contexts": {
                                            "pageInfo": {"hasNextPage": has_next_page},
                                            "nodes": contexts if contexts else [],
                                        }
                                    },
                                }
                            }
                        ]
                    },
                }
            }
        }
    }


def _rollup_client(payload: Any, *, status: int = 200) -> GitHubClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/graphql")
        if status != 200:
            return httpx.Response(status, json={"message": "nope"})
        return httpx.Response(200, json=payload)

    return _client(handler)


@pytest.mark.anyio
async def test_fetch_check_rollup_maps_checkrun_rows() -> None:
    payload = _rollup_payload(
        contexts=[
            {
                "__typename": "CheckRun",
                "name": "test",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-09-08T21:28:18Z",
            },
            {
                "__typename": "CheckRun",
                "name": "queued one",
                "status": "QUEUED",
                "conclusion": None,
                "startedAt": None,
            },
        ]
    )
    async with _rollup_client(payload) as client:
        rollup = await client.fetch_check_rollup(_PR)
    assert rollup is not None
    assert rollup.head_sha == "abc123"
    # Lower-cased so gate_admission's COMPLETED / RED_CONCLUSIONS comparisons (all lower-case)
    # can match at all; a SCREAMING_CASE status would read as "not concluded" forever.
    assert [(r.name, r.status, r.conclusion) for r in rollup.rows] == [
        ("test", "completed", "success"),
        ("queued one", "queued", None),
    ]
    # CheckRun has no createdAt in the GraphQL schema; a queued run contributes no clock, which
    # is the exact rollup shape ci_clock_start's commit-clock fallback exists for.
    assert rollup.rows[1].started_at is None and rollup.rows[1].created_at is None


@pytest.mark.anyio
async def test_fetch_check_rollup_projects_statuscontext_onto_the_checkrow_pair() -> None:
    # StatusContext (legacy commit statuses) has only a ``state``. CheckRow is defined in terms
    # of a status/conclusion pair, so the projection is what lets one row type cover both union
    # members — without it a legacy status would be unclassifiable and drop out of the rollup.
    payload = _rollup_payload(
        contexts=[
            {
                "__typename": "StatusContext",
                "context": "legacy/build",
                "state": "FAILURE",
                "createdAt": "2026-09-08T21:28:00Z",
            },
            {
                "__typename": "StatusContext",
                "context": "legacy/pending",
                "state": "EXPECTED",
                "createdAt": "2026-09-08T21:28:01Z",
            },
        ]
    )
    async with _rollup_client(payload) as client:
        rollup = await client.fetch_check_rollup(_PR)
    assert rollup is not None
    assert [(r.name, r.status, r.conclusion) for r in rollup.rows] == [
        ("legacy/build", "completed", "failure"),
        ("legacy/pending", "pending", None),
    ]


@pytest.mark.anyio
async def test_fetch_check_rollup_drops_an_unknown_union_member() -> None:
    # Dropped rather than guessed, and the direction matters: a dropped row cannot hold
    # _concluded false (it could only have delayed the gate) and cannot make _red true (it
    # could only have withheld an INVOKE). A row of unknown shape must never be able to invent
    # a red CI and route an implementer at it.
    payload = _rollup_payload(
        contexts=[
            {"__typename": "SomethingNew", "name": "?", "status": "COMPLETED"},
            {
                "__typename": "StatusContext",
                "context": "x",
                "state": "WHAT",  # unknown state on a known member: also dropped
                "createdAt": None,
            },
            {
                "__typename": "CheckRun",
                "name": "real",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "startedAt": "2026-09-08T21:28:18Z",
            },
        ]
    )
    async with _rollup_client(payload) as client:
        rollup = await client.fetch_check_rollup(_PR)
    assert rollup is not None
    assert [r.name for r in rollup.rows] == ["real"]


@pytest.mark.anyio
async def test_fetch_check_rollup_falls_back_to_updated_at_when_pusheddate_is_null() -> None:
    # Measured 2026-09-09: GitHub returns pushedDate=null. The fallback is what actually
    # decides R1a / R1b in production, so it is pinned rather than left implicit.
    async with _rollup_client(_rollup_payload(pushed=None)) as client:
        rollup = await client.fetch_check_rollup(_PR)
    assert rollup is not None
    assert rollup.head_pushed_at.isoformat() == "2026-09-08T21:33:42+00:00"
    assert rollup.head_committed_date.isoformat() == "2026-09-08T21:27:42+00:00"


@pytest.mark.anyio
async def test_fetch_check_rollup_prefers_pusheddate_when_present() -> None:
    async with _rollup_client(_rollup_payload(pushed="2026-09-08T21:28:10Z")) as client:
        rollup = await client.fetch_check_rollup(_PR)
    assert rollup is not None
    assert rollup.head_pushed_at.isoformat() == "2026-09-08T21:28:10+00:00"


@pytest.mark.anyio
async def test_fetch_check_rollup_returns_none_on_a_partial_errors_response() -> None:
    # THE case this method's error policy exists for. _fetch_ci_status_graphql omits
    # ``contexts`` because asking for them returns partial data plus a FORBIDDEN error entry on
    # exactly the private repos that fallback exists for. This method must ask for them, so it
    # refuses a half-error response instead of parsing it: returning the partial rollup would
    # hand gate_admission a rollup that is short some checks and let it conclude "concluded".
    payload = _rollup_payload(contexts=[])
    payload["errors"] = [{"type": "FORBIDDEN", "message": "Resource not accessible"}]
    async with _rollup_client(payload) as client:
        assert await client.fetch_check_rollup(_PR) is None


@pytest.mark.anyio
async def test_fetch_check_rollup_returns_none_on_http_error() -> None:
    async with _rollup_client(None, status=403) as client:
        assert await client.fetch_check_rollup(_PR) is None


@pytest.mark.anyio
async def test_fetch_check_rollup_returns_none_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with _client(handler) as client:
        assert await client.fetch_check_rollup(_PR) is None


@pytest.mark.anyio
async def test_fetch_check_rollup_returns_none_when_a_clock_is_missing() -> None:
    # committedDate is ci_clock_start's last resort and exists on every commit; if it did not
    # parse, admission's input would be incomplete. Refusing (None) sends the caller back to
    # its pre-wiring behaviour rather than letting it judge on a clock it never read.
    async with _rollup_client(_rollup_payload(committed=None)) as client:
        assert await client.fetch_check_rollup(_PR) is None
    async with _rollup_client(_rollup_payload(pushed=None, updated=None)) as client:
        assert await client.fetch_check_rollup(_PR) is None


@pytest.mark.anyio
async def test_fetch_check_rollup_distinguishes_measured_empty_from_unread() -> None:
    # The distinction the CheckRollup type exists to carry: a PR with a null statusCheckRollup
    # is a MEASURED empty (rows == ()), which R1a / R1b are entitled to rule on. Only a failed
    # read is None. Conflating them would let "GitHub was down" be judged as "no CI configured".
    payload = _rollup_payload()
    payload["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
        "statusCheckRollup"
    ] = None
    async with _rollup_client(payload) as client:
        rollup = await client.fetch_check_rollup(_PR)
    assert rollup is not None and rollup.rows == ()


@pytest.mark.anyio
async def test_fetch_check_rollup_returns_none_when_contexts_are_truncated() -> None:
    # The failure this guard exists for is silent and looks like success: past the 100-context
    # window GitHub drops the rest, so a build whose 101st check is still queued (or red) arrives
    # as 100 rows that are all completed and all green. gate_admission would read _concluded=True
    # and _red=False off that and fire R7 "CI green: fresh gate" on a build it never saw the end
    # of. So the payload below is deliberately *perfect* — every present row concluded+success —
    # and the only thing wrong with it is the cursor.
    green = [
        {
            "__typename": "CheckRun",
            "name": f"shard-{i}",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-09-08T21:28:18Z",
        }
        for i in range(100)
    ]
    async with _rollup_client(_rollup_payload(contexts=green, has_next_page=True)) as client:
        assert await client.fetch_check_rollup(_PR) is None

    # Negative control: the identical 100 rows with the cursor down are read, not refused. This
    # is what pins the refusal to hasNextPage rather than to "many rows" or "a full window".
    async with _rollup_client(_rollup_payload(contexts=green, has_next_page=False)) as client:
        rollup = await client.fetch_check_rollup(_PR)
    assert rollup is not None
    assert len(rollup.rows) == 100
    assert all(r.status == "completed" and r.conclusion == "success" for r in rollup.rows)


@pytest.mark.anyio
async def test_fetch_check_rollup_query_asks_for_the_page_cursor() -> None:
    # Byte-form pin, same reason as the ci-route marker's: the truncation guard above is only
    # reachable if the request actually selects pageInfo. A payload fixture cannot notice that
    # the real query stopped asking — the field would simply be absent, ``page`` would be None,
    # and the guard would go quiet while still passing every test that feeds it a fixture.
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["query"] = json.loads(request.content)["query"]
        return httpx.Response(200, json=_rollup_payload())

    async with _client(handler) as client:
        assert await client.fetch_check_rollup(_PR) is not None
    # Pinned adjacent to first:100 on purpose: the window and the cursor that detects its
    # overflow are one decision, and a future edit that changes the window must meet this line.
    assert "contexts(first:100){pageInfo{hasNextPage} nodes{" in seen["query"]


# ---------- B-(a) cross-PR head-bound APPROVE resolution ------------------ #
#
# The stacked-PR scenario (msg-456 §R-B / msg-475 §5): PR #21 base=main pulled in PR #19's
# four commits. This lookup resolves *which* commits in ``pr`` already carry a head-bound
# APPROVE on ANOTHER PR — the archive-side accountability marker (msg-473 §3, msg-473 §5).


def _coverage_handler(
    *,
    # Body OR status code.
    pr_commits: list[dict[str, Any]] | int | None = None,
    # sha -> body OR status code.
    commit_pulls: dict[str, list[dict[str, Any]] | int] | None = None,
    # pr_number -> body OR status code.
    reviews: dict[int, list[dict[str, Any]] | int] | None = None,
    calls: list[str] | None = None,
) -> Any:
    """Handler emitting the three endpoints B-(a) resolution reads:

    * ``GET /repos/{o}/{r}/pulls/{n}/commits``
    * ``GET /repos/{o}/{r}/commits/{sha}/pulls``
    * ``GET /repos/{o}/{r}/pulls/{k}/reviews`` (the existing endpoint fetch_pr_reviews reads)

    An ``int`` value stands for a non-2xx status code (fail-soft branch); a list/dict
    value is the JSON body of a 200 response.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if path.endswith("/pulls/42/commits"):
            if isinstance(pr_commits, int):
                return httpx.Response(pr_commits)
            return httpx.Response(200, json=pr_commits or [])
        if "/commits/" in path and path.endswith("/pulls"):
            sha = path.split("/commits/")[1].split("/pulls")[0]
            entry = (commit_pulls or {}).get(sha, [])
            if isinstance(entry, int):
                return httpx.Response(entry)
            return httpx.Response(200, json=entry)
        if path.endswith("/reviews"):
            # /repos/{o}/{r}/pulls/{k}/reviews
            k = int(path.rsplit("/", 2)[1])
            entry = (reviews or {}).get(k, [])
            if isinstance(entry, int):
                return httpx.Response(entry)
            return httpx.Response(200, json=entry)
        return httpx.Response(404)

    return handle


def _pull_row(number: int, *, owner: str, repo: str) -> dict[str, Any]:
    """A minimal ``commits/{sha}/pulls`` row — number + base.repo.name + base.repo.owner.login.

    Extracted so the test data fits inside the 100-char line budget: the flat literal was
    reading as noise across every case and the shape it names is the same for every row.
    """
    return {
        "number": number,
        "base": {"repo": {"name": repo, "owner": {"login": owner}}},
    }


@pytest.mark.anyio
async def test_find_cross_pr_head_bound_approves_identifies_stacked_parent() -> None:
    """Msg-456 §R-B scenario: PR #42 contains commit ``sha1`` which was APPROVE'd on PR #19.

    The returned coverage names the covering PR and the APPROVE's ``commit_id``, wearing
    the same shape the marker renderer consumes.
    """
    coverage_pr = PrRef("SpirrowGames", "spirrow-conclair", 19)
    handler = _coverage_handler(
        pr_commits=[{"sha": "sha1"}, {"sha": "sha2"}],
        commit_pulls={
            "sha1": [_pull_row(19, owner="SpirrowGames", repo="spirrow-conclair")],
            "sha2": [],  # only sha1 has a covering PR
        },
        reviews={
            19: [
                {
                    "user": {"login": "spirrowgames-ops"},
                    "state": "APPROVED",
                    "commit_id": "sha1",
                    "submitted_at": "2026-09-07T04:51:05Z",
                }
            ]
        },
    )
    async with _client(handler) as client:
        result = await client.find_cross_pr_head_bound_approves(
            _PR, reviewer_login="spirrowgames-ops"
        )
    assert result == [
        CrossPrApproveCoverage(sha="sha1", other_pr=coverage_pr, approved_at="2026-09-07T04:51:05Z")
    ]


@pytest.mark.anyio
async def test_find_cross_pr_head_bound_approves_ignores_non_head_bound_approve() -> None:
    """An APPROVE whose ``commit_id`` differs from ``sha`` is NOT head-bound → excluded.

    Msg-456 §R-B is explicit: only ``commit_id=<sha>`` counts. Approving a different head
    of the same PR means the bytes now in this diff were not the ones that received the
    verdict.
    """
    handler = _coverage_handler(
        pr_commits=[{"sha": "sha1"}],
        commit_pulls={"sha1": [_pull_row(19, owner="SpirrowGames", repo="spirrow-conclair")]},
        reviews={
            19: [
                {
                    "user": {"login": "spirrowgames-ops"},
                    "state": "APPROVED",
                    "commit_id": "OTHER_HEAD",  # not sha1 → not head-bound to this commit
                    "submitted_at": "2026-09-07T04:51:05Z",
                }
            ]
        },
    )
    async with _client(handler) as client:
        result = await client.find_cross_pr_head_bound_approves(
            _PR, reviewer_login="spirrowgames-ops"
        )
    assert result == []


@pytest.mark.anyio
async def test_find_cross_pr_head_bound_approves_ignores_wrong_reviewer() -> None:
    """A commit APPROVE'd by a NON-naysayer reviewer (e.g. Copilot) does not cover it.

    Only the caller-named ``reviewer_login`` counts — the debounce, the round-cap, and
    B-(a) share one identity source (msg-478 §3, msg-475 §5).
    """
    handler = _coverage_handler(
        pr_commits=[{"sha": "sha1"}],
        commit_pulls={"sha1": [_pull_row(19, owner="SpirrowGames", repo="spirrow-conclair")]},
        reviews={
            19: [
                {
                    "user": {"login": "copilot-pull-request-reviewer[bot]"},
                    "state": "APPROVED",
                    "commit_id": "sha1",
                    "submitted_at": "2026-09-07T04:51:05Z",
                }
            ]
        },
    )
    async with _client(handler) as client:
        result = await client.find_cross_pr_head_bound_approves(
            _PR, reviewer_login="spirrowgames-ops"
        )
    assert result == []


@pytest.mark.anyio
async def test_find_cross_pr_head_bound_approves_excludes_same_pr() -> None:
    """A commit's APPROVE on the SAME PR (the reviewed one) is not cross-PR coverage.

    Same-PR references are structurally filtered — the marker is about ANOTHER PR's
    prior verdict. GitHub's ``commits/{sha}/pulls`` returns the reviewed PR itself when
    the sha is one of its commits, so the filter is load-bearing.
    """
    handler = _coverage_handler(
        pr_commits=[{"sha": "sha1"}],
        commit_pulls={
            "sha1": [
                # same PR number and owner/repo as _PR → structurally filtered out.
                _pull_row(42, owner="spirrowgames", repo="spirrow-mindwire")
            ]
        },
        reviews={
            42: [
                {
                    "user": {"login": "spirrowgames-ops"},
                    "state": "APPROVED",
                    "commit_id": "sha1",
                    "submitted_at": "2026-09-07T04:51:05Z",
                }
            ]
        },
    )
    async with _client(handler) as client:
        result = await client.find_cross_pr_head_bound_approves(
            _PR, reviewer_login="spirrowgames-ops"
        )
    assert result == []


@pytest.mark.anyio
async def test_find_cross_pr_head_bound_approves_fail_soft_on_commits_error() -> None:
    """``pulls/{n}/commits`` failure → empty result (fail-soft, msg-473 §5 fail-open).

    The driver's outer catch is not the whole story: each underlying read is fail-soft
    on its own too so a partial failure produces "coverage=[]" rather than a raise. The
    outer catch backstops any surprise inside; this test pins the inner path.
    """
    handler = _coverage_handler(pr_commits=500)
    async with _client(handler) as client:
        result = await client.find_cross_pr_head_bound_approves(
            _PR, reviewer_login="spirrowgames-ops"
        )
    assert result == []


@pytest.mark.anyio
async def test_find_cross_pr_head_bound_approves_deduplicates_repeated_sha() -> None:
    """The same ``(sha, other_pr)`` pair returned twice by GitHub → returned once.

    A commit can appear in the PR's commit list under an unusual merge topology; the
    same other PR can carry two APPROVE reviews against the same head (debounce reuse).
    The marker lists the FACT of coverage, not every re-review.
    """
    handler = _coverage_handler(
        pr_commits=[{"sha": "sha1"}, {"sha": "sha1"}],  # sha1 appears twice
        commit_pulls={"sha1": [_pull_row(19, owner="SpirrowGames", repo="spirrow-conclair")]},
        reviews={
            19: [
                {
                    "user": {"login": "spirrowgames-ops"},
                    "state": "APPROVED",
                    "commit_id": "sha1",
                    "submitted_at": "2026-09-07T04:51:05Z",
                }
            ]
        },
    )
    async with _client(handler) as client:
        result = await client.find_cross_pr_head_bound_approves(
            _PR, reviewer_login="spirrowgames-ops"
        )
    assert len(result) == 1
    assert result[0].sha == "sha1"


@pytest.mark.anyio
async def test_find_cross_pr_head_bound_approves_dedup_across_forks() -> None:
    """PR-gate on #245 edge-case: dedup key must include the OWNER/REPO, not just the number.

    GitHub's ``commits/{sha}/pulls`` returns cross-fork associations, so a commit can be
    named by upstream PR #19 AND fork-PR #19 (different owner/repo, same number). The
    prior implementation keyed on ``(sha, number)`` alone and collided those two into
    one row, losing the fork's coverage. The key is now the full :class:`PrRef` so both
    rows survive.
    """
    upstream_pr = _pull_row(19, owner="SpirrowGames", repo="spirrow-conclair")
    fork_pr = _pull_row(19, owner="AnotherOrg", repo="spirrow-conclair-fork")
    handler = _coverage_handler(
        pr_commits=[{"sha": "sha1"}],
        commit_pulls={"sha1": [upstream_pr, fork_pr]},
        reviews={
            # Both PR #19s carry a head-bound APPROVE on the same sha (implausible in
            # practice for cross-fork same numbers, but the point is that the DEDUP KEY
            # must not treat them as identical — the marker must show both rows).
            19: [
                {
                    "user": {"login": "spirrowgames-ops"},
                    "state": "APPROVED",
                    "commit_id": "sha1",
                    "submitted_at": "2026-09-07T04:51:05Z",
                }
            ],
        },
    )
    async with _client(handler) as client:
        result = await client.find_cross_pr_head_bound_approves(
            _PR, reviewer_login="spirrowgames-ops"
        )
    # Both fork associations survive dedup — a number-only key would have collapsed them
    # into one row and this assertion would fail.
    assert len(result) == 2
    slugs = {c.other_pr.slug for c in result}
    assert slugs == {
        "SpirrowGames/spirrow-conclair#19",
        "AnotherOrg/spirrow-conclair-fork#19",
    }


@pytest.mark.anyio
async def test_find_cross_pr_head_bound_approves_never_raises_on_dependency_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-gate on #245 correctness: the "does NOT raise" docstring promise is STRUCTURAL.

    Every underlying read is documented fail-soft today, but the outer method carries an
    ``except Exception`` belt so the guarantee holds even if a dependency later regresses
    to raising. We simulate that regression by monkey-patching ``fetch_pr_reviews`` to
    raise ``GitHubHTTPError`` and asserting the method returns ``[]`` rather than
    propagating.

    Note (T2 §5 ③): the inner ``except`` added for the review-fetch cache also swallows
    the raise, but it re-``continue``s rather than returning; on a one-commit input the
    loop still exits cleanly with ``coverage == []``, so the fail-open contract is
    preserved either way. The many-commit / repeated-exception variant is covered by
    ``test_find_cross_pr_head_bound_approves_does_not_cache_exception``.
    """
    handler = _coverage_handler(
        pr_commits=[{"sha": "sha1"}],
        commit_pulls={"sha1": [_pull_row(19, owner="SpirrowGames", repo="spirrow-conclair")]},
        reviews={},  # irrelevant — we override fetch_pr_reviews below
    )

    async def _raising_reviews(pr: PrRef) -> list[Any]:
        raise GitHubHTTPError("simulated regression: private cross-fork", status_code=403)

    async with _client(handler) as client:
        monkeypatch.setattr(client, "fetch_pr_reviews", _raising_reviews)
        # Must not raise. Must return [] (the fail-open marker-less state).
        result = await client.find_cross_pr_head_bound_approves(
            _PR, reviewer_login="spirrowgames-ops"
        )
    assert result == []


@pytest.mark.anyio
async def test_find_cross_pr_head_bound_approves_caches_reviews_per_invocation() -> None:
    """T2 §5 acceptance ② — the same ``other_pr`` triggers ``fetch_pr_reviews`` ONCE.

    In the stacked-PR shape B-(a) primarily targets (msg-456 §R-B: PR #21 pulling in
    PR #19's 4 commits), every commit in the reviewed PR names the same parent PR. The
    old code re-fetched the parent's review list once per commit — an N+1 pattern the
    naysayer flagged as advisory on msg-680 and re-flagged as an objection on
    msg-692. The per-invocation cache elides those repeat calls without touching
    ``seen`` (which dedupes the OUTPUT rows on ``(sha, other_pr)``, a key that varies
    with ``sha`` and so cannot elide fetches on its own).
    """
    calls: list[str] = []
    handler = _coverage_handler(
        pr_commits=[{"sha": "sha1"}, {"sha": "sha2"}, {"sha": "sha3"}],
        commit_pulls={
            "sha1": [_pull_row(19, owner="SpirrowGames", repo="spirrow-conclair")],
            "sha2": [_pull_row(19, owner="SpirrowGames", repo="spirrow-conclair")],
            "sha3": [_pull_row(19, owner="SpirrowGames", repo="spirrow-conclair")],
        },
        reviews={
            # Only sha1 was APPROVE'd head-bound; the point of the test is the CALL count,
            # not the marker rows. A broken cache would still fetch three times and could
            # coincidentally return the same marker, so the assertion below is on ``calls``.
            19: [
                {
                    "user": {"login": "spirrowgames-ops"},
                    "state": "APPROVED",
                    "commit_id": "sha1",
                    "submitted_at": "2026-09-07T04:51:05Z",
                }
            ],
        },
        calls=calls,
    )
    async with _client(handler) as client:
        result = await client.find_cross_pr_head_bound_approves(
            _PR, reviewer_login="spirrowgames-ops"
        )
    # Marker output is unchanged (§5 ①): sha1 covered, sha2/sha3 not.
    assert result == [
        CrossPrApproveCoverage(
            sha="sha1",
            other_pr=PrRef("SpirrowGames", "spirrow-conclair", 19),
            approved_at="2026-09-07T04:51:05Z",
        )
    ]
    # §5 ② — exactly ONE fetch of PR #19's reviews across the three commits, not three.
    review_calls = [c for c in calls if c.endswith("/pulls/19/reviews")]
    assert len(review_calls) == 1, calls


@pytest.mark.anyio
async def test_find_cross_pr_head_bound_approves_does_not_cache_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2 §5 acceptance ③④ — a raised fetch is NOT cached; the next commit retries.

    Einstein msg-695 blocked T2 on this rule: a naive cache that stored "failure"
    would let one transient 502 silently drop the marker for every remaining commit
    that references the same parent PR. The cache holds only successful returns;
    the exception path escapes to a ``continue`` above the cache write, so a
    subsequent commit against the SAME ``other_pr`` re-invokes the fetch. The
    invariant this test pins: 1st call raises → 2nd call succeeds → marker appears.

    ``fetch_pr_reviews`` is documented fail-soft in the current code (``[]`` on any
    HTTP failure), so a real 502 today would not reach this ``except``; the raise
    here simulates the future regression msg-696 §1 named (the "structural belt"
    the outer ``except Exception`` already exists to catch). The point of §5 ③④ is
    that even when that belt IS reached, one PR-scoped failure must not stick to
    every subsequent commit for the same parent.
    """
    call_count = {"n": 0}
    good_reviews = [
        ReviewInfo(
            login="spirrowgames-ops",
            state="APPROVED",
            commit_id="sha2",
            submitted_at="2026-09-07T04:51:05Z",
        )
    ]

    async def _flaky_reviews(pr: PrRef) -> list[ReviewInfo]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First look-up of PR #19's reviews raises; a negative cache would trap
            # this and mask every downstream commit that names the same parent.
            raise GitHubHTTPError("simulated transient outage", status_code=502)
        return good_reviews

    handler = _coverage_handler(
        pr_commits=[{"sha": "sha1"}, {"sha": "sha2"}],
        commit_pulls={
            # Both commits point to the SAME parent PR #19 — the shape that would
            # let a naive cache poison sha2 based on sha1's transient failure.
            "sha1": [_pull_row(19, owner="SpirrowGames", repo="spirrow-conclair")],
            "sha2": [_pull_row(19, owner="SpirrowGames", repo="spirrow-conclair")],
        },
        reviews={},  # irrelevant — the stub above shadows this
    )

    async with _client(handler) as client:
        monkeypatch.setattr(client, "fetch_pr_reviews", _flaky_reviews)
        result = await client.find_cross_pr_head_bound_approves(
            _PR, reviewer_login="spirrowgames-ops"
        )

    # The second call was actually made (retry, not a cache hit on failure).
    assert call_count["n"] == 2, "cache trapped the exception and skipped the retry"
    # And with the retry succeeding, sha2's marker landed — no silent drop.
    assert result == [
        CrossPrApproveCoverage(
            sha="sha2",
            other_pr=PrRef("SpirrowGames", "spirrow-conclair", 19),
            approved_at="2026-09-07T04:51:05Z",
        )
    ]
