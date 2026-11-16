"""clone_slot: the pooled slot layout (msg-9385 option A, msg-9386 D-1 改訂 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from spirrow_mindwire import clone_slot as cs

REPO = "spirrow-mindwire"


def test_slot_names_are_two_digit_one_based() -> None:
    assert cs.slot_name(1) == "slot-01"
    assert cs.slot_name(12) == "slot-12"
    assert cs.slot_name(100) == "slot-100"
    with pytest.raises(ValueError):
        cs.slot_name(0)


@pytest.mark.parametrize(
    ("name", "index"),
    [
        ("slot-01", 1),
        ("slot-10", 10),
        ("slot-100", 100),
        ("slot-1", None),
        ("slot-00", None),
        ("slot-007", None),
        ("abc123def456", None),
        ("slot-01.lock", None),
    ],
)
def test_slot_index_round_trips_only_canonical_names(name: str, index: int | None) -> None:
    assert cs.slot_index(name) == index
    if index is not None:
        assert cs.slot_name(index) == name


def test_slot_dirs_list_the_whole_pool_in_order(tmp_path: Path) -> None:
    root = tmp_path / REPO
    assert cs.slot_dirs(tmp_path, REPO, 3) == [root / "slot-01", root / "slot-02", root / "slot-03"]
    with pytest.raises(ValueError):
        cs.slot_dirs(tmp_path, REPO, 0)


def test_slot_path_does_not_depend_on_the_thread(tmp_path: Path) -> None:
    """D-1 改訂: a slot belongs to no thread; its path is a function of repo and index only."""
    ref = cs.SlotRef(tmp_path, REPO, 2)
    assert ref.name == "slot-02"
    assert ref.path == tmp_path / REPO / "slot-02"


def test_mirror_and_quarantine_paths(tmp_path: Path) -> None:
    assert cs.mirror_path(tmp_path, REPO) == tmp_path / f"{REPO}.git"
    assert cs.quarantine_root(tmp_path, REPO) == tmp_path / "_quarantine" / REPO


def test_pure_path_arithmetic_creates_nothing(tmp_path: Path) -> None:
    cs.slot_dirs(tmp_path, REPO, 4)
    cs.mirror_path(tmp_path, REPO)
    cs.quarantine_root(tmp_path, REPO)
    assert list(tmp_path.iterdir()) == []
