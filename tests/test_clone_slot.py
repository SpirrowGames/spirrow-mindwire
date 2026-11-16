"""Tests for clone_slot's owner classification and step-0 routing (msg-8070 v2.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spirrow_mindwire import clone_slot as cs

REPO = "spirrow-mindwire"
TID = "T-example-thread"


def _owner(path: Path, **over: object) -> None:
    data = {
        "repo": REPO,
        "thread_id": TID,
        "complete": True,
        "created_at": "2026-11-07T00:00:00+00:00",
    }
    data.update(over)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_hash12_is_sha1_prefix() -> None:
    import hashlib

    assert cs.thread_hash12(TID) == hashlib.sha1(TID.encode()).hexdigest()[:12]


@pytest.mark.parametrize(
    "content",
    [b"", b'{"repo": "spirrow-mindwire", "thread', b"[]", b"\xff\xfe"],
)
def test_unreadable_owner_files(tmp_path: Path, content: bytes) -> None:
    p = tmp_path / "x.owner.json"
    p.write_bytes(content)
    assert cs.read_owner(p, repo=REPO, thread_id=TID).state is cs.OwnerState.UNREADABLE


@pytest.mark.parametrize(
    "over",
    [{"complete": "true"}, {"created_at": 5}, {"thread_id": None}],
)
def test_wrong_type_is_unreadable_not_mismatch(tmp_path: Path, over: dict[str, object]) -> None:
    p = tmp_path / "x.owner.json"
    _owner(p, **over)
    assert cs.read_owner(p, repo=REPO, thread_id=TID).state is cs.OwnerState.UNREADABLE


def test_missing_field_is_unreadable(tmp_path: Path) -> None:
    p = tmp_path / "x.owner.json"
    p.write_text(
        json.dumps({"repo": REPO, "thread_id": "other", "complete": True}), encoding="utf-8"
    )
    assert cs.read_owner(p, repo=REPO, thread_id=TID).state is cs.OwnerState.UNREADABLE


def test_match_and_mismatch(tmp_path: Path) -> None:
    p = tmp_path / "x.owner.json"
    _owner(p)
    assert cs.read_owner(p, repo=REPO, thread_id=TID).state is cs.OwnerState.MATCH
    _owner(p, thread_id="T-other")
    assert cs.read_owner(p, repo=REPO, thread_id=TID).state is cs.OwnerState.MISMATCH
    _owner(p, repo="other-repo")
    assert cs.read_owner(p, repo=REPO, thread_id=TID).state is cs.OwnerState.MISMATCH


A = cs.OwnerRead(cs.OwnerState.ABSENT)
U = cs.OwnerRead(cs.OwnerState.UNREADABLE)
M = cs.OwnerRead(cs.OwnerState.MISMATCH, {})
MF = cs.OwnerRead(cs.OwnerState.MATCH, {"complete": False})
MT = cs.OwnerRead(cs.OwnerState.MATCH, {"complete": True})


@pytest.mark.parametrize(
    ("owner", "dir_exists", "route"),
    [
        (A, False, cs.EntryRoute.CREATE_THEN_PROVISION),
        (A, True, cs.EntryRoute.PARK_OWNER_UNKNOWN),
        (U, False, cs.EntryRoute.PARK_OWNER_UNKNOWN),
        (U, True, cs.EntryRoute.PARK_OWNER_UNKNOWN),
        (M, False, cs.EntryRoute.NEXT_CANDIDATE),
        (M, True, cs.EntryRoute.NEXT_CANDIDATE),
        (MF, True, cs.EntryRoute.QUARANTINE_INCOMPLETE_THEN_PROVISION),
        (MF, False, cs.EntryRoute.PROVISION),
        (MT, True, cs.EntryRoute.STEP1),
        (MT, False, cs.EntryRoute.PARK_OWNER_UNKNOWN),
    ],
)
def test_entry_route_table_v24(owner: cs.OwnerRead, dir_exists: bool, route: cs.EntryRoute) -> None:
    assert cs.entry_route(owner, dir_exists) is route


def test_choose_slot_first_creation(tmp_path: Path) -> None:
    choice = cs.choose_slot(tmp_path, REPO, TID)
    assert choice.route is cs.EntryRoute.CREATE_THEN_PROVISION
    assert choice.paths.name == cs.thread_hash12(TID)
    data = json.loads(choice.paths.owner_file.read_text(encoding="utf-8"))
    assert data["complete"] is False and data["thread_id"] == TID
    # Second tick: owner matches, complete false, no dir -> provision.
    again = cs.choose_slot(tmp_path, REPO, TID)
    assert again.paths == choice.paths
    assert again.route is cs.EntryRoute.PROVISION


def test_choose_slot_hash_collision_moves_to_next_candidate(tmp_path: Path) -> None:
    def same(_: bytes) -> str:
        return "a" * 40

    first = cs.choose_slot(tmp_path, REPO, "T-one", hasher=same)
    second = cs.choose_slot(tmp_path, REPO, "T-two", hasher=same)
    assert first.paths.name == "a" * 12
    assert second.paths.name == "a" * 12 + "-1"
    assert second.route is cs.EntryRoute.CREATE_THEN_PROVISION


def test_crash_right_after_owner_creation_parks_and_does_not_skip(tmp_path: Path) -> None:
    # Empty owner file, no dir: must park owner_unknown, never move to "-1".
    paths = cs.SlotPaths(tmp_path / REPO, cs.thread_hash12(TID))
    paths.owner_file.parent.mkdir(parents=True)
    paths.owner_file.write_bytes(b"")
    choice = cs.choose_slot(tmp_path, REPO, TID)
    assert choice.route is cs.EntryRoute.PARK_OWNER_UNKNOWN
    assert choice.paths == paths
    assert paths.owner_file.read_bytes() == b""  # untouched
    assert not (tmp_path / REPO / f"{paths.name}-1{cs.OWNER_SUFFIX}").exists()


def test_exclusive_create_race_one_wins(tmp_path: Path) -> None:
    p = tmp_path / REPO / "slot.owner.json"
    assert cs.create_owner_exclusive(p, repo=REPO, thread_id="T-one") is True
    assert cs.create_owner_exclusive(p, repo=REPO, thread_id="T-two") is False
    assert cs.read_owner(p, repo=REPO, thread_id="T-two").state is cs.OwnerState.MISMATCH


def test_update_owner_replaces_atomically(tmp_path: Path) -> None:
    p = tmp_path / REPO / "slot.owner.json"
    cs.create_owner_exclusive(p, repo=REPO, thread_id=TID)
    data = cs.read_owner(p, repo=REPO, thread_id=TID).data
    assert data is not None
    cs.update_owner(p, {**data, "complete": True})
    assert cs.read_owner(p, repo=REPO, thread_id=TID).complete is True
