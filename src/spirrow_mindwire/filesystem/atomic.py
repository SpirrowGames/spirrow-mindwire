"""Atomic file write helpers.

architecture.md §3.2 mandates a ``*.tmp`` → ``os.replace(*.tmp → final)``
sequence so the watcher (which only reacts to ``*.md``) never observes a
partially-written file. The temp file is created in the same directory
as the target so the rename stays intra-filesystem and therefore atomic
on POSIX and NTFS alike.
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
