"""Pre-dispatch guard: refuse to start a role on a dirty shared clone.

Thread T-timed-out-implementer-turn-leaves-dirty-shared-clone, design v4 (Bohr msg-5781, endorsed
by Einstein msg-5782). An implementer turn cut off by a timeout leaves ``[loop].repo_dir`` as it
was at the moment of the cut: uncommitted edits, a half-done merge, HEAD on that thread's feature
branch. The next turn — for any thread, in any role — used to start on top of that. This module
detects it and refuses; it never repairs it.

**Detect and refuse, never tidy.** No ``stash`` / ``reset`` / ``clean`` / ``switch``: on a shared
clone a stash records no owner and a reset loses the WIP, and both are irreversible. Every git
command run here is read-only and carries ``--no-optional-locks`` so the guard does not even take
the index lock ``git status`` would otherwise grab to refresh the stat cache (design D-1(b')).

**Git is the only source of truth.** No marker file, no owner record, no same-thread bypass
(Einstein msg-5774 #3). A clone is accepted only when all of these hold:

* no git operation is part-way through (``MERGE_HEAD`` / ``rebase-merge`` / ``rebase-apply`` /
  ``CHERRY_PICK_HEAD`` / ``REVERT_HEAD`` / ``BISECT_START`` / ``BISECT_HEAD``)
  → else ``op_in_progress``;
* ``index.lock`` is absent, or goes away within :data:`LOCK_WAIT_S` (polled every
  :data:`LOCK_POLL_S`) → else ``clone_busy``;
* ``git check-ignore -q .mindwire/pin`` says the pin path is ignored → else ``pin_not_ignored``
  (detail: ``pin is tracked in this repo`` when ``git ls-files --error-unmatch`` finds it,
  ``exclude not effective`` otherwise). Only the spirrow-mindwire repo lists ``.mindwire/`` in
  its ``.gitignore``; for every clone the dispatcher calls
  :func:`spirrow_mindwire.pin_ignore.ensure_pin_ignored` *before* this guard, which keeps
  ``/.mindwire/`` in the clone's ``info/exclude``. This check verifies the result, whichever of the
  two makes it true (T-clone-guard-pin-ignored-only-in-mindwire, design v2.1 D-1);
* ``git status --porcelain=v1 -z --untracked-files=normal`` prints nothing → else ``dirty_tree``
  (the pin is ignored by the check above, so the pin the dispatcher writes never trips this);
* HEAD is a branch and that branch is the default branch → else ``wrong_head``. The default
  branch is ``refs/remotes/origin/HEAD``'s target; only when that ref does not exist does
  :data:`FALLBACK_DEFAULT_BRANCH` apply;
* every git call above exited 0 inside :data:`GIT_TIMEOUT_S` without an ``OSError`` → else
  ``git_unavailable``. A failed ``git status`` prints nothing on stdout, so treating "no output"
  as clean without checking the exit code would pass a broken repo (Einstein msg-5776 #1).

Refusal raises :class:`DirtyCloneError`; ``loop_runner.main`` turns it into exit
:data:`DIRTY_CLONE_EXIT_CODE`, which the sweep wrapper handles without quarantining anyone.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# The daemon's exit code for a refused dispatch. Design v4 said 5, chosen as "the next free code
# after 3 (stand-down) and 4 (stall)"; 5, 6 and 7 turned out to be taken already by the wall-clock
# budget (conductor/run_budget.py RUN_TIMEOUT / RUN_KILLED / RUN_KILL_UNCONFIRMED), so the next
# free one is 8. The wrapper mirrors it as $DirtyCloneExitCode (a pytest pins the two).
DIRTY_CLONE_EXIT_CODE = 8

# stdout sentinel for the one JSON row ``loop_runner.main`` prints before exiting. The wrapper reads
# it only to enrich the notification; the do-not-quarantine decision rides on the exit code alone.
DIRTY_CLONE_PAYLOAD_PREFIX = "MINDWIRE_DIRTY_CLONE_PAYLOAD "

ERROR_CODE = "dispatch.dirty_clone"

GIT_TIMEOUT_S = 10.0  # same budget as loop_resource's git probe
LOCK_WAIT_S = 5.0
LOCK_POLL_S = 0.25
FALLBACK_DEFAULT_BRANCH = "main"
PIN_PATH = ".mindwire/pin"  # the file spec_pin.SpecPinWriter writes, relative to the clone root
PORCELAIN_MAX_ENTRIES = 20

# Files / directories under the git dir that mean an operation stopped half-way.
OP_IN_PROGRESS_MARKERS: tuple[str, ...] = (
    "MERGE_HEAD",
    "rebase-merge",
    "rebase-apply",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    # ``git bisect start`` writes BISECT_START (BISECT_HEAD too under --no-checkout). A bisect
    # left running by a human on the shared clone can sit on a clean tree at the default branch,
    # so nothing else would catch it (PR #432 gate advisory).
    "BISECT_START",
    "BISECT_HEAD",
)


class DirtyCloneReason(StrEnum):
    DIRTY_TREE = "dirty_tree"
    OP_IN_PROGRESS = "op_in_progress"
    CLONE_BUSY = "clone_busy"
    WRONG_HEAD = "wrong_head"
    GIT_UNAVAILABLE = "git_unavailable"
    PIN_NOT_IGNORED = "pin_not_ignored"


class DirtyCloneError(Exception):
    """The clone a role would run in is not clean; the dispatch was refused, nothing was changed.

    An environment fault, not the dispatched thread's: ``loop_runner.main`` routes it by type to
    exit :data:`DIRTY_CLONE_EXIT_CODE`, never to the adapter-error / quarantine path.
    """

    code = ERROR_CODE

    def __init__(
        self,
        *,
        repo_dir: Path,
        reason: DirtyCloneReason,
        head: str | None,
        porcelain: Sequence[str] = (),
        detail: str = "",
    ) -> None:
        self.repo_dir = repo_dir
        self.reason = reason
        self.head = head
        self.porcelain = tuple(porcelain[:PORCELAIN_MAX_ENTRIES])
        self.detail = detail
        super().__init__(
            f"{ERROR_CODE}: reason={reason.value} repo_dir={repo_dir} head={head or '-'}"
            + (f" ({detail})" if detail else "")
        )

    def payload(self) -> dict[str, object]:
        """The JSON object ``loop_runner.main`` prints after :data:`DIRTY_CLONE_PAYLOAD_PREFIX`."""
        return {
            "repo_dir": str(self.repo_dir),
            "reason": self.reason.value,
            "head": self.head,
            "porcelain": list(self.porcelain),
            "detail": self.detail,
        }


def emit_dirty_clone_payload(exc: DirtyCloneError) -> None:
    """Print the one ``MINDWIRE_DIRTY_CLONE_PAYLOAD <json>`` row the sweep wrapper parses.

    The single writer of that row: ``loop_runner.main`` (daemon, exit 8) and
    ``mindwire clone-check`` (parked-repo re-judge, exit 8) both call it, so the wrapper's
    ``Get-DirtyCloneNotice`` reads one format (T-clone-guard-pin-ignored-only-in-mindwire,
    design v2.1 D-3c).
    """
    import json
    import sys

    sys.stdout.write(f"{DIRTY_CLONE_PAYLOAD_PREFIX}{json.dumps(exc.payload())}\n")
    sys.stdout.flush()


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


GitRunner = Callable[[Path, Sequence[str]], GitResult]
"""Runs ``git <args>`` in a repo. Raises ``subprocess.TimeoutExpired`` / ``OSError`` on failure."""


def _subprocess_git(repo_dir: Path, args: Sequence[str]) -> GitResult:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"  # belt to the --no-optional-locks braces below
    proc = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=GIT_TIMEOUT_S,
        env=env,
    )
    return GitResult(proc.returncode, proc.stdout or "", proc.stderr or "")


class CloneGuard:
    """Checks one repo before every dispatch. Read-only; raises :class:`DirtyCloneError`."""

    def __init__(
        self,
        repo_dir: Path,
        *,
        git: GitRunner = _subprocess_git,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo_dir = repo_dir
        self._git = git
        self._sleep = sleep
        self._monotonic = monotonic

    @property
    def repo_dir(self) -> Path:
        return self._repo_dir

    def check(self) -> None:
        """Return if clean and on the default branch; else raise :class:`DirtyCloneError`."""
        git_dir = Path(self._run(["rev-parse", "--absolute-git-dir"], head=None).stdout.strip())

        for marker in OP_IN_PROGRESS_MARKERS:
            if (git_dir / marker).exists():
                head = self._head_or_none()
                raise self._error(
                    DirtyCloneReason.OP_IN_PROGRESS, head=head, detail=f"{marker} present"
                )

        self._wait_for_index_lock(git_dir)

        # No ``--no-index``: a tracked pin is reported as NOT ignored, which is what we want.
        ignored = self._run(["check-ignore", "-q", PIN_PATH], head=None, ok_codes=(0, 1))
        if ignored.returncode == 1:
            tracked = self._run(
                ["ls-files", "--error-unmatch", PIN_PATH], head=None, ok_codes=(0, 1)
            )
            raise self._error(
                DirtyCloneReason.PIN_NOT_IGNORED,
                head=self._head_or_none(),
                detail=(
                    "pin is tracked in this repo"
                    if tracked.returncode == 0
                    else "exclude not effective"
                ),
            )

        status = self._run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=normal"], head=None
        )
        entries = [e for e in status.stdout.split("\0") if e]
        if entries:
            raise self._error(
                DirtyCloneReason.DIRTY_TREE, head=self._head_or_none(), porcelain=entries
            )

        head_ref = self._run(["symbolic-ref", "-q", "HEAD"], head=None, ok_codes=(0, 1))
        if head_ref.returncode == 1:
            raise self._error(DirtyCloneReason.WRONG_HEAD, head=None, detail="HEAD is detached")
        head = head_ref.stdout.strip().removeprefix("refs/heads/")
        default = self._default_branch(head)
        if head != default:
            raise self._error(
                DirtyCloneReason.WRONG_HEAD,
                head=head,
                detail=f"HEAD is on {head!r}, default branch is {default!r}",
            )

    # -- helpers --------------------------------------------------------------------------------

    def _default_branch(self, head: str) -> str:
        res = self._run(
            ["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], head=head, ok_codes=(0, 1)
        )
        if res.returncode == 1:
            # The ref does not exist (exit 1 under -q): the documented fallback, not a git fault.
            return FALLBACK_DEFAULT_BRANCH
        return res.stdout.strip().removeprefix("refs/remotes/origin/")

    def _wait_for_index_lock(self, git_dir: Path) -> None:
        lock = git_dir / "index.lock"
        deadline = self._monotonic() + LOCK_WAIT_S
        while lock.exists():
            if self._monotonic() >= deadline:
                raise self._error(
                    DirtyCloneReason.CLONE_BUSY,
                    head=self._head_or_none(),
                    detail=f"index.lock still present after {LOCK_WAIT_S:g}s",
                )
            self._sleep(LOCK_POLL_S)

    def _head_or_none(self) -> str | None:
        """Best-effort HEAD name for a refusal message; never raises (the refusal is the point)."""
        try:
            res = self._git(self._repo_dir, ["symbolic-ref", "-q", "--short", "HEAD"])
        except (subprocess.TimeoutExpired, OSError):
            return None
        if res.returncode == 0:
            return res.stdout.strip() or None
        if res.returncode == 1:
            return "(detached)"
        return None

    def _run(
        self, args: Sequence[str], *, head: str | None, ok_codes: tuple[int, ...] = (0,)
    ) -> GitResult:
        try:
            res = self._git(self._repo_dir, args)
        except subprocess.TimeoutExpired:
            raise self._error(
                DirtyCloneReason.GIT_UNAVAILABLE,
                head=head,
                detail=f"git {' '.join(args)} timed out ({GIT_TIMEOUT_S:g}s)",
            ) from None
        except OSError as exc:
            raise self._error(
                DirtyCloneReason.GIT_UNAVAILABLE,
                head=head,
                detail=f"git {' '.join(args)} failed to run: {exc}",
            ) from exc
        if res.returncode not in ok_codes:
            err = (res.stderr or res.stdout).strip().splitlines()
            raise self._error(
                DirtyCloneReason.GIT_UNAVAILABLE,
                head=head,
                detail=f"git {' '.join(args)} exit {res.returncode}: {err[0] if err else '-'}",
            )
        return res

    def _error(
        self,
        reason: DirtyCloneReason,
        *,
        head: str | None,
        porcelain: Sequence[str] = (),
        detail: str = "",
    ) -> DirtyCloneError:
        return DirtyCloneError(
            repo_dir=self._repo_dir, reason=reason, head=head, porcelain=porcelain, detail=detail
        )


__all__ = [
    "DIRTY_CLONE_EXIT_CODE",
    "DIRTY_CLONE_PAYLOAD_PREFIX",
    "ERROR_CODE",
    "PIN_PATH",
    "CloneGuard",
    "DirtyCloneError",
    "DirtyCloneReason",
    "GitResult",
    "emit_dirty_clone_payload",
]
