"""Tests for :mod:`spirrow_mindwire.ulid_util`."""

from __future__ import annotations

from pydantic import TypeAdapter

from spirrow_mindwire.schema import UlidStr
from spirrow_mindwire.ulid_util import new_ulid

_ULID_ADAPTER: TypeAdapter[UlidStr] = TypeAdapter(UlidStr)


def test_new_ulid_returns_26_char_string() -> None:
    u = new_ulid()
    assert isinstance(u, str)
    assert len(u) == 26


def test_new_ulid_passes_schema_validation() -> None:
    """Generated values must be acceptable as ``thread_id`` / ``event_id``."""
    u = new_ulid()
    assert _ULID_ADAPTER.validate_python(u) == u


def test_new_ulid_values_are_unique() -> None:
    values = {new_ulid() for _ in range(1024)}
    assert len(values) == 1024
