"""D-16ab single-writer lock (T-stalled-pr-has-no-detector msg-4703 §3/§5, msg-4705 §3/§4).

The concurrency tests use real, separate processes. CI runs Linux only, so CI exercises
the ``fcntl`` side; the ``msvcrt`` side (production) is exercised by running this same
file on Windows (msg-4703 §5 platform caveat) -- nothing here is skipped on Windows.
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spirrow_mindwire.stall_ledger import lock as lock_mod
from spirrow_mindwire.stall_ledger.adapters import QuarantineFileAdapter
from spirrow_mindwire.stall_ledger.driver import TickPaths, run_tick
from spirrow_mindwire.stall_ledger.lock import (
    LOCK_OFFSET,
    PAYLOAD_SIZE,
    LedgerLock,
    encode_payload,
    read_payload,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

# A child that takes the lock, reports, and holds it until ``go`` appears (or forever).
_HOLDER = textwrap.dedent(
    """
    import json, os, sys, time, pathlib
    from spirrow_mindwire.stall_ledger.lock import LedgerLock
    lock = LedgerLock(pathlib.Path(sys.argv[1]))
    ok = lock.acquire({"pid": os.getpid(), "started_at": "child"})
    print("won" if ok else "lost", flush=True)
    if ok:
        release = pathlib.Path(sys.argv[2])
        while not release.exists():
            time.sleep(0.02)
        lock.release({"released_at": "child-end"})
    """
)

# A child that runs one real tick against a quarantine.json source that is slow to read,
# so concurrent children overlap. It waits for ``start`` so all children begin together.
_TICKER = textwrap.dedent(
    """
    import asyncio, io, json, sys, time, pathlib
    from datetime import UTC, datetime
    from spirrow_mindwire.stall_ledger.adapters import QuarantineFileAdapter
    from spirrow_mindwire.stall_ledger.driver import TickPaths, run_tick

    class Slow(QuarantineFileAdapter):
        async def fetch(self):
            await asyncio.sleep(float(sys.argv[3]))
            return await super().fetch()

    state = pathlib.Path(sys.argv[1])
    start = pathlib.Path(sys.argv[2])
    while not start.exists():
        time.sleep(0.01)
    paths = TickPaths(state_dir=state)
    out = asyncio.run(run_tick(
        paths=paths,
        adapters=[Slow(state / "quarantine.json")],
        now=lambda: datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
        out=io.StringIO(),
    ))
    print(json.dumps({"evaluated": out.evaluated, "skipped": out.skipped}), flush=True)
    """
)


def _spawn(code: str, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _readline(proc: subprocess.Popen[str], timeout: float = 30.0) -> str:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line:
            return line.strip()
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(f"child exited early: {err}")
    raise AssertionError("child did not report")


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    (state / "quarantine.json").write_text(
        json.dumps({"p/T-a": {"first_failure_at": "2026-09-01T00:00:00+00:00"}}), encoding="utf-8"
    )
    return state


def _file_id(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_dev, st.st_ino


def _acquire_eventually(path: Path, timeout: float = 10.0) -> LedgerLock:
    # Windows may release a dead process's lock slightly after the process ends
    # (msg-4703 §3-4): that is a delay, not a takeover.
    deadline = time.monotonic() + timeout
    while True:
        lock = LedgerLock(path)
        if lock.acquire({"pid": os.getpid(), "started_at": "test"}):
            return lock
        if time.monotonic() > deadline:
            raise AssertionError("lock never became free")
        time.sleep(0.05)


# ── msg-4703 §5 ───────────────────────────────────────────────────────────────────────


def test_two_concurrent_ticks_one_evaluates_one_skips(tmp_path: Path) -> None:
    state = _seed_state(tmp_path)
    # Reference: what one tick alone produces.
    solo = tmp_path / "solo"
    solo.mkdir()
    solo_state = _seed_state(solo)
    asyncio.run(
        run_tick(
            paths=TickPaths(state_dir=solo_state),
            adapters=[QuarantineFileAdapter(solo_state / "quarantine.json")],
            now=lambda: NOW,
            out=io.StringIO(),
        )
    )
    start = tmp_path / "start"
    procs = [_spawn(_TICKER, str(state), str(start), "1.5") for _ in range(2)]
    start.touch()
    results = [json.loads(_readline(p)) for p in procs]
    for p in procs:
        p.wait(timeout=30)
    assert sorted(r["evaluated"] for r in results) == [False, True]
    assert [r["skipped"] for r in results if not r["evaluated"]] == ["locked"]
    assert json.loads((state / "stall-ledger.json").read_text(encoding="utf-8")) == json.loads(
        (solo_state / "stall-ledger.json").read_text(encoding="utf-8")
    )


def test_killed_holder_releases_the_lock_without_takeover(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.lock"
    child = _spawn(_HOLDER, str(path), str(tmp_path / "never"))
    assert _readline(child) == "won"
    assert not LedgerLock(path).acquire({"pid": 1, "started_at": "x"})
    child.kill()  # SIGKILL on POSIX, TerminateProcess on Windows
    child.wait(timeout=30)
    payload = read_payload(path)
    assert payload is not None and "released_at" not in payload
    lock = _acquire_eventually(path)
    lock.release()


def test_three_ticks_over_a_dead_owner_payload_exactly_one_evaluates(tmp_path: Path) -> None:
    state = _seed_state(tmp_path)
    # The last holder's diagnostics name a pid that does not exist. Nothing reads it.
    (state / "stall-ledger.lock").write_bytes(
        encode_payload({"pid": 2**31 - 7, "started_at": "2026-01-01T00:00:00+00:00"})
    )
    start = tmp_path / "start"
    procs = [_spawn(_TICKER, str(state), str(start), "1.5") for _ in range(3)]
    start.touch()
    results = [json.loads(_readline(p)) for p in procs]
    for p in procs:
        p.wait(timeout=30)
    assert sum(1 for r in results if r["evaluated"]) == 1
    assert sorted(r["skipped"] for r in results if not r["evaluated"]) == ["locked", "locked"]


def test_lock_file_is_never_deleted_or_recreated(tmp_path: Path) -> None:
    state = _seed_state(tmp_path)
    lock_path = state / "stall-ledger.lock"
    paths = TickPaths(state_dir=state)
    asyncio.run(
        run_tick(
            paths=paths,
            adapters=[QuarantineFileAdapter(state / "quarantine.json")],
            now=lambda: NOW,
            out=io.StringIO(),
        )
    )
    ident = _file_id(lock_path)

    def at(hours: int) -> Callable[[], datetime]:
        return lambda: NOW + timedelta(hours=hours)

    for i in range(3):
        held = LedgerLock(lock_path)
        assert held.acquire({"pid": os.getpid(), "started_at": str(i)})
        skipped = asyncio.run(
            run_tick(paths=paths, adapters=[], now=lambda: NOW, out=io.StringIO())
        )
        assert skipped.skipped == "locked"
        held.release({"released_at": "t"})
        asyncio.run(
            run_tick(
                paths=paths,
                adapters=[QuarantineFileAdapter(state / "quarantine.json")],
                now=at(i),
                out=io.StringIO(),
            )
        )
        assert _file_id(lock_path) == ident


# ── msg-4705 §4 ──────────────────────────────────────────────────────────────────────


def test_payload_readable_by_another_process_while_held(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.lock"
    release = tmp_path / "release"
    child = _spawn(_HOLDER, str(path), str(release))
    try:
        assert _readline(child) == "won"
        with open(path, "rb") as fh:  # a plain whole-file read, like Get-Content
            data = fh.read()
        payload = json.loads(data.decode("ascii"))
        assert payload["started_at"] == "child"
        # Not compared with ``child.pid``: on Windows a venv's python.exe is a launcher
        # whose pid differs from the interpreter that holds the lock.
        assert isinstance(payload["pid"], int)
    finally:
        release.touch()
        child.wait(timeout=30)
    after = read_payload(path)
    assert after is not None
    assert (after["started_at"], after["released_at"]) == ("child", "child-end")


def test_losing_tick_leaves_lock_file_bytes_identical(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    holder = LedgerLock(state / "stall-ledger.lock")
    assert holder.acquire({"pid": os.getpid(), "started_at": "holder"})
    try:
        before = (state / "stall-ledger.lock").read_bytes()
        out = asyncio.run(
            run_tick(
                paths=TickPaths(state_dir=state), adapters=[], now=lambda: NOW, out=io.StringIO()
            )
        )
        assert out.skipped == "locked"
        assert out.lines == [
            {
                "at": NOW.isoformat(),
                "kind": "heartbeat",
                "evaluated": False,
                "tick_skipped": "locked",
            }
        ]
        assert (state / "stall-ledger.lock").read_bytes() == before
        assert not (state / "stall-ledger.json").exists()
    finally:
        holder.release()


def test_payload_is_fixed_size_with_no_leftover_tail(tmp_path: Path) -> None:
    path = tmp_path / "stall-ledger.lock"
    long_lock = LedgerLock(path)
    assert long_lock.acquire({"pid": 1, "started_at": "x" * 150})
    long_lock.release({"released_at": "y" * 40})
    assert path.stat().st_size == PAYLOAD_SIZE
    short = LedgerLock(path)
    assert short.acquire({"pid": 2, "started_at": "s"})
    assert path.stat().st_size == PAYLOAD_SIZE
    assert read_payload(path) == {"pid": 2, "started_at": "s"}
    short.release({"released_at": "r"})
    assert read_payload(path) == {"pid": 2, "started_at": "s", "released_at": "r"}


def test_payload_too_large_is_refused() -> None:
    with pytest.raises(lock_mod.PayloadTooLargeError):
        encode_payload({"x": "y" * PAYLOAD_SIZE})


def test_lock_offset_is_beyond_the_payload() -> None:
    assert LOCK_OFFSET > PAYLOAD_SIZE
    assert len(encode_payload({"pid": 1})) == PAYLOAD_SIZE


def test_lock_module_never_truncates() -> None:
    """Static: no ``O_TRUNC`` and no ``open(..., "w...")`` anywhere in ``lock.py``."""
    tree = ast.parse(Path(lock_mod.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "O_TRUNC", "O_TRUNC in lock.py"
        if isinstance(node, ast.Name):
            assert node.id != "O_TRUNC", "O_TRUNC in lock.py"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
        ):
            modes = list(node.args[1:2]) + [k.value for k in node.keywords if k.arg == "mode"]
            for mode in modes:
                assert isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                assert "w" not in mode.value and "+" not in mode.value, mode.value


def test_normal_end_records_released_at(tmp_path: Path) -> None:
    state = _seed_state(tmp_path)
    asyncio.run(
        run_tick(
            paths=TickPaths(state_dir=state),
            adapters=[QuarantineFileAdapter(state / "quarantine.json")],
            now=lambda: NOW,
            out=io.StringIO(),
        )
    )
    payload = read_payload(state / "stall-ledger.lock")
    assert payload is not None
    assert payload["pid"] == os.getpid()
    assert "released_at" in payload
