"""Orphan ``.tmp`` file cleanup at watcher startup (Feature 2).

A ``.tmp`` file in ``threads/<ULID>/messages/`` is the staging step of
:func:`spirrow_mindwire.filesystem.atomic.atomic_write_text`: a writer
creates ``<final>.tmp``, writes the content, then ``os.replace``s it
onto ``<final>``. If the writer crashes between steps, an orphan
``.tmp`` survives.

Naive cleanup at startup would also delete in-flight ``.tmp``s
belonging to a concurrent live writer (e.g. a parallel watcher
process). We guard with an **age threshold**: only ``.tmp`` files
older than
:attr:`spirrow_mindwire.config.WatcherConfig.orphan_tmp_cleanup_age_seconds`
are deleted; recent ``.tmp``s are left untouched.

See ``docs/feature-2-design.md`` §2.3.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def cleanup_orphan_tmp(
    threads_root: Path,
    *,
    age_threshold_seconds: float,
    now_seconds: float | None = None,
) -> int:
    """Delete orphan ``.tmp`` files under ``threads_root`` exceeding age threshold.

    Walks ``threads_root/<ULID>/messages/*.tmp`` (the staging path used
    by :func:`atomic_write_text`). Files younger than
    ``age_threshold_seconds`` (compared against ``now_seconds`` or
    current wall-clock ``time.time()``) are preserved as likely in-
    flight writes.

    Args:
        threads_root: ``settings.paths.threads_dir``.
        age_threshold_seconds: ``orphan_tmp_cleanup_age_seconds`` from
            :class:`spirrow_mindwire.config.WatcherConfig`.
        now_seconds: optional override for the comparison timestamp
            (test-only injection; defaults to ``time.time()``).

    Returns:
        Count of files deleted.

    Notes:
        - Errors during stat / unlink are logged at WARNING and skipped
          (a single bad entry must not abort the whole sweep).
        - ``threads_root`` not existing is a no-op (returns 0).
    """
    if not threads_root.is_dir():
        return 0

    now = now_seconds if now_seconds is not None else time.time()
    cutoff = now - age_threshold_seconds

    deleted = 0
    for tmp_path in threads_root.glob("*/messages/*.tmp"):
        # Skip dot-prefix dirs (e.g. ``.staging-<ULID>/`` — incomplete
        # threads being assembled atomically; see thread_dir.py). Without
        # this filter, ``Path.glob`` would match ``.staging-<ULID>/messages/*.tmp``
        # and we'd race with the in-progress staging logic.
        if tmp_path.parent.parent.name.startswith("."):
            continue
        try:
            mtime = tmp_path.stat().st_mtime
        except OSError:
            logger.warning("orphan_cleanup: stat failed for %s", tmp_path, exc_info=True)
            continue
        if mtime >= cutoff:
            continue  # in-flight, preserve
        try:
            tmp_path.unlink()
            deleted += 1
            logger.info(
                "orphan_cleanup: deleted %s (age=%.1fs)",
                tmp_path,
                now - mtime,
            )
        except OSError:
            logger.warning("orphan_cleanup: unlink failed for %s", tmp_path, exc_info=True)

    return deleted


__all__ = ["cleanup_orphan_tmp"]
