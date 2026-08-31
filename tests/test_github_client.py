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
