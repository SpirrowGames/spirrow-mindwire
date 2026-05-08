"""Thread metadata model (`threads/<ULID>/meta.yaml`).

See ``docs/architecture.md`` §3.1.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from ._common import (
    SCHEMA_VERSION,
    AwareDatetime,
    Participant,
    StrictModel,
    ThreadStatus,
    UlidStr,
)


class ThreadMeta(StrictModel):
    schema_version: int = SCHEMA_VERSION
    thread_id: UlidStr
    title: str = ""
    status: ThreadStatus
    participants: tuple[Participant, ...]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    tags: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("schema_version")
    @classmethod
    def _pin_schema_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported schema_version={v}; ThreadMeta requires "
                f"schema_version={SCHEMA_VERSION}"
            )
        return v

    @field_validator("participants")
    @classmethod
    def _participants_non_empty(
        cls, v: tuple[Participant, ...]
    ) -> tuple[Participant, ...]:
        if len(v) == 0:
            raise ValueError("participants must not be empty")
        return v


__all__ = ["ThreadMeta"]
