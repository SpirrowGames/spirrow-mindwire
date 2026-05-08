"""ULID generation utilities.

architecture.md §7.3 picks ULID for thread_id / event_id (sortable by
time, file-name safe, collision-resistant). Random ULIDs are sufficient
for Phase 0 — the 80-bit random component makes within-millisecond
collisions astronomically unlikely under the watcher's single-process
issue rate. Monotonic ordering will be revisited in Phase 1+ if higher
issue rates surface.
"""

from __future__ import annotations

from ulid import ULID


def new_ulid() -> str:
    """Return a fresh ULID as the canonical 26-character string."""
    return str(ULID())


__all__ = ["new_ulid"]
