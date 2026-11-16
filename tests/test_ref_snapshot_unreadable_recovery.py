"""An unreadable turn ref snapshot recovers itself and stops only its own thread.

Thread T-ref-snapshot-unreadable-file-blocks-thread-forever. Design v2 (Bohr msg-9433, go from
Einstein after it): D-1 recovery (exclusive byte-for-byte copy, then atomic replace by a degraded
file; a degraded thread never records a baseline again), D-2 (``ref_snapshot_unrecorded``, exit
12, never 8; the implementer is not started), D-3 tests. The wrapper side of D-3 is
``tests/Test-RefSnapshotUnrecorded.ps1``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_check_pushed_ref_boundary import (
    _NARROW,
    _PROJECT,
    _THREAD,
    _Adapter,
    _commit,
    _configure,
    _dispatcher,
    _event,
    _feature,
    _git,
)
from test_conductor_core import _thread_ref

from spirrow_mindwire import loop_runner
from spirrow_mindwire.clone_guard import (
    DIRTY_CLONE_EXIT_CODE,
    CloneGuard,
    DirtyCloneError,
    DirtyCloneReason,
)
from spirrow_mindwire.config import MindwireSettings
from spirrow_mindwire.turn_ref_snapshot import (
    DEGRADED_SCHEMA_VERSION,
    REF_SNAPSHOT_UNRECORDED_EXIT_CODE,
    REF_SNAPSHOT_UNRECORDED_PAYLOAD_PREFIX,
    REF_SNAPSHOT_UNRECORDED_REASON,
    SCHEMA_VERSION,
    ContinuationScope,
    RefSnapshotUnrecordedError,
    TurnRefSnapshotStore,
)
from spirrow_mindwire.value_objects import Role

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NOW = datetime(2026, 11, 17, 1, 2, 3, tzinfo=UTC)
_STAMP = "20261117T010203Z"

_GOOD_ENTRY = {
    "trigger_msg_id": "msg-120",
    "repo_dir": "x",
    "recorded_at": "2026-11-16T00:00:00+00:00",
    "refs": {"refs/heads/main": "a" * 40},
}

UNREADABLE: dict[str, bytes] = {
    "broken_json": b"{not json",
    "other_version": json.dumps({"version": 99, "entries": []}).encode(),
    "bad_shape": json.dumps({"version": SCHEMA_VERSION, "entries": [{"refs": 3}]}).encode(),
    "not_utf8": b"\xff\xfe\x00garbage",
    "degraded_version_without_flag": json.dumps({"version": DEGRADED_SCHEMA_VERSION}).encode(),
}


def _seed(store: TurnRefSnapshotStore, raw: bytes) -> Path:
    path = store.path_for("p", "t")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _record(store: TurnRefSnapshotStore, repo: Path, msg_id: str, **kw: Any) -> bool:
    return store.record(
        project="p",
        thread_id="t",
        trigger_msg_id=msg_id,
        repo_dir=repo,
        refs={"refs/heads/main": "b" * 40},
        **kw,
    )


def _copies(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.unreadable-*"))


def _scope(msg_id: str) -> ContinuationScope:
    return ContinuationScope(project="p", thread_id="t", trigger_msg_id=msg_id)


@pytest.fixture
def narrow(tmp_path: Path) -> Path:
    """The same narrow-refspec clone as ``test_check_pushed_ref_boundary.narrow``."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    r = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(r))
    _configure(r)
    (r / ".gitignore").write_text(".mindwire/\n", encoding="utf-8")
    _git(r, "add", ".gitignore")
    _commit(r, "init")
    _git(r, "push", "-q", "-u", "origin", "main")
    _git(r, "remote", "set-head", "origin", "main")
    _git(r, "config", "--replace-all", "remote.origin.fetch", _NARROW)
    return r


# ------------------------------------------------------------------------- D-1 recovery


@pytest.mark.parametrize("kind", sorted(UNREADABLE))
def test_unreadable_file_is_copied_aside_and_replaced_by_degraded(
    kind: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = TurnRefSnapshotStore(tmp_path / "s")
    raw = UNREADABLE[kind]
    path = _seed(store, raw)
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.turn_ref_snapshot"):
        assert _record(store, tmp_path, "msg-100", now=_NOW) is False

    copies = _copies(path)
    assert [c.name for c in copies] == [f"{path.name}.unreadable-{_STAMP}"]
    assert copies[0].read_bytes() == raw  # byte-for-byte
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "version": DEGRADED_SCHEMA_VERSION,
        "degraded": True,
        "reset_at": _NOW.isoformat(),
        "unreadable_copy": copies[0].name,
    }
    # This launch's continuation judges every branch.
    assert store.baseline_for(_scope("msg-100"), tmp_path) is None
    # One notice line, not a warning; it names the copy.
    notices = [r for r in caplog.records if r.getMessage().startswith("notice:")]
    assert len(notices) == 1
    assert notices[0].levelno == logging.INFO
    assert str(copies[0]) in notices[0].getMessage() or os.path.abspath(copies[0]) in (
        notices[0].getMessage()
    )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_degraded_version_is_one_above_the_schema_so_old_code_refuses_it() -> None:
    assert DEGRADED_SCHEMA_VERSION == SCHEMA_VERSION + 1


@pytest.mark.parametrize("order", [("msg-120", "msg-100"), ("msg-100", "msg-120")])
def test_degraded_thread_never_records_a_baseline_whatever_the_retry_order(
    order: tuple[str, str], tmp_path: Path
) -> None:
    # Einstein msg-9432 Obj-1: the lost file had msg-120's baseline; recovery happens on a retry of
    # msg-100. Neither msg-120 nor msg-100 (nor a newer key) may get a fresh baseline afterwards.
    store = TurnRefSnapshotStore(tmp_path / "s")
    lost = {"version": SCHEMA_VERSION, "entries": [_GOOD_ENTRY]}
    path = _seed(store, json.dumps(lost).encode()[:-5])  # truncated: unreadable
    first, second = order
    assert _record(store, tmp_path, first) is False  # recovers
    assert _record(store, tmp_path, second) is False
    assert _record(store, tmp_path, first) is False
    assert _record(store, tmp_path, "msg-999") is False
    for key in (first, second, "msg-999"):
        assert store.baseline_for(_scope(key), tmp_path) is None
    assert json.loads(path.read_text(encoding="utf-8"))["degraded"] is True
    assert len(_copies(path)) == 1  # recovered once; the degraded file is readable


def test_copy_name_collision_keeps_the_existing_copy_and_numbers_the_new_one(
    tmp_path: Path,
) -> None:
    store = TurnRefSnapshotStore(tmp_path / "s")
    path = _seed(store, b"{first")
    taken = path.with_name(f"{path.name}.unreadable-{_STAMP}")
    taken.write_bytes(b"an earlier copy")
    taken1 = path.with_name(f"{path.name}.unreadable-{_STAMP}-1")
    taken1.write_bytes(b"another earlier copy")
    assert _record(store, tmp_path, "msg-1", now=_NOW) is False
    assert taken.read_bytes() == b"an earlier copy"
    assert taken1.read_bytes() == b"another earlier copy"
    new = path.with_name(f"{path.name}.unreadable-{_STAMP}-2")
    assert new.read_bytes() == b"{first"
    assert json.loads(path.read_text(encoding="utf-8"))["unreadable_copy"] == new.name


def test_copy_names_exhausted_stops_with_rename_and_leaves_the_original(tmp_path: Path) -> None:
    store = TurnRefSnapshotStore(tmp_path / "s")
    path = _seed(store, b"{x")
    from spirrow_mindwire.turn_ref_snapshot import UNREADABLE_COPY_MAX_TRIES

    for n in range(UNREADABLE_COPY_MAX_TRIES):
        suffix = "" if n == 0 else f"-{n}"
        path.with_name(f"{path.name}.unreadable-{_STAMP}{suffix}").write_bytes(b"old")
    with pytest.raises(RefSnapshotUnrecordedError) as ei:
        _record(store, tmp_path, "msg-1", now=_NOW)
    assert ei.value.kind == "rename"
    assert path.read_bytes() == b"{x"


def test_failure_between_copy_and_replace_leaves_the_original_and_recovers_next_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Einstein msg-9432 Obj-2: a stop between the two steps must never leave the path missing.
    store = TurnRefSnapshotStore(tmp_path / "s")
    lost = {"version": SCHEMA_VERSION, "entries": [_GOOD_ENTRY]}
    raw = json.dumps(lost).encode()[:-5]
    path = _seed(store, raw)
    real_replace = os.replace

    def _boom(src: Any, dst: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(RefSnapshotUnrecordedError) as ei:
        _record(store, tmp_path, "msg-100", now=_NOW)
    assert ei.value.kind == "rename"
    assert ei.value.payload()["reason"] == REF_SNAPSHOT_UNRECORDED_REASON
    assert path.read_bytes() == raw  # the original is untouched
    assert [c.read_bytes() for c in _copies(path)] == [raw]
    assert not list(path.parent.glob("*.tmp"))
    monkeypatch.setattr(os, "replace", real_replace)

    # "Restart": a fresh store instance, so nothing in memory carries over.
    again = TurnRefSnapshotStore(tmp_path / "s")
    assert _record(again, tmp_path, "msg-120", now=_NOW) is False
    assert json.loads(path.read_text(encoding="utf-8"))["degraded"] is True
    assert len(_copies(path)) == 2  # the first copy is kept, never overwritten
    assert again.baseline_for(_scope("msg-120"), tmp_path) is None
    assert again.baseline_for(_scope("msg-100"), tmp_path) is None


def test_read_oserror_stops_with_read_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TurnRefSnapshotStore(tmp_path / "s")
    path = _seed(store, b"{not json")
    real = Path.read_bytes

    def _deny(self: Path) -> bytes:
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", _deny)
    with pytest.raises(RefSnapshotUnrecordedError) as ei:
        _record(store, tmp_path, "msg-1")
    assert ei.value.kind == "read"
    assert "Permission denied" in ei.value.error
    monkeypatch.setattr(Path, "read_bytes", real)
    assert path.read_bytes() == b"{not json"
    assert _copies(path) == []


def test_write_oserror_stops_with_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = TurnRefSnapshotStore(tmp_path / "s")

    def _full(src: Any, dst: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", _full)
    with pytest.raises(RefSnapshotUnrecordedError) as ei:
        _record(store, tmp_path, "msg-1")
    assert ei.value.kind == "write"
    payload = ei.value.payload()
    assert payload["path"] == os.path.abspath(store.path_for("p", "t"))
    assert "No space left" in str(payload["error"])
    assert "空き容量と権限" in str(payload["remedy"])


# ------------------------------------------------------------------------- D-2 dispatch


@pytest.mark.anyio
@pytest.mark.parametrize("fault", ["read", "write", "rename"])
async def test_state_file_fault_refuses_before_the_adapter_and_is_not_exit_8(
    fault: str, narrow: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TurnRefSnapshotStore(tmp_path / "s")
    path = store.path_for(_PROJECT, _THREAD)
    if fault in ("read", "rename"):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"{not json")
    if fault == "read":
        real = Path.read_bytes

        def _deny(self: Path) -> bytes:
            if self == path:
                raise PermissionError(13, "Permission denied")
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", _deny)
    else:

        def _full(src: Any, dst: Any) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "replace", _full)
    adapter = _Adapter()
    disp = _dispatcher(narrow, adapter, store)
    handle = await disp.spawn_instance(_thread_ref(), Role.IMPLEMENTER, "implementer-1")
    with pytest.raises(RefSnapshotUnrecordedError) as ei:
        await disp.dispatch(handle, _event("m1"))
    assert not isinstance(ei.value, DirtyCloneError)
    assert ei.value.kind == fault
    assert adapter.delivered == 0  # the implementer was never started


@pytest.mark.anyio
async def test_unreadable_file_recovers_and_the_launch_starts(narrow: Path, tmp_path: Path) -> None:
    store = TurnRefSnapshotStore(tmp_path / "s")
    path = store.path_for(_PROJECT, _THREAD)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{not json")
    adapter = _Adapter()
    disp = _dispatcher(narrow, adapter, store)
    handle = await disp.spawn_instance(_thread_ref(), Role.IMPLEMENTER, "implementer-1")
    await disp.dispatch(handle, _event("m1"))
    assert adapter.delivered == 1
    scope = ContinuationScope(project=_PROJECT, thread_id=_THREAD, trigger_msg_id="m1")
    assert store.baseline_for(scope, narrow) is None
    # With no baseline, an unpushed leftover is still refused: judged on every branch.
    _feature(narrow, "feature/leftover", "lo", push=False)
    with pytest.raises(DirtyCloneError):
        CloneGuard(narrow).check_pushed(baseline=store.baseline_for(scope, narrow))


@pytest.mark.anyio
async def test_ref_snapshot_git_failure_stays_git_unavailable(
    narrow: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(self: CloneGuard) -> dict[str, str]:
        raise DirtyCloneError(
            repo_dir=self.repo_dir,
            reason=DirtyCloneReason.GIT_UNAVAILABLE,
            head=None,
            detail="for-each-ref failed",
        )

    monkeypatch.setattr(CloneGuard, "ref_snapshot", _fail)
    adapter = _Adapter()
    disp = _dispatcher(narrow, adapter, TurnRefSnapshotStore(tmp_path / "s"))
    handle = await disp.spawn_instance(_thread_ref(), Role.IMPLEMENTER, "implementer-1")
    with pytest.raises(DirtyCloneError) as ei:
        await disp.dispatch(handle, _event("m1"))
    assert ei.value.reason is DirtyCloneReason.GIT_UNAVAILABLE
    assert "ref snapshot not recorded" in ei.value.detail
    assert adapter.delivered == 0


# ------------------------------------------------------------------------- exit code


def test_main_maps_ref_snapshot_unrecorded_to_exit_12(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    target = tmp_path / "s" / "p" / "t.json"

    async def _fake_run_conductor(_settings: MindwireSettings, **_kw: object) -> None:
        raise RefSnapshotUnrecordedError(
            kind="write", path=target, project="p", thread_id="t", error="disk full"
        )

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: MindwireSettings())
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])
    with pytest.raises(SystemExit) as ei:
        loop_runner.main()
    assert ei.value.code == REF_SNAPSHOT_UNRECORDED_EXIT_CODE == 12
    assert ei.value.code != DIRTY_CLONE_EXIT_CODE
    out = capsys.readouterr().out
    assert "reason=adapter_error" not in out
    assert "MINDWIRE_DIRTY_CLONE_PAYLOAD" not in out
    rows = [ln for ln in out.splitlines() if ln.startswith(REF_SNAPSHOT_UNRECORDED_PAYLOAD_PREFIX)]
    assert len(rows) == 1
    row = json.loads(rows[0][len(REF_SNAPSHOT_UNRECORDED_PAYLOAD_PREFIX) :])
    assert row["reason"] == "ref_snapshot_unrecorded"
    assert row["kind"] == "write"
    assert row["path"] == os.path.abspath(target)
    assert row["error"] == "disk full"
    assert row["project"] == "p" and row["thread_id"] == "t"


def test_exit_code_is_the_next_free_one_and_pinned_to_the_wrapper() -> None:
    from spirrow_mindwire.conductor import run_budget, stall, stand_down
    from spirrow_mindwire.git_sync.errors import GIT_SYNC_DEFERRED_EXIT_CODE
    from spirrow_mindwire.preflight import PREFLIGHT_DEFERRED_EXIT_CODE
    from spirrow_mindwire.turn_isolation.errors import SECRETS_STOP_EXIT_CODE

    taken = {
        1,
        2,
        stand_down.STAND_DOWN_EXIT_CODE,
        stall.STALLED_EXIT_CODE,
        run_budget.RUN_TIMEOUT_EXIT_CODE,
        run_budget.RUN_KILLED_EXIT_CODE,
        run_budget.RUN_KILL_UNCONFIRMED_EXIT_CODE,
        DIRTY_CLONE_EXIT_CODE,
        GIT_SYNC_DEFERRED_EXIT_CODE,
        SECRETS_STOP_EXIT_CODE,
        PREFLIGHT_DEFERRED_EXIT_CODE,
    }
    assert REF_SNAPSHOT_UNRECORDED_EXIT_CODE not in taken
    assert max(taken) + 1 == REF_SNAPSHOT_UNRECORDED_EXIT_CODE
    sweep = (_REPO_ROOT / "deploy" / "run-conductor-scheduled.ps1").read_text(encoding="utf-8")
    m = re.search(r"^\$RefSnapshotUnrecordedExitCode\s*=\s*(\d+)\s*$", sweep, re.MULTILINE)
    assert m is not None and int(m.group(1)) == REF_SNAPSHOT_UNRECORDED_EXIT_CODE
    assert f"^{REF_SNAPSHOT_UNRECORDED_PAYLOAD_PREFIX.strip()}" in sweep
