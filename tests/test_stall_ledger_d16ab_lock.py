"""D-16ab single-writer lock, module level (T-stalled-pr-has-no-detector msg-4703 §3/§5,
msg-4705 §3/§4). The tests that drive the lock through a real tick live in
``test_stall_ledger_d16ab_lock_tick.py``.

The concurrency tests use real, separate processes. CI runs Linux only, so CI exercises
the ``fcntl`` side; the ``msvcrt`` side (production) is exercised by running this same
file on Windows (msg-4703 §5 platform caveat) -- nothing here is skipped on Windows.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spirrow_mindwire.stall_ledger import lock as lock_mod
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
