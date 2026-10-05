"""CLI wiring for Phase 1, and the machine check behind "it writes nothing"."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from spirrow_mindwire.github.client import PrRef, PrResolution, PrState
from spirrow_mindwire.pr_review_sweep.config import ProjectEntry
from spirrow_mindwire.pr_review_sweep.phase1 import Phase1Class

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pr_review_sweep_phase1.py"
_CLOSED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_ENTRY = ProjectEntry(project="spirrow-mindwire", owner="SpirrowGames", repo="spirrow-mindwire")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pr_review_sweep_phase1_cli", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load()


class _Mcp:
    def __init__(self, threads: list[dict[str, Any]], events: list[str]) -> None:
        self._threads = threads
        self._events = events
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if int(arguments.get("offset") or 0) > 0:
            return {"items": [], "total": 0}
        if name == "chatroom_list_threads":
            return {"items": self._threads, "total": len(self._threads)}
        if name == "chatroom_list_events":
            cutoff = datetime.fromisoformat(arguments["since"])
            items = [{"timestamp": t} for t in self._events if datetime.fromisoformat(t) >= cutoff]
            return {"items": items, "total": len(items)}
        raise AssertionError(f"unexpected tool {name}")


class _GitHub:
    def __init__(self, states: dict[int, PrState]) -> None:
        self._states = states
        self.asked: list[int] = []

    async def fetch_pr_state(self, pr: PrRef) -> PrState:
        self.asked.append(pr.number)
        return self._states[pr.number]


def _closed(number: int, *, merged: bool) -> PrState:
    return PrState(
        ref=PrRef("SpirrowGames", "spirrow-mindwire", number),
        resolution=PrResolution.CLOSED,
        closed_at=_CLOSED_AT,
        merged=merged,
    )


def test_sweep_project_classifies_and_filters() -> None:
    threads = [
        {"thread_id": "T-pr-review-spirrow-mindwire-1", "status": "active"},
        {"thread_id": "T-pr-review-spirrow-mindwire-2", "status": "parked"},
        {"thread_id": "T-pr-review-spirrow-mindwire-3", "status": "resolved"},
        {"thread_id": "T-pr-review-173", "status": "active"},
        {"thread_id": "T-pr-review-threads-outlive-their-prs", "status": "active"},
        {"thread_id": "T-something-else", "status": "active"},
    ]
    events = [
        (_CLOSED_AT - timedelta(minutes=5)).isoformat(),
        (_CLOSED_AT + timedelta(minutes=5)).isoformat(),
    ]
    mcp = _Mcp(threads, events)
    github = _GitHub({1: _closed(1, merged=True), 2: _closed(2, merged=False)})

    rows, excluded = asyncio.run(_MODULE.sweep_project(_MODULE.ReadOnlyMcp(mcp), github, _ENTRY))

    by_id = {r.thread_id: r for r in rows}
    assert by_id["T-pr-review-spirrow-mindwire-1"].cls is Phase1Class.TERMINAL_MERGED
    assert by_id["T-pr-review-spirrow-mindwire-1"].post_terminal_messages == 1
    # parked is in scope and NOT protected: S1 is not a gate in Phase 1.
    assert by_id["T-pr-review-spirrow-mindwire-2"].cls is Phase1Class.TERMINAL_CLOSED
    assert by_id["T-pr-review-173"].cls is Phase1Class.UNPARSEABLE
    assert by_id["T-pr-review-threads-outlive-their-prs"].cls is Phase1Class.UNPARSEABLE
    assert "T-something-else" not in by_id
    assert [e.thread_id for e in excluded] == ["T-pr-review-spirrow-mindwire-3"]
    # The resolved thread and the unparseable ids cost no GitHub call.
    assert sorted(github.asked) == [1, 2]


def test_open_pr_issues_no_event_query() -> None:
    mcp = _Mcp([{"thread_id": "T-pr-review-spirrow-mindwire-9", "status": "active"}], [])
    ref = PrRef("SpirrowGames", "spirrow-mindwire", 9)
    github = _GitHub({9: PrState(ref=ref, resolution=PrResolution.OPEN)})
    rows, _ = asyncio.run(_MODULE.sweep_project(_MODULE.ReadOnlyMcp(mcp), github, _ENTRY))
    assert rows[0].cls is Phase1Class.OPEN
    assert [name for name, _ in mcp.calls] == ["chatroom_list_threads"]


def test_allowlist_is_two_reads() -> None:
    assert {"chatroom_list_threads", "chatroom_list_events"} == _MODULE.READ_ONLY_TOOLS


@pytest.mark.parametrize(
    "tool", ["chatroom_post_message", "chatroom_close_thread", "chatroom_get_thread"]
)
def test_any_other_tool_raises(tool: str) -> None:
    mcp = _Mcp([], [])
    with pytest.raises(_MODULE.Phase1WriteAttemptedError):
        asyncio.run(_MODULE.ReadOnlyMcp(mcp).call_tool(tool, {}))
    assert mcp.calls == []
