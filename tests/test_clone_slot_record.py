"""R-7' / R-8' of T-ephemeral-impl-clone-slot-per-thread (Bohr msg-8084 / msg-8086 / msg-8088).

The turn-pre slot procedure writes one ``slot_quarantine`` row per decided outcome to the same
``state/clone-retreats.jsonl`` PR-1b reads; the next implementer gets it through the same
``pending_record`` / ``render_notice`` path, worded per ``action``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.clone_retreat_log import (
    RETREAT_KIND,
    SLOT_ACTION_PARKED,
    SLOT_ACTION_REBUILT,
    SLOT_QUARANTINE_KIND,
    append_slot_quarantine,
    build_slot_quarantine_record,
    mark_delivered,
    pending_record,
    render_notice,
)

SLOT = Path("/slots/mindwire/abcdef012345")
QDIR = Path("/slots/_quarantine/mindwire/abcdef012345-20261108-010203")


def _rebuilt(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "thread_id": "T-x",
        "repo": "mindwire",
        "slot_path": SLOT,
        "action": SLOT_ACTION_REBUILT,
        "quarantine_reason": "modified",
        "quarantine_path": QDIR,
        "patch": Path("/state/clone-retreats/p.patch"),
        "clone_seconds": 0.4,
        "slot_bytes": 1234,
    }
    base.update(kw)
    return build_slot_quarantine_record(**base)


def test_row_shape_uses_null_not_none_string() -> None:
    row = _rebuilt()
    assert row["kind"] == SLOT_QUARANTINE_KIND
    assert row["bundle"] is None and row["untracked"] is None
    assert row["park_code"] is None
    assert row["quarantine_path"] == str(QDIR)
    assert "none" not in [v for v in row.values() if isinstance(v, str)]


def test_parked_row_drops_clone_metrics_and_requires_park_code() -> None:
    row = build_slot_quarantine_record(
        thread_id="T-x",
        repo="mindwire",
        slot_path=SLOT,
        action=SLOT_ACTION_PARKED,
        quarantine_reason="preserve_failed",
        park_code="clone_slot_preserve_failed",
        clone_seconds=3.0,
        slot_bytes=99,
    )
    assert row["clone_seconds"] is None and row["slot_bytes"] is None
    with pytest.raises(ValueError):
        build_slot_quarantine_record(
            thread_id="T-x",
            repo="m",
            slot_path=SLOT,
            action=SLOT_ACTION_PARKED,
            quarantine_reason="modified",
        )
    with pytest.raises(ValueError):
        _rebuilt(park_code="x")
    with pytest.raises(ValueError):
        _rebuilt(quarantine_reason="nope")


def test_pending_record_picks_slot_row_and_delivered_hides_it(tmp_path: Path) -> None:
    log = tmp_path / "clone-retreats.jsonl"
    row = _rebuilt()
    append_slot_quarantine(log, row)
    got = pending_record(log, "T-x")
    assert got is not None and got["record_id"] == row["record_id"]
    mark_delivered(log, got, session_role="implementer")
    assert pending_record(log, "T-x") is None


def test_rebuilt_notice_says_clean_and_shows_only_non_null_artefacts() -> None:
    text = render_notice(_rebuilt())
    assert "clean" in text and "rebuilt" in text
    assert str(QDIR) in text
    assert "git apply" in text
    assert "bundle:" not in text and "untracked list:" not in text
    assert "none" not in text.lower().split()


def test_parked_notice_never_says_rebuilt_or_clean_and_names_untouched_slot() -> None:
    for reason, code in (
        ("preserve_failed", "clone_slot_preserve_failed"),
        ("unpublished", "clone_slot_unpublished"),
    ):
        row = build_slot_quarantine_record(
            thread_id="T-x",
            repo="mindwire",
            slot_path=SLOT,
            action=SLOT_ACTION_PARKED,
            quarantine_reason=reason,
            park_code=code,
            bundle=Path("/state/clone-retreats/b.bundle"),
        )
        text = render_notice(row)
        assert "rebuilt" not in text and "clean" not in text
        assert code in text
        assert f"left untouched at {SLOT}" in text
        assert "git fetch <bundle>" in text


def test_parked_after_quarantine_names_the_original_quarantine_path() -> None:
    row = build_slot_quarantine_record(
        thread_id="T-x",
        repo="mindwire",
        slot_path=SLOT,
        action=SLOT_ACTION_PARKED,
        quarantine_reason="provision_failed",
        park_code="clone_slot_provision_failed",
        quarantine_path=QDIR,
    )
    text = render_notice(row)
    assert f"moved to {QDIR}" in text
    assert "clean" not in text and "untouched" not in text


def test_unconfirmed_notice_names_both_paths_and_asserts_no_place() -> None:
    to = Path("/slots/_quarantine/mindwire/x")
    row = build_slot_quarantine_record(
        thread_id="T-x",
        repo="mindwire",
        slot_path=SLOT,
        action=SLOT_ACTION_PARKED,
        quarantine_reason="quarantine_unconfirmed",
        park_code="clone_slot_quarantine_unconfirmed",
        quarantine_candidates=[SLOT, to],
    )
    assert row["quarantine_path"] is None
    text = render_notice(row)
    assert str(SLOT) in text and str(to) in text
    assert "NOT determined" in text
    assert "untouched" not in text and "moved to" not in text


def test_retreat_rows_still_win_when_newer(tmp_path: Path) -> None:
    """Both kinds compete on recency only; a retreat row renders with the PR-1b wording."""
    log = tmp_path / "clone-retreats.jsonl"
    append_slot_quarantine(log, _rebuilt())
    import json

    with log.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "kind": RETREAT_KIND,
                    "record_id": "T-x@later#1",
                    "thread_id": "T-x",
                    "outcome": "left_for_slot",
                }
            )
            + "\n"
        )
    got = pending_record(log, "T-x")
    assert got is not None and got["kind"] == RETREAT_KIND
