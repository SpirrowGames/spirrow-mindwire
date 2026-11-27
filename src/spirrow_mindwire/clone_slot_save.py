"""Save check and rescue for a lent clone slot (T-ephemeral-impl-clone-slot-per-thread, A2).

The slot pool lends a slot one turn at a time (Bohr msg-9386, D-1 改訂), so a clone check on the
next tick finds nothing: by then another turn may hold the slot. Whether the turn's work reached
the remote is judged before the slot is handed back (Bohr msg-9400 §1, revised by msg-9402 and
msg-9404), and the same judgement runs when a slot is borrowed (Obj-32-1, msg-9402 Obj-36-1 §3).

Judgement (:meth:`SlotSaver.judge`):

1. ``git fetch --prune <remote> '+refs/heads/*:refs/remotes/<remote>/*'`` with the refspec written
   out, never the remote's configured one (msg-9402 Obj-36-2 §1). ``<remote>`` is ``origin``
   (GitHub) at turn end and ``mirror`` (the local bare mirror) at borrow time. A slot has no
   ``mirror`` remote configured (A8, msg-10088 §3: P2 refuses its local URL), so the borrow time
   fetches from the mirror's path into the same ``refs/remotes/mirror/*``.
2. ``git status --porcelain --ignore-submodules=none --untracked-files=all`` is empty.
3. ``git rev-list --branches HEAD --not --remotes=<remote>`` is empty: every local branch and a
   detached HEAD are covered (msg-9402 Obj-36-1 §1).
4. No ``refs/stash``: a stash is always unsaved (msg-9402 Obj-36-1 §1).

A failed fetch is "not saved" and goes to rescue (msg-9400 §1, Obj-32-1 §4); it is never "saved".

Rescue (:meth:`SlotSaver.rescue`, msg-9404 replacing msg-9402 Obj-36-1 §2):

* Every target is pushed to ``refs/heads/rescue/<thread-id>/<turn_id>/<NN>``, ``NN`` a 01-based
  counter within the turn. Branch names never enter a ref name, so ``foo`` / ``foo/bar`` and a
  branch named ``stash`` cannot collide; the namespace is apart from PR-1a's ``wip/<thread-id>``,
  which is itself a ref. ``thread-id`` and ``turn_id`` containing ``/`` (or not forming a valid ref
  component) fail the rescue before anything is pushed.
* Order: (a) a dirty work tree is committed with ``add -A`` onto the current HEAD; (b) local
  branches with commits not on the remote, by name; (c) a detached HEAD with commits no branch
  reaches; (d) every stash entry from ``git stash list --format=%H`` (msg-9404 Obj-37-1). Pushing a
  stash commit carries its index and untracked parents with it.
* Each NN is confirmed by ``git ls-remote`` against the pushed commit (msg-9402 Obj-36-2 §2); the
  remote-tracking refs are not the evidence.
* Any failure along the way — enumerating, committing, pushing, confirming, or a status still
  not empty afterwards — is ``rescue_failed``. Nothing here hands a slot back as saved after a
  partial failure (Einstein msg-9405).

What this module does NOT do: write the ledger row (:func:`record_turn_result` does, and a failed
write is ``rescue_failed`` too), quarantine, notices, or the finally wiring (A3), or the
conductor's continuation decision (A4).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .clone_guard import GitResult, GitRunner
from .clone_retreat import WRAPPER_EMAIL, WRAPPER_NAME, git_env
from .conductor.continuation_ledger import SlotTurnOutcome, record_slot_turn

__all__ = [
    "RESCUE_PREFIX",
    "RescueKind",
    "RescueTarget",
    "SaveJudgement",
    "SaveResult",
    "SlotSaver",
    "record_turn_result",
    "rescue_ref",
]

RESCUE_PREFIX = "refs/heads/rescue/"
NETWORK_TIMEOUT_S = 40.0
LOCAL_TIMEOUT_S = 20.0
_NETWORK = frozenset({"fetch", "push", "ls-remote"})
_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
# A component of the rescue ref: no slash (msg-9404 §1), and nothing git's ref-format rejects.
_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")


class RescueKind(StrEnum):
    BRANCH = "branch"
    DETACHED = "detached"
    STASH = "stash"


@dataclass(frozen=True)
class RescueTarget:
    """One rescued object: the NN it was pushed to, what it was, and the commit."""

    nn: int
    kind: RescueKind
    source: str  # the branch name, ``HEAD``, or ``stash@{n}``
    sha: str
    ref: str

    def ledger_entry(self) -> dict[str, Any]:
        return {
            "nn": f"{self.nn:02d}",
            "kind": self.kind.value,
            "source": self.source,
            "sha": self.sha,
            "ref": self.ref,
        }


@dataclass(frozen=True)
class SaveJudgement:
    """The outcome of :meth:`SlotSaver.judge`. ``saved`` only when ``reasons`` is empty."""

    reasons: tuple[str, ...]
    fetched: bool

    @property
    def saved(self) -> bool:
        return not self.reasons


@dataclass(frozen=True)
class SaveResult:
    outcome: SlotTurnOutcome
    targets: tuple[RescueTarget, ...] = ()
    reasons: tuple[str, ...] = ()
    detail: str | None = None
    refs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def saved(self) -> bool:
        return self.outcome in (SlotTurnOutcome.PUSHED, SlotTurnOutcome.RESCUED)


class _RescueFailedError(Exception):
    pass


def rescue_ref(thread_id: str, turn_id: str, nn: int) -> str:
    """``refs/heads/rescue/<thread-id>/<turn_id>/<NN>``. Raises ``ValueError`` on a bad id."""
    for label, value in (("thread_id", thread_id), ("turn_id", turn_id)):
        if not _COMPONENT.fullmatch(value) or value.startswith(".") or value.endswith(".lock"):
            raise ValueError(f"{label} is not a single ref component: {value!r}")
        if ".." in value:
            raise ValueError(f"{label} contains '..': {value!r}")
    if nn < 1:
        raise ValueError(f"NN is 1-based, got {nn}")
    return f"{RESCUE_PREFIX}{thread_id}/{turn_id}/{nn:02d}"


def _subprocess_git(cwd: Path, args: Sequence[str]) -> GitResult:
    env = git_env()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    timeout = NETWORK_TIMEOUT_S if args and args[0] in _NETWORK else LOCAL_TIMEOUT_S
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


def _first_line(res: GitResult) -> str:
    lines = (res.stderr or res.stdout).strip().splitlines()
    return lines[0] if lines else f"exit {res.returncode}"


class SlotSaver:
    """Judges and rescues one slot. Every git call goes through ``git`` (tests inject it)."""

    def __init__(
        self, slot: Path, *, git: GitRunner = _subprocess_git, push_remote: str = "origin"
    ):
        self.slot = slot
        self._git_runner = git
        self.push_remote = push_remote

    # -- git plumbing ------------------------------------------------------------------------

    def _git(self, *args: str) -> GitResult:
        try:
            return self._git_runner(self.slot, args)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return GitResult(-1, "", f"{type(exc).__name__}: {exc}")

    def _must(self, *args: str) -> str:
        res = self._git(*args)
        if res.returncode != 0:
            raise _RescueFailedError(f"git {args[0]} failed: {_first_line(res)}")
        return res.stdout

    # -- judgement ---------------------------------------------------------------------------

    def fetch(self, remote: str, source: str | None = None) -> bool:
        """Fetch into ``refs/remotes/<remote>/*`` with the refspec written out.

        ``source`` is where to fetch from when it is not the configured remote ``remote`` (A8,
        Bohr msg-10088 §3): a slot has no ``mirror`` remote, because P2 refuses its local URL, so
        the borrow time passes the mirror's path. The refs land in the same place either way.
        """
        res = self._git(
            "fetch",
            "--prune",
            "--no-tags",
            source or remote,
            f"+refs/heads/*:refs/remotes/{remote}/*",
        )
        return res.returncode == 0

    def judge(self, remote: str, source: str | None = None) -> SaveJudgement:
        """Fetch ``remote`` (from ``source`` when given) with an explicit refspec, then the three
        checks. A check that cannot run counts as unsaved; a failed fetch is unsaved too (it is
        never "saved")."""
        reasons: list[str] = []
        fetched = self.fetch(remote, source)
        if not fetched:
            reasons.append("fetch_failed")
        status = self._git(
            "status", "--porcelain", "--ignore-submodules=none", "--untracked-files=all"
        )
        if status.returncode != 0:
            reasons.append("status_failed")
        elif status.stdout.strip():
            reasons.append("dirty")
        unpushed = self._git("rev-list", "--branches", "HEAD", "--not", f"--remotes={remote}")
        if unpushed.returncode != 0:
            reasons.append("rev_list_failed")
        elif unpushed.stdout.strip():
            reasons.append("unpushed")
        stash = self._git("rev-parse", "-q", "--verify", "refs/stash")
        if stash.returncode == 0 and stash.stdout.strip():
            reasons.append("stash")
        elif stash.returncode not in (0, 1):
            reasons.append("stash_unreadable")
        return SaveJudgement(tuple(reasons), fetched)

    # -- rescue ------------------------------------------------------------------------------

    def _commit_work_tree(self) -> None:
        """(a): commit a dirty work tree onto the current HEAD (PR-1a's ``add -A``)."""
        status = self._must(
            "status", "--porcelain", "--ignore-submodules=none", "--untracked-files=all"
        )
        if not status.strip():
            return
        self._must("add", "-A")
        self._must(
            "-c",
            f"user.name={WRAPPER_NAME}",
            "-c",
            f"user.email={WRAPPER_EMAIL}",
            "commit",
            "--no-verify",
            "--allow-empty",
            "-m",
            "mindwire slot rescue: uncommitted work at slot hand-back",
        )

    def _branches(self) -> list[tuple[str, str]]:
        out = self._must(
            "for-each-ref", "--sort=refname", "--format=%(objectname) %(refname)", "refs/heads/"
        )
        rows: list[tuple[str, str]] = []
        for line in out.splitlines():
            sha, _, ref = line.partition(" ")
            if not _SHA.fullmatch(sha) or not ref.startswith("refs/heads/"):
                raise _RescueFailedError(f"unreadable for-each-ref line: {line!r}")
            rows.append((ref.removeprefix("refs/heads/"), sha))
        return rows

    def _has_commits_outside(self, sha: str, *excludes: str) -> bool:
        return bool(self._must("rev-list", "-n", "1", sha, "--not", *excludes).strip())

    def _targets(self, remote: str, fetched: bool) -> list[tuple[RescueKind, str, str]]:
        found: list[tuple[RescueKind, str, str]] = []
        # When the fetch failed the remote-tracking refs may be stale: rescue every branch rather
        # than trust them to say what is saved.
        remote_excl = (f"--remotes={remote}",) if fetched else ()
        # (b) branches with unsaved commits, by name.
        for name, sha in self._branches():
            if not remote_excl or self._has_commits_outside(sha, *remote_excl):
                found.append((RescueKind.BRANCH, name, sha))
        # (c) a detached HEAD with commits no branch reaches.
        sym = self._git("symbolic-ref", "-q", "HEAD")
        if sym.returncode not in (0, 1):
            raise _RescueFailedError(f"git symbolic-ref failed: {_first_line(sym)}")
        if sym.returncode == 1:
            head = self._must("rev-parse", "--verify", "HEAD").strip()
            if not _SHA.fullmatch(head):
                raise _RescueFailedError(f"unreadable HEAD: {head!r}")
            if self._has_commits_outside(head, "--branches", *remote_excl):
                found.append((RescueKind.DETACHED, "HEAD", head))
        # (d) every stash entry (msg-9404 Obj-37-1).
        stash = self._git("rev-parse", "-q", "--verify", "refs/stash")
        if stash.returncode not in (0, 1):
            raise _RescueFailedError(f"refs/stash unreadable: {_first_line(stash)}")
        if stash.returncode == 0:
            listing = self._must("stash", "list", "--format=%H")
            shas = [s.strip() for s in listing.splitlines() if s.strip()]
            if not shas or not all(_SHA.fullmatch(s) for s in shas):
                raise _RescueFailedError("stash list did not enumerate the stash reflog")
            for n, sha in enumerate(shas):
                found.append((RescueKind.STASH, f"stash@{{{n}}}", sha))
        return found

    def _confirm(self, targets: Sequence[RescueTarget]) -> None:
        res = self._git("ls-remote", self.push_remote, *(t.ref for t in targets))
        if res.returncode != 0:
            raise _RescueFailedError(f"git ls-remote failed: {_first_line(res)}")
        seen: dict[str, str] = {}
        for line in res.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            seen[ref.strip()] = sha.strip()
        for t in targets:
            if seen.get(t.ref) != t.sha:
                raise _RescueFailedError(
                    f"{t.ref}: remote has {seen.get(t.ref) or 'nothing'}, pushed {t.sha}"
                )

    def rescue(
        self, thread_id: str, turn_id: str, judgement: SaveJudgement, remote: str
    ) -> SaveResult:
        try:
            rescue_ref(thread_id, turn_id, 1)  # validate before touching anything
        except ValueError as exc:
            return SaveResult(
                SlotTurnOutcome.RESCUE_FAILED, reasons=judgement.reasons, detail=str(exc)
            )
        targets: list[RescueTarget] = []
        try:
            self._commit_work_tree()
            for i, (kind, source, sha) in enumerate(self._targets(remote, judgement.fetched), 1):
                targets.append(
                    RescueTarget(i, kind, source, sha, rescue_ref(thread_id, turn_id, i))
                )
            if targets:
                push = self._git(
                    "push",
                    "--no-verify",
                    "--porcelain",
                    self.push_remote,
                    *(f"{t.sha}:{t.ref}" for t in targets),
                )
                if push.returncode != 0:
                    raise _RescueFailedError(f"git push failed: {_first_line(push)}")
                self._confirm(targets)
            status = self._must(
                "status", "--porcelain", "--ignore-submodules=none", "--untracked-files=all"
            )
            if status.strip():
                raise _RescueFailedError("work tree still not clean after the rescue commit")
            if not targets and "fetch_failed" not in judgement.reasons:
                # Unsaved, yet nothing found to rescue: the two views disagree, so do not claim
                # the work is safe.
                raise _RescueFailedError("judged unsaved but found nothing to rescue")
        except _RescueFailedError as exc:
            return SaveResult(
                SlotTurnOutcome.RESCUE_FAILED,
                tuple(targets),
                judgement.reasons,
                str(exc),
                [t.ledger_entry() for t in targets],
            )
        return SaveResult(
            SlotTurnOutcome.RESCUED,
            tuple(targets),
            judgement.reasons,
            None,
            [t.ledger_entry() for t in targets],
        )

    def check_and_rescue(
        self, thread_id: str, turn_id: str, *, remote: str = "origin"
    ) -> SaveResult:
        """Judge, and rescue when not saved. ``remote``: ``origin`` at turn end, ``mirror`` at
        borrow time (msg-9404 Obj-37-1 §4: one function for both)."""
        judgement = self.judge(remote)
        if judgement.saved:
            return SaveResult(SlotTurnOutcome.PUSHED)
        return self.rescue(thread_id, turn_id, judgement, remote)


def record_turn_result(
    ledger: Path,
    result: SaveResult,
    *,
    project: str,
    thread: str,
    turn_id: str,
    slot: str,
    recovered_by: str | None = None,
) -> SaveResult:
    """Write the turn's row (msg-9400 §3, msg-9404 §4). A write that fails turns a saved result
    into ``rescue_failed``: the slot must not go back as saved without its record (msg-9405)."""
    try:
        record_slot_turn(
            ledger,
            project=project,
            thread=thread,
            turn_id=turn_id,
            outcome=result.outcome,
            slot=slot,
            refs=result.refs,
            detail=result.detail,
            recovered_by=recovered_by,
        )
    except (OSError, ValueError) as exc:
        return SaveResult(
            SlotTurnOutcome.RESCUE_FAILED,
            result.targets,
            result.reasons,
            f"ledger write failed: {type(exc).__name__}: {exc}"
            + (f"; {result.detail}" if result.detail else ""),
            result.refs,
        )
    return result
