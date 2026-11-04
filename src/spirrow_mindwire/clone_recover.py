"""Auto-recover a parked shared clone whose only fault is ``wrong_head`` and which loses nothing.

Thread T-dirty-clone-wip-retreat-and-silent-stall-alerts, PR-0 (operator msg-7774, approved by
Takahito in the 2026-11-04 human decision §2). The most common way an implementer turn ends is:
it pushes, the PR merges, and the turn ends without switching back. The clone is clean but HEAD
is on the feature branch, so :class:`~spirrow_mindwire.clone_guard.CloneGuard` refuses it as
``wrong_head`` and every candidate on the repo stays parked until a person switches it back
(incident 2: voxelworld, ~32 h).

This module switches it back, but only when doing so **destroys nothing**. It changes the clone
only when all three of these hold:

* the guard's verdict is ``wrong_head`` on a named branch. The guard checks every other fault
  first (in-progress op markers, ``index.lock``, pin not ignored, dirty tree), so a ``wrong_head``
  verdict already means none of those holds. A detached HEAD is not eligible;
* ``git fetch --prune origin`` succeeded, so the remote-tracking refs are current. Without the
  fetch, the next check could pass or fail on stale refs, so a failed fetch means not-eligible
  this tick, and the next tick tries again;
* ``git rev-list HEAD --not origin/<default> [origin/develop]`` is empty: every commit on the
  branch is already on the remote default branch, or on ``develop`` when the remote has one.

Then it runs ``git switch <default>``, ``git merge --ff-only origin/<default>`` and
``git branch -D <branch>``. ``-D`` rather than ``-d``: a branch merged into ``develop`` is not
merged into local ``main``, and the rev-list above already proved that its commits are on the
remote. If the fast-forward or the delete fails *after* the switch, the result is still
``recovered``, and the failure is reported in ``detail``. The guard checks only the branch name and
the tree, so the clone is unparked either way.

The guard stays read-only. This is a separate command (``mindwire clone-recover``) that the sweep
wrapper calls only for repos it parked as ``wrong_head``, immediately before its read-only
``clone-check``, which still makes the release decision.

Exit codes: :data:`RECOVERED_EXIT` (0) switched, :data:`NOT_ELIGIBLE_EXIT` (3) nothing changed,
:data:`FAILED_EXIT` (1) a git step before the switch failed in a way that is not a verdict, or the
switch itself failed. Each run prints exactly one :data:`RECOVER_PREFIX` row for the wrapper's log.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .clone_guard import (
    FALLBACK_DEFAULT_BRANCH,
    CloneGuard,
    DirtyCloneError,
    DirtyCloneReason,
    GitResult,
    GitRunner,
)

RECOVER_PREFIX = "MINDWIRE_CLONE_RECOVER "
RECOVERED_EXIT = 0
FAILED_EXIT = 1
NOT_ELIGIBLE_EXIT = 3

# The fetch talks to the network; everything else is local and finishes quickly. The wrapper bounds
# the whole command with $CloneCheckTimeoutSeconds (60), so the fetch gets less than that.
FETCH_TIMEOUT_S = 40.0
LOCAL_TIMEOUT_S = 10.0
DEVELOP_BRANCH = "develop"


class RecoverOutcome(StrEnum):
    RECOVERED = "recovered"
    NOT_ELIGIBLE = "not_eligible"
    FAILED = "failed"


@dataclass(frozen=True)
class RecoverResult:
    outcome: RecoverOutcome
    branch: str | None = None
    default: str | None = None
    detail: str = ""

    @property
    def exit_code(self) -> int:
        return {
            RecoverOutcome.RECOVERED: RECOVERED_EXIT,
            RecoverOutcome.NOT_ELIGIBLE: NOT_ELIGIBLE_EXIT,
            RecoverOutcome.FAILED: FAILED_EXIT,
        }[self.outcome]

    def row(self) -> str:
        return RECOVER_PREFIX + json.dumps(
            {
                "outcome": self.outcome.value,
                "branch": self.branch,
                "default": self.default,
                "detail": self.detail,
            },
            ensure_ascii=False,
        )


def _subprocess_git(repo_dir: Path, args: Sequence[str]) -> GitResult:
    timeout = FETCH_TIMEOUT_S if args and args[0] == "fetch" else LOCAL_TIMEOUT_S
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
    return GitResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _first_line(res: GitResult) -> str:
    lines = (res.stderr or res.stdout).strip().splitlines()
    return lines[0] if lines else "-"


class CloneRecoverer:
    """Switches a ``wrong_head`` clone back to its default branch when that loses no commit."""

    def __init__(
        self,
        repo_dir: Path,
        *,
        git: GitRunner = _subprocess_git,
        guard: CloneGuard | None = None,
    ) -> None:
        self._repo_dir = repo_dir
        self._git = git
        self._guard = guard if guard is not None else CloneGuard(repo_dir)

    def _run(self, args: Sequence[str]) -> GitResult | str:
        """Run git; return the result, or an error string if git timed out or could not start."""
        try:
            return self._git(self._repo_dir, args)
        except subprocess.TimeoutExpired:
            return f"git {' '.join(args)} timed out"
        except OSError as exc:
            return f"git {' '.join(args)} failed to run: {exc}"

    def recover(self) -> RecoverResult:
        try:
            self._guard.check()
        except DirtyCloneError as exc:
            verdict = exc
        else:
            return RecoverResult(RecoverOutcome.NOT_ELIGIBLE, detail="clone is already clean")

        if verdict.reason is not DirtyCloneReason.WRONG_HEAD:
            return RecoverResult(
                RecoverOutcome.NOT_ELIGIBLE,
                branch=verdict.head,
                detail=f"guard reason is {verdict.reason.value}, not wrong_head",
            )
        branch = verdict.head
        if not branch:
            return RecoverResult(RecoverOutcome.NOT_ELIGIBLE, detail="HEAD is detached")

        fetched = self._run(["fetch", "--prune", "origin"])
        if isinstance(fetched, str) or fetched.returncode != 0:
            why = fetched if isinstance(fetched, str) else _first_line(fetched)
            return RecoverResult(
                RecoverOutcome.NOT_ELIGIBLE, branch=branch, detail=f"fetch failed: {why}"
            )

        default = self._default_branch()
        if isinstance(default, RecoverResult):
            return default
        if branch == default:  # cannot happen after a wrong_head verdict; never delete the default
            return RecoverResult(
                RecoverOutcome.NOT_ELIGIBLE,
                branch=branch,
                default=default,
                detail="HEAD is the default branch",
            )

        bases = [f"origin/{default}"]
        if default != DEVELOP_BRANCH and self._ref_exists(f"refs/remotes/origin/{DEVELOP_BRANCH}"):
            bases.append(f"origin/{DEVELOP_BRANCH}")
        if not self._ref_exists(f"refs/remotes/origin/{default}"):
            return RecoverResult(
                RecoverOutcome.NOT_ELIGIBLE,
                branch=branch,
                default=default,
                detail=f"origin/{default} does not exist",
            )
        listed = self._run(["rev-list", "HEAD", "--not", *bases])
        if isinstance(listed, str) or listed.returncode != 0:
            why = listed if isinstance(listed, str) else _first_line(listed)
            return RecoverResult(
                RecoverOutcome.FAILED, branch=branch, default=default, detail=f"rev-list: {why}"
            )
        unpushed = [c for c in listed.stdout.split() if c]
        if unpushed:
            return RecoverResult(
                RecoverOutcome.NOT_ELIGIBLE,
                branch=branch,
                default=default,
                detail=f"{len(unpushed)} commit(s) not on {' / '.join(bases)}",
            )

        switched = self._run(["switch", default])
        if isinstance(switched, str) or switched.returncode != 0:
            why = switched if isinstance(switched, str) else _first_line(switched)
            return RecoverResult(
                RecoverOutcome.FAILED, branch=branch, default=default, detail=f"switch: {why}"
            )

        notes: list[str] = []
        ff = self._run(["merge", "--ff-only", f"origin/{default}"])
        if isinstance(ff, str) or ff.returncode != 0:
            notes.append(f"ff-only failed: {ff if isinstance(ff, str) else _first_line(ff)}")
        deleted = self._run(["branch", "-D", branch])
        if isinstance(deleted, str) or deleted.returncode != 0:
            why = deleted if isinstance(deleted, str) else _first_line(deleted)
            notes.append(f"branch delete failed: {why}")
        return RecoverResult(
            RecoverOutcome.RECOVERED, branch=branch, default=default, detail="; ".join(notes)
        )

    def _ref_exists(self, ref: str) -> bool:
        res = self._run(["show-ref", "--verify", "--quiet", ref])
        return not isinstance(res, str) and res.returncode == 0

    def _default_branch(self) -> str | RecoverResult:
        """The guard's notion of the default branch: origin/HEAD's target, else ``main``."""
        res = self._run(["symbolic-ref", "-q", "refs/remotes/origin/HEAD"])
        if isinstance(res, str) or res.returncode not in (0, 1):
            why = res if isinstance(res, str) else _first_line(res)
            return RecoverResult(RecoverOutcome.FAILED, detail=f"symbolic-ref: {why}")
        if res.returncode == 1:
            return FALLBACK_DEFAULT_BRANCH
        return res.stdout.strip().removeprefix("refs/remotes/origin/")


def run(repo_dir: Path) -> int:
    """``mindwire clone-recover``: print one row, return the outcome's exit code."""
    try:
        result = CloneRecoverer(repo_dir).recover()
    except Exception as exc:  # the guard's own git faults surface as DirtyCloneError above
        summary = " ".join(str(exc).split()) or "-"
        result = RecoverResult(RecoverOutcome.FAILED, detail=f"{type(exc).__name__}: {summary}")
    sys.stdout.write(result.row() + "\n")
    sys.stdout.flush()
    return result.exit_code


__all__ = [
    "FAILED_EXIT",
    "NOT_ELIGIBLE_EXIT",
    "RECOVERED_EXIT",
    "RECOVER_PREFIX",
    "CloneRecoverer",
    "RecoverOutcome",
    "RecoverResult",
    "run",
]
