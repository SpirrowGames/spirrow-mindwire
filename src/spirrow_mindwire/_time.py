"""Time-formatting helper shared across the codebase.

architecture.md §3 mandates UTC ISO 8601 with the ``Z`` suffix for all
MindWire-emitted timestamps. The helper lives in this neutral top-level
module so both the schema-aware prompt builder and the
schema-bypassing MCP tool implementation can share one strict
formatter — and so a future caller never accidentally re-implements a
laxer version.
"""

from __future__ import annotations

from datetime import UTC, datetime


def iso_z(dt: datetime) -> str:
    """Render a UTC datetime as ``YYYY-MM-DDTHH:MM:SSZ`` (architecture.md §3).

    Naive datetimes are rejected loudly so a forgotten ``tzinfo`` does
    not produce a timestamp that *looks* UTC but isn't. Non-UTC aware
    datetimes are converted via ``astimezone(UTC)``; we never emit a
    ``Z`` suffix on a wall-clock value from another zone.
    """

    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            "datetime must be timezone-aware (architecture.md §3 mandates UTC ISO 8601)"
        )
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["iso_z"]
