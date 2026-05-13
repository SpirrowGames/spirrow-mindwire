"""Startup full-scan for state-based recovery (Feature 2 sub-PR 1).

When the watcher starts, it walks ``threads/`` and re-injects
:class:`ThreadEvent` for any thread that still needs work. The
``meta.yaml.status`` field drives the decision:

- ``active`` / ``retrying``: enqueue a synthetic event so the
  dispatcher picks the thread up. The dispatcher's existing
  per-thread lock + dedup machinery handles concurrency with the live
  observer; if the latest message is already from claude-code, the
  same logic that handles the live path short-circuits via
  :meth:`ThreadDispatcher._run_thread`.
- ``terminated`` / ``resolved`` / ``archived``: skip (terminal states
  must not be auto-revived; operator manual transition is the only
  re-entry path, see ``docs/feature-2-design.md`` §3.6).

See ``docs/feature-2-design.md`` §2.1.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import yaml

from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.lifecycle import REQUEUE_STATES

from .events import ThreadEvent
from .loader import load_messages, load_thread_meta

logger = logging.getLogger(__name__)


def startup_full_scan(
    base_dir: Path,
    queue: asyncio.Queue[ThreadEvent],
    *,
    now: datetime | None = None,
) -> int:
    """Walk ``threads/`` and enqueue events for re-queueable thread states.

    For each ``threads/<ULID>/`` directory:

    1. Skip if the directory name isn't a valid ULID (e.g.
       ``.staging-<ULID>/``).
    2. Skip if ``meta.yaml`` fails to parse / validate (logged at WARNING).
    3. If ``meta.status`` is in :data:`spirrow_mindwire.lifecycle.REQUEUE_STATES`
       and the thread has at least one message, enqueue a synthetic
       :class:`ThreadEvent` for the latest message.
    4. Otherwise log at INFO (terminal-state skip) or WARNING (no
       messages but non-terminal status) and move on.

    Args:
        base_dir: ``settings.paths.data_dir``.
        queue: the asyncio queue the live observer also feeds.
        now: optional override for ``detected_at`` (test-only injection;
            defaults to ``datetime.now(UTC)``).

    Returns:
        Count of events enqueued.
    """
    threads_root = base_dir / "threads"
    if not threads_root.is_dir():
        return 0

    detected_at = now if now is not None else datetime.now(UTC)
    enqueued = 0

    for thread_dir in sorted(threads_root.iterdir()):
        if not thread_dir.is_dir():
            continue
        if thread_dir.name.startswith("."):
            # ``.staging-<ULID>/`` etc. — incomplete / hidden, never a final thread.
            continue

        try:
            layout = ThreadDirLayout(base_dir=base_dir, thread_id=thread_dir.name)
        except ValueError:
            logger.warning("startup_scan: skipping non-ULID dir %s", thread_dir.name)
            continue

        try:
            meta = load_thread_meta(layout)
        except (FileNotFoundError, OSError, ValueError, yaml.YAMLError):
            logger.warning(
                "startup_scan: failed to load meta.yaml for %s",
                thread_dir.name,
                exc_info=True,
            )
            continue

        if meta.status not in REQUEUE_STATES:
            logger.info(
                "startup_scan: skipping %s (status=%s, terminal)",
                thread_dir.name,
                meta.status,
            )
            continue

        try:
            messages = load_messages(layout)
        except (FileNotFoundError, OSError, ValueError, yaml.YAMLError):
            logger.warning(
                "startup_scan: failed to load messages for %s",
                thread_dir.name,
                exc_info=True,
            )
            continue

        if not messages:
            logger.warning(
                "startup_scan: %s has no messages but status=%s; skipping",
                thread_dir.name,
                meta.status,
            )
            continue

        latest = messages[-1]
        evt = ThreadEvent(
            thread_id=thread_dir.name,
            seq=latest.seq,
            path=layout.message_path(latest.seq, latest.from_),
            detected_at=detected_at,
        )
        queue.put_nowait(evt)
        enqueued += 1
        logger.info(
            "startup_scan: enqueued %s (status=%s, seq=%d)",
            thread_dir.name,
            meta.status,
            latest.seq,
        )

    return enqueued


__all__ = ["startup_full_scan"]
