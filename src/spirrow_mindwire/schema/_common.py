"""Shared types and base classes for MindWire schemas.

Mirrors the contracts defined in ``docs/architecture.md`` §3. All models
in this package extend :class:`StrictModel` so unknown fields are
rejected at parse time — protecting the world-view boundary called out
in §1 (no ``related.*`` / ``external_refs.*`` / ``metadata.*`` creep).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, assert_never

from pydantic import AfterValidator, BaseModel, ConfigDict
from ulid import ULID

SCHEMA_VERSION = 1
"""Schema version for ThreadMeta on-disk YAML format.

NOTE: This version is INDEPENDENT from event log schema version
(:class:`spirrow_mindwire.schema.event._BaseEvent` ``schema_version``).
The numeric value happening to be 1 in both cases is coincidental;
bumping one does not require bumping the other. See
``docs/architecture.md`` §3 for the snapshot vs audit log boundary.
"""

Participant = Literal["claude.ai", "claude-code"]
ThreadStatus = Literal["active", "retrying", "terminated", "resolved", "archived"]
"""MindWire watcher's filesystem-level thread state.

NOTE: This is a separate namespace from ChatRoom (chatroom-magickit)
thread state (``active / awaiting_reply / resolved / superseded /
parked``). The ``active`` / ``resolved`` name overlap is incidental,
not semantic. See ``docs/feature-2-design.md`` §3 for the lifecycle
state machine.
"""

TerminatedReason = Literal["retry-exhausted", "validation-failed"]
"""Reason for transitioning into ``terminated`` lifecycle state.

- ``retry-exhausted``: retry loop hit ``max_retries`` without success
- ``validation-failed``: schema-level error (no retry would help)

See ``docs/feature-2-design.md`` §3.3 / §3.4.
"""


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


def opposite_of(participant: Participant) -> Participant:
    """Return the other Participant under the Phase 0 2-party invariant.

    ``Participant`` is a 2-party Literal (claude.ai / claude-code) and
    Phase 0 hard-codes this assumption (D-1, docs/feature-2-design.md
    §1.2). The function exists to give the concept of "the other side"
    an explicit name, so callers updating ``awaiting_from`` after a
    write_reply success do not silently re-encode the pairing inline.

    **Exhaustive match + `assert_never`** (PR #40 review Copilot inline-4 /
    claude.ai N1): a plain ``"claude-code" if ... else "claude.ai"``
    silently maps any future Literal expansion (e.g. an additional
    ``"operator"`` participant in Phase 1+) to ``"claude.ai"``. Using
    ``match`` with explicit cases plus ``assert_never`` makes that
    failure mode loud: mypy fails at the call site, and at runtime an
    unknown participant raises ``AssertionError`` instead of returning
    a misleading default.
    """
    match participant:
        case "claude.ai":
            return "claude-code"
        case "claude-code":
            return "claude.ai"
        case _:
            assert_never(participant)


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
    "TerminatedReason",
    "ThreadStatus",
    "UTCDatetime",
    "UlidStr",
    "opposite_of",
]
