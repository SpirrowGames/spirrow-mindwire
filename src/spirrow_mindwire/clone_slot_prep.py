"""Turn-pre procedure for a per-thread clone slot: steps 0-7, the slot lock, the quarantine check.

Thread T-ephemeral-impl-clone-slot-per-thread, PR-A. Specified by Bohr msg-8061 (consolidated
spec: layout, lock, steps 1-7), msg-8068 (v2.3: step 0 and the bundle-before-push order),
msg-8070 (v2.4: owner classification table), msg-8084/8086 (R-7'/R-8' records), msg-8088 (v2.7:
``pending_quarantine`` before the rename and its reconciliation on resume). Naysayer go msg-8107
limits this work to PR-A/PR-B; D-6 (deleting quarantined slots) is out of scope.

**Nothing is ever deleted, reset, stashed or cleaned.** A slot that fails the guard is preserved
(patch / untracked list / bundle under ``state/clone-retreats/``) and then *renamed* into
``<slots_root>/_quarantine/<repo>/``; a fresh slot is cloned from the bare mirror at the same path.
Every failure that this procedure cannot settle leaves the original where it is and either skips
the tick (:attr:`PrepAction.SKIP`, no row) or parks only this thread (:attr:`PrepAction.PARK`).

What the caller supplies (so the module stays testable without the loop):

* ``origin_url`` — the GitHub remote the slot's ``origin`` is pointed back to after the clone;
* ``thread_branch`` — the thread's ``loop/...`` name (``git_sync.branch.generation_name``);
* ``sessions_under(slot_dir) -> int`` — the ``claude_bg`` sessions whose cwd is under the slot
  (step 5 refuses to rename while one is alive);
* ``prepare_slot(slot_dir)`` — the A-4 preparation beyond ``ensure_pin_ignored`` (gate bootstrap,
  hooks preflight P3 reads, trust registration). Raising from it fails the provision.

Optional owner-file fields this module adds beyond the v2.4 required four (all optional, so the
classification in :mod:`clone_slot` is unchanged): ``pending_quarantine`` and
``original_quarantine_path`` (v2.7), plus three bookkeeping fields the spec needs persisted across
ticks but does not name — ``fetch_failures`` / ``provision_failures`` (the "3 tick 続けて"
counters) and ``quarantine_context`` (the reason and saved artefacts of the original, so the row
written when the outcome is finally decided carries them, msg-8086 "行を書くタイミング").
"""

from __future__ import annotations

import contextlib
import enum
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spirrow_mindwire.clone_guard import (
    CloneGuard,
    DirtyCloneError,
    DirtyCloneReason,
    GitResult,
)
from spirrow_mindwire.clone_retreat_log import (
    SLOT_ACTION_PARKED,
    SLOT_ACTION_REBUILT,
    append_slot_quarantine,
    build_slot_quarantine_record,
)
from spirrow_mindwire.clone_slot import (
    PARK_GIT_DIVERGED,
    PARK_GIT_PUSH_FAILED,
    PARK_OWNER_UNKNOWN,
    PARK_PRESERVE_FAILED,
    PARK_PROVISION_FAILED,
    PARK_QUARANTINE_UNCONFIRMED,
    PARK_UNPUBLISHED,
    PARK_UNRECOVERABLE,
    EntryRoute,
    OwnerState,
    SlotPaths,
    choose_slot,
    entry_route,
    mirror_path,
    quarantine_root,
    read_owner,
    thread_hash12,
    update_owner,
)
from spirrow_mindwire.git_sync.branch import ProtectedBranchError, ThreadBranch
from spirrow_mindwire.pin_ignore import ensure_pin_ignored

NETWORK_TIMEOUT_S = 40.0  # msg-8061 step 6: network git ops
LOCAL_TIMEOUT_S = 10.0  # msg-8061 step 6: local git ops
FAILURE_TICKS_TO_PARK = 3  # msg-8061 steps 1 and 6
RENAME_ATTEMPTS = 2  # msg-8061 step 5: two consecutive rename failures park
NETWORK_COMMANDS = frozenset({"fetch", "push", "ls-remote"})


class PrepAction(enum.StrEnum):
    LAUNCH = "launch"  # step 7 holds: owner matches and complete: true
    SKIP = "skip"  # do not launch this tick; nothing parked, no row
    PARK = "park"  # park this thread only, with park_code


@dataclass(frozen=True)
class PrepResult:
    action: PrepAction
    slot_dir: Path | None
    park_code: str | None = None
    detail: str = ""
    record: dict[str, Any] | None = None
    steps: tuple[str, ...] = ()


GitCall = Callable[[Path, Sequence[str], float], GitResult]


def _subprocess_git(cwd: Path, args: Sequence[str], timeout: float) -> GitResult:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        env=env,
    )
    return GitResult(proc.returncode, proc.stdout or "", proc.stderr or "")


# -- locks ------------------------------------------------------------------------------------


class SlotLockBusyError(Exception):
    """Another process holds the lock; the caller skips this tick."""


def _try_lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Non-blocking OS exclusive lock on ``path`` (msg-8061 ロック).

    Raises :class:`SlotLockBusyError` when another process holds it. The lock file is never
    deleted (msg-8061 "やらないこと"); only the OS lock is released.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        try:
            _try_lock(fd)
        except OSError as exc:
            raise SlotLockBusyError(str(path)) from exc
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def mirror_lock_path(slots_root: Path, repo: str) -> Path:
    return slots_root / f"{repo}.git.lock"


def refresh_mirror(
    slots_root: Path, repo: str, origin_url: str, *, git: GitCall = _subprocess_git
) -> bool:
    """Create (``clone --bare``) or fetch the repo's mirror under the repo lock (msg-8061 配置).

    Refspec ``+refs/heads/*:refs/heads/*`` only, never ``--mirror``. A failure keeps the old
    mirror and parks nobody: returns ``False``. Once per tick per repo is the caller's cadence.
    """
    mirror = mirror_path(slots_root, repo)
    try:
        with exclusive_lock(mirror_lock_path(slots_root, repo)):
            try:
                if not mirror.exists():
                    res = git(
                        slots_root,
                        ["clone", "--bare", "--", origin_url, str(mirror)],
                        NETWORK_TIMEOUT_S,
                    )
                    return res.returncode == 0
                res = git(
                    mirror,
                    ["fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*"],
                    NETWORK_TIMEOUT_S,
                )
                return res.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                return False
    except SlotLockBusyError:
        return False


# -- the procedure ----------------------------------------------------------------------------


def _reason_for_guard(exc: DirtyCloneError) -> str:
    """Map a guard refusal to a D-7 ``quarantine_reason`` (msg-8061 記録)."""
    if exc.reason is DirtyCloneReason.DIRTY_TREE:
        if exc.porcelain and all(e.startswith("?? ") for e in exc.porcelain):
            return "untracked_only"
        return "modified"
    if exc.reason is DirtyCloneReason.OP_IN_PROGRESS:
        return "merge_in_progress"
    if exc.reason is DirtyCloneReason.CLONE_BUSY:
        return "index_lock"
    if exc.reason is DirtyCloneReason.WRONG_HEAD:
        return "wrong_head"
    if exc.reason is DirtyCloneReason.PIN_NOT_IGNORED:
        return "modified"
    return "provision_failed"  # git_unavailable: the slot's git itself is unusable


def _dir_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            with contextlib.suppress(OSError):
                total += (Path(dirpath) / f).stat().st_size
    return total


def _bundle_prerequisites(bundle: Path) -> list[str]:
    """``-<sha>`` lines of a v2/v3 bundle header: the commits the bundle does *not* carry."""
    prereqs: list[str] = []
    with bundle.open("rb") as fh:
        fh.readline()  # signature
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line == "":
                break
            if line.startswith("-"):
                prereqs.append(line[1:].split(" ", 1)[0])
    return prereqs


@dataclass
class SlotPrep:
    slots_root: Path
    repo: str
    thread_id: str
    origin_url: str
    thread_branch: str
    default_branch: str
    state_dir: Path
    sessions_under: Callable[[Path], int]
    prepare_slot: Callable[[Path], None] = lambda _slot: None
    git: GitCall = _subprocess_git
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic
    hasher: Callable[[bytes], str] | None = None
    rename: Callable[[Path, Path], None] = os.rename
    guard_factory: Callable[[Path], CloneGuard] = CloneGuard
    _steps: list[str] = field(default_factory=list)

    # -- paths -----------------------------------------------------------------------------

    @property
    def preserve_dir(self) -> Path:
        return self.state_dir / "clone-retreats"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "clone-retreats.jsonl"

    def _stamp(self) -> str:
        return self.now().astimezone(UTC).strftime("%Y%m%d-%H%M%S")

    # -- entry -----------------------------------------------------------------------------

    def run(self) -> PrepResult:
        """Run steps 0-7 for this thread's slot and say whether the turn may launch."""
        self._steps = []
        choice = choose_slot(
            self.slots_root, self.repo, self.thread_id, hasher=self.hasher, now=self.now()
        )
        paths = choice.paths
        try:
            with exclusive_lock(paths.lock_file):
                return self._locked(paths)
        except SlotLockBusyError:
            return self._result(PrepAction.SKIP, paths, detail=f"slot lock held: {paths.lock_file}")

    def _result(
        self,
        action: PrepAction,
        paths: SlotPaths,
        *,
        park_code: str | None = None,
        detail: str = "",
        record: dict[str, Any] | None = None,
    ) -> PrepResult:
        return PrepResult(
            action, paths.slot_dir, park_code, detail, record, steps=tuple(self._steps)
        )

    def _owner(self, paths: SlotPaths) -> dict[str, Any] | None:
        read = read_owner(paths.owner_file, repo=self.repo, thread_id=self.thread_id)
        return dict(read.data) if read.state is OwnerState.MATCH and read.data else None

    def _write_owner(self, paths: SlotPaths, data: dict[str, Any]) -> None:
        update_owner(paths.owner_file, data)

    def _locked(self, paths: SlotPaths) -> PrepResult:
        # Re-read under the lock: choose_slot ran before we held it.
        owner_read = read_owner(paths.owner_file, repo=self.repo, thread_id=self.thread_id)
        owner = dict(owner_read.data) if owner_read.data else None

        # v2.7: reconcile a pending quarantine before looking at ``complete``.
        if owner_read.state is OwnerState.MATCH and owner and owner.get("pending_quarantine"):
            pend = owner["pending_quarantine"]
            dest = Path(pend.get("to", "")) if isinstance(pend, dict) else Path("")
            src_exists = paths.slot_dir.exists()
            dest_exists = bool(str(dest)) and dest.exists()
            if src_exists and not dest_exists:
                self._steps.append("reconcile: rename never happened; pending cleared")
                owner.pop("pending_quarantine", None)
                self._write_owner(paths, owner)
                return self._step1(paths, owner)
            if dest_exists and not src_exists:
                self._steps.append("reconcile: renamed, confirming")
                owner = self._confirm_quarantine(paths, owner, dest)
                return self._provision(paths, owner)
            return self._park(
                paths,
                PARK_QUARANTINE_UNCONFIRMED,
                "quarantine_unconfirmed",
                candidates=[paths.slot_dir, dest],
                detail=(
                    f"pending quarantine unresolved: slot exists={src_exists}, "
                    f"target exists={dest_exists}"
                ),
            )

        route = entry_route(owner_read, paths.slot_dir.exists())
        self._steps.append(f"step0: {route.value}")
        if route is EntryRoute.PARK_OWNER_UNKNOWN:
            return self._park(
                paths,
                PARK_OWNER_UNKNOWN,
                "owner_unknown",
                detail=f"owner={paths.owner_file} slot={paths.slot_dir}",
                touch_owner=False,
            )
        assert owner is not None
        if route is EntryRoute.STEP1:
            return self._step1(paths, owner)
        if route is EntryRoute.QUARANTINE_INCOMPLETE_THEN_PROVISION:
            # v2.3/v2.7: an incomplete slot is mirror content only — no preserve, no pending,
            # original_quarantine_path untouched.
            if self.sessions_under(paths.slot_dir) > 0:
                return self._result(PrepAction.SKIP, paths, detail="claude_bg session in slot")
            moved = self._rename_with_retry(paths)
            if moved is None:
                return self._park(
                    paths,
                    PARK_UNRECOVERABLE,
                    "provision_failed",
                    detail=f"rename of incomplete slot failed twice: {paths.slot_dir}",
                )
        # CREATE_THEN_PROVISION / PROVISION / after the incomplete quarantine
        return self._provision(paths, owner)

    # -- steps 1-5 -------------------------------------------------------------------------

    def _git(self, cwd: Path, args: Sequence[str]) -> GitResult:
        timeout = NETWORK_TIMEOUT_S if args and args[0] in NETWORK_COMMANDS else LOCAL_TIMEOUT_S
        try:
            res = self.git(cwd, args, timeout)
        except subprocess.TimeoutExpired:
            res = GitResult(124, "", f"timed out after {timeout:g}s")
        except OSError as exc:
            res = GitResult(127, "", str(exc))
        self._steps.append(f"git {' '.join(args)} -> {res.returncode}")
        return res

    def _fetch(self, slot: Path) -> bool:
        res = self._git(slot, ["fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"])
        return res.returncode == 0

    def _step1(self, paths: SlotPaths, owner: dict[str, Any]) -> PrepResult:
        slot = paths.slot_dir
        if not self._fetch(slot):
            n = int(owner.get("fetch_failures", 0)) + 1
            owner["fetch_failures"] = n
            self._write_owner(paths, owner)
            if n >= FAILURE_TICKS_TO_PARK:
                return self._park(
                    paths, PARK_PROVISION_FAILED, "provision_failed", detail=f"fetch failed {n}x"
                )
            return self._result(PrepAction.SKIP, paths, detail=f"fetch failed ({n})")
        if owner.pop("fetch_failures", None) is not None:
            self._write_owner(paths, owner)

        # Step 2 (v2.3): unpublished commits -> bundle first, then rescue push or park.
        unpublished = self._unpublished(slot)
        bundle: Path | None = None
        if unpublished is None:
            return self._park(
                paths, PARK_PRESERVE_FAILED, "preserve_failed", detail="rev-list failed"
            )
        if unpublished:
            bundle = self._make_bundle(slot, unpublished)
            if bundle is None:
                return self._park(
                    paths,
                    PARK_PRESERVE_FAILED,
                    "preserve_failed",
                    detail="bundle create/verify failed; original left in place",
                )
            park = self._rescue_push(slot, unpublished)
            if park is not None:
                code, detail = park
                return self._park(paths, code, "unpublished", bundle=bundle, detail=detail)

        # Step 3: guard (default branch or the thread branch, nothing unpublished).
        try:
            self._guard(slot)
        except DirtyCloneError as exc:
            reason = _reason_for_guard(exc)
            self._steps.append(f"guard: {exc.reason.value} -> {reason}")
            return self._preserve_and_quarantine(paths, owner, reason, bundle)
        if bundle is not None:
            # Rescued by push and now clean: still tell the next implementer where the bundle is.
            self._steps.append("rescued by push")
        return self._launch(paths, owner)

    def _unpublished(self, slot: Path) -> list[str] | None:
        head = self._git(slot, ["rev-parse", "--verify", "-q", "HEAD"])
        args = ["rev-list", "--branches", "--not", "--remotes=origin"]
        if head.returncode == 0:
            args.insert(1, "HEAD")
        res = self._git(slot, args)
        if res.returncode != 0:
            return None
        return [s for s in res.stdout.split() if s]

    def _make_bundle(self, slot: Path, unpublished: list[str]) -> Path | None:
        h = thread_hash12(self.thread_id, hasher=self.hasher)
        try:
            self.preserve_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        target = self.preserve_dir / f"{h}-{self._stamp()}.bundle"
        n = 1
        while target.exists():
            target = self.preserve_dir / f"{h}-{self._stamp()}-{n}.bundle"
            n += 1
        head = self._git(slot, ["rev-parse", "--verify", "-q", "HEAD"])
        refs = (["HEAD"] if head.returncode == 0 else []) + ["--branches"]
        res = self._git(slot, ["bundle", "create", str(target), *refs, "--not", "--remotes=origin"])
        if res.returncode != 0 or not target.exists():
            return None
        if self._git(slot, ["bundle", "verify", str(target)]).returncode != 0:
            return None
        heads = self._git(slot, ["bundle", "list-heads", str(target)])
        if heads.returncode != 0:
            return None
        tips = sorted({ln.split()[0] for ln in heads.stdout.splitlines() if ln.strip()})
        try:
            prereqs = _bundle_prerequisites(target)
        except OSError:
            return None
        carried = self._git(
            slot, ["rev-list", *tips, "--not", *prereqs] if prereqs else ["rev-list", *tips]
        )
        if carried.returncode != 0:
            return None
        if not set(unpublished) <= set(carried.stdout.split()):
            self._steps.append("bundle does not carry every unpublished commit")
            return None
        return target

    def _rescue_push(self, slot: Path, unpublished: list[str]) -> tuple[str, str] | None:
        """None when pushed; else ``(park_code, detail)`` (msg-8061 step 2)."""
        tb = self.thread_branch
        head_ref = self._git(slot, ["symbolic-ref", "-q", "HEAD"])
        if head_ref.returncode != 0:
            return PARK_UNPUBLISHED, "unpublished commits on a detached HEAD"
        head = head_ref.stdout.strip().removeprefix("refs/heads/")
        if head != tb:
            return PARK_UNPUBLISHED, f"unpublished commits while HEAD is on {head!r}"
        others = self._git(
            slot, ["rev-list", "--branches", "--not", "--remotes=origin", f"refs/heads/{tb}"]
        )
        if others.returncode != 0 or others.stdout.strip():
            return PARK_UNPUBLISHED, "unpublished commits outside the thread branch"
        try:
            branch = ThreadBranch(tb, default_branch=self.default_branch)
        except ProtectedBranchError as exc:
            return PARK_UNPUBLISHED, str(exc)
        remote = f"refs/remotes/origin/{tb}"
        if self._git(slot, ["rev-parse", "--verify", "-q", remote]).returncode != 0:
            return PARK_UNPUBLISHED, f"origin has no {tb}"
        anc = self._git(slot, ["merge-base", "--is-ancestor", remote, f"refs/heads/{tb}"])
        if anc.returncode != 0:
            return PARK_GIT_DIVERGED, f"origin/{tb} is not an ancestor of {tb}"
        push = self._git(slot, ["push", "origin", branch.refspec])
        if push.returncode != 0:
            return PARK_GIT_PUSH_FAILED, (push.stderr or push.stdout).strip()[:200]
        self._fetch(slot)  # so the guard and the record see origin/<tb> at the pushed tip
        return None

    def _guard(self, slot: Path) -> None:
        ensure_pin_ignored(slot)
        try:
            self.guard_factory(slot).check()
        except DirtyCloneError as exc:
            if exc.reason is DirtyCloneReason.WRONG_HEAD and exc.head == self.thread_branch:
                return  # check() tests HEAD last: everything else already passed
            raise

    def _preserve_and_quarantine(
        self, paths: SlotPaths, owner: dict[str, Any], reason: str, bundle: Path | None
    ) -> PrepResult:
        slot = paths.slot_dir
        # Step 4: patch and untracked list; a missing artefact is None (R-7), not a failure.
        try:
            patch, untracked = self._preserve(slot)
        except _PreserveFailedError as exc:
            return self._park(
                paths, PARK_PRESERVE_FAILED, "preserve_failed", bundle=bundle, detail=str(exc)
            )
        # Step 5: no live session in the slot, then pending -> rename -> confirm (v2.7).
        if self.sessions_under(slot) > 0:
            return self._result(PrepAction.SKIP, paths, detail="claude_bg session in slot")
        owner["quarantine_context"] = {
            "quarantine_reason": reason,
            "patch": None if patch is None else str(patch),
            "untracked": None if untracked is None else str(untracked),
            "bundle": None if bundle is None else str(bundle),
        }
        dest = self._rename_with_retry(paths, owner=owner)
        if dest is None:
            return self._park(
                paths,
                PARK_UNRECOVERABLE,
                reason,
                patch=patch,
                untracked=untracked,
                bundle=bundle,
                detail=f"rename failed twice: {slot}",
            )
        owner = self._confirm_quarantine(paths, owner, dest)
        return self._provision(paths, owner)

    def _preserve(self, slot: Path) -> tuple[Path | None, Path | None]:
        h = thread_hash12(self.thread_id, hasher=self.hasher)
        stamp = self._stamp()
        try:
            self.preserve_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _PreserveFailedError(f"cannot create {self.preserve_dir}: {exc}") from exc
        patch: Path | None = None
        head = self._git(slot, ["rev-parse", "--verify", "-q", "HEAD"])
        if head.returncode == 0:
            diff = self._git(slot, ["diff", "--binary", "HEAD"])
            if diff.returncode != 0:
                raise _PreserveFailedError("git diff --binary HEAD failed")
            if diff.stdout:
                patch = self._write_artefact(f"{h}-{stamp}.patch", diff.stdout)
        others = self._git(slot, ["ls-files", "--others", "--exclude-standard"])
        if others.returncode != 0:
            raise _PreserveFailedError("git ls-files --others failed")
        untracked = (
            self._write_artefact(f"{h}-{stamp}.untracked.txt", others.stdout)
            if others.stdout.strip()
            else None
        )
        return patch, untracked

    def _write_artefact(self, name: str, text: str) -> Path:
        path = self.preserve_dir / name
        try:
            with path.open("x", encoding="utf-8", newline="") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise _PreserveFailedError(f"cannot write {path}: {exc}") from exc
        return path

    def _rename_with_retry(
        self, paths: SlotPaths, *, owner: dict[str, Any] | None = None
    ) -> Path | None:
        """Rename the slot into _quarantine; ``owner`` given = write ``pending_quarantine`` first.

        Two consecutive failures return ``None`` (msg-8061 step 5).
        """
        h = thread_hash12(self.thread_id, hasher=self.hasher)
        qroot = quarantine_root(self.slots_root, self.repo)
        qroot.mkdir(parents=True, exist_ok=True)
        base = f"{h}-{self._stamp()}"
        for attempt in range(RENAME_ATTEMPTS):
            dest = qroot / (base if attempt == 0 else f"{base}-retry{attempt}")
            k = attempt
            while dest.exists():
                k += 1
                dest = qroot / f"{base}-retry{k}"
            if owner is not None:
                owner["pending_quarantine"] = {"to": str(dest), "at": self.now().isoformat()}
                self._write_owner(paths, owner)
            try:
                self.rename(paths.slot_dir, dest)
            except OSError as exc:
                self._steps.append(f"rename {paths.slot_dir} -> {dest} failed: {exc}")
                continue
            self._steps.append(f"rename {paths.slot_dir} -> {dest}")
            return dest
        return None

    def _confirm_quarantine(
        self, paths: SlotPaths, owner: dict[str, Any], dest: Path
    ) -> dict[str, Any]:
        """v2.7 step 3: one replace sets original_quarantine_path, complete false, no pending."""
        owner = dict(owner)
        owner["original_quarantine_path"] = str(dest)
        owner["complete"] = False
        owner.pop("pending_quarantine", None)
        owner["provision_failures"] = 0
        self._write_owner(paths, owner)
        return owner

    # -- step 6 ----------------------------------------------------------------------------

    def _provision(self, paths: SlotPaths, owner: dict[str, Any]) -> PrepResult:
        start = self.monotonic()
        failure = self._provision_steps(paths.slot_dir)
        if failure is not None:
            n = int(owner.get("provision_failures", 0)) + 1
            owner["provision_failures"] = n
            self._write_owner(paths, owner)
            if n >= FAILURE_TICKS_TO_PARK:
                return self._park(
                    paths,
                    PARK_PROVISION_FAILED,
                    "provision_failed",
                    detail=f"provision failed {n}x: {failure}",
                )
            return self._result(PrepAction.SKIP, paths, detail=f"provision failed ({n}): {failure}")
        seconds = round(self.monotonic() - start, 3)
        record = None
        ctx = owner.get("quarantine_context")
        if owner.get("original_quarantine_path") or ctx:
            ctx = ctx if isinstance(ctx, dict) else {}
            record = self._record(
                paths,
                SLOT_ACTION_REBUILT,
                ctx.get("quarantine_reason") or "provision_failed",
                quarantine_path=owner.get("original_quarantine_path"),
                patch=ctx.get("patch"),
                untracked=ctx.get("untracked"),
                bundle=ctx.get("bundle"),
                clone_seconds=seconds,
                slot_bytes=_dir_bytes(paths.slot_dir),
            )
        for k in ("original_quarantine_path", "quarantine_context", "provision_failures"):
            owner.pop(k, None)
        owner.pop("fetch_failures", None)
        owner["complete"] = True
        self._write_owner(paths, owner)
        return self._result(PrepAction.LAUNCH, paths, record=record, detail="provisioned")

    def _provision_steps(self, slot: Path) -> str | None:
        mirror = mirror_path(self.slots_root, self.repo)
        if not mirror.exists():
            return f"mirror missing: {mirror}"
        slot.parent.mkdir(parents=True, exist_ok=True)
        if self._git(slot.parent, ["clone", "--", str(mirror), str(slot)]).returncode != 0:
            return "clone from mirror failed"
        if self._git(slot, ["config", "core.longpaths", "true"]).returncode != 0:
            return "config core.longpaths failed"
        if self._git(slot, ["remote", "set-url", "origin", self.origin_url]).returncode != 0:
            return "remote set-url failed"
        if not self._fetch(slot):
            return "fetch from origin failed"
        tb_remote = f"refs/remotes/origin/{self.thread_branch}"
        has_tb = self._git(slot, ["rev-parse", "--verify", "-q", tb_remote]).returncode == 0
        name = self.thread_branch if has_tb else self.default_branch
        sw = self._git(slot, ["switch", "-C", name, "--track", f"origin/{name}"])
        if sw.returncode != 0:
            return f"switch to {name} failed"
        self._git(slot, ["remote", "set-head", "origin", self.default_branch])
        try:
            self.prepare_slot(slot)
            self._guard(slot)
        except DirtyCloneError as exc:
            return f"guard refused the new slot: {exc.reason.value}"
        except Exception as exc:  # A-4 preparation is the caller's; any failure fails provision
            return f"prepare_slot failed: {type(exc).__name__}: {exc}"
        head = self._git(slot, ["rev-parse", "HEAD"])
        tip = self._git(slot, ["rev-parse", f"origin/{name}"])
        if head.returncode or tip.returncode or head.stdout.strip() != tip.stdout.strip():
            return f"HEAD != origin/{name}"
        return None

    # -- outcomes --------------------------------------------------------------------------

    def _launch(self, paths: SlotPaths, owner: dict[str, Any]) -> PrepResult:
        # Step 7: owner matches and complete: true.
        current = self._owner(paths)
        if not current or current.get("complete") is not True:
            return self._result(PrepAction.SKIP, paths, detail="owner not complete")
        return self._result(PrepAction.LAUNCH, paths)

    def _record(
        self,
        paths: SlotPaths,
        action: str,
        reason: str,
        *,
        quarantine_path: str | Path | None = None,
        park_code: str | None = None,
        patch: str | Path | None = None,
        untracked: str | Path | None = None,
        bundle: str | Path | None = None,
        clone_seconds: float | None = None,
        slot_bytes: int | None = None,
        candidates: list[Path] | None = None,
        detail: str = "",
    ) -> dict[str, Any]:
        def _p(v: str | Path | None) -> Path | None:
            return None if v is None else Path(v)

        row = build_slot_quarantine_record(
            thread_id=self.thread_id,
            repo=self.repo,
            slot_path=paths.slot_dir,
            action=action,
            quarantine_reason=reason,
            quarantine_path=_p(quarantine_path),
            park_code=park_code,
            patch=_p(patch),
            untracked=_p(untracked),
            bundle=_p(bundle),
            clone_seconds=clone_seconds,
            slot_bytes=slot_bytes,
            quarantine_candidates=candidates,
            detail=detail,
            at=self.now(),
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        append_slot_quarantine(self.log_path, row)
        return row

    def _park(
        self,
        paths: SlotPaths,
        code: str,
        reason: str,
        *,
        patch: Path | None = None,
        untracked: Path | None = None,
        bundle: Path | None = None,
        candidates: list[Path] | None = None,
        detail: str = "",
        touch_owner: bool = True,
    ) -> PrepResult:
        """Park this thread only; one ``parked`` row; the original stays where it is."""
        owner = self._owner(paths) if touch_owner else None
        qpath = None
        if candidates is None and owner:
            qpath = owner.get("original_quarantine_path")
            ctx = owner.get("quarantine_context")
            if isinstance(ctx, dict):
                patch = patch or _opt_path(ctx.get("patch"))
                untracked = untracked or _opt_path(ctx.get("untracked"))
                bundle = bundle or _opt_path(ctx.get("bundle"))
                if code == PARK_PROVISION_FAILED and qpath:
                    reason = ctx.get("quarantine_reason") or reason
        record = self._record(
            paths,
            SLOT_ACTION_PARKED,
            reason,
            quarantine_path=qpath,
            park_code=code,
            patch=patch,
            untracked=untracked,
            bundle=bundle,
            candidates=candidates,
            detail=detail,
        )
        if owner and code != PARK_QUARANTINE_UNCONFIRMED:
            # The row now carries the original's location (v2.6: clear after the row is written).
            changed = False
            for k in ("original_quarantine_path", "quarantine_context"):
                changed |= owner.pop(k, None) is not None
            if changed:
                self._write_owner(paths, owner)
        return self._result(PrepAction.PARK, paths, park_code=code, detail=detail, record=record)


class _PreserveFailedError(Exception):
    pass


def _opt_path(v: Any) -> Path | None:
    return Path(v) if isinstance(v, str) and v else None


__all__ = [
    "FAILURE_TICKS_TO_PARK",
    "PrepAction",
    "PrepResult",
    "SlotLockBusyError",
    "SlotPrep",
    "exclusive_lock",
    "refresh_mirror",
]
