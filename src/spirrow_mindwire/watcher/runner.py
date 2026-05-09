"""Watcher entry point: wire observer + dispatcher together and run.

Phase 0 happy path: a single coroutine starts the observer, drains
the asyncio queue, and forwards each :class:`ThreadEvent` to the
dispatcher. ``Ctrl+C`` cancels the consumer task and stops the
observer cleanly. Lifecycle hardening (graceful shutdown grace
window, startup full-scan, orphan staging cleanup) is queued for a
later sub-PR.
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

logger = logging.getLogger(__name__)


async def run_watcher(settings: MindwireSettings, *, api_key: str | None) -> None:
    """Run the watcher until cancellation.

    *api_key* is read from ``os.environ[settings.phanthand.api_key_env]``
    by the CLI wrapper (kept out of :class:`MindwireSettings` so secrets
    don't end up in TOML).
    """

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    dedup = DedupCache(ttl=timedelta(seconds=settings.watcher.dedup_ttl_seconds))

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
    except Exception:
        logger.exception(
            "dispatcher failed on thread_id=%s seq=%d",
            event.thread_id,
            event.seq,
        )


__all__ = ["run_watcher"]
