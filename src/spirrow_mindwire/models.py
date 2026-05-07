"""Core data models for Spirrow MindWire.

Models mirror the schemas defined in `docs/architecture.md` (§3) and
`docs/mcp-interface.md` (§4). Serialization layer (YAML frontmatter /
JSONL events) is implemented in T06 (watcher).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ThreadStatus = Literal["active", "awaiting-cc", "awaiting-cai", "resolved", "archived"]
Participant = Literal["claude.ai", "claude-code"]

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ThreadMeta:
    """Thread metadata, persisted as `meta.yaml` in each thread directory."""

    schema_version: int
    thread_id: str  # ULID
    title: str
    status: ThreadStatus
    participants: tuple[Participant, ...]
    created_at: datetime
    updated_at: datetime
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Message:
    """A single message in a thread.

    Field `from_` (trailing underscore) maps to `from` in YAML frontmatter
    serialization (`from` is a Python reserved keyword).
    """

    schema_version: int
    msg_id: str  # <thread_id>/<seq>
    seq: int
    from_: Participant
    to: Participant
    created_at: datetime
    body: str
    reply_to: int | None = None
