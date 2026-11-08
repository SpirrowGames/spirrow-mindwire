"""Per-thread clone slots: naming, owner files and the step-0 entry decision.

Thread T-ephemeral-impl-clone-slot-per-thread, PR-A. Specified by Bohr msg-8061 (consolidated
spec), msg-8068 (v2.3 turn-pre steps), msg-8070 (v2.4 step 0), msg-8084/8086/8088 (v2.5-v2.7
records, notices and the quarantine hand-over); naysayer go on msg-8107 limited to PR-A/PR-B.

This module holds the pure, filesystem-only part of the slot layout:

* the slot path ``<slots_root>/<repo>/<hash12>[-N]/`` (``hash12`` = first 12 hex chars of the
  sha1 of the full thread id),
* the owner file ``<slots_root>/<repo>/<slot>.owner.json`` and its four-way classification
  (absent / match / mismatch / unreadable, v2.4),
* exclusive owner creation: write a ``.tmp-<pid>`` file, fsync, then publish it with a
  non-overwriting ``os.link`` (v2.4) — an owner file is never half-written in place,
* the step-0 routing table (v2.4) that decides, from the owner classification and whether the
  slot directory exists, where the turn-pre procedure goes next.

No git command runs here and nothing is ever deleted (msg-8061 "やらないこと").
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

OWNER_SUFFIX = ".owner.json"
LOCK_SUFFIX = ".lock"
REQUIRED_OWNER_FIELDS: dict[str, type] = {
    "repo": str,
    "thread_id": str,
    "complete": bool,
    "created_at": str,
}

# Park codes named by the spec (msg-8061 / v2.3 / v2.4 / v2.7).
PARK_OWNER_UNKNOWN = "clone_slot_owner_unknown"
PARK_UNPUBLISHED = "clone_slot_unpublished"
PARK_PRESERVE_FAILED = "clone_slot_preserve_failed"
PARK_PROVISION_FAILED = "clone_slot_provision_failed"
PARK_UNRECOVERABLE = "clone_slot_unrecoverable"
PARK_QUARANTINE_UNCONFIRMED = "clone_slot_quarantine_unconfirmed"
PARK_INDEX_LOCK_STALE = "clone_slot_index_lock_stale"  # Bohr msg-8206
PARK_SESSION_UNKNOWN = "clone_slot_session_unknown"  # Bohr msg-8234 判断 2
# Written by the sweep wrapper (not SlotPrep) when `mindwire slot-prep` exits 1 or with a code it
# does not define: parked on that tick, no counter anywhere (Bohr msg-8236, Obj-18-1).
PARK_PREP_CRASHED = "clone_slot_prep_crashed"
PARK_GIT_DIVERGED = "git.diverged"
PARK_GIT_PUSH_FAILED = "git.push_failed"


def thread_hash12(thread_id: str, *, hasher: Callable[[bytes], str] | None = None) -> str:
    """First 12 hex chars of sha1(thread_id). ``hasher`` exists so tests can force collisions."""
    if hasher is not None:
        return hasher(thread_id.encode("utf-8"))[:12]
    return hashlib.sha1(thread_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def slot_name(hash12: str, n: int) -> str:
    return hash12 if n == 0 else f"{hash12}-{n}"


@dataclass(frozen=True)
class SlotPaths:
    repo_root: Path  # <slots_root>/<repo>
    name: str

    @property
    def slot_dir(self) -> Path:
        return self.repo_root / self.name

    @property
    def owner_file(self) -> Path:
        return self.repo_root / f"{self.name}{OWNER_SUFFIX}"

    @property
    def lock_file(self) -> Path:
        return self.repo_root / f"{self.name}{LOCK_SUFFIX}"


def mirror_path(slots_root: Path, repo: str) -> Path:
    return slots_root / f"{repo}.git"


def quarantine_root(slots_root: Path, repo: str) -> Path:
    return slots_root / "_quarantine" / repo


class OwnerState(enum.Enum):
    ABSENT = "absent"
    MATCH = "match"
    MISMATCH = "mismatch"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class OwnerRead:
    state: OwnerState
    data: dict[str, Any] | None = None

    @property
    def complete(self) -> bool:
        return bool(self.data and self.data.get("complete") is True)


def read_owner(path: Path, *, repo: str, thread_id: str) -> OwnerRead:
    """Classify an owner file exactly as v2.4 (msg-8070) does.

    *match* and *mismatch* both require readable JSON carrying all four required fields with the
    right types; anything else that exists is *unreadable* and is never treated as someone else's.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return OwnerRead(OwnerState.ABSENT)
    except OSError:
        return OwnerRead(OwnerState.UNREADABLE)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return OwnerRead(OwnerState.UNREADABLE)
    if not isinstance(data, dict):
        return OwnerRead(OwnerState.UNREADABLE)
    for field, typ in REQUIRED_OWNER_FIELDS.items():
        if field not in data or type(data[field]) is not typ:
            return OwnerRead(OwnerState.UNREADABLE)
    if data["repo"] == repo and data["thread_id"] == thread_id:
        return OwnerRead(OwnerState.MATCH, data)
    return OwnerRead(OwnerState.MISMATCH, data)


def _write_tmp(path: Path, payload: dict[str, Any]) -> Path:
    # pid + nonce, opened O_EXCL: a tmp name is never reused, so truncating it can never reach an
    # owner file it was hard-linked to by an earlier publish.
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return tmp


def create_owner_exclusive(
    path: Path, *, repo: str, thread_id: str, now: datetime | None = None
) -> bool:
    """Publish a fresh ``complete: false`` owner file without overwriting. False if one exists.

    A tmp file left by a crash or a lost race is not deleted (v2.4).
    After a successful publish the tmp name is only a second hard link to the owner file's own
    bytes, so unlinking that name removes no data.
    """
    payload = {
        "repo": repo,
        "thread_id": thread_id,
        "complete": False,
        "created_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _write_tmp(path, payload)
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    os.unlink(tmp)
    return True


def update_owner(path: Path, data: dict[str, Any]) -> None:
    """Replace an owner file this thread already owns (caller holds the slot lock).

    Write a tmp file, fsync, then ``os.replace`` — one atomic swap (v2.6/v2.7).
    """
    tmp = _write_tmp(path, data)
    os.replace(tmp, path)


class EntryRoute(enum.Enum):
    """Where step 0 (v2.4 table) sends the turn-pre procedure."""

    CREATE_THEN_PROVISION = "create_then_provision"  # absent / absent
    NEXT_CANDIDATE = "next_candidate"  # mismatch
    QUARANTINE_INCOMPLETE_THEN_PROVISION = "quarantine_incomplete_then_provision"
    PROVISION = "provision"  # match, complete false, no dir
    STEP1 = "step1"  # match, complete true, dir present
    PARK_OWNER_UNKNOWN = "park_owner_unknown"


def entry_route(owner: OwnerRead, dir_exists: bool) -> EntryRoute:
    if owner.state is OwnerState.ABSENT:
        return EntryRoute.CREATE_THEN_PROVISION if not dir_exists else EntryRoute.PARK_OWNER_UNKNOWN
    if owner.state is OwnerState.UNREADABLE:
        return EntryRoute.PARK_OWNER_UNKNOWN
    if owner.state is OwnerState.MISMATCH:
        return EntryRoute.NEXT_CANDIDATE
    if owner.complete:
        return EntryRoute.STEP1 if dir_exists else EntryRoute.PARK_OWNER_UNKNOWN
    return EntryRoute.QUARANTINE_INCOMPLETE_THEN_PROVISION if dir_exists else EntryRoute.PROVISION


@dataclass(frozen=True)
class SlotChoice:
    paths: SlotPaths
    route: EntryRoute
    owner: OwnerRead


def choose_slot(
    slots_root: Path,
    repo: str,
    thread_id: str,
    *,
    hasher: Callable[[bytes], str] | None = None,
    max_candidates: int = 64,
    now: datetime | None = None,
) -> SlotChoice:
    """Walk ``<hash12>``, ``<hash12>-1``, ... and stop at the first non-mismatch candidate.

    An absent/absent candidate is claimed here with :func:`create_owner_exclusive`; losing that
    race re-reads the candidate (the winner's file is then a *mismatch*, or ours if same thread).
    """
    repo_root = slots_root / repo
    h = thread_hash12(thread_id, hasher=hasher)
    for n in range(max_candidates):
        paths = SlotPaths(repo_root, slot_name(h, n))
        for _attempt in range(2):
            owner = read_owner(paths.owner_file, repo=repo, thread_id=thread_id)
            route = entry_route(owner, paths.slot_dir.exists())
            if route is not EntryRoute.CREATE_THEN_PROVISION:
                break
            if create_owner_exclusive(paths.owner_file, repo=repo, thread_id=thread_id, now=now):
                owner = read_owner(paths.owner_file, repo=repo, thread_id=thread_id)
                return SlotChoice(paths, EntryRoute.CREATE_THEN_PROVISION, owner)
        else:  # pragma: no cover - two consecutive lost races on the same candidate
            route = EntryRoute.PARK_OWNER_UNKNOWN
        if route is EntryRoute.NEXT_CANDIDATE:
            continue
        return SlotChoice(paths, route, owner)
    raise RuntimeError(f"no free slot candidate for {thread_id!r} under {repo_root}")
