"""Quarantine export and GC (T-ephemeral-impl-clone-slot-per-thread A5, D-6: Bohr msg-9901 /
msg-9903 / msg-9905, go Einstein msg-9906). Real git throughout."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.cli import main
from spirrow_mindwire.clone_retreat import git_env
from spirrow_mindwire.clone_slot_gc import (
    DELETING_DIR,
    EXPORTS_DIR,
    GC_STATE_FILE,
    STATUS_CHANGED,
    STATUS_STUCK,
    STATUS_UNVERIFIED,
    STATUS_VERIFY_TIMEOUT,
    QuarantineGc,
    export_slot,
    write_manifest,
)

REPO = "r"
AT = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
LATER = AT + timedelta(days=8)
BINARY = bytes(range(256)) * 50


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=git_env()
    ).stdout


def _gitb(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, env=git_env()
    ).stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.invalid")
    _git(seed, "config", "user.name", "t")
    (seed / "a.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    return bare


def _slot(tmp_path: Path, origin: Path, name: str = "slot-01") -> Path:
    slot = tmp_path / "slots" / REPO / name
    slot.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(slot)], check=True)
    _git(slot, "config", "user.email", "t@example.invalid")
    _git(slot, "config", "user.name", "t")
    return slot


def _dirty(slot: Path, *, commit: bool = True) -> None:
    if commit:
        (slot / "c.txt").write_text("local only\n", encoding="utf-8")
        _git(slot, "add", "c.txt")
        _git(slot, "commit", "-q", "-m", "local")
    (slot / "a.txt").write_text("changed\n", encoding="utf-8")
    (slot / "new.bin").write_bytes(BINARY)  # untracked, only in the slot


def _quarantine(
    tmp_path: Path, slot: Path, at: datetime = AT, **kw: object
) -> tuple[Path, dict[str, Any]]:
    exports = tmp_path / "state" / EXPORTS_DIR
    res = export_slot(slot, exports, REPO, f"{slot.name}-T-a", **kw)  # type: ignore[arg-type]
    target = tmp_path / "slots" / "_quarantine" / REPO / f"{slot.name}-T-a-{at:%Y%m%d%H%M%S}"
    target.parent.mkdir(parents=True, exist_ok=True)
    slot.rename(target)
    mp = write_manifest(res, target, at)
    assert mp is not None
    return target, json.loads(mp.read_text(encoding="utf-8"))


def _gc(tmp_path: Path, now: datetime = LATER, **kw: object) -> QuarantineGc:
    return QuarantineGc(
        slots_root=tmp_path / "slots",
        state_dir=tmp_path / "state",
        clock=lambda: now,
        **kw,  # type: ignore[arg-type]
    )


def _reasons(res: object) -> dict[str, str]:
    return {n["path"]: n["reason"] for n in res.notices}  # type: ignore[attr-defined]


# --- export -----------------------------------------------------------------------------


def test_export_is_complete_with_contents_and_bundle(tmp_path: Path, origin: Path) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    head = _git(slot, "rev-parse", "HEAD").strip()
    target, m = _quarantine(tmp_path, slot)
    assert m["complete"] is True, m["incomplete_reasons"]
    assert m["head"] == head and m["local_commits"] == [head]
    assert m["quarantine_path"] == str(target)
    assert m["bundle"]["verify"] == "ok"
    assert "restore_checked" not in m  # msg-9905 Obj-3-1: only checks that ran
    untracked = {e["path"]: e["sha256"] for e in m["untracked"]}
    assert untracked == {"new.bin": hashlib.sha256(BINARY).hexdigest()}
    base = tmp_path / "state" / EXPORTS_DIR / REPO
    with tarfile.open(base / m["untracked_tar"]["file"]) as tar:
        member = tar.extractfile("new.bin")
        assert member is not None and member.read() == BINARY


def test_restore_from_bundle_patch_and_tar_matches_the_quarantine(
    tmp_path: Path, origin: Path
) -> None:
    # msg-9903 §1 / msg-9905: clone anew, then bundle, patch, tar, and compare.
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    target, m = _quarantine(tmp_path, slot)
    base = tmp_path / "state" / EXPORTS_DIR / REPO
    restored = tmp_path / "restored"
    subprocess.run(["git", "clone", "-q", str(origin), str(restored)], check=True)
    bundle = base / m["bundle"]["file"]
    _git(restored, "fetch", "-q", str(bundle), f"{m['head']}")
    _git(restored, "checkout", "-q", "--detach", str(m["head"]))
    _git(restored, "apply", "--index", "--binary", str(base / m["patch"]["file"]))
    with tarfile.open(base / m["untracked_tar"]["file"]) as tar:
        tar.extractall(restored, filter="data")
    diff = ("diff", "--binary", "--no-color", "--no-ext-diff", "HEAD")
    assert _git(restored, "rev-parse", "HEAD") == _git(target, "rev-parse", "HEAD")
    assert _git(restored, "rev-list", "HEAD", "--not", "--remotes") == _git(
        target, "rev-list", "HEAD", "--not", "--remotes"
    )
    assert _gitb(restored, *diff) == _gitb(target, *diff)
    assert (restored / "new.bin").read_bytes() == (target / "new.bin").read_bytes()


def test_export_over_the_untracked_cap_is_incomplete(tmp_path: Path, origin: Path) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    _, m = _quarantine(tmp_path, slot, untracked_cap=10)
    assert m["complete"] is False and "untracked_cap" in m["incomplete_reasons"]


def test_export_of_a_non_repository_is_incomplete_and_never_raises(tmp_path: Path) -> None:
    d = tmp_path / "slots" / REPO / "slot-01"
    d.mkdir(parents=True)
    res = export_slot(d, tmp_path / "state" / EXPORTS_DIR, REPO, "slot-01-x")
    assert res.complete is False


# --- GC: deletes only what is verified ----------------------------------------------------


def test_verified_old_quarantine_is_deleted_and_exports_kept(tmp_path: Path, origin: Path) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    target, m = _quarantine(tmp_path, slot)
    res = _gc(tmp_path).run()
    assert not target.exists()
    assert not (tmp_path / "slots" / "_quarantine" / DELETING_DIR / REPO / target.name).exists()
    assert res.notices == [] and len(res.deleted) == 1
    base = tmp_path / "state" / EXPORTS_DIR / REPO
    assert (base / m["patch"]["file"]).is_file()  # the saved copy stays


def test_young_quarantine_under_the_count_is_kept(tmp_path: Path, origin: Path) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    target, _ = _quarantine(tmp_path, slot)
    res = _gc(tmp_path, now=AT + timedelta(days=1)).run()
    assert target.is_dir() and res.deleted == [] and res.notices == []


def test_without_manifest_kept_and_noticed_not_counted(tmp_path: Path, origin: Path) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    target = tmp_path / "slots" / "_quarantine" / REPO / "slot-01-pre-a5"
    target.parent.mkdir(parents=True)
    slot.rename(target)
    res = _gc(tmp_path).run()
    assert target.is_dir()
    assert _reasons(res) == {str(target): "no_manifest"}


def test_local_commits_without_bundle_are_kept(tmp_path: Path, origin: Path) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    target, m = _quarantine(tmp_path, slot)
    (tmp_path / "state" / EXPORTS_DIR / REPO / m["bundle"]["file"]).unlink()
    res = _gc(tmp_path).run()
    assert target.is_dir() and _reasons(res) == {str(target): STATUS_UNVERIFIED}


def test_tracked_change_after_quarantine_is_kept(tmp_path: Path, origin: Path) -> None:
    # Einstein msg-9902: HEAD and untracked files unchanged, a tracked file changed.
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    target, _ = _quarantine(tmp_path, slot)
    (target / "a.txt").write_text("changed again\n", encoding="utf-8")
    res = _gc(tmp_path).run()
    assert target.is_dir() and _reasons(res) == {str(target): STATUS_CHANGED}
    again = _gc(tmp_path).run()  # stays kept, same notice signature (sent once by the wrapper)
    assert target.is_dir() and again.notices == res.notices


def test_untracked_change_after_quarantine_is_kept(tmp_path: Path, origin: Path) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    target, _ = _quarantine(tmp_path, slot)
    (target / "new.bin").write_bytes(b"other")
    res = _gc(tmp_path).run()
    assert target.is_dir() and _reasons(res) == {str(target): STATUS_CHANGED}


def test_unreadable_diff_is_kept(tmp_path: Path, origin: Path) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    target, _ = _quarantine(tmp_path, slot)
    (target / ".git" / "HEAD").write_text("garbage\n", encoding="utf-8")
    res = _gc(tmp_path).run()
    assert target.is_dir()
    assert _reasons(res)[str(target)] in (STATUS_UNVERIFIED, STATUS_CHANGED)


def test_incomplete_export_is_kept(tmp_path: Path, origin: Path) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    target, _ = _quarantine(tmp_path, slot, untracked_cap=10)
    res = _gc(tmp_path).run()
    assert target.is_dir() and _reasons(res) == {str(target): "export_incomplete"}


# --- GC: count, bounds, _deleting ----------------------------------------------------------


def test_count_limit_and_item_bound_carry_over_to_the_next_tick(
    tmp_path: Path, origin: Path
) -> None:
    targets = []
    for n in range(14):
        slot = _slot(tmp_path, origin, f"slot-{n + 1:02d}")
        (slot / "u.txt").write_text(f"{n}\n", encoding="utf-8")
        targets.append(_quarantine(tmp_path, slot, at=AT + timedelta(minutes=n))[0])
    young = AT + timedelta(days=1)  # none is older than 7 days: only the count makes them due
    first = _gc(tmp_path, now=young).run()
    assert len(first.deleted) == 3 and first.remaining_due == 1  # 4 over 10, 3 per tick
    assert [t.exists() for t in targets[:3]] == [False] * 3 and targets[3].exists()
    second = _gc(tmp_path, now=young).run()
    assert len(second.deleted) == 1 and not targets[3].exists()
    assert all(t.exists() for t in targets[4:])
    assert len(list((tmp_path / "slots" / "_quarantine" / REPO).iterdir())) == 10


class _Clock:
    def __init__(self, step: float) -> None:
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def test_deletion_stops_at_the_deadline_and_resumes_under_deleting(
    tmp_path: Path, origin: Path
) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    target, _ = _quarantine(tmp_path, slot)
    files = sum(1 for p in target.rglob("*") if p.is_file())
    removed: list[Path] = []
    clock = _Clock(0.0)

    def remove(p: Path) -> None:
        removed.append(p)
        if len(removed) == 2:
            clock.step = 100.0  # the deadline passes after the second file
        Path(p).unlink()

    first = _gc(tmp_path, monotonic=clock, remove=remove).run()
    deleting = tmp_path / "slots" / "_quarantine" / DELETING_DIR / REPO / target.name
    assert not target.exists() and deleting.is_dir()  # renamed first: never a restore candidate
    assert first.deleting_pending == 1 and len(removed) < files
    second = _gc(tmp_path).run()  # a fresh tick finishes it; it is not noticed as "no manifest"
    assert not deleting.exists() and second.notices == []
    assert str(deleting) in second.deleted


def test_deleting_entries_are_never_restore_candidates(tmp_path: Path) -> None:
    leftover = tmp_path / "slots" / "_quarantine" / DELETING_DIR / REPO / "slot-01-x"
    leftover.mkdir(parents=True)
    (leftover / "f").write_text("x", encoding="utf-8")
    res = _gc(tmp_path).run()
    assert res.notices == [] and not leftover.exists()


def test_verification_timing_out_three_times_is_kept_and_noticed_once(
    tmp_path: Path, origin: Path
) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    target, _ = _quarantine(tmp_path, slot)
    results = [_gc(tmp_path, budget_s=0.5, monotonic=_Clock(0.3)).run() for _ in range(4)]
    assert target.is_dir()
    assert [len(r.notices) for r in results[:2]] == [0, 0]
    assert _reasons(results[2]) == {str(target): STATUS_VERIFY_TIMEOUT}
    assert results[3].notices == results[2].notices  # same key + signature: sent once
    state = json.loads((tmp_path / "state" / GC_STATE_FILE).read_text(encoding="utf-8"))
    assert any(v.get("status") == STATUS_VERIFY_TIMEOUT for v in state.values())
    # kept whatever the age: a later tick with a normal budget does not delete it
    assert _gc(tmp_path, now=LATER + timedelta(days=30)).run().deleted == []


def test_rename_failing_three_times_is_gc_stuck(tmp_path: Path, origin: Path) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    target, _ = _quarantine(tmp_path, slot)

    def refuse(src: Path, dst: Path) -> None:
        raise PermissionError("in use")

    results = [_gc(tmp_path, rename=refuse).run() for _ in range(3)]
    assert target.is_dir()
    assert _reasons(results[2]) == {str(target): STATUS_STUCK}


def test_leased_path_is_not_deleted(tmp_path: Path, origin: Path) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    target, _ = _quarantine(tmp_path, slot)
    res = _gc(tmp_path, leased_paths=[target]).run()
    assert target.is_dir() and res.deleted == []


# --- CLI ----------------------------------------------------------------------------------


def test_cli_prints_summary_and_appends_a_gc_row(
    tmp_path: Path, origin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    slot = _slot(tmp_path, origin)
    _dirty(slot)
    _quarantine(tmp_path, slot, at=datetime.now(UTC) - timedelta(days=8))
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "quarantine-gc",
                "--slots-root", str(tmp_path / "slots"),
                "--state-dir", str(tmp_path / "state"),
            ]
        )  # fmt: skip
    assert exc.value.code == 0
    (line,) = [
        ln
        for ln in capsys.readouterr().out.splitlines()
        if ln.startswith("MINDWIRE_QUARANTINE_GC ")
    ]
    payload = json.loads(line.removeprefix("MINDWIRE_QUARANTINE_GC "))
    assert len(payload["deleted"]) == 1 and payload["notices"] == []
    rows = (tmp_path / "state" / "clone-retreats.jsonl").read_text(encoding="utf-8").splitlines()
    row = json.loads(rows[-1])
    assert row["kind"] == "quarantine_gc" and row["deleted"] == payload["deleted"]
