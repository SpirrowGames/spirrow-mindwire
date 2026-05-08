"""Shared types and base classes for MindWire schemas.

Mirrors the contracts defined in ``docs/architecture.md`` §3. All models
in this package extend :class:`StrictModel` so unknown fields are
rejected at parse time — protecting the world-view boundary called out
in §1 (no ``related.*`` / ``external_refs.*`` / ``metadata.*`` creep).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict
from ulid import ULID

SCHEMA_VERSION = 1

Participant = Literal["claude.ai", "claude-code"]
ThreadStatus = Literal["active", "awaiting-cc", "awaiting-cai", "resolved", "archived"]


def _validate_ulid(v: str) -> str:
    ULID.from_str(v)
    return v


def _require_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError(
            "datetime must be timezone-aware (architecture.md §3 mandates UTC ISO 8601)"
        )
    return v


UlidStr = Annotated[str, AfterValidator(_validate_ulid)]
AwareDatetime = Annotated[datetime, AfterValidator(_require_aware)]


class StrictModel(BaseModel):
    """Base for all schema models: forbid extras, frozen, populate-by-name."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


__all__ = [
    "SCHEMA_VERSION",
    "AwareDatetime",
    "Participant",
    "StrictModel",
    "ThreadStatus",
    "UlidStr",
]
