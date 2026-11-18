"""Slot lease records for the pooled clone slots (T-ephemeral-impl-clone-slot-per-thread, A2).

A slot is lent to one turn at a time (Bohr msg-9386, D-1 改訂 2). Which turn holds which slot lives
in ``state/slot-leases.json``, updated the way ``dirty-clones.json`` is: take ``<file>.lock``,
change one key, save atomically (write a sibling temp file, then ``os.replace``).

Record of a held slot (msg-9386 改訂 2, msg-9388 Obj-32-2, msg-9391 Obj-33-1, msg-9393 Obj-34-1):
``slot``, ``thread_id``, ``turn_id``, ``acquired_at``, ``wrapper_pid``, ``child_pid``,
``session_name``, ``job_name``, ``state`` and the ``spawning`` marker.

States:

* ``launching`` — written when the conductor lends the slot; the ``--bg`` session name and the Job
  name are decided before the launch and recorded here.
* ``running`` — the child pid is recorded and nothing is mid-spawn. :meth:`LeaseStore.mark_spawning`
  raises the ``spawning`` marker right before the child starts; :meth:`LeaseStore.mark_running`
  records the pid and lowers it.
* ``quarantine_failed`` — the turn-end rename failed even with ``-retryN`` (msg-9402 Obj-36-4): the
  lease is never returned and the slot is never lent again until a person clears it.

A free slot keeps ``last_thread_id`` so the same thread's next turn prefers it (改訂 2: fewer
re-checkouts). One turn holds at most one slot and one slot is held by at most one turn (msg-9386
D-5): a thread whose previous lease in the repo is still outstanding is not lent a second slot.

Wrapper crash (改訂 3, revised by msg-9395 Obj-35-1): automatic reclaim was conditional on the
``--bg`` session and its descendants being contained in the wrapper's Job Object. That containment
was not confirmed (fix_cut, read-back of msg-9395 §3), so a lease the finally did not return is
never reclaimed here: :func:`judge_unreturned` keeps it and names ``job_containment_unverified`` for
the notice. Other threads of the repo use the remaining slots or wait.

No free slot (改訂 4): the thread is neither launched nor parked on that tick. A thread that has
waited more than :data:`WAIT_NOTICE_AFTER_S` gets exactly one notice (:attr:`AcquireResult.notice`).

Pure bookkeeping: no git command runs here and no slot directory is touched.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from dataclasses import replace as _replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from .clone_slot import slot_index, slot_name

__all__ = [
    "LEASES_FILE",
    "WAIT_NOTICE_AFTER_S",
    "AcquireResult",
    "Lease",
    "LeaseError",
    "LeaseLockTimeoutError",
    "LeaseState",
    "LeaseStore",
    "UnreturnedVerdict",
    "judge_unreturned",
    "pid_alive",
]

LEASES_FILE = "slot-leases.json"
SCHEMA_VERSION = 1
WAIT_NOTICE_AFTER_S = 30 * 60
LOCK_WAIT_S = 10.0
LOCK_POLL_S = 0.05

REASON_NO_FREE_SLOT = "no_free_slot"
REASON_THREAD_HOLDS_LEASE = "thread_holds_lease"
REASON_JOB_CONTAINMENT_UNVERIFIED = "job_containment_unverified"
REASON_QUARANTINE_FAILED = "quarantine_failed"


class LeaseState(StrEnum):
    LAUNCHING = "launching"
    RUNNING = "running"
    QUARANTINE_FAILED = "quarantine_failed"


class LeaseError(Exception):
    """A lease operation that does not match the record (wrong turn, unknown slot, bad file)."""


class LeaseLockTimeoutError(LeaseError):
    """``<file>.lock`` stayed held by a live process for :data:`LOCK_WAIT_S`."""


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a live process. Never signals it (``os.kill`` kills on Windows)."""
    import psutil

    if pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        return bool(proc.status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


@dataclass(frozen=True)
class Lease:
    repo: str
    slot: str
    thread_id: str
    turn_id: str
    acquired_at: str
    state: LeaseState
    wrapper_pid: int | None = None
    child_pid: int | None = None
    session_name: str | None = None
    job_name: str | None = None
    spawning: bool = False

    @property
    def index(self) -> int:
        n = slot_index(self.slot)
        if n is None:
            raise LeaseError(f"not a slot name: {self.slot!r}")
        return n

    def to_json(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "acquired_at": self.acquired_at,
            "state": self.state.value,
            "wrapper_pid": self.wrapper_pid,
            "child_pid": self.child_pid,
            "session_name": self.session_name,
            "job_name": self.job_name,
            "spawning": self.spawning,
        }

    @classmethod
    def from_json(cls, repo: str, raw: dict[str, Any]) -> Lease:
        try:
            return cls(
                repo=repo,
                slot=str(raw["slot"]),
                thread_id=str(raw["thread_id"]),
                turn_id=str(raw["turn_id"]),
                acquired_at=str(raw["acquired_at"]),
                state=LeaseState(raw["state"]),
                wrapper_pid=_opt_int(raw.get("wrapper_pid")),
                child_pid=_opt_int(raw.get("child_pid")),
                session_name=_opt_str(raw.get("session_name")),
                job_name=_opt_str(raw.get("job_name")),
                spawning=bool(raw.get("spawning", False)),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise LeaseError(f"malformed lease record in {repo}: {raw!r} ({exc})") from exc


def _opt_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"expected an int, got {v!r}")
    return v


def _opt_str(v: Any) -> str | None:
    return None if v is None else str(v)


@dataclass(frozen=True)
class AcquireResult:
    """What :meth:`LeaseStore.acquire` decided. ``lease`` is set only when a slot was lent."""

    lease: Lease | None
    reason: str | None = None
    waiting_since: str | None = None
    notice: bool = False  # True exactly once per wait longer than WAIT_NOTICE_AFTER_S


@dataclass(frozen=True)
class UnreturnedVerdict:
    """What to do with a held lease whose turn is not known to be live."""

    in_use: bool  # the wrapper is alive: the lease is simply held
    reclaim: bool
    reason: str | None = None  # for the notice when kept and not in use


def judge_unreturned(lease: Lease, alive: Callable[[int], bool] = pid_alive) -> UnreturnedVerdict:
    """Keep or reclaim a lease the wrapper's finally did not return (msg-9395 Obj-35-1, fix_cut).

    * ``quarantine_failed``: never lent, never reclaimed; the notice names it.
    * wrapper pid recorded and alive: in use.
    * otherwise the wrapper is gone without returning the slot. Job containment of the ``--bg``
      session was not confirmed, so there is no automatic reclaim: keep it, notice
      ``job_containment_unverified``.
    """
    if lease.state is LeaseState.QUARANTINE_FAILED:
        return UnreturnedVerdict(in_use=False, reclaim=False, reason=REASON_QUARANTINE_FAILED)
    if lease.wrapper_pid is not None and alive(lease.wrapper_pid):
        return UnreturnedVerdict(in_use=True, reclaim=False)
    return UnreturnedVerdict(in_use=False, reclaim=False, reason=REASON_JOB_CONTAINMENT_UNVERIFIED)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class LeaseStore:
    """``state/slot-leases.json``: ``{"version": 1, "repos": {repo: {"slots": {...},
    "waiting": {...}}}}``. Every method takes the lock, reads, changes one key, saves."""

    def __init__(
        self,
        state_dir: Path,
        *,
        lock_wait_s: float = LOCK_WAIT_S,
        alive: Callable[[int], bool] = pid_alive,
    ) -> None:
        self.path = state_dir / LEASES_FILE
        self.lock_path = state_dir / (LEASES_FILE + ".lock")
        self._lock_wait_s = lock_wait_s
        self._alive = alive

    # -- locking and persistence -------------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._lock_wait_s
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._steal_if_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise LeaseLockTimeoutError(
                        f"{self.lock_path} held for more than {self._lock_wait_s:g}s"
                    ) from None
                time.sleep(LOCK_POLL_S)
                continue
            break
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            data = self._read()
            yield data
            self._write(data)
        finally:
            with suppress(FileNotFoundError):
                self.lock_path.unlink()

    def _steal_if_stale(self) -> bool:
        """Remove a lock whose recorded holder is no longer alive. A lock without a readable pid
        is left alone (it may be mid-write); the wait bound then decides."""
        try:
            text = self.lock_path.read_text(encoding="ascii").strip()
        except (FileNotFoundError, UnicodeDecodeError, OSError):
            return False
        if not text.isdigit() or self._alive(int(text)):
            return False
        with suppress(FileNotFoundError):
            self.lock_path.unlink()
        return True

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "repos": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LeaseError(f"cannot read {self.path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            raise LeaseError(f"{self.path}: unsupported content (version != {SCHEMA_VERSION})")
        if not isinstance(data.get("repos"), dict):
            raise LeaseError(f"{self.path}: 'repos' is not an object")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=LEASES_FILE + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    @staticmethod
    def _repo(data: dict[str, Any], repo: str) -> dict[str, Any]:
        r: dict[str, Any] = data["repos"].setdefault(repo, {})
        r.setdefault("slots", {})
        r.setdefault("waiting", {})
        return r

    # -- reads ------------------------------------------------------------------------------

    def leases(self, repo: str) -> list[Lease]:
        """Every held lease of ``repo`` (free slots are not leases)."""
        with self._locked() as data:
            slots = self._repo(data, repo)["slots"]
            return [
                Lease.from_json(repo, v)
                for _, v in sorted(slots.items())
                if isinstance(v, dict) and "state" in v
            ]

    # -- transitions ------------------------------------------------------------------------

    def acquire(
        self,
        repo: str,
        slots_per_repo: int,
        *,
        thread_id: str,
        turn_id: str,
        now: datetime,
        wrapper_pid: int | None = None,
        session_name: str | None = None,
        job_name: str | None = None,
    ) -> AcquireResult:
        """Lend a free slot to ``turn_id`` in state ``launching``, or say why not.

        Preference: a free slot ``thread_id`` used last, then the lowest free index.
        """
        if slots_per_repo < 1:
            raise ValueError(f"slots_per_repo must be >= 1, got {slots_per_repo}")
        with self._locked() as data:
            r = self._repo(data, repo)
            slots: dict[str, Any] = r["slots"]
            held = {
                k: Lease.from_json(repo, v)
                for k, v in slots.items()
                if isinstance(v, dict) and "state" in v
            }
            if any(lease.thread_id == thread_id for lease in held.values()):
                return AcquireResult(None, REASON_THREAD_HOLDS_LEASE)
            free = [slot_name(n) for n in range(1, slots_per_repo + 1) if slot_name(n) not in held]
            if not free:
                return self._wait(r, thread_id, now)
            preferred = [s for s in free if (slots.get(s) or {}).get("last_thread_id") == thread_id]
            chosen = (preferred or free)[0]
            lease = Lease(
                repo=repo,
                slot=chosen,
                thread_id=thread_id,
                turn_id=turn_id,
                acquired_at=_iso(now),
                state=LeaseState.LAUNCHING,
                wrapper_pid=wrapper_pid,
                session_name=session_name,
                job_name=job_name,
            )
            slots[chosen] = lease.to_json()
            r["waiting"].pop(thread_id, None)
            return AcquireResult(lease)

    @staticmethod
    def _wait(r: dict[str, Any], thread_id: str, now: datetime) -> AcquireResult:
        w = r["waiting"].get(thread_id)
        if not isinstance(w, dict) or "since" not in w:
            w = {"since": _iso(now), "notified": False}
            r["waiting"][thread_id] = w
        since = _parse_iso(str(w["since"]))
        notice = False
        if not w.get("notified") and now - since > timedelta(seconds=WAIT_NOTICE_AFTER_S):
            w["notified"] = True
            notice = True
        return AcquireResult(
            None, REASON_NO_FREE_SLOT, waiting_since=str(w["since"]), notice=notice
        )

    def _update(
        self, repo: str, slot: str, turn_id: str, change: Callable[[Lease], Lease]
    ) -> Lease:
        with self._locked() as data:
            slots = self._repo(data, repo)["slots"]
            raw = slots.get(slot)
            if not isinstance(raw, dict) or "state" not in raw:
                raise LeaseError(f"{repo}/{slot} is not leased")
            cur = Lease.from_json(repo, raw)
            if cur.turn_id != turn_id:
                raise LeaseError(f"{repo}/{slot} is leased to turn {cur.turn_id}, not {turn_id}")
            new = change(cur)
            slots[slot] = new.to_json()
            return new

    def mark_spawning(self, repo: str, slot: str, turn_id: str) -> Lease:
        """Raise the ``spawning`` marker right before the child process starts."""

        def change(cur: Lease) -> Lease:
            if cur.state is LeaseState.QUARANTINE_FAILED:
                raise LeaseError(f"{repo}/{slot} is quarantine_failed")
            return _replace(cur, spawning=True)

        return self._update(repo, slot, turn_id, change)

    def mark_running(self, repo: str, slot: str, turn_id: str, *, child_pid: int) -> Lease:
        """Record the child pid, lower the marker, and enter ``running``."""

        def change(cur: Lease) -> Lease:
            if cur.state is LeaseState.QUARANTINE_FAILED:
                raise LeaseError(f"{repo}/{slot} is quarantine_failed")
            return _replace(cur, state=LeaseState.RUNNING, child_pid=child_pid, spawning=False)

        return self._update(repo, slot, turn_id, change)

    def mark_quarantine_failed(self, repo: str, slot: str, turn_id: str) -> Lease:
        """The turn-end rename failed (msg-9402 Obj-36-4): keep the lease; never lend the slot."""
        return self._update(
            repo, slot, turn_id, lambda cur: _replace(cur, state=LeaseState.QUARANTINE_FAILED)
        )

    def release(self, repo: str, slot: str, turn_id: str) -> None:
        """Hand the slot back after the finally. A ``quarantine_failed`` lease is not released."""
        with self._locked() as data:
            slots = self._repo(data, repo)["slots"]
            raw = slots.get(slot)
            if not isinstance(raw, dict) or "state" not in raw:
                raise LeaseError(f"{repo}/{slot} is not leased")
            cur = Lease.from_json(repo, raw)
            if cur.turn_id != turn_id:
                raise LeaseError(f"{repo}/{slot} is leased to turn {cur.turn_id}, not {turn_id}")
            if cur.state is LeaseState.QUARANTINE_FAILED:
                raise LeaseError(f"{repo}/{slot} is quarantine_failed and is not returned")
            slots[slot] = {"last_thread_id": cur.thread_id}

    def unreturned(self, repo: str) -> list[tuple[Lease, UnreturnedVerdict]]:
        """Every held lease of ``repo`` whose turn is not live, with the kept-lease reason."""
        out = []
        for lease in self.leases(repo):
            v = judge_unreturned(lease, self._alive)
            if not v.in_use:
                out.append((lease, v))
        return out
