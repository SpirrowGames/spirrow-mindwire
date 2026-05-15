"""Cross-process integration tests (Feature 3-A sub-PR 3, D3-2).

Verifies the D2-6 invariant (chatroom ``T-feat3-d2-mcp-server`` msg-127
§1): the ``mindwire-mcp-server`` write side and the watcher dispatcher
coordinate **only** via the on-disk thread directory — no shared
memory, no RPC.

Two layers (msg-131 §3, integrator decide):

- **option-b (CI default, deterministic)**: ``WriteTools`` (mcp-server
  side) and ``ThreadDispatcher`` (watcher side) are constructed as
  fully independent objects sharing nothing but the ``tmp_path``
  string. Tests assert the multi-turn round trip works through the
  filesystem alone, and that the msg-127 D2-6 "accepted race" never
  corrupts state beyond the documented silent overwrite.
- **option-a (manual gate)**: a real ``mindwire-mcp-server`` subprocess
  is launched (true process separation) and smoke-checked. Marked
  ``@pytest.mark.manual`` so CI skips it (addopts ``-m "not manual"``);
  it is run via the Phase 1 dogfooding-resume gate
  (``uv run pytest -m manual``), see docs/feature-3-design.md §2.3.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from factories import seed_thread_meta, write_message_file

from spirrow_mindwire.claude_code import InvokeResult
from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.mcp_write_server.http import MCP_PATH
from spirrow_mindwire.mcp_write_server.tools_write import WriteTools
from spirrow_mindwire.phanthand import PhanthandClient
from spirrow_mindwire.watcher.dedup import DedupCache
from spirrow_mindwire.watcher.dispatcher import ThreadDispatcher
from spirrow_mindwire.watcher.events import ThreadEvent
from spirrow_mindwire.watcher.loader import load_messages, load_thread_meta
from spirrow_mindwire.watcher.startup_scan import _detect_race_gap

NOW = datetime(2026, 5, 7, 8, 43, 7, tzinfo=UTC)


def _ok_result() -> InvokeResult:
    return InvokeResult(
        is_error=False,
        duration_ms=120,
        text_output="ok",
        result_text="ok",
        stop_reason="end_turn",
    )


def _reply_invoker(layout: ThreadDirLayout, next_seq: int) -> object:
    """Fake SDK invoker that lands a claude-code reply, then returns success.

    Mirrors production: the SDK calls ``mcp__mindwire__write_reply``
    before finishing. Writing through ``factories.write_message_file``
    keeps the filesystem the only channel between the two components.
    """

    async def fake(**_kwargs: object) -> InvokeResult:
        write_message_file(layout, next_seq, "claude-code", "fake reply", atomic=True)
        return _ok_result()

    return fake


def _dispatcher(tmp_path: Path, layout: ThreadDirLayout, next_seq: int) -> ThreadDispatcher:
    """A watcher-side dispatcher sharing nothing with WriteTools but tmp_path."""
    return ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_reply_invoker(layout, next_seq),  # type: ignore[arg-type]
    )


def _event(thread_id: str, seq: int) -> ThreadEvent:
    return ThreadEvent(thread_id=thread_id, seq=seq, path=Path("ignored"), detected_at=NOW)


# ---------------------------------------------------------------------------
# option-b (CI default, deterministic) — D2-6 invariant verify
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_filesystem_mediated_roundtrip(tmp_path: Path) -> None:
    """A full multi-turn round trip coordinated only through the filesystem.

    ``WriteTools`` (mcp-server side) and ``ThreadDispatcher`` (watcher
    side) are independent objects; the thread directory is the only
    shared state. This is the core D2-6 invariant assertion.
    """
    write_tools = WriteTools(data_dir=tmp_path)

    # Turn 1: claude.ai opens a thread via the write server.
    opened = await write_tools.open_thread("hello from claude.ai")
    thread_id = opened["thread_id"]
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=thread_id)
    assert load_thread_meta(layout).awaiting_from == "claude-code"

    # Turn 1 reply: the watcher dispatcher (separate object) picks it up.
    dispatcher = _dispatcher(tmp_path, layout, next_seq=2)
    await dispatcher.handle(_event(thread_id, seq=1))

    meta = load_thread_meta(layout)
    assert meta.awaiting_from == "claude.ai"  # toggled back to claude.ai
    msgs = load_messages(layout)
    assert [(m.seq, m.from_) for m in msgs] == [(1, "claude.ai"), (2, "claude-code")]

    # Turn 2: claude.ai sends again — only valid because awaiting_from
    # was toggled to claude.ai by the *other* component via the FS.
    sent = await write_tools.send_message(thread_id, "second turn")
    assert sent["seq"] == 3
    assert load_thread_meta(layout).awaiting_from == "claude-code"
    assert _detect_race_gap(load_messages(layout)) is None  # clean sequence


@pytest.mark.anyio
async def test_concurrent_same_thread_no_corruption(tmp_path: Path) -> None:
    """The msg-127 D2-6 accepted race never corrupts state beyond silent overwrite.

    Running ``send_message`` (claude.ai) and the dispatcher (claude-code
    reply) concurrently on the same thread, the assertions are
    scheduling-independent: no crash, meta.yaml stays a valid
    ``ThreadMeta``, every message file stays a valid ``Message``, and
    the race-gap detector's verdict is consistent with the on-disk
    reality.
    """
    thread_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=thread_id)
    seed_thread_meta(layout, status="active", awaiting_from="claude.ai")
    write_message_file(layout, seq=1, sender="claude.ai", atomic=False)

    write_tools = WriteTools(data_dir=tmp_path)
    dispatcher = _dispatcher(tmp_path, layout, next_seq=2)

    results = await asyncio.gather(
        write_tools.send_message(thread_id, "racing claude.ai write"),
        dispatcher.handle(_event(thread_id, seq=1)),
        return_exceptions=True,
    )

    # No crash. send_message may legitimately raise ToolError if the
    # dispatcher toggled awaiting_from first (= turn-discipline guard,
    # an accepted outcome, not corruption).
    from mcp.server.fastmcp.exceptions import ToolError

    for r in results:
        if isinstance(r, BaseException):
            assert isinstance(r, ToolError), f"unexpected error: {r!r}"

    # Filesystem stays parseable regardless of who won the race.
    meta = load_thread_meta(layout)  # raises if meta.yaml corrupt
    assert meta.status == "active"
    msgs = load_messages(layout)  # raises if any message file corrupt
    assert msgs  # at least the seed message survives

    # The metric's verdict must agree with reality: if two files share a
    # seq, the detector must see it; if not, it must not invent one.
    seqs = [m.seq for m in msgs]
    has_dup = len(seqs) != len(set(seqs))
    gap = _detect_race_gap(msgs)
    assert (gap is not None) == (has_dup or sorted(seqs) != list(range(seqs[0], seqs[-1] + 1)))


# ---------------------------------------------------------------------------
# option-a (manual gate) — real subprocess, true process separation
# ---------------------------------------------------------------------------


@pytest.mark.manual
@pytest.mark.anyio
async def test_real_subprocess_server_health_and_auth(tmp_path: Path) -> None:
    """Launch a real ``mindwire-mcp-server`` subprocess and smoke it.

    True process separation: the server runs in its own OS process with
    its own data dir. This doubles as the C2 server-vs-connector triage
    smoke (msg-131 §4) — exactly the ``curl -H 'Authorization: Bearer
    ...'`` health check the recipe doc prescribes, expressed as code.

    Skipped in CI (``@pytest.mark.manual``); run via the Phase 1
    dogfooding-resume gate.
    """
    port = int(os.environ.get("MINDWIRE_TEST_MCP_PORT", "7491"))
    api_key = "manual-e2e-secret"
    env = {
        **os.environ,
        "MINDWIRE_MCP_API_KEY": api_key,
        "MINDWIRE_PATHS__DATA_DIR": str(tmp_path),
        "MINDWIRE_MCP_SERVER__PORT": str(port),
        "MINDWIRE_MCP_SERVER__HOST": "127.0.0.1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uv", "run", "mindwire-mcp-server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}{MCP_PATH}"
    try:
        # Wait for the port to bind (true separate process startup).
        deadline = time.monotonic() + 30.0
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError("mindwire-mcp-server subprocess exited early")
            try:
                async with httpx.AsyncClient() as client:
                    # No auth header → middleware must reject with 401
                    # (proves the server is up *and* auth is active).
                    resp = await client.post(base, json={}, timeout=2.0)
                if resp.status_code == 401:
                    break
            except httpx.HTTPError as e:  # not bound yet
                last_exc = e
            await asyncio.sleep(0.5)
        else:
            raise AssertionError(f"server never reached 401-without-auth state: {last_exc!r}")

        # With a bogus token → still 401 (auth actually validates).
        async with httpx.AsyncClient() as client:
            bad = await client.post(
                base,
                json={},
                headers={"Authorization": "Bearer wrong"},
                timeout=2.0,
            )
        assert bad.status_code == 401
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.wait(timeout=5)
