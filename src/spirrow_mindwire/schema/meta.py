"""Thread metadata model (`threads/<ULID>/meta.yaml`).

See ``docs/architecture.md`` §3.1.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from ._common import (
    AwareDatetime,
    Participant,
    StrictModel,
    ThreadStatus,
    UlidStr,
)


class ThreadMeta(StrictModel):
    """Thread metadata persisted as ``meta.yaml``.

    ``title`` defaults to ``""`` — untitled threads are valid in Phase 0
    because the watcher creates them before any human / agent has reason
    to title them. ``tags`` likewise defaults to an empty tuple; per §3.1,
    only the participants themselves may write tags (Connectors must not).
    """

    schema_version: Literal[1]
    thread_id: UlidStr
    title: str = ""
    status: ThreadStatus
    participants: tuple[Participant, ...]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    tags: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("participants")
    @classmethod
    def _participants_non_empty(cls, v: tuple[Participant, ...]) -> tuple[Participant, ...]:
        if len(v) == 0:
            raise ValueError("participants must not be empty")
        return v


__all__ = ["ThreadMeta"]
