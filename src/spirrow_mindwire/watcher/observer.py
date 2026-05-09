"""Bridges watchdog (native threads) to asyncio.

A single ``Observer`` watches the threads root recursively. The
PatternMatchingEventHandler keeps us from reacting to ``*.tmp`` /
``*.rejected`` / ``*.error`` (T06 Q4 F-A). On every match we extract
the thread_id + seq from the path and push a :class:`ThreadEvent` onto
an asyncio queue using ``loop.call_soon_threadsafe`` so the consumer
side stays single-threaded.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

from watchdog.events import (
    FileSystemEvent,
    PatternMatchingEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from .events import ThreadEvent

_MESSAGE_PATH_RE = re.compile(
    r"threads[\\/](?P<thread_id>[0-9A-HJKMNP-TV-Z]{26})[\\/]"
    r"messages[\\/](?P<seq>\d{3,})-from-(?:cai|cc)\.md$"
)


class _Handler(PatternMatchingEventHandler):
    """Forward matched filesystem events into the asyncio queue."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[ThreadEvent],
    ) -> None:
        super().__init__(
            patterns=["*.md"],
            ignore_patterns=["*.tmp", "*.md.tmp", "*.rejected", "*.error"],
            ignore_directories=True,
            case_sensitive=True,
        )
        self._loop = loop
        self._queue = queue

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_emit(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._maybe_emit(event.dest_path)

    def _maybe_emit(self, raw_path: str | bytes) -> None:
        path_str = raw_path.decode() if isinstance(raw_path, bytes) else raw_path
        match = _MESSAGE_PATH_RE.search(path_str)
        if match is None:
            return
        evt = ThreadEvent(
            thread_id=match.group("thread_id"),
            seq=int(match.group("seq")),
            path=Path(path_str),
            detected_at=datetime.now(UTC),
        )
        self._loop.call_soon_threadsafe(self._queue.put_nowait, evt)


class WatcherObserver:
    """Lifetime wrapper over ``watchdog.observers.Observer``."""

    def __init__(
        self,
        threads_root: Path,
        queue: asyncio.Queue[ThreadEvent],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._threads_root = threads_root
        self._queue = queue
        self._loop = loop
        self._observer: BaseObserver | None = None

    def start(self) -> None:
        if self._observer is not None:
            return
        self._threads_root.mkdir(parents=True, exist_ok=True)
        observer = Observer()
        observer.schedule(
            _Handler(self._loop, self._queue),
            str(self._threads_root),
            recursive=True,
        )
        observer.start()
        self._observer = observer

    def stop(self, *, timeout: float = 5.0) -> None:
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=timeout)
        self._observer = None


__all__ = ["WatcherObserver"]
