"""Per-message model (`threads/<ULID>/messages/NNN-from-{cai|cc}.md`).

See ``docs/architecture.md`` §3.2.

- ``from`` is a Python reserved word, so the attribute is named
  ``from_`` and exposed via ``alias="from"`` for YAML frontmatter
  round-trips. ``populate_by_name=True`` is inherited from
  :class:`StrictModel` so test code can pass ``from_=...`` directly;
  on-disk YAML always uses the canonical ``from`` key.
- ``from_`` and ``to`` identify *sender* and *recipient*, not the
  direction relative to MindWire. A claude.ai → claude-code message
  has ``from="claude.ai"``, ``to="claude-code"`` regardless of which
  side serializes it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from ulid import ULID

from ._common import (
    AwareDatetime,
    Participant,
    StrictModel,
)


def _zero_padded_seq(seq: int) -> str:
    """Render *seq* with the §3.2 padding rule (3 digits, 4+ on overflow).

    Kept symmetric with ``filesystem.thread_dir._message_filename`` so
    ``msg_id`` and the on-disk filename always agree.
    """

    width = 3 if seq < 1000 else len(str(seq))
    return f"{seq:0{width}d}"


class Message(StrictModel):
    """A single message in a thread.

    ``reply_to`` is an *intra-thread* seq reference — a message can only
    reply to an earlier message in the same thread (``reply_to < seq``).
    Cross-thread replies are intentionally unrepresentable in this
    schema; if Phase 1+ needs them, the field type would change to
    ``str (msg_id)`` rather than be silently widened.
    """

    schema_version: Literal[1]
    msg_id: str
    seq: int = Field(ge=1)
    from_: Participant = Field(alias="from")
    to: Participant
    created_at: AwareDatetime
    reply_to: int | None = Field(default=None, ge=1)
    body: str

    @model_validator(mode="after")
    def _check_msg_id_matches_seq(self) -> Message:
        try:
            thread_id, seq_part = self.msg_id.split("/", 1)
        except ValueError as e:
            raise ValueError(f"msg_id must be '<thread_id>/<seq>', got {self.msg_id!r}") from e
        if not thread_id:
            raise ValueError(f"msg_id missing thread_id prefix: {self.msg_id!r}")
        try:
            ULID.from_str(thread_id)
        except ValueError as e:
            raise ValueError(f"msg_id thread_id prefix is not a ULID: {thread_id!r}") from e
        expected_seq_part = _zero_padded_seq(self.seq)
        if seq_part != expected_seq_part:
            raise ValueError(
                f"msg_id seq part {seq_part!r} does not match seq={self.seq} "
                f"(expected {expected_seq_part!r}, see architecture.md §3.2 "
                "padding rule)"
            )
        return self

    @model_validator(mode="after")
    def _from_and_to_must_differ(self) -> Message:
        if self.from_ == self.to:
            raise ValueError(f"from and to must differ; both are {self.from_!r}")
        return self

    @model_validator(mode="after")
    def _reply_to_must_precede_seq(self) -> Message:
        if self.reply_to is not None and self.reply_to >= self.seq:
            raise ValueError(
                f"reply_to={self.reply_to} must be < seq={self.seq} "
                "(messages can only reply to earlier seqs in the same thread)"
            )
        return self


__all__ = ["Message"]
