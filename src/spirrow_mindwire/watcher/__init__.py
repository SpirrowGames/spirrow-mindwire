"""MindWire watcher daemon (Phase 0 happy path).

Sub-modules:

- :mod:`events` — internal ``ThreadEvent`` dataclass
- :mod:`dedup` — TTL-bounded ``(thread_id, seq)`` dedup
- :mod:`observer` — watchdog → asyncio bridge
- :mod:`loader` — read meta.yaml + messages from disk
- :mod:`dispatcher` — per-event invoke + event-log persist
- :mod:`runner` — entry point that wires everything

Robustness (timeout, retry, dead-letter, validation) and lifecycle
(startup full-scan, graceful shutdown grace window) live in Feature 2
(``develop/feat-robustness``) — this module is intentionally minimal.
"""

from __future__ import annotations

import asyncio
import logging
import os

from spirrow_mindwire.config import load_settings

from .dedup import DedupCache
from .dispatcher import ThreadDispatcher
from .events import ThreadEvent
from .loader import load_messages, load_thread_meta
from .observer import WatcherObserver
from .runner import run_watcher

__all__ = [
    "DedupCache",
    "ThreadDispatcher",
    "ThreadEvent",
    "WatcherObserver",
    "load_messages",
    "load_thread_meta",
    "main",
    "run_watcher",
]


def main() -> None:
    """Entry point for the ``mindwire-watcher`` console script."""
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    api_key = os.environ.get(settings.phanthand.api_key_env)
    asyncio.run(run_watcher(settings, api_key=api_key))
