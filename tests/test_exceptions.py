"""Tests for ADR-2026-05-21-06 §3.4 Port exception catalog (PR-A / Step 0)."""

from __future__ import annotations

import pytest

from spirrow_mindwire.exceptions import (
    AdapterDeliveryError,
    AdapterError,
    AdapterHaltError,
    AdapterHealthError,
    AdapterSpawnError,
)

_PORT_EXCEPTIONS = (
    AdapterSpawnError,
    AdapterHaltError,
    AdapterHealthError,
    AdapterDeliveryError,
)


def test_all_port_exceptions_share_adapter_error_root() -> None:
    for cls in _PORT_EXCEPTIONS:
        assert issubclass(cls, AdapterError)
        assert issubclass(cls, Exception)


def test_adapter_error_root_catches_every_port_exception() -> None:
    # The dispatcher catches the base class hierarchy (ADR-06 §3.4).
    for cls in _PORT_EXCEPTIONS:
        with pytest.raises(AdapterError):
            raise cls("boom")
