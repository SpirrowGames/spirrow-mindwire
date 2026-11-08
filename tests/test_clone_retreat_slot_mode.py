"""R-9 of T-ephemeral-impl-clone-slot-per-thread (Bohr msg-8084): slot mode never discards.

With ``[loop].slots_root`` set, PR-1a's launch-end retreat only commits to ``wip/`` and pushes.
Whatever it cannot do is left in the slot (``left_for_slot``): no export, no abort, no
``reset --hard``, no ``clean -fd``, no ``branch -D``. Without slot mode, PR-1a is unchanged (INV-0).
Real git repositories, reusing the PR-1a fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from test_clone_retreat import THREAD, _conflicted_merge, _git
from test_clone_retreat import clone as clone
from test_clone_retreat import origin as origin

from spirrow_mindwire.clone_retreat import OK_EXIT, CloneRetreater, RetreatOutcome
from spirrow_mindwire.clone_retreat_log import build_record, render_notice

DESTRUCTIVE = ("reset", "clean", "branch -D", "merge --abort", "rebase --abort")


def _retreater(clone: Path, state: Path, *, slot_mode: bool) -> CloneRetreater:
    return CloneRetreater(
        clone,
        thread_id=THREAD,
        head_msg="msg-1",
        stop_reason="adapter.turn_timeout",
        state_dir=state,
        now=lambda: datetime(2026, 11, 6, 1, 2, 3, tzinfo=UTC),
        slot_mode=slot_mode,
    )


def _ran(steps: list[str]) -> list[str]:
    return [s.split(" -> ")[0] for s in steps]


def _assert_nothing_destructive(steps: list[str]) -> None:
    for ran in _ran(steps):
        assert not any(ran.startswith(f"git {d}") for d in DESTRUCTIVE), ran


def test_slot_mode_push_failure_is_left_for_slot_without_reset_or_clean(
    clone: Path, tmp_path: Path
) -> None:
    _git(clone, "remote", "set-url", "--push", "origin", str(tmp_path / "no-such-remote.git"))
    (clone / "a.txt").write_text("base\nlocal\n", encoding="utf-8")
    res = _retreater(clone, tmp_path / "state", slot_mode=True).retreat()
    assert res.outcome is RetreatOutcome.LEFT_FOR_SLOT, res.row()
    assert res.pushed is False
    assert res.exit_code == OK_EXIT
    _assert_nothing_destructive(res.steps)
    # The work is in the local wip commit, which the next turn-pre bundles (unpublished).
    assert _git(clone, "show", f"{res.branch}:a.txt") == "base\nlocal\n"
    assert not (tmp_path / "state").exists(), "slot mode exports nothing at launch end"


def test_slot_mode_unretreatable_merge_is_left_as_is(
    clone: Path, origin: Path, tmp_path: Path
) -> None:
    _conflicted_merge(clone, origin, tmp_path)
    (clone / "a.txt").write_text("HAND-RESOLVED\n", encoding="utf-8")
    res = _retreater(clone, tmp_path / "state", slot_mode=True).retreat()
    assert res.outcome is RetreatOutcome.LEFT_FOR_SLOT, res.row()
    assert res.steps == [], "nothing at all runs on an unretreatable slot"
    assert (clone / ".git" / "MERGE_HEAD").exists()
    assert (clone / "a.txt").read_text(encoding="utf-8") == "HAND-RESOLVED\n"


def test_slot_mode_successful_retreat_is_unchanged(clone: Path, tmp_path: Path) -> None:
    (clone / "a.txt").write_text("base\nmore\n", encoding="utf-8")
    res = _retreater(clone, tmp_path / "state", slot_mode=True).retreat()
    assert res.outcome is RetreatOutcome.RETREATED and res.pushed is True


def test_without_slot_mode_pr1a_discard_still_runs(
    clone: Path, origin: Path, tmp_path: Path
) -> None:
    """INV-0: the export -> abort -> reset/clean path is untouched when slots are off."""
    _conflicted_merge(clone, origin, tmp_path)
    res = _retreater(clone, tmp_path / "state", slot_mode=False).retreat()
    assert res.outcome is RetreatOutcome.DISCARDED, res.row()
    assert any(r.startswith("git merge --abort") for r in _ran(res.steps))


def test_left_for_slot_notice_wording(clone: Path, tmp_path: Path) -> None:
    _git(clone, "remote", "set-url", "--push", "origin", str(tmp_path / "no-such-remote.git"))
    (clone / "a.txt").write_text("base\nlocal\n", encoding="utf-8")
    res = _retreater(clone, tmp_path / "state", slot_mode=True).retreat()
    row = build_record(res, repo_dir=clone, head_msg="msg-1", stop_reason="x")
    assert row is not None and row["outcome"] == "left_for_slot"
    text = render_notice(row)
    assert "left in the slot" in text
    assert "quarantines the slot" in text
