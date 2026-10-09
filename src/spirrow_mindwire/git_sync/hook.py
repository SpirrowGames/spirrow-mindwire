"""The git-sync hook around one dispatched turn (ADR-2026-10-09-23 unit 1, design v1.2 msg-6766).

Off by default (``[turn.git] sync = false``): the composition root then builds no hook, and the
dispatcher runs no git beyond the clone guard's (INV-0). When on, the dispatcher calls

* :meth:`GitSyncHook.pre_turn` after ``ensure_pin_ignored`` → :class:`~..clone_guard.CloneGuard`
  (so a clone dirtied by an earlier turn is the guard's exit 8, never this thread's park — B-3),
  and before the pin write;
* :meth:`GitSyncHook.post_turn` after the adapter's ``deliver_event``, whether it returned, raised
  or was cancelled, as long as the process is alive (B-4).

**Programmatic, not an LLM rule (D7).** Every git step is run here. The role session is only told
the branch name, as information (``ChatroomEvent.git_branch``).

**Detect and refuse, never tidy** (the clone guard's rule, D7): no automatic commit, no stash, no
reset, no clean, no force. The one write this hook makes to the working tree besides
switch / fast-forward is the final ``git switch <default>`` on a tree it has just verified clean,
so the next turn can pass the clone guard (B-4 step 4).

Pre-turn (B-3): fetch → resolve the name (override, else the generation search) → check it with
``git check-ref-format`` → switch / fast-forward / create → ``git lfs pull`` on the thread branch
when ``lfs = true``, then ``git lfs ls-files`` must show no pointer left. A park after the switch
puts HEAD back on the default branch first when the tree is clean, so one thread's park does not
leave the clone refusing every other thread.

Post-turn (B-4): operation in progress or uncommitted change → ``git.dirty_after_turn``; HEAD not
on the thread branch → ``git.head_moved``; local commits not on the remote → push with an
explicit refspec and no ``--force`` (failure → ``git.push_failed``); then switch back.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from ..clone_guard import FALLBACK_DEFAULT_BRANCH, OP_IN_PROGRESS_MARKERS, GitResult
from ..github.client import PrHeadRow
from .branch import ProtectedBranchError, ThreadBranch, normalize_stem
from .errors import GitParkCode, GitSyncParkError, GitSyncPhase
from .resolve import Sleep, resolve_generation

logger = logging.getLogger(__name__)

GIT_TIMEOUT_S = 30.0
GIT_NETWORK_TIMEOUT_S = 300.0  # fetch / push
GIT_LFS_TIMEOUT_S = 1800.0
PORCELAIN_SHOWN = 5

_GITHUB_REMOTE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")

GitRunner = Callable[[Path, Sequence[str], float], GitResult]
"""``git -C repo <args>`` with a timeout. Raises ``subprocess.TimeoutExpired`` / ``OSError``."""


def _subprocess_git(repo_dir: Path, args: Sequence[str], timeout: float) -> GitResult:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # an unattended tick must fail, never wait on a prompt
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        env=env,
    )
    return GitResult(proc.returncode, proc.stdout or "", proc.stderr or "")


class PrHeadClient(Protocol):
    """The slice of :class:`~spirrow_mindwire.github.GitHubClient` the hook uses."""

    async def list_prs_by_head(self, owner: str, repo: str, branch: str) -> list[PrHeadRow]: ...

    async def aclose(self) -> None: ...


def parse_github_remote(url: str) -> tuple[str, str] | None:
    """``(owner, repo)`` from a GitHub https / ssh remote URL, else ``None``."""
    m = _GITHUB_REMOTE.search(url.strip())
    if m is None:
        return None
    return m.group(1), m.group(2)


class _GitFailedError(Exception):
    def __init__(self, args: Sequence[str], detail: str) -> None:
        self.detail = f"git {' '.join(args)}: {detail}"
        super().__init__(self.detail)


class GitSyncHook:
    """Pre-turn / post-turn git sync for one clone. See the module docstring."""

    def __init__(
        self,
        repo_dir: Path,
        *,
        remote: str = "origin",
        lfs: bool = False,
        branch_override: Mapping[str, str] | None = None,
        github_factory: Callable[[], PrHeadClient],
        git: GitRunner = _subprocess_git,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._repo_dir = repo_dir
        self._remote = remote
        self._lfs = lfs
        self._override = dict(branch_override or {})
        self._github_factory = github_factory
        self._git = git
        self._sleep = sleep

    @property
    def repo_dir(self) -> Path:
        return self._repo_dir

    # -- pre-turn -------------------------------------------------------------------------------

    async def pre_turn(self, thread_id: str) -> ThreadBranch:
        """Bring the clone onto ``thread_id``'s branch. Raises park / defer; returns the branch."""

        def park(code: GitParkCode, branch: str | None, summary: str) -> GitSyncParkError:
            return GitSyncParkError(
                code,
                thread_id=thread_id,
                branch=branch,
                summary=summary,
                phase=GitSyncPhase.PRE_TURN,
            )

        try:
            await asyncio.to_thread(
                self._run, ["fetch", self._remote], timeout=GIT_NETWORK_TIMEOUT_S
            )
        except _GitFailedError as exc:
            raise park(GitParkCode.FETCH_FAILED, None, exc.detail) from None
        default = await asyncio.to_thread(self._default_branch)
        branch = await self._resolve(thread_id, default, park)
        await asyncio.to_thread(self._checkout, thread_id, branch, default, park)
        if self._lfs:
            try:
                await asyncio.to_thread(self._lfs_pull, branch, park)
            except GitSyncParkError:
                await asyncio.to_thread(self._switch_back_if_clean, default)
                raise
        return branch

    async def _resolve(
        self,
        thread_id: str,
        default: str,
        park: Callable[[GitParkCode, str | None, str], GitSyncParkError],
    ) -> ThreadBranch:
        override = self._override.get(thread_id)
        if override is not None:
            name, overridden = override, True
        else:
            stem = normalize_stem(thread_id)
            url = await asyncio.to_thread(self._remote_url, park)
            slug = parse_github_remote(url)
            if slug is None:
                raise park(
                    GitParkCode.BRANCH_RESOLVE_FAILED,
                    None,
                    f"remote {self._remote!r} is not a GitHub repository URL: {url!r}",
                )
            owner, repo = slug
            client = self._github_factory()
            try:

                async def lister(candidate: str) -> list[PrHeadRow]:
                    return await client.list_prs_by_head(owner, repo, candidate)

                name = await resolve_generation(thread_id, stem, lister, sleep=self._sleep)
            finally:
                await client.aclose()
            overridden = False
        valid = await asyncio.to_thread(
            self._try, ["check-ref-format", f"refs/heads/{name}"], GIT_TIMEOUT_S
        )
        if valid is None or valid.returncode != 0:
            raise park(
                GitParkCode.BRANCH_RESOLVE_FAILED,
                name,
                "git check-ref-format rejected the name",
            )
        try:
            return ThreadBranch(name=name, default_branch=default, overridden=overridden)
        except ProtectedBranchError as exc:
            raise park(GitParkCode.BRANCH_RESOLVE_FAILED, name, str(exc)) from None

    def _checkout(
        self,
        thread_id: str,
        branch: ThreadBranch,
        default: str,
        park: Callable[[GitParkCode, str | None, str], GitSyncParkError],
    ) -> None:
        b = branch.name
        remote_ref = f"refs/remotes/{self._remote}/{b}"
        local_ref = f"refs/heads/{b}"
        try:
            has_remote = self._ref_exists(remote_ref)
            has_local = self._ref_exists(local_ref)
            if has_remote:
                if not has_local:
                    self._run(["switch", "-c", b, "--track", f"{self._remote}/{b}"])
                    return
                if self._is_ancestor(local_ref, remote_ref):
                    self._run(["switch", b])
                    try:
                        self._run(["merge", "--ff-only", remote_ref])
                    except _GitFailedError:
                        self._switch_back_if_clean(default)
                        raise
                    return
                if self._is_ancestor(remote_ref, local_ref):
                    raise park(
                        GitParkCode.LOCAL_AHEAD, b, f"local {b} has commits not on {remote_ref}"
                    )
                raise park(GitParkCode.DIVERGED, b, f"local {b} and {remote_ref} have diverged")
            if not has_local:
                base = f"refs/remotes/{self._remote}/{default}"
                self._run(["switch", "--no-track", "-c", b, base])
                return
            if self._unpushed(local_ref):
                raise park(
                    GitParkCode.LOCAL_AHEAD,
                    b,
                    f"local {b} has commits on no {self._remote} branch and {remote_ref} is absent",
                )
            self._run(["switch", b])
        except _GitFailedError as exc:
            raise park(GitParkCode.CHECKOUT_FAILED, b, exc.detail) from None

    def _lfs_pull(
        self,
        branch: ThreadBranch,
        park: Callable[[GitParkCode, str | None, str], GitSyncParkError],
    ) -> None:
        try:
            self._run(["lfs", "pull", self._remote], timeout=GIT_LFS_TIMEOUT_S)
            listing = self._run(["lfs", "ls-files"], timeout=GIT_LFS_TIMEOUT_S)
        except _GitFailedError as exc:
            raise park(GitParkCode.LFS_FAILED, branch.name, exc.detail) from None
        # ``<oid> <*|-> <path>``: ``*`` = content present, ``-`` = still a pointer file.
        pointers = [
            parts[2]
            for parts in (line.split(" ", 2) for line in listing.stdout.splitlines())
            if len(parts) == 3 and parts[1] == "-"
        ]
        if pointers:
            shown = ", ".join(pointers[:PORCELAIN_SHOWN])
            raise park(
                GitParkCode.LFS_FAILED,
                branch.name,
                f"{len(pointers)} LFS file(s) still pointers after lfs pull: {shown}",
            )

    # -- post-turn ------------------------------------------------------------------------------

    async def post_turn(self, thread_id: str, branch: ThreadBranch) -> None:
        """Verify, push and switch back after the turn. Raises :class:`GitSyncParkError`."""
        await asyncio.to_thread(self._post_turn, thread_id, branch)

    def _post_turn(self, thread_id: str, branch: ThreadBranch) -> None:
        def park(code: GitParkCode, summary: str) -> GitSyncParkError:
            return GitSyncParkError(
                code,
                thread_id=thread_id,
                branch=branch.name,
                summary=summary,
                phase=GitSyncPhase.POST_TURN,
            )

        try:
            git_dir = Path(self._run(["rev-parse", "--absolute-git-dir"]).stdout.strip())
            for marker in OP_IN_PROGRESS_MARKERS:
                if (git_dir / marker).exists():
                    raise park(GitParkCode.DIRTY_AFTER_TURN, f"{marker} present (left as is)")
            status = self._run(["status", "--porcelain=v1", "-z", "--untracked-files=normal"])
            entries = [e for e in status.stdout.split("\0") if e]
            if entries:
                shown = "; ".join(entries[:PORCELAIN_SHOWN])
                raise park(
                    GitParkCode.DIRTY_AFTER_TURN,
                    f"{len(entries)} uncommitted change(s) left as is: {shown}",
                )
            head = self._try(["symbolic-ref", "-q", "HEAD"], GIT_TIMEOUT_S)
            head_ref = head.stdout.strip() if head is not None and head.returncode == 0 else ""
            if head_ref != f"refs/heads/{branch.name}":
                raise park(
                    GitParkCode.HEAD_MOVED,
                    f"HEAD is {head_ref.removeprefix('refs/heads/') or '(detached)'}, "
                    f"not {branch.name}",
                )
            default = self._default_branch()
            if self._unpushed(f"refs/heads/{branch.name}"):
                try:
                    self.push(branch)
                except _GitFailedError as exc:
                    self._switch_back_if_clean(default)
                    raise park(GitParkCode.PUSH_FAILED, exc.detail) from None
            self._run(["switch", default])
        except _GitFailedError as exc:
            raise park(GitParkCode.CHECKOUT_FAILED, exc.detail) from None

    def push(self, branch: ThreadBranch) -> None:
        """``git push <remote> refs/heads/<b>:refs/heads/<b>`` — no ``--force``, ever (B-5).

        Takes only a :class:`ThreadBranch`, whose constructor already refused the default branch
        and the protected names; the ``isinstance`` check keeps a bare ``str`` from sneaking in
        through an untyped call site.
        """
        if not isinstance(branch, ThreadBranch):
            raise TypeError(f"push takes a ThreadBranch, not {type(branch).__name__}")
        self._run(["push", self._remote, branch.refspec], timeout=GIT_NETWORK_TIMEOUT_S)

    # -- helpers --------------------------------------------------------------------------------

    def _try(self, args: Sequence[str], timeout: float) -> GitResult | None:
        try:
            return self._git(self._repo_dir, args, timeout)
        except (subprocess.TimeoutExpired, OSError):
            return None

    def _run(self, args: Sequence[str], *, timeout: float = GIT_TIMEOUT_S) -> GitResult:
        try:
            res = self._git(self._repo_dir, args, timeout)
        except subprocess.TimeoutExpired:
            raise _GitFailedError(args, f"timed out ({timeout:g}s)") from None
        except OSError as exc:
            raise _GitFailedError(args, f"failed to run: {exc}") from exc
        if res.returncode != 0:
            err = (res.stderr or res.stdout).strip().splitlines()
            raise _GitFailedError(args, f"exit {res.returncode}: {err[-1] if err else '-'}")
        return res

    def _ref_exists(self, ref: str) -> bool:
        res = self._try(["rev-parse", "--verify", "-q", ref], GIT_TIMEOUT_S)
        if res is None or res.returncode not in (0, 1):
            raise _GitFailedError(["rev-parse", "--verify", ref], "could not read the ref")
        return res.returncode == 0

    def _is_ancestor(self, a: str, b: str) -> bool:
        res = self._try(["merge-base", "--is-ancestor", a, b], GIT_TIMEOUT_S)
        if res is None or res.returncode not in (0, 1):
            raise _GitFailedError(
                ["merge-base", "--is-ancestor", a, b], "could not decide ancestry"
            )
        return res.returncode == 0

    def _unpushed(self, ref: str) -> bool:
        """``True`` when ``ref`` has a commit that no ``<remote>/*`` branch contains."""
        res = self._run(["rev-list", "--count", ref, "--not", f"--remotes={self._remote}"])
        return int(res.stdout.strip() or "0") > 0

    def _default_branch(self) -> str:
        res = self._try(["symbolic-ref", "-q", f"refs/remotes/{self._remote}/HEAD"], GIT_TIMEOUT_S)
        if res is None or res.returncode != 0 or not res.stdout.strip():
            return FALLBACK_DEFAULT_BRANCH
        return res.stdout.strip().removeprefix(f"refs/remotes/{self._remote}/")

    def _remote_url(self, park: Callable[[GitParkCode, str | None, str], GitSyncParkError]) -> str:
        try:
            # The configured value, not ``git remote get-url`` (which applies ``insteadOf``): the
            # owner / repo the PR search asks GitHub about is the one the clone names.
            return self._run(["config", "--get", f"remote.{self._remote}.url"]).stdout.strip()
        except _GitFailedError as exc:
            raise park(GitParkCode.BRANCH_RESOLVE_FAILED, None, exc.detail) from None

    def _switch_back_if_clean(self, default: str) -> None:
        """Best effort: back to the default branch only when nothing would be disturbed."""
        status = self._try(
            ["status", "--porcelain=v1", "-z", "--untracked-files=normal"], GIT_TIMEOUT_S
        )
        if status is None or status.returncode != 0 or status.stdout.strip("\0"):
            logger.warning("git sync: clone not clean, HEAD left where it is (%s)", self._repo_dir)
            return
        res = self._try(["switch", default], GIT_TIMEOUT_S)
        if res is None or res.returncode != 0:
            logger.warning("git sync: could not switch %s back to %s", self._repo_dir, default)


__all__ = ["GitRunner", "GitSyncHook", "PrHeadClient", "parse_github_remote"]
