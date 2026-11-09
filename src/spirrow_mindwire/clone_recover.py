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
  verdict already means none of those holds. A detached HEAD is not eligible (outside preserve
  mode, below);
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

**Preserve mode** (``--preserve``; thread T-role-exit-leaves-clone-off-default-branch, PR-A, Bohr
msg-8698, naysayer go msg-8699). The sweep wrapper calls it once at the end of a launch whose
``clone-retreat`` was ``not_eligible``. It also takes the two cases the plain mode refuses: a
clean ``wrong_head`` whose commits are not all on the remote, and a detached HEAD. It moves those
commits to a **local** ref instead of leaving the clone parked:

1. ``git branch wip/<branch|detached-<sha8>>-<UTC ts> HEAD``;
2. ``rev-parse`` checks that the new ref and ``HEAD`` name the same commit;
3. ``git switch <default>``, ``git merge --ff-only origin/<default>``;
4. on a named branch, ``git branch -D <branch>``, so the next session can ``checkout -b`` that
   name again.

Nothing is pushed and no bundle is written (msg-8690 / msg-8692: a remote push is an external side
effect, and one local ref is the single place the commits are kept). A detached HEAD whose commits
are all on the remote switches back without a ref. If step 1 or 2 fails, the result is ``failed``
and the clone's HEAD and tree are unchanged, so the guard keeps it parked. The row gains
``preserved_ref`` and ``preserved_sha``, and :func:`run` appends a ``retreat`` row with outcome
``preserved`` to ``<state_dir>/clone-retreats.jsonl`` (see
:func:`spirrow_mindwire.clone_retreat_log.record_preserved`) for that thread's next implementer.
The record assumes a local reader: the next turn reads this machine's ``clone-retreats.jsonl``.
How an executor pool (ADR-23) carries it to another machine is decided in the pool's unit 4.

Exit codes: :data:`RECOVERED_EXIT` (0) switched, :data:`NOT_ELIGIBLE_EXIT` (3) nothing changed,
:data:`FAILED_EXIT` (1) a git step before the switch failed in a way that is not a verdict, or the
switch itself failed. Each run prints exactly one :data:`RECOVER_PREFIX` row for the wrapper's log.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
PRESERVE_REF_PREFIX = "wip/"


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
    preserved_ref: str | None = None
    preserved_sha: str | None = None

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
                "preserved_ref": self.preserved_ref,
                "preserved_sha": self.preserved_sha,
            },
            ensure_ascii=False,
        )


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


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


def _why(res: GitResult | str) -> str:
    return res if isinstance(res, str) else _first_line(res)


def _ok(res: GitResult | str) -> bool:
    return not isinstance(res, str) and res.returncode == 0


class CloneRecoverer:
    """Switches a ``wrong_head`` clone back to its default branch when that loses no commit.

    With ``preserve=True`` it also switches back a clone whose commits are not all on the remote,
    after keeping them on a local ``wip/`` ref (see the module docstring).
    """

    def __init__(
        self,
        repo_dir: Path,
        *,
        git: GitRunner = _subprocess_git,
        guard: CloneGuard | None = None,
        preserve: bool = False,
        clock: Callable[[], str] = _utc_stamp,
    ) -> None:
        self._repo_dir = repo_dir
        self._git = git
        self._guard = guard if guard is not None else CloneGuard(repo_dir)
        self._preserve = preserve
        self._clock = clock

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
        branch = verdict.head or None
        if branch is None and not self._preserve:
            return RecoverResult(RecoverOutcome.NOT_ELIGIBLE, detail="HEAD is detached")

        fetched = self._run(["fetch", "--prune", "origin"])
        if not _ok(fetched):
            return RecoverResult(
                RecoverOutcome.NOT_ELIGIBLE, branch=branch, detail=f"fetch failed: {_why(fetched)}"
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
            return RecoverResult(
                RecoverOutcome.FAILED,
                branch=branch,
                default=default,
                detail=f"rev-list: {_why(listed)}",
            )
        unpushed = [c for c in listed.stdout.split() if c]
        ref: str | None = None
        sha: str | None = None
        if unpushed:
            if not self._preserve:
                return RecoverResult(
                    RecoverOutcome.NOT_ELIGIBLE,
                    branch=branch,
                    default=default,
                    detail=f"{len(unpushed)} commit(s) not on {' / '.join(bases)}",
                )
            kept = self._preserve_head(branch, default)
            if isinstance(kept, RecoverResult):
                return kept
            ref, sha = kept

        switched = self._run(["switch", default])
        if not _ok(switched):
            return RecoverResult(
                RecoverOutcome.FAILED,
                branch=branch,
                default=default,
                detail=f"switch: {_why(switched)}",
                preserved_ref=ref,
                preserved_sha=sha,
            )

        notes: list[str] = []
        if ref is not None:
            notes.append(f"{len(unpushed)} unpushed commit(s) kept on local ref {ref}")
        ff = self._run(["merge", "--ff-only", f"origin/{default}"])
        if not _ok(ff):
            notes.append(f"ff-only failed: {_why(ff)}")
        if branch is not None:
            deleted = self._run(["branch", "-D", branch])
            if not _ok(deleted):
                notes.append(f"branch delete failed: {_why(deleted)}")
        return RecoverResult(
            RecoverOutcome.RECOVERED,
            branch=branch,
            default=default,
            detail="; ".join(notes),
            preserved_ref=ref,
            preserved_sha=sha,
        )

    def _preserve_head(self, branch: str | None, default: str) -> tuple[str, str] | RecoverResult:
        """Preserve steps 1-2: ``(ref, sha)``, or ``failed`` with HEAD and tree unchanged."""
        head = self._run(["rev-parse", "--verify", "HEAD"])
        if isinstance(head, str) or head.returncode != 0:
            return RecoverResult(
                RecoverOutcome.FAILED,
                branch=branch,
                default=default,
                detail=f"rev-parse HEAD: {_why(head)}",
            )
        sha = head.stdout.strip()
        name = branch if branch is not None else f"detached-{sha[:8]}"
        ref = f"{PRESERVE_REF_PREFIX}{name}-{self._clock()}"
        made = self._run(["branch", ref, "HEAD"])
        if not _ok(made):
            return RecoverResult(
                RecoverOutcome.FAILED,
                branch=branch,
                default=default,
                detail=f"preserve: creating {ref} failed: {_why(made)}",
            )
        check = self._run(["rev-parse", "--verify", f"refs/heads/{ref}"])
        got = check.stdout.strip() if not isinstance(check, str) and check.returncode == 0 else ""
        if got != sha:
            return RecoverResult(
                RecoverOutcome.FAILED,
                branch=branch,
                default=default,
                detail=f"preserve: {ref} is {got or 'unreadable'} but HEAD is {sha}; not switched",
            )
        return ref, sha

    def _ref_exists(self, ref: str) -> bool:
        res = self._run(["show-ref", "--verify", "--quiet", ref])
        return not isinstance(res, str) and res.returncode == 0

    def _default_branch(self) -> str | RecoverResult:
        """The guard's notion of the default branch: origin/HEAD's target, else ``main``."""
        res = self._run(["symbolic-ref", "-q", "refs/remotes/origin/HEAD"])
        if isinstance(res, str) or res.returncode not in (0, 1):
            return RecoverResult(RecoverOutcome.FAILED, detail=f"symbolic-ref: {_why(res)}")
        if res.returncode == 1:
            return FALLBACK_DEFAULT_BRANCH
        return res.stdout.strip().removeprefix("refs/remotes/origin/")


def run(
    repo_dir: Path,
    *,
    preserve: bool = False,
    thread_id: str | None = None,
    state_dir: Path | None = None,
) -> int:
    """``mindwire clone-recover``: print one row, return the outcome's exit code.

    In preserve mode, a result that kept commits on a local ref also appends a ``retreat`` row
    (outcome ``preserved``) for ``thread_id`` to ``<state_dir>/clone-retreats.jsonl``. That write
    only records what happened: if it fails, one line goes to stderr and the exit code is still
    the outcome's.
    """
    try:
        result = CloneRecoverer(repo_dir, preserve=preserve).recover()
    except Exception as exc:  # the guard's own git faults surface as DirtyCloneError above
        summary = " ".join(str(exc).split()) or "-"
        result = RecoverResult(RecoverOutcome.FAILED, detail=f"{type(exc).__name__}: {summary}")
    sys.stdout.write(result.row() + "\n")
    sys.stdout.flush()
    if result.preserved_ref and result.preserved_sha and thread_id and state_dir is not None:
        from .clone_retreat_log import CLONE_RETREAT_LOG_FILENAME, record_preserved

        try:
            record_preserved(
                state_dir / CLONE_RETREAT_LOG_FILENAME,
                thread_id=thread_id,
                repo_dir=repo_dir,
                branch=result.branch,
                preserved_ref=result.preserved_ref,
                preserved_sha=result.preserved_sha,
                recovered=result.outcome is RecoverOutcome.RECOVERED,
                detail=result.detail,
            )
        except Exception as exc:
            sys.stderr.write(f"clone-recover: writing the preserve record failed: {exc}\n")
    return result.exit_code


__all__ = [
    "FAILED_EXIT",
    "NOT_ELIGIBLE_EXIT",
    "PRESERVE_REF_PREFIX",
    "RECOVERED_EXIT",
    "RECOVER_PREFIX",
    "CloneRecoverer",
    "RecoverOutcome",
    "RecoverResult",
    "run",
]
