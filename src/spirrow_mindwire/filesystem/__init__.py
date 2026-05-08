"""On-disk layout and atomic write primitives for MindWire.

Splits cleanly into three concerns:

- :func:`atomic_write_text` — durable, watcher-safe write of a single file
- :class:`ThreadDirLayout` — path computation for a thread (no I/O)
- :class:`EventLogWriter` — append-only JSONL writer for the per-thread
  event log
"""

from __future__ import annotations

from .atomic import atomic_write_text
from .event_log import EventLogWriter
from .thread_dir import ThreadDirLayout

__all__ = ["EventLogWriter", "ThreadDirLayout", "atomic_write_text"]
