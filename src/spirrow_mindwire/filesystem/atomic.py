"""Atomic file write helpers.

architecture.md §3.2 mandates a ``*.tmp`` → ``os.replace(*.tmp → final)``
sequence so the watcher (which only reacts to ``*.md``) never observes a
partially-written file. The temp file is created in the same directory
as the target so the rename stays intra-filesystem and therefore atomic
on POSIX and NTFS alike.

Scope of guarantees:
- *Watcher-safe* atomicity (no partial reads of the final path) — yes,
  via ``os.replace``.
- *Crash-recovery* durability (file content is on disk after a power
  loss) — best effort. The file's bytes are fsynced when ``fsync=True``
  but we do **not** fsync the parent directory entry, so a crash
  between ``os.replace`` and the directory pagecache flush could lose
  the rename. Phase 0 accepts this; if Phase 1+ needs hard durability
  we'd add an OS-portable parent-dir fsync.
- *Cross-platform*: ``os.replace`` is atomic on Linux / macOS;
  on Windows it raises ``PermissionError`` (ERROR_SHARING_VIOLATION)
  if another process has the target open for reading. Watchers that
  read ``*.md`` while a writer replaces the same path may need a small
  retry loop on Windows; Phase 0 leaves that to the caller.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    fsync: bool = True,
) -> None:
    """Atomically write *content* to *path*.

    Writes ``<path>.tmp`` next to the target and replaces the final
    path. ``fsync`` defaults to ``True`` (architecture.md §3.2 calls
    it optional but durability matters for message persistence);
    callers can opt out for high-frequency or recoverable writes.

    Any failure unlinks the temp file before re-raising so no orphan
    ``.tmp`` survives a crashed write.
    """

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding=encoding, newline="\n") as f:
            f.write(content)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


__all__ = ["atomic_write_text"]
