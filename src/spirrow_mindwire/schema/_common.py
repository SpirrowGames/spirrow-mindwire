"""Shared types and base classes for MindWire schemas.

Mirrors the contracts defined in ``docs/architecture.md`` §3. All models
in this package extend :class:`StrictModel` so unknown fields are
rejected at parse time — protecting the world-view boundary called out
in §1 (no ``related.*`` / ``external_refs.*`` / ``metadata.*`` creep).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict
from ulid import ULID

SCHEMA_VERSION = 1

Participant = Literal["claude.ai", "claude-code"]
ThreadStatus = Literal["active", "awaiting-cc", "awaiting-cai", "resolved", "archived"]


def _validate_ulid(v: str) -> str:
    ULID.from_str(v)
    return v


def _normalize_to_utc(v: datetime) -> datetime:
    """Coerce a UTC-equivalent datetime to a UTC-tagged datetime.

    architecture.md §3 mandates UTC ISO 8601 in the *recorded* form,
    so aware datetimes in other zones (e.g. ``datetime.now()`` with
    the system tz) are silently normalized via ``astimezone(UTC)``.

    Programmer-error inputs are still rejected:
    - naive datetimes (``tzinfo is None``)
    - pseudo-aware datetimes whose ``utcoffset()`` returns ``None``
    """

    offset = v.utcoffset() if v.tzinfo is not None else None
    if offset is None:
        raise ValueError(
            "datetime must be timezone-aware (architecture.md §3 mandates UTC ISO 8601)"
        )
    return v.astimezone(UTC)


UlidStr = Annotated[str, AfterValidator(_validate_ulid)]
UTCDatetime = Annotated[datetime, AfterValidator(_normalize_to_utc)]
"""A UTC-tagged ``datetime`` annotation for pydantic models.

The ``AfterValidator`` runs ``_normalize_to_utc`` on every input, which
means **the stored value differs in representation** from inputs
carrying non-UTC offsets — same instant in time, but different
``tzinfo`` / ``utcoffset()`` / ``isoformat()`` output:

>>> # input: 17:43 in JST (offset +09:00)
>>> # stored: 08:43 in UTC (offset +00:00)
>>> # ``input == stored`` is True (Python compares aware datetimes by
>>> # absolute time), but the on-disk YAML form, ``isoformat()``, and
>>> # ``tzinfo`` all differ — so anything that hashes / serializes the
>>> # datetime sees a different value after the round-trip.

Naive datetimes are rejected outright. The name was chosen over the
shorter ``AwareDatetime`` (the prior name) because the latter only
implies tz-awareness, not the *forced UTC normalization* that callers
need to know about when round-tripping values through the schema.
"""


class StrictModel(BaseModel):
    """Base for all *schema* models: forbid extras, frozen, populate-by-name.

    Deliberately distinct from :class:`spirrow_mindwire.config._StrictModel`
    (which is ``extra='forbid'`` only): on-disk schema values must be
    immutable so a parsed-then-mutated record cannot drift from its
    on-disk representation, while config sub-models stay mutable in
    Phase 0 to avoid an unaudited interaction with pydantic-settings'
    env-override path. The two bases share the *spirit* (forbid
    unknowns) but not the implementation, and they live in their own
    layers on purpose — see ``feedback_decoupling_preference``.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


__all__ = [
    "SCHEMA_VERSION",
    "Participant",
    "StrictModel",
    "ThreadStatus",
    "UTCDatetime",
    "UlidStr",
]
