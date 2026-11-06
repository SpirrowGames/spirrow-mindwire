"""Retreat a shared clone a launch left dirty: WIP to a ``wip/`` branch, or export-then-discard.

Thread T-dirty-clone-wip-retreat-and-silent-stall-alerts, PR-1a (design v1.5: Bohr msg-7907,
msg-7909, msg-7911, msg-7913; naysayer go msg-7914; human Tier-C decision 2026-11-06 resolving
msg-7916, which re-confirmed the discard Takahito allowed in msg-7863 §1).

The sweep wrapper calls ``mindwire clone-retreat`` once after every launch that ran a session,
whatever the stop reason. A clone that is clean, or whose only fault is ``wrong_head`` on a clean
tree (PR-0's ``clone-recover`` handles that), is left alone. Otherwise:

1. **Retreat.** When no git operation is in progress and the index has no unmerged path:
   ``git switch -c wip/<thread_id>-<UTC ts>``, ``git add -A``, commit (message carries thread_id,
   head msg and stop reason), try ``git push origin <wip branch>``, ``git switch <default>``.
   A push that fails leaves the branch local; the clone is cleaned anyway, because unparking the
   clone is the point (msg-7637 D-1). Result ``retreated``.
2. **Export, then discard**, when the retreat cannot run (op in progress, unmerged paths) or fails
   part-way. Before *any* command that destroys work (msg-7908 / v1.3 Step 3):

   * ``state/<repo>-discarded-<thread_id>-<ts>.patch`` — ``git diff --binary HEAD``;
   * ``state/<repo>-discarded-<thread_id>-<ts>.untracked.txt`` — a ``#`` header naming the
     in-progress op and the ``HEAD`` / ``ORIG_HEAD`` / ``MERGE_HEAD`` / ``REBASE_HEAD`` /
     ``CHERRY_PICK_HEAD`` / ``REVERT_HEAD`` shas, then the untracked paths.

   A failed export does not stop the discard (msg-7863 §1). Then, unconditionally and in this order,
   ``merge --abort``, ``rebase --abort``, ``am --abort``, ``cherry-pick --abort``,
   ``revert --abort`` (v1.5 Step 4: no marker parsing; exit codes ignored; stderr kept in the
   result row for the tick log), each with ``GIT_EDITOR=true`` so nothing waits on an editor. If the
   clone is still dirty: ``reset --hard`` and ``clean -fd`` (never ``-x``: the ignored
   ``.mindwire/pin`` survives), then ``git switch <default>``.
3. **Re-check.** :class:`~spirrow_mindwire.clone_guard.CloneGuard` runs again, read-only. Clean →
   ``discarded``. Anything left (an op marker the aborts did not clear) → ``discard_incomplete``:
   nothing under ``.git/`` is removed by hand, the clone stays parked by the guard, and PR-2's
   staged re-notification tells a person.

This command never posts to a chatroom thread (v1.1: the wrapper does not post). Its readers are
the wrapper's tick log, through the one :data:`RETREAT_PREFIX` row it prints, and (PR-1b, v1.7)
the thread's next implementer turn, through the row :func:`run` appends to
``<state_dir>/clone-retreats.jsonl`` (see :mod:`spirrow_mindwire.clone_retreat_log`).

Exit codes: 0 ``clean`` / ``not_eligible`` / ``retreated`` / ``discarded``, 4
``discard_incomplete``, 1 ``failed`` (the clone's state could not be judged).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .clone_guard import (
    FALLBACK_DEFAULT_BRANCH,
    CloneGuard,
    DirtyCloneError,
    DirtyCloneReason,
    GitResult,
)
from .clone_retreat_log import CLONE_RETREAT_LOG_FILENAME, record_result

RETREAT_PREFIX = "MINDWIRE_CLONE_RETREAT "
OK_EXIT = 0
FAILED_EXIT = 1
INCOMPLETE_EXIT = 4

PUSH_TIMEOUT_S = 40.0
LOCAL_TIMEOUT_S = 20.0
WIP_PREFIX = "wip/"
WRAPPER_NAME = "mindwire-wrapper"
WRAPPER_EMAIL = "mindwire-wrapper@users.noreply.invalid"

# v1.5 Step 4: all five, in this order, unconditionally.
ABORT_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("merge", "--abort"),
    ("rebase", "--abort"),
    ("am", "--abort"),
    ("cherry-pick", "--abort"),
    ("revert", "--abort"),
)
STATE_REFS: tuple[str, ...] = (
    "HEAD",
    "ORIG_HEAD",
    "MERGE_HEAD",
    "REBASE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
)
# Marker -> op name for the export header only. Nothing branches on it (v1.5).
OP_NAMES: tuple[tuple[str, str], ...] = (
    ("MERGE_HEAD", "merge"),
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase/am"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("sequencer", "sequencer (cherry-pick/revert)"),
    ("BISECT_START", "bisect"),
)


class RetreatOutcome(StrEnum):
    CLEAN = "clean"
    NOT_ELIGIBLE = "not_eligible"
    RETREATED = "retreated"
    DISCARDED = "discarded"
    DISCARD_INCOMPLETE = "discard_incomplete"
    FAILED = "failed"


@dataclass
class RetreatResult:
    outcome: RetreatOutcome
    thread_id: str
    branch: str | None = None
    pushed: bool | None = None
    files: int | None = None
    insertions: int | None = None
    deletions: int | None = None
    patch: str | None = None
    untracked: str | None = None
    marker: str | None = None
    detail: str = ""
    steps: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.outcome is RetreatOutcome.DISCARD_INCOMPLETE:
            return INCOMPLETE_EXIT
        if self.outcome is RetreatOutcome.FAILED:
            return FAILED_EXIT
        return OK_EXIT

    def row(self) -> str:
        return RETREAT_PREFIX + json.dumps(
            {
                "outcome": self.outcome.value,
                "thread_id": self.thread_id,
                "branch": self.branch,
                "pushed": self.pushed,
                "files": self.files,
                "insertions": self.insertions,
                "deletions": self.deletions,
                "patch": self.patch,
                "untracked": self.untracked,
                "marker": self.marker,
                "detail": self.detail,
                "steps": self.steps,
            },
            ensure_ascii=False,
        )


RetreatGit = Callable[[Path, Sequence[str]], GitResult]


def git_env() -> dict[str, str]:
    """The environment every retreat git call runs with. ``GIT_EDITOR=true``: never wait on an
    editor (v1.5). On Windows git runs the editor through its bundled ``sh``, where ``true`` is a
    builtin, so this needs no ``true.exe`` on the PATH (tests/test_clone_retreat.py pins it)."""
    env = dict(os.environ)
    env["GIT_EDITOR"] = "true"
    env["GIT_SEQUENCE_EDITOR"] = "true"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _subprocess_git(repo_dir: Path, args: Sequence[str]) -> GitResult:
    timeout = PUSH_TIMEOUT_S if args and args[0] == "push" else LOCAL_TIMEOUT_S
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        env=git_env(),
    )
    return GitResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "unknown"


def _first_line(res: GitResult | str) -> str:
    if isinstance(res, str):
        return res
    lines = (res.stderr or res.stdout).strip().splitlines()
    return lines[0] if lines else "-"


class CloneRetreater:
    def __init__(
        self,
        repo_dir: Path,
        *,
        thread_id: str,
        head_msg: str,
        stop_reason: str,
        state_dir: Path,
        git: RetreatGit = _subprocess_git,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        guard_factory: Callable[[Path], CloneGuard] = CloneGuard,
    ) -> None:
        self._repo_dir = repo_dir
        self._thread_id = thread_id
        self._head_msg = head_msg or "-"
        self._stop_reason = stop_reason or "-"
        self._state_dir = state_dir
        self._git = git
        self._ts = now().strftime("%Y%m%dT%H%M%SZ")
        self._guard_factory = guard_factory
        self._steps: list[str] = []
        self._pending_branch_cleanup: str | None = None

    # -- helpers --------------------------------------------------------------------------------

    def _run(self, args: Sequence[str]) -> GitResult | str:
        try:
            return self._git(self._repo_dir, args)
        except subprocess.TimeoutExpired:
            return f"git {' '.join(args)} timed out"
        except OSError as exc:
            return f"git {' '.join(args)} failed to run: {exc}"

    def _ok(self, res: GitResult | str) -> bool:
        return not isinstance(res, str) and res.returncode == 0

    def _step(self, args: Sequence[str]) -> GitResult | str:
        res = self._run(args)
        code = "x" if isinstance(res, str) else str(res.returncode)
        tail = "" if self._ok(res) else f": {_first_line(res)}"
        self._steps.append(f"git {' '.join(args)} -> {code}{tail}")
        return res

    def _check(self) -> DirtyCloneError | None:
        try:
            self._guard_factory(self._repo_dir).check()
        except DirtyCloneError as exc:
            return exc
        return None

    def _default_branch(self) -> str:
        res = self._run(["symbolic-ref", "-q", "refs/remotes/origin/HEAD"])
        if self._ok(res) and not isinstance(res, str):
            return res.stdout.strip().removeprefix("refs/remotes/origin/")
        return FALLBACK_DEFAULT_BRANCH

    def _git_dir(self) -> Path | None:
        res = self._run(["rev-parse", "--absolute-git-dir"])
        if self._ok(res) and not isinstance(res, str):
            return Path(res.stdout.strip())
        return None

    def _result(self, outcome: RetreatOutcome, **kw: object) -> RetreatResult:
        return RetreatResult(outcome, self._thread_id, steps=self._steps, **kw)  # type: ignore[arg-type]

    # -- entry ----------------------------------------------------------------------------------

    def retreat(self) -> RetreatResult:
        verdict = self._check()
        if verdict is None:
            return self._result(RetreatOutcome.CLEAN)
        if verdict.reason not in (DirtyCloneReason.DIRTY_TREE, DirtyCloneReason.OP_IN_PROGRESS):
            return self._result(
                RetreatOutcome.NOT_ELIGIBLE,
                detail=f"guard reason is {verdict.reason.value}: {verdict.detail}".rstrip(": "),
            )
        default = self._default_branch()
        why_not = self._retreat_blocker(verdict)
        if why_not is None:
            done = self._try_retreat(default)
            if isinstance(done, RetreatResult):
                return done
            why_not = done
        return self._discard(default, why_not)

    def _retreat_blocker(self, verdict: DirtyCloneError) -> str | None:
        if verdict.reason is DirtyCloneReason.OP_IN_PROGRESS:
            return f"op in progress ({verdict.detail})"
        unmerged = self._run(["ls-files", "-u"])
        if not self._ok(unmerged):
            return f"ls-files -u: {_first_line(unmerged)}"
        if not isinstance(unmerged, str) and unmerged.stdout.strip():
            return "unmerged paths in the index"
        return None

    def _try_retreat(self, default: str) -> RetreatResult | str:
        """Commit the WIP to a ``wip/`` branch. A string return names why it could not."""
        branch = f"{WIP_PREFIX}{_safe(self._thread_id)}-{self._ts}"
        if not self._ok(self._step(["switch", "-c", branch])):
            return "switch -c to the wip branch failed"
        if not self._ok(self._step(["add", "-A"])):
            return self._undo_branch(branch, "git add -A failed")
        message = (
            f"WIP retreat by {WRAPPER_NAME}: thread {self._thread_id}\n\n"
            f"thread_id: {self._thread_id}\nhead_msg: {self._head_msg}\n"
            f"stop_reason: {self._stop_reason}\n"
        )
        committed = self._step(
            [
                "-c",
                f"user.name={WRAPPER_NAME}",
                "-c",
                f"user.email={WRAPPER_EMAIL}",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--no-verify",
                "-q",
                "-m",
                message,
            ]
        )
        if not self._ok(committed):
            return self._undo_branch(branch, "commit failed")
        files = ins = dels = 0
        stat = self._run(["diff", "--shortstat", "HEAD~1", "HEAD"])
        if self._ok(stat) and not isinstance(stat, str):
            nums = {
                m.group(2)[0]: int(m.group(1))
                for m in re.finditer(r"(\d+) (file|insertion|deletion)", stat.stdout)
            }
            files, ins, dels = nums.get("f", 0), nums.get("i", 0), nums.get("d", 0)
        pushed = self._ok(self._step(["push", "origin", branch]))
        if not self._ok(self._step(["switch", default])):
            # The WIP is safe in a commit; the guard keeps the clone parked as wrong_head.
            return self._result(
                RetreatOutcome.FAILED,
                branch=branch,
                pushed=pushed,
                files=files,
                insertions=ins,
                deletions=dels,
                detail=f"committed to {branch} but could not switch back to {default}",
            )
        left = self._check()
        return self._result(
            RetreatOutcome.RETREATED if left is None else RetreatOutcome.FAILED,
            branch=branch,
            pushed=pushed,
            files=files,
            insertions=ins,
            deletions=dels,
            detail="" if left is None else f"still refused after retreat: {left}",
        )

    def _undo_branch(self, branch: str, why: str) -> str:
        """Before the commit the wip branch holds no new commit; drop it once off it."""
        self._steps.append(f"retreat abandoned: {why}")
        self._pending_branch_cleanup = branch
        return why

    # -- export, then discard -------------------------------------------------------------------

    def _export(self) -> tuple[str | None, str | None, list[str]]:
        notes: list[str] = []
        stem = f"{_safe(self._repo_dir.name)}-discarded-{_safe(self._thread_id)}-{self._ts}"
        patch_path = self._state_dir / f"{stem}.patch"
        list_path = self._state_dir / f"{stem}.untracked.txt"
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            notes.append(f"state dir: {exc}")
        patch_out: str | None = None
        diff = self._run(["diff", "--binary", "HEAD"])
        if self._ok(diff) and not isinstance(diff, str):
            try:
                patch_path.write_text(diff.stdout, encoding="utf-8", newline="\n")
                patch_out = str(patch_path)
            except OSError as exc:
                notes.append(f"patch write: {exc}")
        else:
            notes.append(f"git diff HEAD: {_first_line(diff)}")
        header = [
            f"# thread_id: {self._thread_id}",
            f"# head_msg: {self._head_msg}",
            f"# stop_reason: {self._stop_reason}",
        ]
        git_dir = self._git_dir()
        ops = [
            name for marker, name in OP_NAMES if git_dir is not None and (git_dir / marker).exists()
        ]
        header.append(f"# op_in_progress: {', '.join(ops) if ops else 'none'}")
        for ref in STATE_REFS:
            sha = self._run(["rev-parse", "--verify", "-q", ref])
            value = sha.stdout.strip() if self._ok(sha) and not isinstance(sha, str) else "-"
            header.append(f"# {ref}: {value}")
        untracked = self._run(["ls-files", "--others", "--exclude-standard"])
        paths = (
            untracked.stdout.splitlines()
            if self._ok(untracked) and not isinstance(untracked, str)
            else [f"# (untracked list unavailable: {_first_line(untracked)})"]
        )
        list_out: str | None = None
        try:
            list_path.write_text("\n".join([*header, *paths]) + "\n", encoding="utf-8")
            list_out = str(list_path)
        except OSError as exc:
            notes.append(f"untracked write: {exc}")
        return patch_out, list_out, notes

    def _discard(self, default: str, why: str) -> RetreatResult:
        # Step 3 first: nothing destructive has run yet. A failed retreat attempt may have switched
        # to the wip branch (the tree is carried over unchanged) and, when it failed at ``commit``,
        # already run ``git add -A``: then the index holds every change, new files included, and
        # those staged new files are no longer "untracked". ``git diff --binary HEAD`` compares
        # HEAD with the working tree, index entries included, so it still writes them into the
        # patch (pinned by test_failed_commit_keeps_staged_new_file_in_patch); the .untracked.txt
        # list names only what ``git add -A`` had not staged.
        patch, untracked, notes = self._export()
        for args in ABORT_COMMANDS:  # Step 4: all five, exit codes ignored
            self._step(args)
        status = self._run(["status", "--porcelain=v1", "--untracked-files=normal"])
        if not (self._ok(status) and not isinstance(status, str) and not status.stdout.strip()):
            self._step(["reset", "--hard", "-q"])
            self._step(["clean", "-fd", "-q"])
        self._step(["switch", default])
        if self._pending_branch_cleanup:
            self._step(["branch", "-D", self._pending_branch_cleanup])
        left = self._check()
        detail = "; ".join([f"retreat not possible: {why}", *notes])
        if left is None:
            return self._result(
                RetreatOutcome.DISCARDED, patch=patch, untracked=untracked, detail=detail
            )
        marker = left.detail.removesuffix(" present") if left.detail else left.reason.value
        return self._result(
            RetreatOutcome.DISCARD_INCOMPLETE,
            patch=patch,
            untracked=untracked,
            marker=marker,
            detail=f"{detail}; guard still refuses: {left.reason.value}",
        )


def run(repo_dir: Path, *, thread_id: str, head_msg: str, stop_reason: str, state_dir: Path) -> int:
    """``mindwire clone-retreat``: print one row, return the outcome's exit code."""
    try:
        result = CloneRetreater(
            repo_dir,
            thread_id=thread_id,
            head_msg=head_msg,
            stop_reason=stop_reason,
            state_dir=state_dir,
        ).retreat()
    except Exception as exc:
        summary = " ".join(str(exc).split()) or "-"
        result = RetreatResult(
            RetreatOutcome.FAILED, thread_id, detail=f"{type(exc).__name__}: {summary}"
        )
    sys.stdout.write(result.row() + "\n")
    sys.stdout.flush()
    # PR-1b (v1.7): the durable record the thread's next implementer turn is shown. Observation,
    # never gating: a failed write is one stderr line and the exit code stays the outcome's.
    try:
        record_result(
            state_dir / CLONE_RETREAT_LOG_FILENAME,
            result,
            repo_dir=repo_dir,
            head_msg=head_msg,
            stop_reason=stop_reason,
        )
    except Exception as exc:
        sys.stderr.write(f"clone-retreat: writing the retreat record failed: {exc}\n")
    return result.exit_code


__all__ = [
    "ABORT_COMMANDS",
    "FAILED_EXIT",
    "INCOMPLETE_EXIT",
    "OK_EXIT",
    "RETREAT_PREFIX",
    "CloneRetreater",
    "RetreatOutcome",
    "RetreatResult",
    "git_env",
    "run",
]
