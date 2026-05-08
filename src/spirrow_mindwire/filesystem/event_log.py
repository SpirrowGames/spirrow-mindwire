"""Append-only JSONL writer for thread event logs.

``logs/threads/<ULID>.jsonl`` — see ``docs/architecture.md`` §3.3. Each
line is one serialized :data:`spirrow_mindwire.schema.Event`.

Phase 0 invariants keep this simple: a single watcher process owns
writes, and per-thread operations serialize naturally, so a plain
``open(mode='a')`` is correct. Multi-process or multi-host concurrency
hardening (lock files, fsync per write, log rotation) is deferred to
Phase 1+ where the cost-benefit actually flips.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter

from spirrow_mindwire.schema import Event

_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


class EventLogWriter:
    """Append :data:`Event` instances to a thread's JSONL log."""

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path

    @property
    def log_path(self) -> Path:
        return self._log_path

    def append(self, event: Event) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        line = _EVENT_ADAPTER.dump_json(event, by_alias=True).decode("utf-8")
        with open(self._log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")


__all__ = ["EventLogWriter"]
