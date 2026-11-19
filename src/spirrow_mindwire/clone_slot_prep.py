"""Borrowing a pooled clone slot for one turn (T-ephemeral-impl-clone-slot-per-thread, A2).

:meth:`SlotPrep.borrow` runs right before the conductor launches a turn (Bohr msg-9386 D-1 改訂 2):

1. The repo's bare mirror ``<slots_root>/<repo>.git`` is created (``clone --mirror``) or fetched
   once (D-2). A failed fetch of an existing mirror is not fatal: the mirror is a local cache and a
   stale tip can only make a later push non-fast-forward, never lose work.
2. A slot is leased (:class:`~.clone_slot_lease.LeaseStore`). No free slot: the turn is not
   launched and not parked (改訂 4). Leases kept after a wrapper crash or a failed quarantine are
   returned in :attr:`PrepResult.kept` for the notice (A3 sends it).
3. The borrow-time check (改訂 5, revised by msg-9402 Obj-36-1 §3 and msg-9404 Obj-37-1 §4):
   no ``index.lock``, no operation in progress (``MERGE_HEAD``, ``REBASE_HEAD``, ``rebase-merge``,
   ``rebase-apply``, ``CHERRY_PICK_HEAD``, ``REVERT_HEAD``), and :meth:`SlotSaver.judge` against
   ``mirror`` (explicit-refspec fetch, status, ``rev-list --branches HEAD --not --remotes=mirror``,
   no stash). HEAD being some other thread's branch is normal and is not checked.
4. Passed: switch to the thread's branch from the mirror tip, then delete every other local branch
   whose tip is confirmed to be in the mirror's refs (msg-9402 Obj-36-1 §3). A branch that is not
   confirmed is kept (the judge has just said it is saved, so this only happens on a race).
5. Failed: the previous borrower's work is rescued with :meth:`SlotSaver.rescue` — the same
   function as the turn-end check — to ``refs/heads/rescue/<previous thread>/<this turn>/<NN>`` on
   ``origin``; ``recoverable`` is decided (msg-9391 Obj-33-2, msg-9393 Obj-34-2); the slot is
   renamed into ``_quarantine/<repo>/`` (``-retryN`` on failure, D-3) and provisioned anew from the
   mirror. A failed rescue does not stop the quarantine and the rebuild (改訂 6); it is recorded
   as ``recoverable: false``. A rename that fails every retry marks the lease
   ``quarantine_failed`` and parks this thread only with ``clone_slot_unrecoverable``.
6. The trust check is read-only (改訂 7): an untrusted slot path parks this turn with
   ``workspace-untrusted`` and the lease is returned.

Nothing here deletes a directory: quarantine is a rename. ``reset --hard`` is never used. What
this module does NOT do: the finally wiring, notices, ``reap_orphans`` (A3), or the conductor's
switch to the ledger-based continuation decision (A4).
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .clone_guard import GitResult, GitRunner
from .clone_retreat import git_env
from .clone_slot import (
    PARK_PROVISION_FAILED,
    PARK_UNRECOVERABLE,
    PARK_WORKSPACE_UNTRUSTED,
    SlotRef,
    mirror_path,
    quarantine_root,
)
from .clone_slot_lease import AcquireResult, Lease, LeaseStore, UnreturnedVerdict
from .clone_slot_save import SaveResult, SlotSaver, rescue_ref
from .conductor.continuation_ledger import SlotTurnOutcome

__all__ = [
    "MIRROR_REMOTE",
    "PrepOutcome",
    "PrepResult",
    "QuarantineInfo",
    "SlotPrep",
    "judge_recoverable",
    "rename_to_quarantine",
]

MIRROR_REMOTE = "mirror"
PUSH_REMOTE = "origin"
UNKNOWN_BORROWER = "unknown-borrower"
RENAME_RETRIES = 2
CLONE_TIMEOUT_S = 600.0
_OP_MARKERS = (
    ("MERGE_HEAD", "merge_in_progress"),
    ("REBASE_HEAD", "merge_in_progress"),
    ("rebase-merge", "merge_in_progress"),
    ("rebase-apply", "merge_in_progress"),
    ("CHERRY_PICK_HEAD", "merge_in_progress"),
    ("REVERT_HEAD", "merge_in_progress"),
)


class PrepOutcome(StrEnum):
    READY = "ready"  # the slot is leased, checked, and on the thread's branch
    WAITING = "waiting"  # no free slot: not launched, not parked
    PARKED = "parked"  # this thread only


@dataclass(frozen=True)
class QuarantineInfo:
    reason: str  # a D-7 reason (clone_retreat_log.QUARANTINE_REASONS)
    previous_thread: str
    quarantine_path: Path | None
    rescue: SaveResult | None
    recoverable: bool
    recoverable_reason: str | None
    detail: str = ""


@dataclass(frozen=True)
class PrepResult:
    outcome: PrepOutcome
    slot: SlotRef | None = None
    lease: Lease | None = None
    park_code: str | None = None
    detail: str = ""
    acquire: AcquireResult | None = None
    quarantine: QuarantineInfo | None = None
    provisioned: bool = False
    clone_seconds: float | None = None
    deleted_branches: tuple[str, ...] = ()
    kept: list[tuple[Lease, UnreturnedVerdict]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        q = self.quarantine
        return {
            "outcome": self.outcome.value,
            "slot": None if self.slot is None else str(self.slot.path),
            "slot_name": None if self.slot is None else self.slot.name,
            "turn_id": None if self.lease is None else self.lease.turn_id,
            "park_code": self.park_code,
            "detail": self.detail,
            "waiting_reason": None if self.acquire is None else self.acquire.reason,
            "waiting_since": None if self.acquire is None else self.acquire.waiting_since,
            "wait_notice": bool(self.acquire is not None and self.acquire.notice),
            "provisioned": self.provisioned,
            "clone_seconds": self.clone_seconds,
            "deleted_branches": list(self.deleted_branches),
            "quarantine": None
            if q is None
            else {
                "reason": q.reason,
                "previous_thread": q.previous_thread,
                "quarantine_path": None if q.quarantine_path is None else str(q.quarantine_path),
                "rescue_outcome": None if q.rescue is None else q.rescue.outcome.value,
                "rescue_refs": [] if q.rescue is None else q.rescue.refs,
                "rescue_detail": None if q.rescue is None else q.rescue.detail,
                "recoverable": q.recoverable,
                "recoverable_reason": q.recoverable_reason,
                "detail": q.detail,
            },
            "kept": [
                {
                    "slot": lease.slot,
                    "thread_id": lease.thread_id,
                    "turn_id": lease.turn_id,
                    "state": lease.state.value,
                    "reason": v.reason,
                }
                for lease, v in self.kept
            ],
        }


def _subprocess_git(cwd: Path, args: Sequence[str]) -> GitResult:
    env = git_env()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=CLONE_TIMEOUT_S,
        env=env,
    )
    return GitResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _first_line(res: GitResult) -> str:
    lines = (res.stderr or res.stdout).strip().splitlines()
    return lines[0] if lines else f"exit {res.returncode}"


def _call(git: GitRunner, cwd: Path, *args: str) -> GitResult:
    try:
        return git(cwd, args)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitResult(-1, "", f"{type(exc).__name__}: {exc}")


#: How deep :func:`judge_recoverable` follows submodules inside submodules. Deeper is unverified.
SUBMODULE_MAX_DEPTH = 8


def judge_recoverable(slot: Path, git: GitRunner = _subprocess_git) -> tuple[bool, str | None]:
    """``recoverable`` of a slot whose work is saved (msg-9391 Obj-33-2, msg-9393 Obj-34-2).

    True only when the status (submodules included) is empty, every gitlink (mode 160000) in the
    HEAD tree and in the index is a ``.gitmodules`` submodule, and for each of those the
    submodule's HEAD and the commit the parent records are both on the submodule's ``origin``
    after a fetch. The same check is applied inside every submodule, recursively (PR #589 gate
    round 1, Obj-1-3: ``ls-tree -r`` does not descend into gitlinks, so a published submodule
    commit can still point at a nested commit that exists only in this slot). A nested submodule
    without its own repository, or nesting deeper than :data:`SUBMODULE_MAX_DEPTH`, cannot be
    confirmed. Anything that cannot be confirmed is ``False`` with ``embedded_repo`` or
    ``submodule_unverified`` (or ``status_unverified``).
    """
    verdict = _judge_gitlinks(slot, git, depth=0)
    if verdict is not None:
        return False, verdict
    status = _call(
        git, slot, "status", "--porcelain", "--ignore-submodules=none", "--untracked-files=all"
    )
    if status.returncode != 0 or status.stdout.strip():
        return False, "status_unverified"
    return True, None


def _judge_gitlinks(repo: Path, git: GitRunner, *, depth: int) -> str | None:
    """``None`` when every gitlink of ``repo`` (and, recursively, of each submodule) is a declared
    submodule whose HEAD and recorded commits are on that submodule's ``origin``; otherwise the
    ``recoverable_reason``."""
    links: dict[str, set[str]] = {}
    tree = _call(git, repo, "ls-tree", "-r", "-z", "HEAD")
    index = _call(git, repo, "ls-files", "-s", "-z")
    if tree.returncode != 0 or index.returncode != 0:
        return "submodule_unverified"
    for entry in tree.stdout.split("\0"):
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[0] == "160000":
            links.setdefault(path, set()).add(parts[2])
    for entry in index.stdout.split("\0"):
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[0] == "160000":
            links.setdefault(path, set()).add(parts[1])
    if not links:
        return None
    if depth >= SUBMODULE_MAX_DEPTH:
        return "submodule_unverified"
    declared: set[str] = set()
    if (repo / ".gitmodules").is_file():
        cfg = _call(
            git, repo, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"
        )
        if cfg.returncode not in (0, 1):
            return "submodule_unverified"
        for line in cfg.stdout.splitlines():
            _, _, p = line.partition(" ")
            if p.strip():
                declared.add(p.strip())
    if any(path not in declared for path in links):
        return "embedded_repo"
    for path, recorded in sorted(links.items()):
        sub = repo / path
        if not (sub / ".git").exists():
            # Without its own repository, git -C would answer for the parent instead.
            return "submodule_unverified"
        fetch = _call(
            git,
            sub,
            "fetch",
            "--prune",
            "--no-tags",
            "origin",
            "+refs/heads/*:refs/remotes/origin/*",
        )
        if fetch.returncode != 0:
            return "submodule_unverified"
        outside = _call(
            git, sub, "rev-list", "-n", "1", "HEAD", *sorted(recorded), "--not", "--remotes=origin"
        )
        if outside.returncode != 0 or outside.stdout.strip():
            return "submodule_unverified"
        nested = _judge_gitlinks(sub, git, depth=depth + 1)
        if nested is not None:
            return nested
    return None


def rename_to_quarantine(
    ref: SlotRef,
    who: str,
    now: datetime,
    rename: Callable[[Path, Path], None] = os.rename,
) -> tuple[Path | None, list[str]]:
    """Rename the slot to ``_quarantine/<repo>/<slot>-<who>-<stamp>``, then ``-retryN`` (D-3).

    Returns the new path, or ``None`` with one error per attempt when every attempt failed. Shared
    by the borrow-time check and the turn-end rescue failure (msg-9400 §2, msg-9402 Obj-36-4).
    """
    root = quarantine_root(ref.slots_root, ref.repo)
    root.mkdir(parents=True, exist_ok=True)
    base_name = f"{ref.name}-{who}-{now.strftime('%Y%m%d-%H%M%S')}"
    errors: list[str] = []
    for attempt in range(RENAME_RETRIES + 1):
        target = root / (base_name if attempt == 0 else f"{base_name}-retry{attempt}")
        if target.exists():
            errors.append(f"{target.name} exists")
            continue
        try:
            rename(ref.path, target)
        except OSError as exc:
            errors.append(f"{target.name}: {exc}")
            continue
        return target, errors
    return None, errors


class SlotPrep:
    """Borrow-time procedure of one repo's slot pool. Every git call goes through ``git``."""

    def __init__(
        self,
        *,
        slots_root: Path,
        repo: str,
        origin_url: str,
        store: LeaseStore,
        slots_per_repo: int,
        git: GitRunner = _subprocess_git,
        rename: Callable[[Path, Path], None] = os.rename,
        trusted: Callable[[Path], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.slots_root = slots_root
        self.repo = repo
        self.origin_url = origin_url
        self.store = store
        self.slots_per_repo = slots_per_repo
        self._git = git
        self._rename = rename
        self._trusted = trusted
        self._clock = clock

    # -- mirror ----------------------------------------------------------------------------

    def _ensure_mirror(self) -> str | None:
        """Create or fetch the mirror. Returns an error only when there is no usable mirror."""
        mirror = mirror_path(self.slots_root, self.repo)
        if (mirror / "HEAD").is_file():
            _call(self._git, mirror, "fetch", "--prune", "origin")  # stale is not fatal
            return None
        mirror.parent.mkdir(parents=True, exist_ok=True)
        res = _call(self._git, mirror.parent, "clone", "--mirror", self.origin_url, mirror.name)
        return None if res.returncode == 0 else f"mirror clone failed: {_first_line(res)}"

    def _mirror_default(self) -> str | None:
        """The default branch the bare mirror's ``HEAD`` names (``--base`` not given)."""
        res = _call(
            self._git, mirror_path(self.slots_root, self.repo), "symbolic-ref", "--short", "HEAD"
        )
        name = res.stdout.strip()
        return name if res.returncode == 0 and name else None

    # -- borrow ----------------------------------------------------------------------------

    def borrow(
        self,
        *,
        thread_id: str,
        turn_id: str,
        branch: str,
        base: str | None,
        wrapper_pid: int | None = None,
        session_name: str | None = None,
        job_name: str | None = None,
    ) -> PrepResult:
        kept = self.store.unreturned(self.repo)
        acq = self.store.acquire(
            self.repo,
            self.slots_per_repo,
            thread_id=thread_id,
            turn_id=turn_id,
            now=self._clock(),
            wrapper_pid=wrapper_pid,
            session_name=session_name,
            job_name=job_name,
        )
        if acq.lease is None:
            return PrepResult(PrepOutcome.WAITING, acquire=acq, detail=acq.reason or "", kept=kept)
        lease = acq.lease
        ref = SlotRef(self.slots_root, self.repo, lease.index)
        try:
            return self._prepare(ref, lease, acq, kept, branch=branch, base=base)
        except Exception:
            # Never leave a lease behind for a turn that will not launch.
            self.store.release(self.repo, lease.slot, turn_id)
            raise

    def _park(self, ref: SlotRef, lease: Lease, code: str, detail: str, **kw: Any) -> PrepResult:
        self.store.release(self.repo, lease.slot, lease.turn_id)
        return PrepResult(PrepOutcome.PARKED, ref, lease, park_code=code, detail=detail, **kw)

    def _prepare(
        self,
        ref: SlotRef,
        lease: Lease,
        acq: AcquireResult,
        kept: list[tuple[Lease, UnreturnedVerdict]],
        *,
        branch: str,
        base: str | None,
    ) -> PrepResult:
        err = self._ensure_mirror()
        if err is not None:
            return self._park(ref, lease, PARK_PROVISION_FAILED, err, acquire=acq, kept=kept)
        slot = ref.path
        quarantine: QuarantineInfo | None = None
        provisioned = False
        clone_seconds: float | None = None
        if slot.exists():
            reason = self._check(slot)
            if reason is not None:
                quarantine = self._quarantine(ref, lease, reason)
                if quarantine.quarantine_path is None:
                    self.store.mark_quarantine_failed(self.repo, lease.slot, lease.turn_id)
                    return PrepResult(
                        PrepOutcome.PARKED,
                        ref,
                        lease,
                        park_code=PARK_UNRECOVERABLE,
                        detail=f"{slot}: {quarantine.detail}",
                        acquire=acq,
                        quarantine=quarantine,
                        kept=kept,
                    )
        if not slot.exists():
            t0 = time.monotonic()
            err = self._provision(slot)
            clone_seconds = round(time.monotonic() - t0, 3)
            provisioned = True
            if err is not None:
                return self._park(
                    ref,
                    lease,
                    PARK_PROVISION_FAILED,
                    f"{slot}: {err}",
                    acquire=acq,
                    quarantine=quarantine,
                    kept=kept,
                )
        if base is None:
            base = self._mirror_default()
            if base is None:
                return self._park(
                    ref,
                    lease,
                    PARK_PROVISION_FAILED,
                    "the mirror's HEAD names no default branch; pass --base",
                    acquire=acq,
                    quarantine=quarantine,
                    kept=kept,
                )
        err = self._switch(slot, branch, base)
        if err is not None:
            return self._park(
                ref,
                lease,
                PARK_PROVISION_FAILED,
                f"{slot}: {err}",
                acquire=acq,
                quarantine=quarantine,
                kept=kept,
            )
        deleted = self._cleanup_branches(slot, branch)
        if self._trusted is not None and not self._trusted(slot):
            return self._park(
                ref,
                lease,
                PARK_WORKSPACE_UNTRUSTED,
                f"{slot} has no trusted entry in Claude Code's config; see docs/deploy.md",
                acquire=acq,
                quarantine=quarantine,
                kept=kept,
            )
        return PrepResult(
            PrepOutcome.READY,
            ref,
            lease,
            acquire=acq,
            quarantine=quarantine,
            provisioned=provisioned,
            clone_seconds=clone_seconds,
            deleted_branches=deleted,
            kept=kept,
        )

    # -- check -----------------------------------------------------------------------------

    def _git_dir(self, slot: Path) -> Path | None:
        res = _call(self._git, slot, "rev-parse", "--absolute-git-dir")
        if res.returncode != 0 or not res.stdout.strip():
            return None
        return Path(res.stdout.strip())

    def _check(self, slot: Path) -> str | None:
        """The D-7 reason the slot fails the borrow-time check, or ``None`` when it passes."""
        git_dir = self._git_dir(slot)
        if git_dir is None:
            return "provision_failed"
        if (git_dir / "index.lock").exists():
            return "index_lock"
        for marker, reason in _OP_MARKERS:
            if (git_dir / marker).exists():
                return reason
        judgement = SlotSaver(slot, git=self._git).judge(MIRROR_REMOTE)
        if judgement.saved:
            return None
        if "dirty" in judgement.reasons:
            status = _call(self._git, slot, "status", "--porcelain", "--untracked-files=all").stdout
            lines = [ln for ln in status.splitlines() if ln.strip()]
            only_untracked = bool(lines) and all(ln.startswith("??") for ln in lines)
            return "untracked_only" if only_untracked else "modified"
        return "unpublished"

    # -- quarantine ------------------------------------------------------------------------

    def _previous_thread(self, slot: Path, lease: Lease) -> str:
        """Who left the slot (lease record), when it names a valid ref component."""
        prev = lease.previous_thread_id
        if prev is not None:
            try:
                rescue_ref(prev, lease.turn_id, 1)
                return prev
            except ValueError:
                pass
        return UNKNOWN_BORROWER

    def _quarantine(self, ref: SlotRef, lease: Lease, reason: str) -> QuarantineInfo:
        slot = ref.path
        prev = self._previous_thread(slot, lease)
        rescue: SaveResult | None = None
        recoverable = False
        rec_reason: str | None = "rescue_failed"
        if reason != "provision_failed":
            saver = SlotSaver(slot, git=self._git, push_remote=PUSH_REMOTE)
            judgement = saver.judge(MIRROR_REMOTE)
            rescue = saver.rescue(prev, lease.turn_id, judgement, MIRROR_REMOTE)
            if rescue.outcome is SlotTurnOutcome.RESCUED:
                recoverable, rec_reason = judge_recoverable(slot, self._git)
        else:
            rec_reason = "not_a_repository"
        target, errors = rename_to_quarantine(ref, prev, self._clock(), self._rename)
        if target is not None:
            return QuarantineInfo(reason, prev, target, rescue, recoverable, rec_reason)
        return QuarantineInfo(
            reason,
            prev,
            None,
            rescue,
            False,
            rec_reason or "quarantine_failed",
            detail="rename failed: " + "; ".join(errors),
        )

    # -- provision / switch / cleanup ------------------------------------------------------

    def _provision(self, slot: Path) -> str | None:
        mirror = mirror_path(self.slots_root, self.repo)
        slot.parent.mkdir(parents=True, exist_ok=True)
        res = _call(
            self._git,
            slot.parent,
            "clone",
            "--no-checkout",
            "--origin",
            MIRROR_REMOTE,
            str(mirror),
            slot.name,
        )
        if res.returncode != 0:
            return f"clone from mirror failed: {_first_line(res)}"
        res = _call(self._git, slot, "remote", "add", PUSH_REMOTE, self.origin_url)
        if res.returncode != 0:
            return f"remote add failed: {_first_line(res)}"
        return None

    def _switch(self, slot: Path, branch: str, base: str) -> str | None:
        fetch = _call(
            self._git,
            slot,
            "fetch",
            "--prune",
            "--no-tags",
            MIRROR_REMOTE,
            f"+refs/heads/*:refs/remotes/{MIRROR_REMOTE}/*",
        )
        if fetch.returncode != 0:
            return f"fetch from mirror failed: {_first_line(fetch)}"
        start = f"{MIRROR_REMOTE}/{branch}"
        has = _call(self._git, slot, "rev-parse", "-q", "--verify", f"refs/remotes/{start}")
        if has.returncode != 0:
            start = f"{MIRROR_REMOTE}/{base}"
        # The check has just confirmed nothing local is unsaved, so -C only moves a branch whose
        # commits are all in the mirror.
        res = _call(self._git, slot, "switch", "--no-track", "-C", branch, start)
        if res.returncode != 0:
            return f"switch to {branch} from {start} failed: {_first_line(res)}"
        return None

    def _cleanup_branches(self, slot: Path, own: str) -> tuple[str, ...]:
        """Delete other local branches confirmed to be in the mirror (msg-9402 Obj-36-1 §3)."""
        listing = _call(
            self._git, slot, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/"
        )
        if listing.returncode != 0:
            return ()
        deleted: list[str] = []
        for line in listing.stdout.splitlines():
            sha, _, refname = line.partition(" ")
            name = refname.removeprefix("refs/heads/")
            if not refname.startswith("refs/heads/") or name == own:
                continue
            outside = _call(
                self._git, slot, "rev-list", "-n", "1", sha, "--not", f"--remotes={MIRROR_REMOTE}"
            )
            if outside.returncode != 0 or outside.stdout.strip():
                continue
            res = _call(self._git, slot, "branch", "-D", "--", name)
            if res.returncode == 0:
                deleted.append(name)
        return tuple(deleted)
