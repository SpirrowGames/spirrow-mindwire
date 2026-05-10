"""Watcher entry point: wire observer + dispatcher together and run.

Phase 0 happy path: a single coroutine starts the observer, drains
the asyncio queue, and forwards each :class:`ThreadEvent` to the
dispatcher. ``Ctrl+C`` cancels the consumer task and stops the
observer cleanly.

Feature 2 sub-PR 1 adds two startup hooks before the queue loop:

- :func:`spirrow_mindwire.watcher.orphan_cleanup.cleanup_orphan_tmp` —
  delete ``messages/*.tmp`` files older than the age threshold.
- :func:`spirrow_mindwire.watcher.startup_scan.startup_full_scan` —
  enqueue synthetic :class:`ThreadEvent` for ``active`` / ``retrying``
  threads so the dispatcher picks them up after a restart.

Further robustness (timeout, retry, terminate) is queued for sub-PR
2 / 3 / 4.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from spirrow_mindwire.config import MindwireSettings
from spirrow_mindwire.phanthand import PhanthandClient

from .dedup import DedupCache
from .dispatcher import ThreadDispatcher
from .events import ThreadEvent
from .observer import WatcherObserver
from .orphan_cleanup import cleanup_orphan_tmp
from .startup_scan import startup_full_scan

logger = logging.getLogger(__name__)


async def run_watcher(settings: MindwireSettings, *, api_key: str | None) -> None:
    """Run the watcher until cancellation.

    *api_key* is read from ``os.environ[settings.phanthand.api_key_env]``
    by the CLI wrapper (kept out of :class:`MindwireSettings` so secrets
    don't end up in TOML).
    """

    # Feature 2: orphan .tmp cleanup at startup (docs §2.3).
    deleted = cleanup_orphan_tmp(
        settings.paths.threads_dir,
        age_threshold_seconds=settings.watcher.orphan_tmp_cleanup_age_seconds,
    )
    if deleted > 0:
        logger.info("orphan_cleanup: removed %d orphan .tmp files at startup", deleted)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    dedup = DedupCache(ttl=timedelta(seconds=settings.watcher.dedup_ttl_seconds))

    # Feature 2: state-based recovery — enqueue active/retrying threads (docs §2.1).
    enqueued = startup_full_scan(settings.paths.data_dir, queue)
    if enqueued > 0:
        logger.info("startup_scan: enqueued %d thread events for recovery", enqueued)

    observer = WatcherObserver(
        threads_root=settings.paths.threads_dir,
        queue=queue,
        loop=loop,
    )

    async with PhanthandClient(
        endpoint=settings.phanthand.endpoint,
        api_key=api_key,
        timeout_seconds=settings.phanthand.timeout_seconds,
    ) as phanthand:
        dispatcher = ThreadDispatcher(
            base_dir=settings.paths.data_dir,
            phanthand_client=phanthand,
            dedup=dedup,
            max_concurrent=settings.watcher.max_concurrent_threads,
        )

        observer.start()
        # Strong refs to in-flight tasks: asyncio holds these only
        # weakly, so we keep them alive until they complete. We discard
        # finished tasks via the done callback to bound memory.
        in_flight: set[asyncio.Task[None]] = set()
        try:
            while True:
                event = await queue.get()
                # Fan out into a background task so the consumer keeps
                # draining the queue while a slow invoke runs.
                task = asyncio.create_task(_safe_handle(dispatcher, event))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
        finally:
            observer.stop()


async def _safe_handle(dispatcher: ThreadDispatcher, event: ThreadEvent) -> None:
    try:
        await dispatcher.handle(event)
    except asyncio.CancelledError:
        # Cancellation must propagate so shutdown can complete; ``Exception``
        # below would catch this in versions where ``CancelledError`` is a
        # subclass, swallowing the cancel signal.
        raise
    except Exception:
        logger.exception(
            "dispatcher failed on thread_id=%s seq=%d",
            event.thread_id,
            event.seq,
        )


__all__ = ["run_watcher"]
