"""The lane's state files and the rules for touching them (D3'' - D3⁸).

Three files under ``<data_dir>\\state\\``, each with ONE job:

* ``operator_lane.mutex`` — mutual exclusion only (D3⁸). Empty, never renamed, never deleted. The
  first byte is locked (``msvcrt.locking`` on Windows, ``flock`` elsewhere); a process that dies
  has its handle closed by the OS, so the mutex can never go stale.
* ``operator_lane.lock`` — who is working, and nothing else (D3''' - D3⁸). Read and written ONLY
  while holding the mutex, replaced with ``os.replace`` and reclaimed by copy-then-delete, so no
  holder ever keeps it open across a rename (the ``WinError 32`` of msg-7423).
  - ``holder=conductor``: ``spawning`` (``pid`` = the wrapper, ``spawn_deadline`` = +120 s) ->
    the runner's compare-and-swap to ``running`` (own ``pid`` + ``pid_started_at``) -> ``result``.
  - ``holder=manual``: a lease (``lease_id``, ``expires_at``). No pid is ever read for it (D3⁵).
* ``operator_lane.json`` — ``heads`` only (D3''/D3⁗): per thread, the head, how many spawns failed
  and whether it was dispatched. Its only writer is the conductor's operator pass. Missing or
  corrupt means NO dispatch (fail-closed); only ``reset`` re-creates it, except on the very first
  enable (no file AND no ``operator_reports`` directory yet — the lane has never run here).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, Protocol

HOLDER_CONDUCTOR = "conductor"
HOLDER_MANUAL = "manual"
STATE_SPAWNING = "spawning"
STATE_RUNNING = "running"

#: D3⁶: a ``spawning`` lock older than this is stale whatever its pid says.
SPAWN_DEADLINE = timedelta(seconds=120)
#: D3⁸: the mutex is retried every 100 ms and given up after 10 s.
MUTEX_TIMEOUT_SEC = 10.0
MUTEX_RETRY_SEC = 0.1
#: D3⁵: a manual lease lasts 60 minutes unless ``--ttl`` says otherwise.
DEFAULT_LEASE = timedelta(minutes=60)

#: D2⁶: the only values the runner writes to ``result.kind``.
RESULT_POSTED = "posted"
RESULT_POST_FAILED = "post_failed"
RESULT_INVALID_REPORT = "invalid_report"
RESULT_SESSION_FAILED = "session_failed"
RESULT_KINDS = frozenset(
    {RESULT_POSTED, RESULT_POST_FAILED, RESULT_INVALID_REPORT, RESULT_SESSION_FAILED}
)


class MutexTimeoutError(RuntimeError):
    """The mutex could not be taken within :data:`MUTEX_TIMEOUT_SEC`."""


class LockHeldError(RuntimeError):
    """A live lock exists; the caller may not take it."""


class LockCorruptError(RuntimeError):
    """``operator_lane.lock`` exists but is not a lock this module wrote. Nobody reclaims it."""


class HeadsUnavailableError(RuntimeError):
    """``operator_lane.json`` is missing or corrupt: dispatch stops until ``reset``."""


@dataclass(frozen=True)
class LanePaths:
    data_dir: Path

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def mutex(self) -> Path:
        return self.state_dir / "operator_lane.mutex"

    @property
    def lock(self) -> Path:
        return self.state_dir / "operator_lane.lock"

    @property
    def heads(self) -> Path:
        return self.state_dir / "operator_lane.json"

    @property
    def reports(self) -> Path:
        return self.state_dir / "operator_reports"

    @property
    def reports_done(self) -> Path:
        return self.reports / "done"

    @property
    def config(self) -> Path:
        return self.data_dir / "config" / "operator_lane.json"

    @property
    def logs(self) -> Path:
        return self.data_dir / "logs" / "operator-lane"

    def report(self, run_id: str) -> Path:
        return self.reports / f"{run_id}.md"

    def report_tmp(self, run_id: str) -> Path:
        return self.reports / f"{run_id}.md.tmp"

    def session_log(self, run_id: str) -> Path:
        return self.logs / f"{run_id}.log"


# --- time -------------------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# --- atomic write -----------------------------------------------------------------------------


def atomic_write_text(path: Path, text: str, *, unique_tmp: bool = False) -> None:
    """Write ``path`` so a reader sees the old content or the whole new content, never a part.

    The bytes go to ``<path>.tmp``, are flushed and ``fsync``-ed, and only then ``os.replace``-d
    over ``path`` (D2⁷ for reports, D3'' / D3⁸ for the state files). A crash leaves at most the
    ``.tmp``, which no reader looks at.

    The fixed ``.tmp`` name is safe only for a single writer: the state files are written under
    the mutex and a report has its own ``run_id``. A file with no such owner (the config, which
    :func:`.config.disable` rewrites) passes ``unique_tmp=True``: each writer gets its own
    ``<path>.<pid>-<random>.tmp``, so concurrent writers never share or truncate a temp file and
    the last ``os.replace`` wins with a whole file (PR #505 gate).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if unique_tmp:
        tmp = path.with_name(f"{path.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    else:
        tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, path, retries=_REPLACE_RETRIES if unique_tmp else 0)
    except BaseException:
        if unique_tmp:
            with contextlib.suppress(OSError):
                tmp.unlink()
        raise


# Windows refuses a replace while another writer's replace onto the same target is in flight
# (PermissionError); with distinct temp files that is transient, so the unique path retries.
_REPLACE_RETRIES = 50


def _replace(tmp: Path, path: Path, *, retries: int) -> None:
    for attempt in range(retries + 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt >= retries:
                raise
            time.sleep(0.02)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


# --- mutex ------------------------------------------------------------------------------------


def _try_lock_byte(fh: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_byte(fh: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def hold_mutex(
    path: Path,
    *,
    timeout: float = MUTEX_TIMEOUT_SEC,
    interval: float = MUTEX_RETRY_SEC,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    """Hold ``operator_lane.mutex`` for the body of the ``with`` (D3⁸)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")  # noqa: SIM115 - the handle must outlive the try below
    try:
        deadline = clock() + timeout
        while True:
            try:
                _try_lock_byte(fh)
                break
            except OSError as exc:
                if clock() >= deadline:
                    raise MutexTimeoutError(
                        f"operator_lane.mutex not acquired within {timeout:.0f}s"
                    ) from exc
                sleep(interval)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                _unlock_byte(fh)
    finally:
        fh.close()


# --- process identity -------------------------------------------------------------------------


class ProcessProbe(Protocol):
    def started_at(self, pid: int) -> datetime | None:
        """The start time of process ``pid``, or ``None`` when no such process exists."""


class PsutilProbe:
    def started_at(self, pid: int) -> datetime | None:
        import psutil

        try:
            return datetime.fromtimestamp(psutil.Process(pid).create_time(), UTC)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, OSError):
            return None


def process_alive(probe: ProcessProbe, pid: Any, started_at: Any) -> bool:
    """D3''': alive means the pid exists AND its start time is the recorded one (no pid reuse)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    actual = probe.started_at(pid)
    recorded = parse_iso(started_at)
    if actual is None or recorded is None:
        return False
    return abs((actual - recorded).total_seconds()) < 1.0


# --- lock (call these only while holding the mutex) -------------------------------------------


def read_lock(paths: LanePaths) -> dict[str, Any] | None:
    try:
        raw = paths.lock.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LockCorruptError(f"{paths.lock} is not JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("holder") not in (HOLDER_CONDUCTOR, HOLDER_MANUAL):
        raise LockCorruptError(f"{paths.lock} has no known holder")
    return data


def create_lock(paths: LanePaths, data: dict[str, Any]) -> None:
    """Create the lock; refuses when one exists (the mutex makes check-then-write atomic)."""
    if paths.lock.exists():
        raise LockHeldError(f"{paths.lock} already exists")
    _write_json(paths.lock, data)


def replace_lock(paths: LanePaths, data: dict[str, Any]) -> None:
    _write_json(paths.lock, data)


def reclaim_lock(paths: LanePaths, now: datetime) -> Path:
    """D3⁸: copy the lock to ``operator_lane.lock.stale-<ts>`` (audit trail), then delete it."""
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    dest = paths.lock.with_name(f"{paths.lock.name}.stale-{stamp}")
    shutil.copyfile(paths.lock, dest)
    paths.lock.unlink()
    return dest


def delete_lock(paths: LanePaths) -> None:
    with contextlib.suppress(FileNotFoundError):
        paths.lock.unlink()


def conductor_lock(
    *,
    run_id: str,
    wrapper_pid: int,
    wrapper_started_at: datetime | None,
    now: datetime,
    target: dict[str, str],
) -> dict[str, Any]:
    """The lock the operator pass creates before it starts the runner (D3⁵ step 2, D3⁶)."""
    return {
        "holder": HOLDER_CONDUCTOR,
        "run_id": run_id,
        "state": STATE_SPAWNING,
        "pid": wrapper_pid,
        "pid_started_at": iso(wrapper_started_at) if wrapper_started_at else None,
        "acquired_at": iso(now),
        "spawn_deadline": iso(now + SPAWN_DEADLINE),
        **target,
    }


def stale_reason(lock: dict[str, Any], now: datetime, probe: ProcessProbe) -> str | None:
    """Why ``lock`` may be reclaimed, or ``None`` while it is live.

    * manual: the lease ran out (``lease_expired``). Its pid is never looked at (D3⁵).
    * conductor ``spawning``: past ``spawn_deadline`` (``spawn_deadline_passed``) or the recorded
      process is gone (``spawner_dead``) — D3⁶ / D3⁷.
    * conductor ``running``: a ``result`` was written (``finished``) or the runner is gone
      (``runner_dead``).
    """
    if lock.get("holder") == HOLDER_MANUAL:
        expires = parse_iso(lock.get("expires_at"))
        return "lease_expired" if expires is None or now > expires else None
    state = lock.get("state")
    if state == STATE_SPAWNING:
        deadline = parse_iso(lock.get("spawn_deadline"))
        if deadline is None or now > deadline:
            return "spawn_deadline_passed"
        if not process_alive(probe, lock.get("pid"), lock.get("pid_started_at")):
            return "spawner_dead"
        return None
    if isinstance(lock.get("result"), dict):
        return "finished"
    if not process_alive(probe, lock.get("pid"), lock.get("pid_started_at")):
        return "runner_dead"
    return None


def runner_cas_to_running(
    paths: LanePaths,
    *,
    run_id: str,
    pid: int,
    pid_started_at: datetime | None,
    now: datetime,
) -> str | None:
    """D3⁷: ``spawning`` -> ``running``, only for this run and only before the deadline.

    Returns ``None`` on success, or the reason the runner must stop WITHOUT starting a session.
    Must be called while holding the mutex: the conductor's reclaim of an expired ``spawning``
    lock takes the same mutex, so exactly one of the two happens.
    """
    lock = read_lock(paths)
    if lock is None:
        return "lock_missing"
    if lock.get("holder") != HOLDER_CONDUCTOR or lock.get("run_id") != run_id:
        return "run_id_mismatch"
    if lock.get("state") != STATE_SPAWNING:
        return "not_spawning"
    deadline = parse_iso(lock.get("spawn_deadline"))
    if deadline is None or now > deadline:
        return "spawn_deadline_passed"
    lock.update(
        state=STATE_RUNNING,
        pid=pid,
        pid_started_at=iso(pid_started_at) if pid_started_at else None,
        running_at=iso(now),
    )
    replace_lock(paths, lock)
    return None


def record_abort(paths: LanePaths, *, run_id: str, reason: str) -> None:
    """A runner that stops before its CAS leaves why on the ``spawning`` lock (mutex held)."""
    lock = read_lock(paths)
    if lock is None or lock.get("run_id") != run_id or lock.get("state") != STATE_SPAWNING:
        return
    lock["abort_reason"] = reason
    replace_lock(paths, lock)


def record_result(paths: LanePaths, *, run_id: str, result: dict[str, Any]) -> bool:
    """The runner writes its ``result`` onto its own ``running`` lock (mutex held)."""
    if result.get("kind") not in RESULT_KINDS:
        raise ValueError(f"unknown result kind {result.get('kind')!r}")
    lock = read_lock(paths)
    if lock is None or lock.get("run_id") != run_id or lock.get("state") != STATE_RUNNING:
        return False
    lock["result"] = result
    replace_lock(paths, lock)
    return True


# --- manual lease (D3⁵) -----------------------------------------------------------------------


def manual_acquire(
    paths: LanePaths,
    *,
    lease_id: str,
    ttl: timedelta,
    now: datetime,
    target: dict[str, str],
) -> dict[str, Any]:
    """Take the lock for a hand-driven ``/mindwire-operator`` session (mutex held).

    An expired manual lease is reclaimed; a conductor lock — live or not — is refused, because a
    finished conductor run still owes its completion judgement to the next operator pass.
    """
    lock = read_lock(paths)
    if lock is not None:
        if lock.get("holder") == HOLDER_MANUAL and stale_reason(lock, now, PsutilProbe()):
            reclaim_lock(paths, now)
        else:
            raise LockHeldError(_describe(lock))
    data = {
        "holder": HOLDER_MANUAL,
        "lease_id": lease_id,
        "acquired_at": iso(now),
        "expires_at": iso(now + ttl),
        **target,
    }
    create_lock(paths, data)
    return data


def manual_renew(paths: LanePaths, *, lease_id: str, ttl: timedelta, now: datetime) -> datetime:
    """Extend a lease. Fails once it was reclaimed (fencing: the ``lease_id`` no longer matches)."""
    lock = read_lock(paths)
    if lock is None or lock.get("holder") != HOLDER_MANUAL or lock.get("lease_id") != lease_id:
        raise LockHeldError(f"lease {lease_id} is not held (reclaimed or never taken)")
    expires = now + ttl
    lock["expires_at"] = iso(expires)
    replace_lock(paths, lock)
    return expires


def manual_release(paths: LanePaths, *, lease_id: str) -> None:
    lock = read_lock(paths)
    if lock is None or lock.get("holder") != HOLDER_MANUAL or lock.get("lease_id") != lease_id:
        raise LockHeldError(f"lease {lease_id} is not held (reclaimed or never taken)")
    delete_lock(paths)


def force_release(paths: LanePaths, now: datetime) -> Path | None:
    """The operator's escape hatch: keep a stale copy and remove whatever lock is there."""
    if not paths.lock.exists():
        return None
    return reclaim_lock(paths, now)


def _describe(lock: dict[str, Any]) -> str:
    where = (
        f"{lock.get('project', '?')}/{lock.get('thread_id', '?')}/{lock.get('head_msg_id', '?')}"
    )
    if lock.get("holder") == HOLDER_MANUAL:
        return f"manual lease {lock.get('lease_id')} until {lock.get('expires_at')} ({where})"
    return f"lane run {lock.get('run_id')} is {lock.get('state')} ({where})"


def describe_lock(lock: dict[str, Any]) -> str:
    return _describe(lock)


# --- heads (D3'') -----------------------------------------------------------------------------


def head_key(project: str, thread_id: str) -> str:
    return f"{project}::{thread_id}"


def load_heads(paths: LanePaths) -> dict[str, dict[str, Any]]:
    try:
        raw = paths.heads.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HeadsUnavailableError(f"{paths.heads} is missing") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HeadsUnavailableError(f"{paths.heads} is not JSON: {exc}") from exc
    heads = data.get("heads") if isinstance(data, dict) else None
    if not isinstance(heads, dict) or not all(isinstance(v, dict) for v in heads.values()):
        raise HeadsUnavailableError(f"{paths.heads} has no valid 'heads' map")
    return heads


def save_heads(paths: LanePaths, heads: dict[str, dict[str, Any]]) -> None:
    _write_json(paths.heads, {"heads": heads})


def first_enable(paths: LanePaths) -> bool:
    """True only when the lane has never run here: no heads file AND no reports directory."""
    return not paths.heads.exists() and not paths.reports.exists()


def initialise(paths: LanePaths) -> None:
    """Create an empty ``heads`` (first enable, or an explicit ``reset``)."""
    paths.reports.mkdir(parents=True, exist_ok=True)
    save_heads(paths, {})


__all__ = [
    "DEFAULT_LEASE",
    "HOLDER_CONDUCTOR",
    "HOLDER_MANUAL",
    "RESULT_INVALID_REPORT",
    "RESULT_KINDS",
    "RESULT_POSTED",
    "RESULT_POST_FAILED",
    "RESULT_SESSION_FAILED",
    "SPAWN_DEADLINE",
    "STATE_RUNNING",
    "STATE_SPAWNING",
    "HeadsUnavailableError",
    "LanePaths",
    "LockCorruptError",
    "LockHeldError",
    "MutexTimeoutError",
    "ProcessProbe",
    "PsutilProbe",
    "atomic_write_text",
    "conductor_lock",
    "create_lock",
    "delete_lock",
    "describe_lock",
    "first_enable",
    "force_release",
    "head_key",
    "hold_mutex",
    "initialise",
    "load_heads",
    "manual_acquire",
    "manual_release",
    "manual_renew",
    "process_alive",
    "read_lock",
    "reclaim_lock",
    "record_abort",
    "record_result",
    "replace_lock",
    "runner_cas_to_running",
    "save_heads",
    "stale_reason",
    "utcnow",
]
