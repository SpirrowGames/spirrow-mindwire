"""Append-only JSONL writer for thread event logs.

``logs/threads/<ULID>.jsonl`` — see ``docs/architecture.md`` §3.3. Each
line is one serialized :data:`spirrow_mindwire.schema.Event`.

Concurrency contract:
- The **caller** (watcher) is responsible for serializing appends to a
  given thread's log. ``EventLogWriter`` holds no lock; concurrent
  ``append()`` calls on the same instance from different threads /
  processes can interleave bytes within a single line.
- Per-thread serialization comes naturally from the Phase 0 design
  (single watcher process, per-thread asyncio task), so this is a
  contract of inheritance, not an enforcement gap.

Atomicity & durability:
- ``append()`` does one binary ``write()`` of ``payload + b"\\n"``,
  preserving the "single ``write(2)`` ≤ PIPE_BUF (=4096) is atomic"
  property on local POSIX filesystems described in
  ``docs/logging-design.md``. NFS / network filesystems can split a
  single ``write()`` and need separate locking.
- Asymmetric with :func:`atomic_write_text`: the event log is a
  *best-effort* audit trail (an unclean shutdown may lose the last few
  appends), while message and meta.yaml writes go through
  ``atomic_write_text`` and fsync because their loss would corrupt the
  thread's authoritative state. The asymmetry is intentional.
- Multi-process / multi-host hardening (lock files, fsync per write,
  log rotation) is deferred to Phase 1+ where the cost-benefit flips.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter

from spirrow_mindwire.schema import Event

_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


class EventLogWriter:
    """Append :data:`Event` instances to a thread's JSONL log.

    See module docstring for the concurrency / durability contract.
    Briefly: caller serializes appends, no internal lock; one binary
    ``write()`` per event; best-effort durability (no fsync).
    """

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path

    @property
    def log_path(self) -> Path:
        return self._log_path

    def append(self, event: Event) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = _EVENT_ADAPTER.dump_json(event, by_alias=True) + b"\n"
        # Binary append keeps each event a single low-level write,
        # preserving the "<4KB write(2) is atomic" property described in
        # docs/logging-design.md and avoiding an encode/decode round
        # trip.
        with open(self._log_path, "ab") as f:
            f.write(payload)


__all__ = ["EventLogWriter"]
