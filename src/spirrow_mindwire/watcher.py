"""Mindwire watcher daemon.

Monitors `new/` for new threads and `threads/<ULID>/messages/` for replies,
invokes claude-code as a subprocess, and writes events to JSONL logs.
Implementation is part of T06.
"""

from __future__ import annotations


def main() -> None:
    """Entry point for the `mindwire-watcher` CLI."""
    raise NotImplementedError("Watcher implementation is part of T06.")
