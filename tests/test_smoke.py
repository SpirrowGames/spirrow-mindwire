"""Smoke test: verify the package imports cleanly."""

from __future__ import annotations

from spirrow_mindwire import __version__


def test_version_is_string() -> None:
    assert isinstance(__version__, str)
    assert len(__version__) > 0
