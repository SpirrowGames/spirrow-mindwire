"""Per-message model (`threads/<ULID>/messages/NNN-from-{cai|cc}.md`).

See ``docs/architecture.md`` §3.2. ``from`` is a Python reserved word, so
the attribute is named ``from_`` and exposed via ``alias="from"`` for
YAML frontmatter round-trips.
"""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from ._common import (
    SCHEMA_VERSION,
    AwareDatetime,
    Participant,
    StrictModel,
)


class Message(StrictModel):
    schema_version: int = SCHEMA_VERSION
    msg_id: str
    seq: int = Field(ge=1)
    from_: Participant = Field(alias="from")
    to: Participant
    created_at: AwareDatetime
    reply_to: int | None = Field(default=None, ge=1)
    body: str

    @field_validator("schema_version")
    @classmethod
    def _pin_schema_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported schema_version={v}; Message requires "
                f"schema_version={SCHEMA_VERSION}"
            )
        return v

    @model_validator(mode="after")
    def _check_msg_id_matches_seq(self) -> Message:
        try:
            thread_id, seq_part = self.msg_id.split("/", 1)
        except ValueError as e:
            raise ValueError(
                f"msg_id must be '<thread_id>/<seq>', got {self.msg_id!r}"
            ) from e
        if not thread_id:
            raise ValueError(f"msg_id missing thread_id prefix: {self.msg_id!r}")
        if seq_part != str(self.seq):
            raise ValueError(
                f"msg_id seq part {seq_part!r} does not match seq={self.seq}"
            )
        return self

    @model_validator(mode="after")
    def _from_and_to_must_differ(self) -> Message:
        if self.from_ == self.to:
            raise ValueError(
                f"from and to must differ; both are {self.from_!r}"
            )
        return self


__all__ = ["Message"]
