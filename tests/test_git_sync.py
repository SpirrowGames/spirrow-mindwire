"""git-sync hook (ADR-2026-10-09-23 unit 1, design v1.2 — Bohr msg-6766, go Einstein msg-6765).

Real git repositories (a bare "origin" + a clone) for every branch state git can produce; a fake
PR listing for the generation search; ``httpx.MockTransport`` for the client's request shape.
The clone's ``remote.origin.url`` is a GitHub URL rewritten to the local bare repo by
``url.<path>.insteadOf``, so the hook parses a real owner / repo while git talks to the disk.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from spirrow_mindwire import loop_runner
from spirrow_mindwire.clone_guard import DIRTY_CLONE_EXIT_CODE
from spirrow_mindwire.conductor.stop_marker import parse_stop_marker
from spirrow_mindwire.config import MindwireSettings, load_settings
from spirrow_mindwire.dispatcher.core import Dispatcher
from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
from spirrow_mindwire.git_sync import (
    GIT_SYNC_DEFERRED_EXIT_CODE,
    GIT_SYNC_DEFERRED_PAYLOAD_PREFIX,
    MAX_GENERATION,
    GitParkCode,
    GitSyncDeferredError,
    GitSyncHook,
    GitSyncParkError,
    GitSyncPhase,
    ProtectedBranchError,
    ThreadBranch,
    generation_name,
    normalize_stem,
    parse_github_remote,
    resolve_generation,
)
from spirrow_mindwire.git_sync.notice import git_sync_park_event, render_git_sync_park_notice
from spirrow_mindwire.github.client import (
    GitHubClient,
    GitHubHTTPError,
    PrHeadListingShapeError,
    PrHeadRow,
)
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.thread_context import build_turn_prompt
from spirrow_mindwire.ulid_util import new_ulid
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    EventType,
    HealthStatus,
    NewMessagePayload,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TS = datetime(2026, 10, 9, tzinfo=UTC)
THREAD = "T-Executor_Pool unit1"
STEM = "t-executor_pool-unit1"
BRANCH = f"loop/{STEM}"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo: Path, name: str, text: str = "x\n") -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD").strip()


class Env:
    """A bare origin, a 'peer' clone that plays the other machine, and the loop's clone."""

    def __init__(self, tmp: Path) -> None:
        self.origin = tmp / "origin.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
            check=True,
            capture_output=True,
        )
        self.url = "https://github.com/acme/widgets.git"
        self.peer = self._clone(tmp / "peer", seed=True)
        self.clone = self._clone(tmp / "clone", seed=False)

    def _clone(self, path: Path, *, seed: bool) -> Path:
        path.mkdir()
        _git(path, "init", "-q", "-b", "main")
        _identity(path)
        _git(path, "config", f"url.{self.origin.as_posix()}.insteadOf", self.url)
        _git(path, "remote", "add", "origin", self.url)
        if seed:
            (path / ".gitignore").write_text(".mindwire/\n", encoding="utf-8")
            _git(path, "add", ".gitignore")
            _git(path, "commit", "-q", "-m", "init")
            _git(path, "push", "-q", "origin", "main")
        else:
            _git(path, "fetch", "-q", "origin")
            _git(path, "switch", "-q", "-c", "main", "--track", "origin/main")
            _git(path, "remote", "set-head", "origin", "main")
        return path

    def head(self) -> str:
        return _git(self.clone, "symbolic-ref", "--short", "HEAD").strip()

    def remote_sha(self, branch: str) -> str:
        out = _git(self.origin, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}")
        return out.strip()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


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


def _hook(env: Env, gh: _FakeGitHub | None = None, **kw: Any) -> tuple[GitSyncHook, _FakeGitHub]:
    fake = gh or _FakeGitHub()
    return GitSyncHook(env.clone, github_factory=lambda: fake, **kw), fake


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


def test_push_refuses_anything_but_a_thread_branch(env: Env) -> None:
    hook, _ = _hook(env)
    with pytest.raises(TypeError):
        hook.push("main")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        hook.push("develop")  # type: ignore[arg-type]


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


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/acme/widgets.git", ("acme", "widgets")),
        ("https://github.com/acme/widgets", ("acme", "widgets")),
        ("git@github.com:acme/widgets.git", ("acme", "widgets")),
        ("ssh://git@github.com/acme/widgets.git", ("acme", "widgets")),
        ("C:/repos/widgets.git", None),
    ],
)
def test_parse_github_remote(url: str, slug: tuple[str, str] | None) -> None:
    assert parse_github_remote(url) == slug


# ===================================================================== pre-turn (B-3), real git


@pytest.mark.anyio
async def test_new_branch_is_cut_from_the_remote_default(env: Env) -> None:
    hook, gh = _hook(env)
    branch = await hook.pre_turn(THREAD)
    assert branch.name == BRANCH
    assert env.head() == BRANCH
    assert _git(env.clone, "rev-parse", "HEAD") == _git(env.clone, "rev-parse", "origin/main")
    assert gh.calls == [("acme", "widgets", BRANCH)]
    assert gh.closed == 1


@pytest.mark.anyio
async def test_remote_only_branch_is_tracked(env: Env) -> None:
    _git(env.peer, "switch", "-q", "-c", BRANCH)
    sha = _commit(env.peer, "peer.txt")
    _git(env.peer, "push", "-q", "origin", BRANCH)
    hook, _ = _hook(env)
    await hook.pre_turn(THREAD)
    assert env.head() == BRANCH
    assert _git(env.clone, "rev-parse", "HEAD").strip() == sha


@pytest.mark.anyio
async def test_local_behind_remote_fast_forwards(env: Env) -> None:
    hook, _ = _hook(env)
    await hook.pre_turn(THREAD)  # creates the local branch at origin/main
    _git(env.clone, "switch", "-q", "main")
    _git(env.peer, "switch", "-q", "-c", BRANCH)
    sha = _commit(env.peer, "peer.txt")
    _git(env.peer, "push", "-q", "origin", BRANCH)
    await hook.pre_turn(THREAD)
    assert env.head() == BRANCH
    assert _git(env.clone, "rev-parse", "HEAD").strip() == sha


@pytest.mark.anyio
async def test_local_ahead_of_remote_parks_and_stays_on_default(env: Env) -> None:
    _git(env.peer, "switch", "-q", "-c", BRANCH)
    _commit(env.peer, "peer.txt")
    _git(env.peer, "push", "-q", "origin", BRANCH)
    _git(env.clone, "fetch", "-q", "origin")
    _git(env.clone, "branch", BRANCH, f"origin/{BRANCH}")
    _git(env.clone, "switch", "-q", BRANCH)
    _commit(env.clone, "local.txt")
    _git(env.clone, "switch", "-q", "main")
    hook, _ = _hook(env)
    with pytest.raises(GitSyncParkError) as ei:
        await hook.pre_turn(THREAD)
    assert ei.value.code is GitParkCode.LOCAL_AHEAD
    assert ei.value.phase is GitSyncPhase.PRE_TURN
    assert env.head() == "main"


@pytest.mark.anyio
async def test_diverged_parks(env: Env) -> None:
    _git(env.peer, "switch", "-q", "-c", BRANCH)
    _commit(env.peer, "peer.txt")
    _git(env.peer, "push", "-q", "origin", BRANCH)
    _git(env.clone, "switch", "-q", "-c", BRANCH)
    _commit(env.clone, "local.txt")
    _git(env.clone, "switch", "-q", "main")
    hook, _ = _hook(env)
    with pytest.raises(GitSyncParkError) as ei:
        await hook.pre_turn(THREAD)
    assert ei.value.code is GitParkCode.DIVERGED
    assert ei.value.reason_line.startswith(f"git.diverged: {THREAD} {BRANCH} ")
    assert env.head() == "main"


@pytest.mark.anyio
async def test_local_only_branch_with_unpushed_commits_parks(env: Env) -> None:
    _git(env.clone, "switch", "-q", "-c", BRANCH)
    _commit(env.clone, "local.txt")
    _git(env.clone, "switch", "-q", "main")
    hook, _ = _hook(env)
    with pytest.raises(GitSyncParkError) as ei:
        await hook.pre_turn(THREAD)
    assert ei.value.code is GitParkCode.LOCAL_AHEAD


@pytest.mark.anyio
async def test_fetch_failure_parks(env: Env, tmp_path: Path) -> None:
    gone = "https://github.com/acme/gone.git"
    _git(env.clone, "config", f"url.{(tmp_path / 'missing.git').as_posix()}.insteadOf", gone)
    _git(env.clone, "remote", "set-url", "origin", gone)
    hook, _ = _hook(env)
    with pytest.raises(GitSyncParkError) as ei:
        await hook.pre_turn(THREAD)
    assert ei.value.code is GitParkCode.FETCH_FAILED
    assert env.head() == "main"


@pytest.mark.anyio
async def test_override_skips_the_search_and_keeps_the_name(env: Env) -> None:
    hook, gh = _hook(env, branch_override={THREAD: "feat/whatever"})
    branch = await hook.pre_turn(THREAD)
    assert branch.name == "feat/whatever" and branch.overridden
    assert gh.calls == []
    assert env.head() == "feat/whatever"


@pytest.mark.anyio
async def test_override_naming_a_protected_branch_parks(env: Env) -> None:
    hook, _ = _hook(env, branch_override={THREAD: "develop"})
    with pytest.raises(GitSyncParkError) as ei:
        await hook.pre_turn(THREAD)
    assert ei.value.code is GitParkCode.BRANCH_RESOLVE_FAILED
    assert env.head() == "main"


@pytest.mark.anyio
async def test_ref_illegal_name_parks(env: Env) -> None:
    hook, _ = _hook(env)
    with pytest.raises(GitSyncParkError) as ei:
        await hook.pre_turn("...")  # stem "" → "loop/" is not a legal ref
    assert ei.value.code is GitParkCode.BRANCH_RESOLVE_FAILED
    assert "check-ref-format" in ei.value.summary


@pytest.mark.anyio
async def test_non_github_remote_parks(env: Env) -> None:
    _git(env.clone, "remote", "set-url", "origin", env.origin.as_posix())
    hook, gh = _hook(env)
    with pytest.raises(GitSyncParkError) as ei:
        await hook.pre_turn(THREAD)
    assert ei.value.code is GitParkCode.BRANCH_RESOLVE_FAILED
    assert gh.calls == []


@pytest.mark.anyio
async def test_merged_generation_cuts_the_next_name(env: Env) -> None:
    hook, gh = _hook(env, _FakeGitHub({BRANCH: [_merged()]}))
    branch = await hook.pre_turn(THREAD)
    assert branch.name == f"{BRANCH}-2"
    assert [c[2] for c in gh.calls] == [BRANCH, f"{BRANCH}-2"]
    assert env.head() == f"{BRANCH}-2"


# --------------------------------------------------------------------------- LFS (B-3 step 5)


def _lfs_available() -> bool:
    try:
        return (
            subprocess.run(["git", "lfs", "version"], capture_output=True, check=False).returncode
            == 0
        )
    except OSError:
        return False


@pytest.mark.anyio
@pytest.mark.skipif(not _lfs_available(), reason="git-lfs is not installed")
async def test_lfs_objects_only_on_the_thread_branch_are_materialised(env: Env) -> None:
    _git(env.peer, "lfs", "install", "--local")
    _git(env.peer, "switch", "-q", "-c", BRANCH)
    _git(env.peer, "lfs", "track", "*.bin")
    (env.peer / "big.bin").write_bytes(b"real lfs content\n" * 64)
    _git(env.peer, "add", ".gitattributes", "big.bin")
    _git(env.peer, "commit", "-q", "-m", "lfs")
    _git(env.peer, "push", "-q", "origin", BRANCH)
    # The loop's clone: LFS hooks installed but smudge skipped, so a checkout leaves pointers and
    # only the hook's explicit `git lfs pull` can materialise them.
    _git(env.clone, "lfs", "install", "--local", "--skip-smudge")
    hook, _ = _hook(env, lfs=True)
    await hook.pre_turn(THREAD)
    assert env.head() == BRANCH
    assert (env.clone / "big.bin").read_bytes() == b"real lfs content\n" * 64


@pytest.mark.anyio
@pytest.mark.skipif(not _lfs_available(), reason="git-lfs is not installed")
async def test_without_lfs_pull_the_same_file_stays_a_pointer(env: Env) -> None:
    # The control for the test above: same setup, ``lfs = false`` → still a pointer file, so the
    # test above passes only because of the hook's pull on the thread branch.
    _git(env.peer, "lfs", "install", "--local")
    _git(env.peer, "switch", "-q", "-c", BRANCH)
    _git(env.peer, "lfs", "track", "*.bin")
    (env.peer / "big.bin").write_bytes(b"real lfs content\n" * 64)
    _git(env.peer, "add", ".gitattributes", "big.bin")
    _git(env.peer, "commit", "-q", "-m", "lfs")
    _git(env.peer, "push", "-q", "origin", BRANCH)
    _git(env.clone, "lfs", "install", "--local", "--skip-smudge")
    hook, _ = _hook(env, lfs=False)
    await hook.pre_turn(THREAD)
    assert (env.clone / "big.bin").read_bytes().startswith(b"version https://git-lfs")


@pytest.mark.anyio
@pytest.mark.skipif(not _lfs_available(), reason="git-lfs is not installed")
async def test_lfs_pointer_left_behind_parks_and_returns_to_default(env: Env) -> None:
    calls: list[tuple[str, ...]] = []
    real = __import__("spirrow_mindwire.git_sync.hook", fromlist=["_subprocess_git"])

    def git(repo: Path, args: Sequence[str], timeout: float) -> Any:
        calls.append(tuple(args))
        if tuple(args[:2]) == ("lfs", "pull"):
            return real.GitResult(0, "", "")
        if tuple(args[:2]) == ("lfs", "ls-files"):
            return real.GitResult(0, "0123456789 - big.bin\nabcdef0123 * ok.bin\n", "")
        return real._subprocess_git(repo, args, timeout)

    hook = GitSyncHook(env.clone, lfs=True, github_factory=_FakeGitHub, git=git)
    with pytest.raises(GitSyncParkError) as ei:
        await hook.pre_turn(THREAD)
    assert ei.value.code is GitParkCode.LFS_FAILED
    assert "big.bin" in ei.value.summary and "ok.bin" not in ei.value.summary
    assert env.head() == "main"


# ===================================================================== post-turn (B-4)


async def _on_thread_branch(env: Env) -> tuple[GitSyncHook, ThreadBranch]:
    hook, _ = _hook(env)
    return hook, await hook.pre_turn(THREAD)


@pytest.mark.anyio
async def test_post_turn_pushes_new_commits_and_switches_back(env: Env) -> None:
    hook, branch = await _on_thread_branch(env)
    sha = _commit(env.clone, "work.txt")
    await hook.post_turn(THREAD, branch)
    assert env.remote_sha(BRANCH) == sha
    assert env.head() == "main"


@pytest.mark.anyio
async def test_post_turn_without_commits_pushes_nothing(env: Env) -> None:
    hook, branch = await _on_thread_branch(env)
    await hook.post_turn(THREAD, branch)
    assert env.remote_sha(BRANCH) == ""
    assert env.head() == "main"


@pytest.mark.anyio
async def test_post_turn_dirty_tree_parks_and_touches_nothing(env: Env) -> None:
    hook, branch = await _on_thread_branch(env)
    (env.clone / "wip.txt").write_text("wip\n", encoding="utf-8")
    with pytest.raises(GitSyncParkError) as ei:
        await hook.post_turn(THREAD, branch)
    assert ei.value.code is GitParkCode.DIRTY_AFTER_TURN
    assert ei.value.phase is GitSyncPhase.POST_TURN
    assert env.head() == BRANCH  # nothing tidied
    assert (env.clone / "wip.txt").exists()


@pytest.mark.anyio
async def test_post_turn_operation_in_progress_parks(env: Env) -> None:
    hook, branch = await _on_thread_branch(env)
    git_dir = Path(_git(env.clone, "rev-parse", "--absolute-git-dir").strip())
    (git_dir / "MERGE_HEAD").write_text(_git(env.clone, "rev-parse", "HEAD"), encoding="utf-8")
    with pytest.raises(GitSyncParkError) as ei:
        await hook.post_turn(THREAD, branch)
    assert ei.value.code is GitParkCode.DIRTY_AFTER_TURN
    assert "MERGE_HEAD" in ei.value.summary


@pytest.mark.anyio
async def test_post_turn_head_moved_parks(env: Env) -> None:
    hook, branch = await _on_thread_branch(env)
    _git(env.clone, "switch", "-q", "-c", "feature/elsewhere")
    with pytest.raises(GitSyncParkError) as ei:
        await hook.post_turn(THREAD, branch)
    assert ei.value.code is GitParkCode.HEAD_MOVED
    assert env.head() == "feature/elsewhere"


@pytest.mark.anyio
async def test_post_turn_push_refused_parks_and_returns_to_default(env: Env) -> None:
    hook, branch = await _on_thread_branch(env)
    _commit(env.clone, "work.txt")
    hooks = env.origin / "hooks"
    hooks.mkdir(exist_ok=True)
    pre_receive = hooks / "pre-receive"
    pre_receive.write_text("#!/bin/sh\necho refused >&2\nexit 1\n", encoding="utf-8")
    pre_receive.chmod(0o755)
    with pytest.raises(GitSyncParkError) as ei:
        await hook.post_turn(THREAD, branch)
    assert ei.value.code is GitParkCode.PUSH_FAILED
    assert env.remote_sha(BRANCH) == ""
    assert env.head() == "main"  # clean tree: back on the default branch for the next turn


@pytest.mark.anyio
async def test_push_never_forces(env: Env) -> None:
    seen: list[tuple[str, ...]] = []
    real = __import__("spirrow_mindwire.git_sync.hook", fromlist=["_subprocess_git"])

    def git(repo: Path, args: Sequence[str], timeout: float) -> Any:
        seen.append(tuple(args))
        return real._subprocess_git(repo, args, timeout)

    hook = GitSyncHook(env.clone, github_factory=_FakeGitHub, git=git)
    branch = await hook.pre_turn(THREAD)
    _commit(env.clone, "work.txt")
    await hook.post_turn(THREAD, branch)
    pushes = [a for a in seen if a and a[0] == "push"]
    assert pushes == [("push", "origin", f"refs/heads/{BRANCH}:refs/heads/{BRANCH}")]
    assert not any(a for a in seen if "--force" in a or "-f" in a)


# ===================================================================== dispatcher wiring


class _Adapter:
    capabilities = frozenset(
        {Capability.READ_THREAD, Capability.POST_REPLY, Capability.EXECUTE_CODE}
    )

    def __init__(self, *, fail: bool = False) -> None:
        self.adapter_id = "fake"
        self.events: list[ChatroomEvent] = []
        self.fail = fail
        self.log: list[str] | None = None

    async def spawn(self, thread_ref: ThreadRef, role: Role, ctx: SpawnContext) -> SessionHandle:
        return SessionHandle(
            session_id=new_ulid(),
            instance_id=ctx.own_instance_id,
            adapter_id=self.adapter_id,
            thread_ref=thread_ref,
            role=role,
            started_at=_TS,
        )

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        if self.log is not None:
            self.log.append("deliver")
        self.events.append(event)
        if self.fail:
            raise RuntimeError("turn blew up")

    async def halt(self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)) -> None:
        return None

    async def health(self, handle: SessionHandle) -> HealthStatus:
        return HealthStatus(state=SessionState.IDLE, last_active_at=_TS, error=None, details={})


class _Gateway:
    async def post_reply(self, *a: Any, **kw: Any) -> str:
        return "m"


class _RecordingHook:
    def __init__(self, log: list[str], *, post_park: bool = False) -> None:
        self.log = log
        self.post_park = post_park

    async def pre_turn(self, thread_id: str) -> ThreadBranch:
        self.log.append(f"pre:{thread_id}")
        return ThreadBranch(name="loop/t-x", default_branch="main")

    async def post_turn(self, thread_id: str, branch: ThreadBranch) -> None:
        self.log.append(f"post:{branch.name}")
        if self.post_park:
            raise GitSyncParkError(
                GitParkCode.DIRTY_AFTER_TURN,
                thread_id=thread_id,
                branch=branch.name,
                summary="1 uncommitted change(s) left as is: ?? wip.txt",
                phase=GitSyncPhase.POST_TURN,
            )


def _event() -> ChatroomEvent:
    return ChatroomEvent(
        event_id=new_ulid(),
        event_type=EventType.NEW_MESSAGE,
        thread_ref=ThreadRef(project_id="p", thread_id="T-x", chatroom_uri="mc://t"),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id="m1", author="Bohr", body="go", parent_msg_id=None),
    )


def _dispatcher(adapter: _Adapter, hook: Any) -> Dispatcher:
    reg = InMemoryAdapterRegistry()
    reg.register(adapter)
    return Dispatcher(registry=reg, gateway=_Gateway(), git_sync=hook)


@pytest.mark.anyio
async def test_dispatcher_runs_pre_deliver_post_and_tells_the_branch() -> None:
    log: list[str] = []
    adapter = _Adapter()
    adapter.log = log
    disp = _dispatcher(adapter, _RecordingHook(log))
    handle = await disp.spawn_instance(_event().thread_ref, Role.IMPLEMENTER, "implementer-1")
    await disp.dispatch(handle, _event())
    assert log == ["pre:T-x", "deliver", "post:loop/t-x"]
    assert adapter.events[0].git_branch == "loop/t-x"
    prompt = build_turn_prompt(adapter.events[0], Role.IMPLEMENTER, "Reply.")
    assert "`loop/t-x`" in prompt


@pytest.mark.anyio
async def test_post_turn_runs_after_a_failed_turn_and_keeps_its_exception() -> None:
    log: list[str] = []
    adapter = _Adapter(fail=True)
    adapter.log = log
    disp = _dispatcher(adapter, _RecordingHook(log, post_park=True))
    handle = await disp.spawn_instance(_event().thread_ref, Role.IMPLEMENTER, "implementer-1")
    with pytest.raises(RuntimeError, match="turn blew up"):
        await disp.dispatch(handle, _event())
    assert log == ["pre:T-x", "deliver", "post:loop/t-x"]


@pytest.mark.anyio
async def test_post_turn_runs_when_the_pin_write_raises() -> None:
    """PR #473 review objection 1: a pin-write failure after ``pre_turn`` must still run
    ``post_turn`` (B-4), so the clone is not left on the thread branch; the pin error keeps its
    routing and the role never sees the event."""

    class _FailingPinWriter:
        async def write_before_dispatch_async(self, role: Role, thread_id: str) -> None:
            log.append("pin")
            raise OSError("disk full")

    log: list[str] = []
    adapter = _Adapter()
    adapter.log = log
    reg = InMemoryAdapterRegistry()
    reg.register(adapter)
    disp = Dispatcher(
        registry=reg,
        gateway=_Gateway(),
        git_sync=_RecordingHook(log, post_park=True),  # type: ignore[arg-type]
        spec_pin_writer=_FailingPinWriter(),  # type: ignore[arg-type]
    )
    handle = await disp.spawn_instance(_event().thread_ref, Role.IMPLEMENTER, "implementer-1")
    with pytest.raises(OSError, match="disk full"):
        await disp.dispatch(handle, _event())
    assert log == ["pre:T-x", "pin", "post:loop/t-x"]
    assert adapter.events == []


@pytest.mark.anyio
async def test_post_turn_park_after_a_good_turn_propagates() -> None:
    log: list[str] = []
    disp = _dispatcher(_Adapter(), _RecordingHook(log, post_park=True))
    handle = await disp.spawn_instance(_event().thread_ref, Role.IMPLEMENTER, "implementer-1")
    with pytest.raises(GitSyncParkError) as ei:
        await disp.dispatch(handle, _event())
    assert ei.value.code is GitParkCode.DIRTY_AFTER_TURN


@pytest.mark.anyio
async def test_pre_turn_park_never_reaches_the_role() -> None:
    class _ParkingHook(_RecordingHook):
        async def pre_turn(self, thread_id: str) -> ThreadBranch:
            self.log.append("pre")
            raise GitSyncParkError(
                GitParkCode.DIVERGED,
                thread_id=thread_id,
                branch="loop/t-x",
                summary="diverged",
                phase=GitSyncPhase.PRE_TURN,
            )

    log: list[str] = []
    adapter = _Adapter()
    disp = _dispatcher(adapter, _ParkingHook(log))
    handle = await disp.spawn_instance(_event().thread_ref, Role.IMPLEMENTER, "implementer-1")
    with pytest.raises(GitSyncParkError):
        await disp.dispatch(handle, _event())
    assert log == ["pre"] and adapter.events == []


# ===================================================================== INV-0: off by default


def test_sync_is_off_by_default_and_builds_no_hook(tmp_path: Path) -> None:
    settings = MindwireSettings()
    assert settings.turn.git.sync is False
    settings.loop.repo_dir = tmp_path
    assert loop_runner.build_git_sync_hook(settings) is None


def test_sync_off_dispatch_runs_no_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(loop_runner, "build_registry", lambda **_kw: InMemoryAdapterRegistry())
    settings = MindwireSettings()
    settings.loop.repo_dir = tmp_path
    _mcp, _reg, disp = loop_runner._build_dispatcher(
        settings,
        mcp=object(),  # type: ignore[arg-type]
        proposer=_Adapter(),
        implementer=_Adapter(),
        naysayer=_Adapter(),
    )
    assert disp._git_sync is None


@pytest.mark.anyio
async def test_sync_off_event_and_prompt_are_unchanged() -> None:
    adapter = _Adapter()
    disp = _dispatcher(adapter, None)
    handle = await disp.spawn_instance(_event().thread_ref, Role.IMPLEMENTER, "implementer-1")
    ev = _event()
    await disp.dispatch(handle, ev)
    assert adapter.events[0] is ev and ev.git_branch is None
    assert "Git sync" not in build_turn_prompt(ev, Role.IMPLEMENTER, "Reply.")


def test_turn_git_table_loads_from_toml(tmp_path: Path) -> None:
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text(
        "[loop]\n"
        f"repo_dir = {json.dumps(str(tmp_path))}\n"
        '[turn.git]\nsync = true\nremote = "upstream"\nlfs = true\n'
        '[turn.git.branch_override]\n"T-some-existing-thread" = "feat/whatever"\n',
        encoding="utf-8",
    )
    settings = load_settings(cfg)
    assert settings.turn.git.sync and settings.turn.git.lfs
    assert settings.turn.git.remote == "upstream"
    assert settings.turn.git.branch_override == {"T-some-existing-thread": "feat/whatever"}
    assert isinstance(loop_runner.build_git_sync_hook(settings), GitSyncHook)


# ===================================================================== park notice / exit code


def test_park_notice_leads_with_the_reason_line_and_ends_next_human() -> None:
    from spirrow_mindwire.conductor.core import _with_stop_marker

    exc = GitSyncParkError(
        GitParkCode.PUSH_FAILED,
        thread_id="T-x",
        branch="loop/t-x",
        summary="git push ...: exit 1: refused",
        phase=GitSyncPhase.POST_TURN,
    )
    event = git_sync_park_event(exc, project="spirrow-mindwire")
    body = _with_stop_marker(render_git_sync_park_notice(exc), event)
    lines = body.splitlines()
    assert lines[0] == "git.push_failed: T-x loop/t-x git push ...: exit 1: refused"
    assert lines[-1] == "NEXT: human"
    assert lines[-2] == ""
    marker = parse_stop_marker(body)
    assert marker is not None and marker["kind"] == "git_sync.park"
    assert marker["code"] == "git.push_failed" and marker["thread"] == "T-x"


def test_main_maps_git_sync_deferral_to_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _fake_run_conductor(_settings: MindwireSettings, **_kw: object) -> None:
        raise GitSyncDeferredError(thread_id="T-x", candidate="loop/t-x", detail="HTTP 502")

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: MindwireSettings())
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])
    with pytest.raises(SystemExit) as ei:
        loop_runner.main()
    assert ei.value.code == GIT_SYNC_DEFERRED_EXIT_CODE == 9
    out = capsys.readouterr().out
    assert "reason=adapter_error" not in out
    rows = [ln for ln in out.splitlines() if ln.startswith(GIT_SYNC_DEFERRED_PAYLOAD_PREFIX)]
    assert len(rows) == 1
    assert json.loads(rows[0][len(GIT_SYNC_DEFERRED_PAYLOAD_PREFIX) :]) == {
        "thread_id": "T-x",
        "candidate": "loop/t-x",
        "detail": "HTTP 502",
    }


def test_deferred_exit_code_is_distinct_and_pinned_to_the_wrapper() -> None:
    from spirrow_mindwire.conductor import run_budget, stall, stand_down

    taken = {
        1,
        2,
        stand_down.STAND_DOWN_EXIT_CODE,
        stall.STALLED_EXIT_CODE,
        run_budget.RUN_TIMEOUT_EXIT_CODE,
        run_budget.RUN_KILLED_EXIT_CODE,
        run_budget.RUN_KILL_UNCONFIRMED_EXIT_CODE,
        DIRTY_CLONE_EXIT_CODE,
    }
    assert GIT_SYNC_DEFERRED_EXIT_CODE not in taken
    assert max(taken) + 1 == GIT_SYNC_DEFERRED_EXIT_CODE  # the smallest free code
    sweep = (_REPO_ROOT / "deploy" / "run-conductor-scheduled.ps1").read_text(encoding="utf-8")
    m = re.search(r"^\$GitSyncDeferredExitCode\s*=\s*(\d+)\s*$", sweep, re.MULTILINE)
    assert m is not None and int(m.group(1)) == GIT_SYNC_DEFERRED_EXIT_CODE
    assert f"^{GIT_SYNC_DEFERRED_PAYLOAD_PREFIX.strip()}" in sweep
