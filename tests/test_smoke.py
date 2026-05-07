"""Smoke test: verify the package imports and core models construct."""

from __future__ import annotations

from datetime import UTC, datetime

from spirrow_mindwire import __version__
from spirrow_mindwire.models import SCHEMA_VERSION, Message, ThreadMeta


def test_version_is_string() -> None:
    """The package exposes a non-empty version string."""
    assert isinstance(__version__, str)
    assert len(__version__) > 0


def test_thread_meta_construct() -> None:
    """ThreadMeta dataclass constructs with expected fields."""
    now = datetime.now(UTC)
    meta = ThreadMeta(
        schema_version=SCHEMA_VERSION,
        thread_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        title="test thread",
        status="active",
        participants=("claude.ai", "claude-code"),
        created_at=now,
        updated_at=now,
    )
    assert meta.thread_id == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert meta.tags == ()


def test_message_construct() -> None:
    """Message dataclass constructs with expected fields."""
    now = datetime.now(UTC)
    msg = Message(
        schema_version=SCHEMA_VERSION,
        msg_id="01ARZ3NDEKTSV4RRFFQ69G5FAV/001",
        seq=1,
        from_="claude.ai",
        to="claude-code",
        created_at=now,
        body="hello",
    )
    assert msg.seq == 1
    assert msg.reply_to is None
