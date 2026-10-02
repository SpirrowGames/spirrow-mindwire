"""D-16ab lock driven through real ticks (T-stalled-pr-has-no-detector msg-4703 §5,
msg-4705 §4): concurrent ticks, the 3-way race over a dead owner's payload, lock-file
identity, the losing tick, and ``released_at`` after a normal end.

Real separate processes; nothing is skipped on Windows (msg-4703 §5 platform caveat).
"""

from __future__ import annotations

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

from spirrow_mindwire.stall_ledger.adapters import QuarantineFileAdapter
from spirrow_mindwire.stall_ledger.driver import TickPaths, run_tick
from spirrow_mindwire.stall_ledger.lock import (
    LedgerLock,
    encode_payload,
    read_payload,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

# A child that takes the lock, reports, and holds it until ``go`` appears (or forever).
# A child that runs one real tick against a quarantine.json source that is slow to read,
# so concurrent children overlap. It prints ``ready`` once its imports are done, then
# waits for ``start``: the parent touches ``start`` only after EVERY child is ready, so a
# child still importing under load cannot begin after another child's tick has already
# ended (which made "exactly one evaluates" flake under the full gate).
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
    print("ready", flush=True)
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


def _start_together(procs: list[subprocess.Popen[str]], start: Path) -> None:
    for p in procs:
        assert _readline(p, timeout=60.0) == "ready"
    start.touch()


def _file_id(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_dev, st.st_ino


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
    _start_together(procs, start)
    results = [json.loads(_readline(p)) for p in procs]
    for p in procs:
        p.wait(timeout=30)
    assert sorted(r["evaluated"] for r in results) == [False, True]
    assert [r["skipped"] for r in results if not r["evaluated"]] == ["locked"]
    assert json.loads((state / "stall-ledger.json").read_text(encoding="utf-8")) == json.loads(
        (solo_state / "stall-ledger.json").read_text(encoding="utf-8")
    )


def test_three_ticks_over_a_dead_owner_payload_exactly_one_evaluates(tmp_path: Path) -> None:
    state = _seed_state(tmp_path)
    # The last holder's diagnostics name a pid that does not exist. Nothing reads it.
    (state / "stall-ledger.lock").write_bytes(
        encode_payload({"pid": 2**31 - 7, "started_at": "2026-01-01T00:00:00+00:00"})
    )
    start = tmp_path / "start"
    procs = [_spawn(_TICKER, str(state), str(start), "1.5") for _ in range(3)]
    _start_together(procs, start)
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
