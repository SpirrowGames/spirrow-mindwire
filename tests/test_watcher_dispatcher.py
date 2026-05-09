"""Tests for :class:`spirrow_mindwire.watcher.dispatcher.ThreadDispatcher`.

The SDK invoker is replaced by a fake so we exercise the dispatcher's
control flow (load → dedup → invoke → log) without spinning up a real
Claude session. Phanthand is left as a bare ``AsyncMock`` because the
dispatcher only forwards the client into the tool factory.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
NOW = datetime(2026, 5, 7, 8, 43, 7, tzinfo=UTC)


def _seed_thread(base: Path, sender: str = "claude.ai", seq: int = 1) -> ThreadDirLayout:
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
    target = layout.message_path(seq, sender)  # type: ignore[arg-type]
    target.write_text(
        "---\n"
        + yaml.safe_dump(
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
        + "---\n\nhello\n",
        encoding="utf-8",
    )
    return layout


def _event(thread_id: str = ULID_A, seq: int = 1, when: datetime | None = None) -> ThreadEvent:
    return ThreadEvent(
        thread_id=thread_id,
        seq=seq,
        path=Path("ignored"),
        detected_at=when or NOW,
    )


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
