"""The stall ledger's single-writer lock: an OS lock held on a file handle.

Spec: T-stalled-pr-has-no-detector msg-4703 §3, amended by msg-4705 §3.

* ``stall-ledger.lock`` is permanent. **No code ever deletes or recreates it** --
  deleting and recreating a lock file hands two processes locks on two different files.
* Acquire = open the file WITHOUT truncation, then take a non-blocking exclusive lock:
  Windows ``msvcrt.locking(fd, LK_NBLCK, 1)`` on the one byte at :data:`LOCK_OFFSET`;
  POSIX ``fcntl.flock(fd, LOCK_EX | LOCK_NB)`` (whole-file, advisory, so the offset does
  not matter there -- the shim still seeks to it so both platforms read the same).
* Mutual exclusion is decided inside one kernel call. The lock cannot go stale: when the
  owning process ends -- normally, by crash, or by kill -- the OS closes its handle and
  releases the lock. There is no takeover, no pid check, no token.
* The lock byte sits at 1 MiB, well past the payload, because a Windows byte-range lock
  is MANDATORY: a lock on byte 0 would make the diagnostic payload unreadable to
  ``Get-Content`` at the exact moment an operator needs it (msg-4704 / msg-4705 §3-1).
* The payload is diagnostics only (``{pid, started_at}``, plus ``released_at`` after a
  normal end). It is written only by the process that HOLDS the lock, in place from
  offset 0, space-padded to exactly :data:`PAYLOAD_SIZE` bytes, and never truncated. It
  is "the last holder", not a claim about who holds the lock now, and no code makes a
  decision from it.

``O_TRUNC`` and ``"w"`` modes are forbidden in this module for the lock path; a static
test enforces it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

LOCK_FILENAME = "stall-ledger.lock"

#: The byte that is locked. Far past :data:`PAYLOAD_SIZE`, so the two never overlap.
LOCK_OFFSET = 1 << 20

#: The payload's fixed size in bytes. JSON, right-padded with spaces.
PAYLOAD_SIZE = 256

_OPEN_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)


class PayloadTooLargeError(ValueError):
    pass


def encode_payload(payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(data) > PAYLOAD_SIZE:
        raise PayloadTooLargeError(f"lock payload is {len(data)} bytes > {PAYLOAD_SIZE}")
    return data.ljust(PAYLOAD_SIZE, b" ")


def read_payload(path: Path) -> dict[str, Any] | None:
    """The last holder's diagnostics, or ``None`` if absent / unparseable.

    A plain read. It works while another process holds the lock, because the lock byte
    is at :data:`LOCK_OFFSET`, not inside the payload.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(PAYLOAD_SIZE)
    except OSError:
        return None
    try:
        value = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    else:
        import fcntl

        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class LedgerLock:
    """Acquire / hold / release the ledger lock.

    Usage::

        lock = LedgerLock(path)
        if not lock.acquire({"pid": os.getpid(), "started_at": ...}):
            ...  # tick_skipped=locked
        try:
            ...
        finally:
            lock.release({"released_at": ...})
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None
        self._payload: dict[str, Any] = {}

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, payload: dict[str, Any]) -> bool:
        """Non-blocking. True when this process now holds the lock.

        On False nothing in the file has changed: the open does not truncate, and only
        a holder writes the payload.
        """
        if self._fd is not None:
            raise RuntimeError("lock already held by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, _OPEN_FLAGS)
        if not _try_lock(fd):
            os.close(fd)
            return False
        self._fd = fd
        self._payload = dict(payload)
        self._write_payload(self._payload)
        return True

    def _write_payload(self, payload: dict[str, Any]) -> None:
        assert self._fd is not None
        data = encode_payload(payload)
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, data)

    def release(self, extra: dict[str, Any] | None = None) -> None:
        """Overwrite the payload in place with ``extra`` merged in, unlock, close.

        The file stays. A process that dies without reaching here leaves the payload
        without ``released_at`` -- which is how an operator tells a crash from a normal
        end (msg-4705 §3-4).
        """
        fd = self._fd
        if fd is None:
            return
        try:
            if extra:
                self._write_payload({**self._payload, **extra})
        finally:
            try:
                _unlock(fd)
            finally:
                os.close(fd)
                self._fd = None
