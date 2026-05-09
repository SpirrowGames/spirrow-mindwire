"""End-to-end happy-path test for the Phase 0 watcher.

Stitches together the real watchdog observer, the real dispatcher, and
a fake SDK invoker. The point is to catch wiring regressions — the
unit tests cover each component in isolation, this one verifies they
compose. The Claude SDK and Phanthand are mocked because exercising
either in CI is out of scope.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from spirrow_mindwire.claude_code import InvokeResult
from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.phanthand import PhanthandClient
from spirrow_mindwire.watcher.dedup import DedupCache
from spirrow_mindwire.watcher.dispatcher import ThreadDispatcher
from spirrow_mindwire.watcher.events import ThreadEvent
from spirrow_mindwire.watcher.observer import WatcherObserver

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
EVENT_TIMEOUT = 5.0


def _seed_thread(base: Path) -> ThreadDirLayout:
    layout = ThreadDirLayout(base_dir=base, thread_id=ULID_A)
    layout.thread_dir.mkdir(parents=True, exist_ok=True)
    layout.meta_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "thread_id": ULID_A,
                "title": "",
                "status": "awaiting-cc",
                "participants": ["claude.ai", "claude-code"],
                "created_at": "2026-05-07T08:43:07Z",
                "updated_at": "2026-05-07T08:43:07Z",
                "tags": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    layout.messages_dir.mkdir(parents=True, exist_ok=True)
    return layout


def _write_message_atomically(layout: ThreadDirLayout, seq: int, sender: str, body: str) -> None:
    target = layout.message_path(seq, sender)  # type: ignore[arg-type]
    fm = yaml.safe_dump(
        {
            "schema_version": 1,
            "msg_id": f"{ULID_A}/{seq:03d}",
            "seq": seq,
            "from": sender,
            "to": "claude-code" if sender == "claude.ai" else "claude.ai",
            "created_at": "2026-05-07T08:43:07Z",
        },
        sort_keys=False,
    )
    content = f"---\n{fm}---\n\n{body}\n"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)


@pytest.mark.anyio
async def test_observer_to_dispatcher_happy_path(tmp_path: Path) -> None:
    layout = _seed_thread(tmp_path)
    # Seed an existing claude.ai message so the dispatcher has prior
    # state to load when the new event arrives.
    _write_message_atomically(layout, 1, "claude.ai", "first")

    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    invoker_calls: list[dict[str, Any]] = []

    async def fake_invoker(**kwargs: Any) -> InvokeResult:
        invoker_calls.append(kwargs)
        return InvokeResult(
            is_error=False,
            duration_ms=42,
            text_output="ok",
            result_text="ok",
            stop_reason="end_turn",
        )

    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=fake_invoker,
    )

    observer = WatcherObserver(threads_root=tmp_path / "threads", queue=queue, loop=loop)
    observer.start()
    try:
        await asyncio.sleep(0.1)  # let the observer thread settle
        # New claude.ai turn — this is what the watcher exists to react to.
        _write_message_atomically(layout, 2, "claude.ai", "second")

        event = await asyncio.wait_for(queue.get(), timeout=EVENT_TIMEOUT)
        await dispatcher.handle(event)
    finally:
        observer.stop()

    # Observer routed the right event.
    assert event.thread_id == ULID_A
    assert event.seq == 2

    # Dispatcher invoked the SDK with the right wiring.
    assert len(invoker_calls) == 1
    call = invoker_calls[0]
    assert call["cwd"] == layout.thread_dir
    assert "<mw_thread" in call["prompt"]
    assert "mindwire" in call["mcp_servers"]
    assert call["allowed_tools"][0] == "mcp__mindwire__write_reply"

    # Event log carries the full start/end pair.
    log_lines = layout.event_log_path.read_text(encoding="utf-8").splitlines()
    types = [json.loads(line)["type"] for line in log_lines]
    assert types == ["claude_code.invoke.start", "claude_code.invoke.end"]
    end_event = json.loads(log_lines[-1])
    assert end_event["duration_ms"] == 42
    assert end_event["exit_code"] == 0
