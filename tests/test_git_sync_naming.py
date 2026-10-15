"""git-sync naming layer (ADR-2026-10-09-23 unit 1, D8; design v1.2 — Bohr msg-6766).

The pure pieces of the git-sync hook, testable without git: the mechanical branch name, the
push-target guard on :class:`ThreadBranch`, the generation search over merged PRs (with a fake
PR listing) and the GitHub client's head-filtered listing (``httpx.MockTransport``). Split out of
``tests/test_git_sync.py`` so the naming layer lands as its own PR (PR #473 size split).
"""

from __future__ import annotations

import re

import httpx
import pytest

from spirrow_mindwire.git_sync import (
    MAX_GENERATION,
    GitParkCode,
    GitSyncDeferredError,
    GitSyncParkError,
    ProtectedBranchError,
    ThreadBranch,
    generation_name,
    normalize_stem,
    resolve_generation,
)
from spirrow_mindwire.github.client import (
    GitHubClient,
    GitHubHTTPError,
    PrHeadListingShapeError,
    PrHeadRow,
)


class _FakeGitHub:
    def __init__(self, answers: dict[str, list[PrHeadRow]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, str, str]] = []
        self.closed = 0

    async def list_prs_by_head(self, owner: str, repo: str, branch: str) -> list[PrHeadRow]:
        self.calls.append((owner, repo, branch))
        return self.answers.get(branch, [])

    async def aclose(self) -> None:
        self.closed += 1


async def _no_sleep(_s: float) -> None:
    return None


def _merged(n: int = 1) -> PrHeadRow:
    return PrHeadRow(number=n, state="closed", merged_at="2026-10-01T00:00:00Z")


# ===================================================================== names (D8)


@pytest.mark.parametrize(
    ("thread_id", "stem"),
    [
        ("T-executor-pool-under-one-conductor", "t-executor-pool-under-one-conductor"),
        ("T-Foo  Bar//baz", "t-foo-bar-baz"),
        ("--T.x--", "t.x"),
        ("T_日本語_x", "t_-_x"),
        ("...", ""),
    ],
)
def test_normalize_stem(thread_id: str, stem: str) -> None:
    assert normalize_stem(thread_id) == stem


def test_long_stem_is_cut_and_hashed() -> None:
    long_id = "T-" + "a" * 100
    stem = normalize_stem(long_id)
    assert len(stem) == 55 + 1 + 8
    assert stem.startswith("t-" + "a" * 53 + "-")
    assert re.fullmatch(r"[0-9a-f]{8}", stem[-8:])
    # Two ids that share the 55-char prefix still get different names.
    assert normalize_stem(long_id) != normalize_stem(long_id + "b")
    assert normalize_stem("T-" + "a" * 62) == "t-" + "a" * 62  # 64 exactly: untouched


def test_generation_name() -> None:
    assert generation_name("x", 1) == "loop/x"
    assert generation_name("x", 2) == "loop/x-2"
    with pytest.raises(ValueError):
        generation_name("x", 0)


# ===================================================================== push guard (B-5)


@pytest.mark.parametrize(
    "name", ["main", "develop", "master", "release/1.0", "production", "gh-pages", "trunk"]
)
def test_thread_branch_refuses_protected_names_even_as_override(name: str) -> None:
    with pytest.raises(ProtectedBranchError):
        ThreadBranch(name=name, default_branch="trunk", overridden=True)


def test_thread_branch_outside_loop_needs_override() -> None:
    with pytest.raises(ProtectedBranchError):
        ThreadBranch(name="feature/x", default_branch="main")
    assert ThreadBranch(name="feature/x", default_branch="main", overridden=True).name


def test_thread_branch_refspec_is_explicit() -> None:
    b = ThreadBranch(name="loop/t-x", default_branch="main")
    assert b.refspec == "refs/heads/loop/t-x:refs/heads/loop/t-x"


# ===================================================================== generation search (B-2)


@pytest.mark.anyio
async def test_no_merged_pr_settles_on_the_first_query() -> None:
    gh = _FakeGitHub()
    name = await resolve_generation(
        "T-x", "t-x", lambda b: gh.list_prs_by_head("o", "r", b), sleep=_no_sleep
    )
    assert name == "loop/t-x"
    assert [c[2] for c in gh.calls] == ["loop/t-x"]


@pytest.mark.anyio
async def test_merged_generation_moves_to_the_next() -> None:
    gh = _FakeGitHub({"loop/t-x": [_merged()]})
    name = await resolve_generation(
        "T-x", "t-x", lambda b: gh.list_prs_by_head("o", "r", b), sleep=_no_sleep
    )
    assert name == "loop/t-x-2"
    assert [c[2] for c in gh.calls] == ["loop/t-x", "loop/t-x-2"]


@pytest.mark.anyio
async def test_closed_unmerged_or_open_pr_reuses_the_name() -> None:
    gh = _FakeGitHub(
        {
            "loop/t-x": [
                PrHeadRow(number=1, state="closed", merged_at=None),
                PrHeadRow(number=2, state="open", merged_at=None),
            ]
        }
    )
    name = await resolve_generation(
        "T-x", "t-x", lambda b: gh.list_prs_by_head("o", "r", b), sleep=_no_sleep
    )
    assert name == "loop/t-x"


@pytest.mark.anyio
async def test_generation_limit_parks() -> None:
    async def always_merged(_b: str) -> list[PrHeadRow]:
        return [_merged()]

    with pytest.raises(GitSyncParkError) as ei:
        await resolve_generation("T-x", "t-x", always_merged, sleep=_no_sleep)
    assert ei.value.code is GitParkCode.BRANCH_RESOLVE_FAILED
    assert "generation limit exceeded" in ei.value.summary
    assert MAX_GENERATION == 20


@pytest.mark.anyio
async def test_one_transient_failure_then_success() -> None:
    calls: list[str] = []
    slept: list[float] = []

    async def flaky(b: str) -> list[PrHeadRow]:
        calls.append(b)
        if len(calls) == 1:
            raise GitHubHTTPError("bad gateway", status_code=502)
        return []

    async def sleep(s: float) -> None:
        slept.append(s)

    assert await resolve_generation("T-x", "t-x", flaky, sleep=sleep) == "loop/t-x"
    assert len(calls) == 2 and len(slept) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "exc",
    [
        GitHubHTTPError("502", status_code=502),
        GitHubHTTPError("conn reset"),  # transport: no status
        GitHubHTTPError("429", status_code=429, retry_after=1.0, rate_limited=True),
        GitHubHTTPError("403 rl", status_code=403, rate_limited=True),
    ],
)
async def test_transient_failures_exhausted_defer_not_park(exc: GitHubHTTPError) -> None:
    async def down(_b: str) -> list[PrHeadRow]:
        raise exc

    with pytest.raises(GitSyncDeferredError) as ei:
        await resolve_generation("T-x", "t-x", down, sleep=_no_sleep)
    assert ei.value.candidate == "loop/t-x"


@pytest.mark.anyio
async def test_transient_failure_on_the_second_query_defers() -> None:
    calls: list[str] = []

    async def lister(b: str) -> list[PrHeadRow]:
        calls.append(b)
        if b == "loop/t-x":
            return [_merged()]
        raise GitHubHTTPError("503", status_code=503)

    with pytest.raises(GitSyncDeferredError) as ei:
        await resolve_generation("T-x", "t-x", lister, sleep=_no_sleep)
    assert ei.value.candidate == "loop/t-x-2"
    assert calls == ["loop/t-x"] + ["loop/t-x-2"] * 3  # 1 + RETRIES


@pytest.mark.anyio
@pytest.mark.parametrize(
    "exc",
    [
        GitHubHTTPError("401", status_code=401),
        GitHubHTTPError("403", status_code=403),
        GitHubHTTPError("404", status_code=404),
        PrHeadListingShapeError("not a list"),
    ],
)
async def test_permanent_failures_park(exc: Exception) -> None:
    calls = 0

    async def refused(_b: str) -> list[PrHeadRow]:
        nonlocal calls
        calls += 1
        raise exc

    with pytest.raises(GitSyncParkError) as ei:
        await resolve_generation("T-x", "t-x", refused, sleep=_no_sleep)
    assert ei.value.code is GitParkCode.BRANCH_RESOLVE_FAILED
    assert calls == 1  # no retry on a permanent failure
    if isinstance(exc, GitHubHTTPError):
        assert f"HTTP {exc.status_code}" in ei.value.summary


# ===================================================================== client request shape


@pytest.mark.anyio
async def test_list_prs_by_head_sends_an_exact_owner_qualified_head() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            json=[
                {"number": 7, "state": "closed", "merged_at": "2026-10-01T00:00:00Z"},
                {"number": 8, "state": "open", "merged_at": None},
            ],
        )

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    try:
        rows = await client.list_prs_by_head("acme", "widgets", "loop/t-x-2")
    finally:
        await client.aclose()
    assert rows == [
        PrHeadRow(number=7, state="closed", merged_at="2026-10-01T00:00:00Z"),
        PrHeadRow(number=8, state="open", merged_at=None),
    ]
    (req,) = seen
    assert req.url.path == "/repos/acme/widgets/pulls"
    assert req.url.params["head"] == "acme:loop/t-x-2"
    assert req.url.params["state"] == "all"
    assert req.url.params["per_page"] == "100"


@pytest.mark.anyio
async def test_list_prs_by_head_errors_carry_status_and_rate_limit_hints() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={"message": "rl"})

    client = GitHubClient(token="t", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GitHubHTTPError) as ei:
            await client.list_prs_by_head("acme", "widgets", "loop/t-x")
    finally:
        await client.aclose()
    assert ei.value.status_code == 403 and ei.value.rate_limited


@pytest.mark.anyio
@pytest.mark.parametrize("body", [{"not": "a list"}, [{"number": "7", "state": "open"}], ["x"]])
async def test_list_prs_by_head_shape_errors(body: object) -> None:
    client = GitHubClient(
        token="t", transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
    )
    try:
        with pytest.raises(PrHeadListingShapeError):
            await client.list_prs_by_head("acme", "widgets", "loop/t-x")
    finally:
        await client.aclose()
