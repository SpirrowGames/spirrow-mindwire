"""Turn-pre slot procedure (T-ephemeral-impl-clone-slot-per-thread PR-A, msg-8061/8068/8070/8088).

Real git repositories in tmp dirs: a bare ``origin`` stands in for GitHub, ``refresh_mirror``
builds ``<slots_root>/<repo>.git`` from it, and :class:`SlotPrep` runs steps 0-7.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.clone_guard import CloneGuard
from spirrow_mindwire.clone_retreat_log import (
    INDEX_LOCK_STALE_PARK_CODE,
    append_slot_preserve,
    append_slot_quarantine,
    build_slot_preserve_record,
    build_slot_quarantine_record,
    pending_record,
    render_slot_notice,
)
from spirrow_mindwire.clone_slot import (
    PARK_GIT_DIVERGED,
    PARK_GIT_PUSH_FAILED,
    PARK_INDEX_LOCK_STALE,
    PARK_OWNER_UNKNOWN,
    PARK_PRESERVE_FAILED,
    PARK_PROVISION_FAILED,
    PARK_QUARANTINE_UNCONFIRMED,
    PARK_UNPUBLISHED,
    PARK_UNRECOVERABLE,
    SlotPaths,
    quarantine_root,
    slot_name,
    thread_hash12,
)
from spirrow_mindwire.clone_slot_prep import (
    PrepAction,
    SlotLockBusyError,
    SlotPrep,
    exclusive_lock,
    refresh_mirror,
)

THREAD = "T-slot-prep-demo"
OTHER = "T-slot-prep-other"
REPO = "demo"
TB = "loop/t-slot-prep-demo"


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _commit(cwd: Path, name: str, text: str) -> str:
    (cwd / name).write_text(text, encoding="utf-8")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", f"add {name}")
    return _git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    _git(seed, "switch", "-q", "-c", "main")
    _commit(seed, "a.txt", "base\n")
    _git(seed, "push", "-q", "origin", "main")
    slots = tmp_path / "slots"
    slots.mkdir()
    assert refresh_mirror(slots, REPO, str(origin))
    return {"origin": origin, "seed": seed, "slots": slots, "state": tmp_path / "state"}


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 8, 1, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


def _prep(world: dict[str, Path], thread: str = THREAD, tb: str = TB, **kw: Any) -> SlotPrep:
    defaults: dict[str, object] = {
        "slots_root": world["slots"],
        "repo": REPO,
        "thread_id": thread,
        "origin_url": str(world["origin"]),
        "thread_branch": tb,
        "default_branch": "main",
        "state_dir": world["state"],
        "sessions_under": lambda _p: 0,
        "now": Clock(),
    }
    defaults.update(kw)
    return SlotPrep(**defaults)  # type: ignore[arg-type]


def _paths(world: dict[str, Path], thread: str = THREAD, n: int = 0) -> SlotPaths:
    return SlotPaths(world["slots"] / REPO, slot_name(thread_hash12(thread), n))


def _owner(world: dict[str, Path], thread: str = THREAD) -> dict[str, object]:
    data: dict[str, object] = json.loads(
        _paths(world, thread).owner_file.read_text(encoding="utf-8")
    )
    return data


def _rows(world: dict[str, Path]) -> list[dict[str, object]]:
    log = world["state"] / "clone-retreats.jsonl"
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]


def _ready(world: dict[str, Path], **kw: Any) -> Path:
    res = _prep(world, **kw).run()
    assert res.action is PrepAction.LAUNCH, res
    assert res.slot_dir is not None
    return res.slot_dir


def _quarantined(world: dict[str, Path]) -> list[Path]:
    q = quarantine_root(world["slots"], REPO)
    return sorted(q.iterdir()) if q.exists() else []


# -- first creation, launch, INV of step 7 -----------------------------------------------------


def test_first_creation_provisions_and_launches(world: dict[str, Path]) -> None:
    res = _prep(world).run()
    assert res.action is PrepAction.LAUNCH
    assert res.record is None  # nothing was quarantined: no row
    slot = res.slot_dir
    assert slot is not None and slot == _paths(world).slot_dir
    assert _owner(world)["complete"] is True
    assert _git(slot, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(slot, "remote", "get-url", "origin") == str(world["origin"])
    assert _git(slot, "config", "core.longpaths") == "true"
    # A second turn on the clean slot just launches.
    again = _prep(world).run()
    assert again.action is PrepAction.LAUNCH and again.record is None


def test_provision_checks_out_thread_branch_when_origin_has_it(world: dict[str, Path]) -> None:
    _git(world["seed"], "switch", "-q", "-c", TB)
    tip = _commit(world["seed"], "b.txt", "b\n")
    _git(world["seed"], "push", "-q", "origin", TB)
    slot = _ready(world)
    assert _git(slot, "symbolic-ref", "--short", "HEAD") == TB
    assert _git(slot, "rev-parse", "HEAD") == tip


def test_stale_mirror_after_push_still_provisions_origin_tip(world: dict[str, Path]) -> None:
    _git(world["seed"], "switch", "-q", "-c", TB)
    _commit(world["seed"], "b.txt", "b\n")
    _git(world["seed"], "push", "-q", "origin", TB)
    refresh_mirror(world["slots"], REPO, str(world["origin"]))
    newer = _commit(world["seed"], "c.txt", "c\n")
    _git(world["seed"], "push", "-q", "origin", TB)  # mirror not refreshed: stale
    slot = _ready(world)
    assert _git(slot, "rev-parse", "HEAD") == newer


def test_branch_deleted_on_origin_is_not_provisioned_from_stale_mirror_ref(
    world: dict[str, Path],
) -> None:
    _git(world["seed"], "switch", "-q", "-c", TB)
    _commit(world["seed"], "b.txt", "b\n")
    _git(world["seed"], "push", "-q", "origin", TB)
    refresh_mirror(world["slots"], REPO, str(world["origin"]))
    _git(world["seed"], "push", "-q", "origin", f":{TB}")  # deleted on "GitHub"
    slot = _ready(world)
    assert _git(slot, "symbolic-ref", "--short", "HEAD") == "main"


# -- dirty slot: preserve, quarantine, rebuild ---------------------------------------------------


def test_tracked_change_only_is_quarantined_rebuilt_and_launched_without_bundle(
    world: dict[str, Path],
) -> None:
    slot = _ready(world)
    (slot / "a.txt").write_text("base\nlocal edit\n", encoding="utf-8")
    res = _prep(world).run()
    assert res.action is PrepAction.LAUNCH, res
    row = res.record
    assert row is not None and row["action"] == "rebuilt"
    assert row["quarantine_reason"] == "modified"
    assert row["bundle"] is None and row["untracked"] is None
    assert row["patch"] and "local edit" in Path(str(row["patch"])).read_text(encoding="utf-8")
    q = _quarantined(world)
    assert [str(p) for p in q] == [row["quarantine_path"]]
    assert (q[0] / "a.txt").read_text(encoding="utf-8") == "base\nlocal edit\n"  # original kept
    assert (slot / "a.txt").read_text(encoding="utf-8") == "base\n"
    owner = _owner(world)
    assert owner["complete"] is True
    assert "original_quarantine_path" not in owner and "pending_quarantine" not in owner
    assert isinstance(row["clone_seconds"], float) and isinstance(row["slot_bytes"], int)
    assert _rows(world)[-1]["record_id"] == row["record_id"]


def test_untracked_only_reason_and_list(world: dict[str, Path]) -> None:
    slot = _ready(world)
    (slot / "new.txt").write_text("x\n", encoding="utf-8")
    row = _prep(world).run().record
    assert row is not None and row["quarantine_reason"] == "untracked_only"
    assert row["patch"] is None
    assert Path(str(row["untracked"])).read_text(encoding="utf-8").strip() == "new.txt"


def test_merge_in_progress_reason(world: dict[str, Path]) -> None:
    slot = _ready(world)
    git_dir = Path(_git(slot, "rev-parse", "--absolute-git-dir"))
    (git_dir / "MERGE_HEAD").write_text(_git(slot, "rev-parse", "HEAD") + "\n", encoding="utf-8")
    row = _prep(world).run().record
    assert row is not None and row["quarantine_reason"] == "merge_in_progress"


def test_wrong_head_reason(world: dict[str, Path]) -> None:
    slot = _ready(world)
    _git(slot, "switch", "-q", "-c", "scratch", "--no-track")
    row = _prep(world).run().record
    assert row is not None and row["quarantine_reason"] == "wrong_head"


def test_live_session_in_slot_skips_without_rename(world: dict[str, Path]) -> None:
    slot = _ready(world)
    (slot / "a.txt").write_text("edit\n", encoding="utf-8")
    res = _prep(world, sessions_under=lambda p: 1).run()
    assert res.action is PrepAction.SKIP
    assert slot.exists() and (slot / "a.txt").read_text(encoding="utf-8") == "edit\n"
    assert _quarantined(world) == [] and _rows(world) == []


def test_held_slot_lock_skips_and_touches_nothing(world: dict[str, Path]) -> None:
    slot = _ready(world)
    (slot / "a.txt").write_text("edit\n", encoding="utf-8")
    with exclusive_lock(_paths(world).lock_file):
        res = _prep(world).run()
    assert res.action is PrepAction.SKIP
    assert (slot / "a.txt").read_text(encoding="utf-8") == "edit\n" and _quarantined(world) == []


def test_lock_is_exclusive(tmp_path: Path) -> None:
    lock = tmp_path / "x.lock"
    with exclusive_lock(lock), pytest.raises(SlotLockBusyError):
        subprocess_probe(lock)
    subprocess_probe(lock)  # released
    assert lock.exists()  # never deleted


def subprocess_probe(lock: Path) -> None:
    """Try the lock from another process (msvcrt locks are per-process on Windows)."""
    code = (
        "import sys; from pathlib import Path\n"
        "from spirrow_mindwire.clone_slot_prep import exclusive_lock, SlotLockBusyError\n"
        "try:\n"
        f"    with exclusive_lock(Path({str(lock)!r})): pass\n"
        "except SlotLockBusyError: sys.exit(3)\n"
    )
    import sys

    rc = subprocess.run([sys.executable, "-c", code], check=False).returncode
    if rc == 3:
        raise SlotLockBusyError(str(lock))
    assert rc == 0


def test_preserve_failure_parks_and_leaves_original(world: dict[str, Path]) -> None:
    slot = _ready(world)
    (slot / "a.txt").write_text("edit\n", encoding="utf-8")
    world["state"].mkdir(parents=True, exist_ok=True)
    (world["state"] / "clone-retreats").write_text("not a dir", encoding="utf-8")  # mkdir fails
    res = _prep(world).run()
    assert res.action is PrepAction.PARK and res.park_code == PARK_PRESERVE_FAILED
    assert (slot / "a.txt").read_text(encoding="utf-8") == "edit\n"
    assert _quarantined(world) == []
    row = _rows(world)[-1]
    assert row["action"] == "parked" and row["quarantine_path"] is None
    notice = render_slot_notice(row)
    assert str(slot) in notice and "作り直" not in notice


def test_two_rename_failures_park_unrecoverable(world: dict[str, Path]) -> None:
    slot = _ready(world)
    (slot / "a.txt").write_text("edit\n", encoding="utf-8")
    calls: list[Path] = []

    def bad_rename(src: Path, dst: Path) -> None:
        calls.append(dst)
        raise PermissionError("in use")

    res = _prep(world, rename=bad_rename).run()
    assert res.action is PrepAction.PARK and res.park_code == PARK_UNRECOVERABLE
    assert len(calls) == 2 and calls[1].name.endswith("-retry1")
    assert slot.exists() and str(slot) in res.detail


# -- unpublished commits (step 2) ------------------------------------------------------------------


def _bundle_commits(world: dict[str, Path], bundle: str) -> set[str]:
    out = _git(world["seed"], "bundle", "list-heads", bundle)
    return {ln.split()[0] for ln in out.splitlines()}


def test_unpublished_on_default_branch_bundles_then_parks(world: dict[str, Path]) -> None:
    slot = _ready(world)
    sha = _commit(slot, "x.txt", "x\n")
    res = _prep(world).run()
    assert res.action is PrepAction.PARK and res.park_code == PARK_UNPUBLISHED
    row = res.record
    assert row is not None and row["bundle"]
    assert sha in _bundle_commits(world, str(row["bundle"]))
    _git(slot, "bundle", "verify", str(row["bundle"]))
    assert _git(slot, "rev-parse", "HEAD") == sha  # original untouched


def test_unpublished_on_detached_head_is_bundled(world: dict[str, Path]) -> None:
    slot = _ready(world)
    _git(slot, "switch", "-q", "--detach")
    sha = _commit(slot, "x.txt", "x\n")
    res = _prep(world).run()
    assert res.park_code == PARK_UNPUBLISHED
    assert sha in _bundle_commits(world, str(res.record["bundle"]))  # type: ignore[index]


def test_commit_only_on_another_remote_counts_as_unpublished(
    world: dict[str, Path], tmp_path: Path
) -> None:
    slot = _ready(world)
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    _git(slot, "remote", "add", "fork", str(other))
    sha = _commit(slot, "x.txt", "x\n")
    _git(slot, "push", "-q", "fork", "main")
    res = _prep(world).run()
    assert res.park_code == PARK_UNPUBLISHED
    assert sha in _bundle_commits(world, str(res.record["bundle"]))  # type: ignore[index]


def _slot_on_tb(world: dict[str, Path]) -> Path:
    _git(world["seed"], "switch", "-q", "-c", TB)
    _commit(world["seed"], "b.txt", "b\n")
    _git(world["seed"], "push", "-q", "origin", TB)
    return _ready(world)


def test_rescue_push_on_thread_branch_then_launch(world: dict[str, Path]) -> None:
    slot = _slot_on_tb(world)
    sha = _commit(slot, "x.txt", "x\n")
    res = _prep(world).run()
    assert res.action is PrepAction.LAUNCH, res
    assert _git(world["origin"], "rev-parse", TB) == sha
    # msg-8198 判断 4: pushed and clean -> one slot_preserve row, no slot_quarantine row.
    row = res.record
    assert row is not None and row["kind"] == "slot_preserve"
    assert row["pushed_ref"] == f"refs/heads/{TB}" and row["commits"] == 1
    assert sha in _bundle_commits(world, str(row["bundle_path"]))
    assert set(row) == {
        "kind",
        "record_id",
        "at",
        "thread_id",
        "repo",
        "bundle_path",
        "pushed_ref",
        "commits",
    }
    assert [r["kind"] for r in _rows(world)] == ["slot_preserve"]
    assert pending_record(world["state"] / "clone-retreats.jsonl", THREAD) is None  # no notice


def test_slot_preserve_row_does_not_hide_an_undelivered_notice_row(
    world: dict[str, Path],
) -> None:
    log = world["state"] / "clone-retreats.jsonl"
    world["state"].mkdir(parents=True, exist_ok=True)
    earlier = build_slot_quarantine_record(
        thread_id=THREAD,
        repo=REPO,
        slot_path=_paths(world).slot_dir,
        action="parked",
        quarantine_reason="unpublished",
        park_code=PARK_UNPUBLISHED,
    )
    append_slot_quarantine(log, earlier)
    retreat = {"kind": "retreat", "record_id": f"{THREAD}@r#1", "thread_id": THREAD}
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(retreat) + "\n")
    append_slot_preserve(
        log,
        build_slot_preserve_record(
            thread_id=THREAD,
            repo=REPO,
            bundle_path=world["state"] / "b.bundle",
            pushed_ref=f"refs/heads/{TB}",
            commits=2,
        ),
    )
    picked = pending_record(log, THREAD)
    assert picked is not None and picked["record_id"] == retreat["record_id"]


def test_rescue_push_then_quarantine_records_bundle_once(world: dict[str, Path]) -> None:
    slot = _slot_on_tb(world)
    _commit(slot, "x.txt", "x\n")
    (slot / "stray.txt").write_text("u\n", encoding="utf-8")
    res = _prep(world).run()
    assert res.action is PrepAction.LAUNCH
    row = res.record
    assert row is not None and row["kind"] == "slot_quarantine" and row["bundle"]
    assert row["quarantine_reason"] == "untracked_only"
    assert [r["kind"] for r in _rows(world)] == ["slot_quarantine"]  # no slot_preserve


# -- msg-8198 判断 1 / 2 ---------------------------------------------------------------------------


def test_fetch_failure_counter_resets_on_success(world: dict[str, Path], tmp_path: Path) -> None:
    slot = _ready(world)
    bad, good = str(tmp_path / "nope.git"), str(world["origin"])
    _git(slot, "remote", "set-url", "origin", bad)
    assert _prep(world).run().action is PrepAction.SKIP
    _git(slot, "remote", "set-url", "origin", good)
    assert _prep(world).run().action is PrepAction.LAUNCH
    assert "fetch_failures" not in _owner(world)
    _git(slot, "remote", "set-url", "origin", bad)
    assert _prep(world).run().action is PrepAction.SKIP
    assert _prep(world).run().action is PrepAction.SKIP  # 1 fail, ok, 2 fails: not 3 in a row
    assert _owner(world)["fetch_failures"] == 2 and _rows(world) == []


def test_provision_failure_counter_resets_on_success(world: dict[str, Path]) -> None:
    def boom(_s: Path) -> None:
        raise RuntimeError("trust")

    assert _prep(world, prepare_slot=boom).run().action is PrepAction.SKIP
    assert _owner(world)["provision_failures"] == 1
    slot = _ready(world)
    assert "provision_failures" not in _owner(world)
    (slot / "a.txt").write_text("edit\n", encoding="utf-8")
    assert _prep(world, prepare_slot=boom).run().action is PrepAction.SKIP
    assert _prep(world, prepare_slot=boom).run().action is PrepAction.SKIP
    assert _owner(world)["provision_failures"] == 2
    assert [r for r in _rows(world) if r.get("action") == "parked"] == []


def test_pin_not_ignored_is_provision_failed_with_guard_detail(world: dict[str, Path]) -> None:
    slot = _ready(world)
    (slot / ".mindwire").mkdir(exist_ok=True)
    (slot / ".mindwire" / "pin").write_text("x\n", encoding="utf-8")
    _git(slot, "add", "-f", ".mindwire/pin")
    res = _prep(world).run()
    assert res.action is PrepAction.LAUNCH
    row = res.record
    assert row is not None and row["quarantine_reason"] == "provision_failed"
    assert "pin is tracked in this repo" in str(row["detail"])


def _fast_guard(p: Path) -> CloneGuard:
    """CloneGuard whose LOCK_WAIT_S elapses at once (no real 5 s wait per tick)."""
    tick = itertools.count(0, 10)
    return CloneGuard(p, sleep=lambda _s: None, monotonic=lambda: float(next(tick)))


def _stale_index_lock(slot: Path) -> Path:
    lock = Path(_git(slot, "rev-parse", "--absolute-git-dir")) / "index.lock"
    lock.write_text("", encoding="utf-8")
    return lock


def test_index_lock_with_live_session_skips_forever_without_counting(
    world: dict[str, Path],
) -> None:
    slot = _ready(world)
    lock = _stale_index_lock(slot)
    for _ in range(5):
        res = _prep(world, guard_factory=_fast_guard, sessions_under=lambda _p: 1).run()
        assert res.action is PrepAction.SKIP and "live session" in res.detail
    assert "index_lock_ticks" not in _owner(world)
    assert lock.exists() and slot.exists() and _quarantined(world) == [] and _rows(world) == []


def test_stale_index_lock_parks_on_third_tick_without_moving_anything(
    world: dict[str, Path],
) -> None:
    slot = _ready(world)
    lock = _stale_index_lock(slot)
    for n in (1, 2):
        res = _prep(world, guard_factory=_fast_guard).run()
        assert res.action is PrepAction.SKIP, res
        assert _owner(world)["index_lock_ticks"] == n and _rows(world) == []
    res = _prep(world, guard_factory=_fast_guard).run()
    assert res.action is PrepAction.PARK and res.park_code == PARK_INDEX_LOCK_STALE
    row = res.record
    assert row is not None and _rows(world) == [row]
    assert row["action"] == "parked" and row["quarantine_reason"] == "index_lock"
    assert row["quarantine_path"] is None
    assert row["patch"] is None and row["untracked"] is None and row["bundle"] is None
    assert lock.exists() and slot.exists() and _quarantined(world) == []  # nothing moved
    notice = render_slot_notice(row)
    assert str(lock.resolve()) in notice and "not moved or changed" in notice
    assert "rebuilt" not in notice and "clean" not in notice


def test_index_lock_counter_resets_when_the_lock_goes(world: dict[str, Path]) -> None:
    slot = _ready(world)
    lock = _stale_index_lock(slot)
    for _ in range(2):
        assert _prep(world, guard_factory=_fast_guard).run().action is PrepAction.SKIP
    os.rename(lock, lock.with_name("index.lock.gone"))  # the lock disappears
    assert _prep(world, guard_factory=_fast_guard).run().action is PrepAction.LAUNCH
    assert "index_lock_ticks" not in _owner(world)
    os.rename(lock.with_name("index.lock.gone"), lock)
    for _ in range(2):
        assert _prep(world, guard_factory=_fast_guard).run().action is PrepAction.SKIP
    assert _owner(world)["index_lock_ticks"] == 2 and _rows(world) == []


def test_unpark_with_lock_still_present_recounts_and_reparks(world: dict[str, Path]) -> None:
    """msg-8212: park clears index_lock_ticks, so an unpark that leaves the lock
    in place counts 3 ticks again and parks again with a second row."""
    slot = _ready(world)
    lock = _stale_index_lock(slot)
    for _ in range(3):
        last = _prep(world, guard_factory=_fast_guard).run()
    assert last.action is PrepAction.PARK and last.park_code == PARK_INDEX_LOCK_STALE
    assert "index_lock_ticks" not in _owner(world) and len(_rows(world)) == 1
    # unpark: the loop launches prep again for this thread, lock untouched
    for n in (1, 2):
        res = _prep(world, guard_factory=_fast_guard).run()
        assert res.action is PrepAction.SKIP, res
        assert _owner(world)["index_lock_ticks"] == n and len(_rows(world)) == 1
    res = _prep(world, guard_factory=_fast_guard).run()
    assert res.action is PrepAction.PARK and res.park_code == PARK_INDEX_LOCK_STALE
    rows = _rows(world)
    assert len(rows) == 2 and rows[1] == res.record
    assert rows[1]["quarantine_reason"] == "index_lock" and rows[1]["action"] == "parked"
    assert lock.exists() and slot.exists() and _quarantined(world) == []


def test_stale_index_lock_park_leaves_other_slot_of_repo_launching(
    world: dict[str, Path],
) -> None:
    mine = _ready(world)
    _ready(world, thread=OTHER, tb="loop/t-slot-prep-other")
    _stale_index_lock(mine)
    for _ in range(3):
        last = _prep(world, guard_factory=_fast_guard).run()
    assert last.park_code == PARK_INDEX_LOCK_STALE
    other = _prep(world, thread=OTHER, tb="loop/t-slot-prep-other").run()
    assert other.action is PrepAction.LAUNCH


def test_index_lock_park_code_constant_matches_log_module() -> None:
    assert PARK_INDEX_LOCK_STALE == INDEX_LOCK_STALE_PARK_CODE


def test_rescue_push_failure_parks_with_bundle(world: dict[str, Path], tmp_path: Path) -> None:
    slot = _slot_on_tb(world)
    sha = _commit(slot, "x.txt", "x\n")
    _git(slot, "remote", "set-url", "--push", "origin", str(tmp_path / "nope.git"))
    res = _prep(world).run()
    assert res.action is PrepAction.PARK and res.park_code == PARK_GIT_PUSH_FAILED
    assert sha in _bundle_commits(world, str(res.record["bundle"]))  # type: ignore[index]


def test_diverged_thread_branch_parks(world: dict[str, Path]) -> None:
    slot = _slot_on_tb(world)
    _commit(slot, "x.txt", "x\n")
    _commit(world["seed"], "y.txt", "y\n")
    _git(world["seed"], "push", "-q", "origin", TB)
    res = _prep(world).run()
    assert res.action is PrepAction.PARK and res.park_code == PARK_GIT_DIVERGED
    assert res.record["bundle"]  # type: ignore[index]


def test_squash_merged_and_deleted_branch_is_not_pushed(world: dict[str, Path]) -> None:
    slot = _slot_on_tb(world)
    _commit(slot, "x.txt", "x\n")
    _git(world["seed"], "push", "-q", "origin", f":{TB}")  # merged by squash, branch deleted
    res = _prep(world).run()
    assert res.action is PrepAction.PARK and res.park_code == PARK_UNPUBLISHED
    assert "origin has no" in res.detail
    assert TB not in _git(world["origin"], "branch", "--list")


def test_bundle_failure_on_unpublished_parks_preserve_failed_without_push(
    world: dict[str, Path],
) -> None:
    slot = _slot_on_tb(world)
    _commit(slot, "x.txt", "x\n")
    before = _git(world["origin"], "rev-parse", TB)
    world["state"].mkdir(parents=True, exist_ok=True)
    (world["state"] / "clone-retreats").write_text("blocker", encoding="utf-8")
    res = _prep(world).run()
    assert res.park_code == PARK_PRESERVE_FAILED
    assert _git(world["origin"], "rev-parse", TB) == before  # no push before the bundle


# -- owner file, collisions, other threads -------------------------------------------------------


def test_complete_owner_with_missing_dir_parks_owner_unknown(world: dict[str, Path]) -> None:
    slot = _ready(world)
    os.rename(slot, slot.with_name("moved-away"))
    res = _prep(world).run()
    assert res.park_code == PARK_OWNER_UNKNOWN
    assert not slot.exists()  # not rebuilt


def test_unreadable_owner_parks_without_change(world: dict[str, Path]) -> None:
    p = _paths(world)
    p.repo_root.mkdir(parents=True, exist_ok=True)
    p.owner_file.write_text('{"repo": ', encoding="utf-8")
    res = _prep(world).run()
    assert res.park_code == PARK_OWNER_UNKNOWN
    assert p.owner_file.read_text(encoding="utf-8") == '{"repo": '
    assert not p.slot_dir.exists()


def test_hash_collision_uses_next_candidate(world: dict[str, Path]) -> None:
    def same(_b: bytes) -> str:
        return "a" * 40

    a = _ready(world, thread=THREAD, hasher=same)
    b = _ready(world, thread=OTHER, tb="loop/t-slot-prep-other", hasher=same)
    assert a.name == "a" * 12 and b.name == "a" * 12 + "-1"


def test_other_slot_of_same_repo_is_not_parked(world: dict[str, Path]) -> None:
    mine = _ready(world)
    _ready(world, thread=OTHER, tb="loop/t-slot-prep-other")
    _commit(mine, "x.txt", "x\n")  # makes my slot park as unpublished
    assert _prep(world).run().action is PrepAction.PARK
    other = _prep(world, thread=OTHER, tb="loop/t-slot-prep-other").run()
    assert other.action is PrepAction.LAUNCH


# -- interrupted creation / provision failures ----------------------------------------------------


def test_incomplete_slot_dir_is_quarantined_and_rebuilt(world: dict[str, Path]) -> None:
    _ready(world)
    p = _paths(world)
    owner = _owner(world)
    owner["complete"] = False
    p.owner_file.write_text(json.dumps(owner), encoding="utf-8")  # simulate crash mid-clone
    res = _prep(world).run()
    assert res.action is PrepAction.LAUNCH
    assert res.record is None  # an incomplete slot holds no work: nothing to report
    assert len(_quarantined(world)) == 1
    assert _owner(world)["complete"] is True


def test_provision_failing_three_ticks_parks_with_first_original_location(
    world: dict[str, Path],
) -> None:
    slot = _ready(world)
    (slot / "a.txt").write_text("edit\n", encoding="utf-8")
    calls = {"n": 0}

    def flaky(slot_dir: Path) -> None:
        calls["n"] += 1
        raise RuntimeError("trust registration failed")

    first = _prep(world, prepare_slot=flaky).run()
    assert first.action is PrepAction.SKIP and _rows(world) == []  # no row mid-failure
    original = _owner(world)["original_quarantine_path"]
    second = _prep(world, prepare_slot=flaky).run()
    assert second.action is PrepAction.SKIP and _rows(world) == []
    third = _prep(world, prepare_slot=flaky).run()
    assert third.action is PrepAction.PARK and third.park_code == PARK_PROVISION_FAILED
    row = third.record
    assert row is not None and row["quarantine_path"] == original
    assert row["quarantine_reason"] == "modified" and row["patch"]
    assert (Path(str(original)) / "a.txt").read_text(encoding="utf-8") == "edit\n"
    assert len(_quarantined(world)) == 3  # original + two partial slots, none deleted
    notice = render_slot_notice(row)
    assert str(original) in notice


def test_provision_failing_once_then_rebuilt_keeps_original_location(
    world: dict[str, Path],
) -> None:
    slot = _ready(world)
    (slot / "a.txt").write_text("edit\n", encoding="utf-8")

    def boom(_s: Path) -> None:
        raise RuntimeError("once")

    assert _prep(world, prepare_slot=boom).run().action is PrepAction.SKIP
    original = _owner(world)["original_quarantine_path"]
    res = _prep(world).run()
    assert res.action is PrepAction.LAUNCH
    assert res.record is not None and res.record["action"] == "rebuilt"
    assert res.record["quarantine_path"] == original


# -- v2.7 pending_quarantine reconciliation -------------------------------------------------------


def _dirty_slot_with_pending(world: dict[str, Path], *, renamed: bool) -> tuple[Path, Path]:
    slot = _ready(world)
    (slot / "a.txt").write_text("edit\n", encoding="utf-8")
    dest = quarantine_root(world["slots"], REPO) / "pending-target"
    dest.parent.mkdir(parents=True, exist_ok=True)
    owner = _owner(world)
    owner["pending_quarantine"] = {"to": str(dest), "at": "2026-11-08T00:00:00+00:00"}
    _paths(world).owner_file.write_text(json.dumps(owner), encoding="utf-8")
    if renamed:
        os.rename(slot, dest)
    return slot, dest


def test_pending_before_rename_restarts_from_preserve(world: dict[str, Path]) -> None:
    _slot, dest = _dirty_slot_with_pending(world, renamed=False)
    res = _prep(world).run()
    assert res.action is PrepAction.LAUNCH
    assert not dest.exists()
    row = res.record
    assert row is not None and row["action"] == "rebuilt" and row["patch"]
    assert (Path(str(row["quarantine_path"])) / "a.txt").read_text(encoding="utf-8") == "edit\n"


def test_pending_after_rename_is_confirmed_then_rebuilt(world: dict[str, Path]) -> None:
    _slot, dest = _dirty_slot_with_pending(world, renamed=True)
    res = _prep(world).run()
    assert res.action is PrepAction.LAUNCH
    assert res.record is not None and res.record["quarantine_path"] == str(dest)
    assert (dest / "a.txt").read_text(encoding="utf-8") == "edit\n"
    assert "pending_quarantine" not in _owner(world)


@pytest.mark.parametrize("both", [True, False])
def test_pending_unresolvable_parks_unconfirmed(world: dict[str, Path], both: bool) -> None:
    slot, dest = _dirty_slot_with_pending(world, renamed=False)
    if both:
        dest.mkdir()
    else:
        os.rename(slot, slot.with_name("gone-elsewhere"))
    before = _paths(world).owner_file.read_text(encoding="utf-8")
    res = _prep(world).run()
    assert res.action is PrepAction.PARK and res.park_code == PARK_QUARANTINE_UNCONFIRMED
    assert _paths(world).owner_file.read_text(encoding="utf-8") == before  # nothing changed
    row = res.record
    assert row is not None and row["quarantine_path"] is None
    assert row["quarantine_candidates"] == [str(slot), str(dest)]
    notice = render_slot_notice(row)
    assert str(slot) in notice and str(dest) in notice
