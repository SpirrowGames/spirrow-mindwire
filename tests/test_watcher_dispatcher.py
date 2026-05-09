"""Tests for :class:`spirrow_mindwire.watcher.dispatcher.ThreadDispatcher`.

The SDK invoker is replaced by a fake so we exercise the dispatcher's
control flow (load → dedup → invoke → log) without spinning up a real
Claude session. Phanthand is left as a bare ``AsyncMock`` because the
dispatcher only forwards the client into the tool factory.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from factories import seed_thread_meta, write_message_file

from spirrow_mindwire.claude_code import InvokeResult
from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.phanthand import PhanthandClient
from spirrow_mindwire.watcher.dedup import DedupCache
from spirrow_mindwire.watcher.dispatcher import ThreadDispatcher
from spirrow_mindwire.watcher.events import ThreadEvent

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
NOW = datetime(2026, 5, 7, 8, 43, 7, tzinfo=UTC)


def _seed_thread(base: Path, sender: str = "claude.ai", seq: int = 1) -> ThreadDirLayout:
    """Seed meta + one in-place message; the dispatcher does not need atomic writes."""
    layout = ThreadDirLayout(base_dir=base, thread_id=ULID_A)
    seed_thread_meta(layout)
    write_message_file(layout, seq, sender, atomic=False)
    return layout


def _event(thread_id: str = ULID_A, seq: int = 1, when: datetime | None = None) -> ThreadEvent:
    return ThreadEvent(
        thread_id=thread_id,
        seq=seq,
        path=Path("ignored"),
        detected_at=when or NOW,
    )


def _write_message(layout: ThreadDirLayout, seq: int, sender: str, body: str = "hello") -> None:
    write_message_file(layout, seq, sender, body, atomic=False)


def _invoker(captured: dict[str, Any], result: InvokeResult) -> Any:
    async def fake(**kwargs: Any) -> InvokeResult:
        captured.update(kwargs)
        return result

    return fake


def _ok_result() -> InvokeResult:
    return InvokeResult(
        is_error=False,
        duration_ms=120,
        text_output="ok",
        result_text="ok",
        stop_reason="end_turn",
    )


@pytest.mark.anyio
async def test_dispatcher_invokes_for_claude_ai_message(tmp_path: Path) -> None:
    layout = _seed_thread(tmp_path)
    captured: dict[str, Any] = {}
    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_invoker(captured, _ok_result()),
    )

    await dispatcher.handle(_event())

    # Invoker received the SDK call with the right wiring.
    assert captured["cwd"] == layout.thread_dir
    assert captured["allowed_tools"] == [
        "mcp__mindwire__write_reply",
        "mcp__mindwire__read_file",
        "mcp__mindwire__list_dir",
        "mcp__mindwire__search",
        "mcp__mindwire__file_info",
    ]
    assert "mindwire" in captured["mcp_servers"]
    assert "<mw_thread" in captured["prompt"]

    # Event log got start + end entries.
    log_lines = layout.event_log_path.read_text(encoding="utf-8").splitlines()
    types = [json.loads(line)["type"] for line in log_lines]
    assert types == ["claude_code.invoke.start", "claude_code.invoke.end"]
    end = json.loads(log_lines[-1])
    assert end["duration_ms"] == 120
    assert end["exit_code"] == 0


@pytest.mark.anyio
async def test_dispatcher_skips_when_latest_from_claude_code(tmp_path: Path) -> None:
    """Don't loop on our own write_reply output."""
    _seed_thread(tmp_path, sender="claude-code")
    captured: dict[str, Any] = {}
    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_invoker(captured, _ok_result()),
    )

    await dispatcher.handle(_event())

    assert captured == {}  # invoker was never called


@pytest.mark.anyio
async def test_dispatcher_dedups_repeated_event(tmp_path: Path) -> None:
    _seed_thread(tmp_path)
    call_count = 0

    async def counting_invoker(**kwargs: Any) -> InvokeResult:
        nonlocal call_count
        call_count += 1
        return _ok_result()

    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=counting_invoker,
    )

    await dispatcher.handle(_event())
    await dispatcher.handle(_event(when=NOW + timedelta(seconds=2)))

    assert call_count == 1


@pytest.mark.anyio
async def test_dispatcher_logs_error_end_when_invoke_raises(tmp_path: Path) -> None:
    layout = _seed_thread(tmp_path)

    async def failing_invoker(**kwargs: Any) -> InvokeResult:
        raise RuntimeError("SDK boom")

    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=failing_invoker,
    )

    with pytest.raises(RuntimeError, match="SDK boom"):
        await dispatcher.handle(_event())

    log_lines = layout.event_log_path.read_text(encoding="utf-8").splitlines()
    types = [json.loads(line)["type"] for line in log_lines]
    assert types == ["claude_code.invoke.start", "claude_code.invoke.end"]
    end = json.loads(log_lines[-1])
    assert end["exit_code"] == 1


@pytest.mark.anyio
async def test_dispatcher_serializes_invocations_per_thread(tmp_path: Path) -> None:
    """Two events on the same thread must run one-after-the-other.

    Architecture.md §4.0 forbids concurrent invocations on a single
    thread — they would collide on ``write_reply``'s next_seq compute
    and interleave the event log. We block the first invoker on an
    asyncio.Event, fire the second, and assert the second task is still
    pending. The Event-based wait is deterministic: it doesn't depend
    on a tick race the way an "in_progress" boolean would.
    """
    layout = _seed_thread(tmp_path)  # seq=1 from claude.ai already on disk

    first_active = asyncio.Event()
    allow_first_finish = asyncio.Event()
    invoker_seqs: list[int] = []

    async def fake_invoker(**kwargs: Any) -> InvokeResult:
        prompt = kwargs["prompt"]
        m = re.search(r'<mw_message\s+seq="(\d+)"[^>]*is_latest="true"', prompt)
        seq_in_prompt = int(m.group(1)) if m else -1
        # First invocation: signal active and block until the test releases
        if not first_active.is_set():
            first_active.set()
            await allow_first_finish.wait()
        invoker_seqs.append(seq_in_prompt)
        return _ok_result()

    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=fake_invoker,
    )

    task1 = asyncio.create_task(dispatcher.handle(_event(seq=1)))
    await asyncio.wait_for(first_active.wait(), timeout=2.0)

    # Add a fresh seq=2 from claude.ai and dispatch a second event.
    _write_message(layout, 2, "claude.ai", "second")
    task2 = asyncio.create_task(dispatcher.handle(_event(seq=2)))

    # Give the loop a tick to expose any concurrent entry; task2 must
    # still be parked behind the per-thread lock.
    await asyncio.sleep(0.1)
    assert not task2.done(), (
        "second invoke ran concurrently with first — per-thread serialization broken"
    )
    assert invoker_seqs == [], "first invoker has not appended yet (still blocked)"

    # Release the first invoker; both tasks should now complete in order.
    allow_first_finish.set()
    await asyncio.gather(task1, task2)

    assert invoker_seqs == [1, 2], f"expected per-thread FIFO [1, 2], got {invoker_seqs}"


@pytest.mark.anyio
async def test_dispatcher_runs_distinct_threads_in_parallel(tmp_path: Path) -> None:
    """Different threads must NOT serialize against each other.

    Counter-test to ``test_dispatcher_serializes_invocations_per_thread``:
    the per-thread lock must key on ``thread_id``, not be a global mutex.
    """
    other_ulid = "01ARZ3NDEKTSV4RRFFQ69G5FBZ"

    # Seed two distinct threads, each with one claude.ai message.
    layout_a = _seed_thread(tmp_path)  # ULID_A, seq=1
    layout_b = ThreadDirLayout(base_dir=tmp_path, thread_id=other_ulid)
    seed_thread_meta(layout_b)
    _write_message(layout_b, 1, "claude.ai", "from-b")
    assert layout_a.thread_dir.exists()  # silence linter

    a_active = asyncio.Event()
    b_active = asyncio.Event()
    allow_finish = asyncio.Event()

    async def fake_invoker(**kwargs: Any) -> InvokeResult:
        thread_dir = kwargs["cwd"]
        if thread_dir.name == ULID_A:
            a_active.set()
        elif thread_dir.name == other_ulid:
            b_active.set()
        await allow_finish.wait()
        return _ok_result()

    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=fake_invoker,
    )

    task_a = asyncio.create_task(dispatcher.handle(_event(thread_id=ULID_A, seq=1)))
    task_b = asyncio.create_task(dispatcher.handle(_event(thread_id=other_ulid, seq=1)))

    # Both must enter the invoker without waiting on each other.
    await asyncio.wait_for(asyncio.gather(a_active.wait(), b_active.wait()), timeout=2.0)

    allow_finish.set()
    await asyncio.gather(task_a, task_b)


@pytest.mark.anyio
async def test_dispatcher_propagates_is_error_to_event_log(tmp_path: Path) -> None:
    layout = _seed_thread(tmp_path)
    err_result = InvokeResult(
        is_error=True,
        duration_ms=50,
        text_output="",
        result_text=None,
        stop_reason="error",
    )
    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_invoker({}, err_result),
    )

    await dispatcher.handle(_event())

    log_lines = layout.event_log_path.read_text(encoding="utf-8").splitlines()
    end = json.loads(log_lines[-1])
    assert end["exit_code"] == 1
    assert end["duration_ms"] == 50
