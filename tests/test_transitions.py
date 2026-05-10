"""Tests for spirrow_mindwire.lifecycle.transitions.

Covers the lifecycle state machine introduced in Feature 2:

- :data:`_ALLOWED_TRANSITIONS` table — all 8 allowed + a representative
  set of forbidden transitions (parametrized).
- :func:`transition_state` — atomic meta.yaml write, terminated_reason
  requirement, terminal-out audit trail preservation, retry_count
  pass-through.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.lifecycle import InvalidTransitionError, transition_state
from spirrow_mindwire.schema import ThreadMeta, ThreadStatus

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

# All 8 allowed transitions from Decide #3b-1 (docs/feature-2-design.md §3.3)
_ALLOWED: list[tuple[ThreadStatus, ThreadStatus]] = [
    ("active", "retrying"),
    ("active", "terminated"),
    ("active", "resolved"),
    ("retrying", "active"),
    ("retrying", "terminated"),
    ("terminated", "resolved"),
    ("terminated", "archived"),
    ("resolved", "archived"),
]

# Representative forbidden transitions (full coverage is impractical;
# pick the ones explicitly discussed in Decide #3b-1).
_FORBIDDEN: list[tuple[ThreadStatus, ThreadStatus]] = [
    ("terminated", "active"),  # auto-revival forbidden
    ("archived", "resolved"),  # immutable
    ("archived", "active"),
    ("archived", "retrying"),
    ("archived", "terminated"),
    ("resolved", "retrying"),  # backward forbidden
    ("resolved", "terminated"),
    ("resolved", "active"),
    ("active", "archived"),  # skip-resolved forbidden
    ("retrying", "resolved"),  # Naysayer reversal of claude.ai proposal
    ("retrying", "archived"),
    ("active", "active"),  # identity not allowed
]

_TERMINAL_STATES: set[ThreadStatus] = {"terminated", "resolved", "archived"}


def _seed_meta(layout: ThreadDirLayout, **overrides: Any) -> None:
    """Seed meta.yaml directly, bypassing transition_state."""
    payload: dict[str, Any] = {
        "schema_version": 1,
        "thread_id": layout.thread_id,
        "title": "",
        "status": "active",
        "awaiting_from": "claude-code",
        "participants": ["claude.ai", "claude-code"],
        "created_at": "2026-05-07T08:43:07Z",
        "updated_at": "2026-05-07T08:43:07Z",
        "tags": [],
        "retry_count": 0,
    }
    payload.update(overrides)
    layout.thread_dir.mkdir(parents=True, exist_ok=True)
    layout.meta_path.write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


@pytest.fixture
def layout(tmp_path: Path) -> ThreadDirLayout:
    return ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)


@pytest.mark.parametrize(("old", "new"), _ALLOWED)
def test_allowed_transitions_pass(
    layout: ThreadDirLayout, old: ThreadStatus, new: ThreadStatus
) -> None:
    _seed_meta(
        layout,
        status=old,
        awaiting_from=None if old in _TERMINAL_STATES else "claude-code",
    )
    extra: dict[str, Any] = {}
    if new == "terminated":
        extra["terminated_reason"] = "retry-exhausted"
    new_meta = transition_state(
        layout,
        new,
        awaiting_from=None if new in _TERMINAL_STATES else "claude.ai",
        **extra,
    )
    assert new_meta.status == new


@pytest.mark.parametrize(("old", "new"), _FORBIDDEN)
def test_forbidden_transitions_raise(
    layout: ThreadDirLayout, old: ThreadStatus, new: ThreadStatus
) -> None:
    _seed_meta(
        layout,
        status=old,
        awaiting_from=None if old in _TERMINAL_STATES else "claude-code",
    )
    extra: dict[str, Any] = {}
    if new == "terminated":
        extra["terminated_reason"] = "retry-exhausted"
    with pytest.raises(InvalidTransitionError) as exc:
        transition_state(
            layout,
            new,
            awaiting_from=None if new in _TERMINAL_STATES else "claude.ai",
            **extra,
        )
    assert exc.value.old == old
    assert exc.value.new == new


def test_transition_state_writes_meta_atomically(layout: ThreadDirLayout) -> None:
    _seed_meta(layout, status="active", awaiting_from="claude-code")
    new_meta = transition_state(layout, "retrying", awaiting_from="claude-code")

    assert new_meta.status == "retrying"
    assert new_meta.awaiting_from == "claude-code"

    on_disk = ThreadMeta.model_validate(
        yaml.safe_load(layout.meta_path.read_text(encoding="utf-8"))
    )
    assert on_disk.status == "retrying"
    assert on_disk.awaiting_from == "claude-code"


def test_transition_state_terminated_requires_reason(layout: ThreadDirLayout) -> None:
    _seed_meta(layout, status="active", awaiting_from="claude-code")
    with pytest.raises(ValueError, match="terminated_reason is required"):
        transition_state(layout, "terminated", awaiting_from=None)


def test_transition_state_sets_terminated_fields_on_entry(
    layout: ThreadDirLayout,
) -> None:
    _seed_meta(layout, status="active", awaiting_from="claude-code")
    before = datetime.now(UTC)
    new_meta = transition_state(
        layout,
        "terminated",
        awaiting_from=None,
        terminated_reason="retry-exhausted",
    )
    after = datetime.now(UTC)

    assert new_meta.status == "terminated"
    assert new_meta.awaiting_from is None
    assert new_meta.terminated_reason == "retry-exhausted"
    assert new_meta.terminated_at is not None
    assert before <= new_meta.terminated_at <= after
    # Single-now invariant: updated_at and terminated_at must share the
    # same instant within one atomic transition (no second now() drift).
    assert new_meta.updated_at == new_meta.terminated_at


def test_transition_state_preserves_terminated_fields_on_terminal_out(
    layout: ThreadDirLayout,
) -> None:
    """terminated → resolved 遷移で terminated_reason / terminated_at が保持されること.

    Audit trail として残す (Decide #3b-2、 docs §3.4)。
    """
    seeded_at = datetime(2026, 5, 7, 8, 43, 7, tzinfo=UTC)
    _seed_meta(
        layout,
        status="terminated",
        awaiting_from=None,
        terminated_reason="retry-exhausted",
        terminated_at="2026-05-07T08:43:07Z",
    )
    new_meta = transition_state(layout, "resolved", awaiting_from=None)

    assert new_meta.status == "resolved"
    assert new_meta.terminated_reason == "retry-exhausted"
    assert new_meta.terminated_at == seeded_at


def test_invalid_transition_error_message_lists_allowed(layout: ThreadDirLayout) -> None:
    _seed_meta(layout, status="archived", awaiting_from=None)
    with pytest.raises(InvalidTransitionError) as exc:
        transition_state(layout, "active", awaiting_from="claude-code")
    msg = str(exc.value)
    assert "archived" in msg
    assert "active" in msg
    assert "not allowed" in msg


def test_transition_state_retry_count_pass_through(layout: ThreadDirLayout) -> None:
    """retry_count=None preserves; explicit value updates."""
    _seed_meta(layout, status="active", awaiting_from="claude-code", retry_count=2)

    m1 = transition_state(layout, "retrying", awaiting_from="claude-code")
    assert m1.retry_count == 2

    m2 = transition_state(layout, "active", awaiting_from="claude.ai", retry_count=3)
    assert m2.retry_count == 3
