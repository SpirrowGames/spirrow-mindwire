"""Tests for T20 PR-review orchestrator (WIRING_ALLOWLIST_SPEC §A.2).

Fake :class:`McpToolCaller` + a stub watcher exercise thread creation, number
derivation, and naysayer-watch wiring. ``ChatroomWatcher.add_watch`` is tested
directly with a fake dispatcher.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from spirrow_mindwire.magickit.watcher import ChatroomWatcher, WatchSpec
from spirrow_mindwire.orchestrator import PrReviewOrchestrator
from spirrow_mindwire.ulid_util import new_ulid
from spirrow_mindwire.value_objects import Role, SessionHandle, ThreadRef

_TS = datetime(2026, 5, 23, tzinfo=UTC)


class _FakeMcp:
    """Records call_tool invocations; returns programmed results by tool name."""

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return self._results.get(name, {})

    def args_for(self, name: str) -> dict[str, Any]:
        return next(args for n, args in self.calls if n == name)


class _StubWatcher:
    def __init__(self) -> None:
        self.added: list[tuple[WatchSpec, bool]] = []

    async def add_watch(self, watch: WatchSpec, *, baseline: bool = True) -> None:
        self.added.append((watch, baseline))


@pytest.mark.anyio
async def test_fire_pr_review_opens_thread_with_explicit_number() -> None:
    mcp = _FakeMcp()
    orch = PrReviewOrchestrator(mcp)
    ref = await orch.fire_pr_review(project="spirrow-mindwire", pr_ref="org/repo#5", number=3)
    assert ref.thread_id == "T-pr-review-3"
    assert ref.project_id == "spirrow-mindwire"
    args = mcp.args_for("chatroom_open_thread")
    assert args["thread_id"] == "T-pr-review-3"
    assert args["owner"] == "orchestrator"
    assert "org/repo#5" in args["propose_content"]
    assert "pr-review" in args["tags"]


@pytest.mark.anyio
async def test_fire_pr_review_derives_next_number() -> None:
    # Highest existing T-pr-review-<n> is 4 → next is 5 (ignores non-matching ids).
    mcp = _FakeMcp(
        {
            "chatroom_list_threads": {
                "items": [
                    {"thread_id": "T-pr-review-2"},
                    {"thread_id": "T-pr-review-4"},
                    {"thread_id": "T-other-9"},
                ]
            }
        }
    )
    orch = PrReviewOrchestrator(mcp)
    ref = await orch.fire_pr_review(project="p", pr_ref="o/r#1")
    assert ref.thread_id == "T-pr-review-5"


@pytest.mark.anyio
async def test_fire_pr_review_first_number_when_none_exist() -> None:
    mcp = _FakeMcp({"chatroom_list_threads": {"items": []}})
    orch = PrReviewOrchestrator(mcp)
    ref = await orch.fire_pr_review(project="p", pr_ref="o/r#1")
    assert ref.thread_id == "T-pr-review-1"


@pytest.mark.anyio
async def test_fire_pr_review_wires_naysayer_watch_without_baseline() -> None:
    mcp = _FakeMcp()
    watcher = _StubWatcher()
    orch = PrReviewOrchestrator(mcp, watcher=watcher)  # type: ignore[arg-type]
    ref = await orch.fire_pr_review(project="p", pr_ref="o/r#7", number=1)
    assert len(watcher.added) == 1
    watch, baseline = watcher.added[0]
    assert watch.thread_ref == ref
    assert watch.role is Role.NAYSAYER
    assert baseline is False  # the review-request message must be dispatched


@pytest.mark.anyio
async def test_fire_pr_review_no_watcher_is_fine() -> None:
    mcp = _FakeMcp()
    orch = PrReviewOrchestrator(mcp)
    ref = await orch.fire_pr_review(project="p", pr_ref="o/r#7", number=1)
    assert ref.thread_id == "T-pr-review-1"  # no watcher → just opens the thread


# ---------- ChatroomWatcher.add_watch ------------------------------------- #


class _FakeDispatcher:
    def __init__(self) -> None:
        self.spawned: list[tuple[ThreadRef, Role]] = []

    async def spawn_role(self, thread_ref: ThreadRef, role: Role) -> SessionHandle:
        self.spawned.append((thread_ref, role))
        return SessionHandle(
            session_id=new_ulid(),
            adapter_id="fake",
            thread_ref=thread_ref,
            role=role,
            started_at=_TS,
        )

    async def dispatch(self, handle: SessionHandle, event: Any) -> None:
        return None

    async def halt(self, handle: SessionHandle) -> None:
        return None


class _StubMcp:
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return {"messages": []}


@pytest.mark.anyio
async def test_add_watch_spawns_session() -> None:
    dispatcher = _FakeDispatcher()
    watcher = ChatroomWatcher(_StubMcp(), dispatcher, watches=[])  # type: ignore[arg-type]
    ref = ThreadRef(project_id="p", thread_id="T-pr-review-1", chatroom_uri="mc://t")
    await watcher.add_watch(WatchSpec(thread_ref=ref, role=Role.NAYSAYER), baseline=False)
    assert dispatcher.spawned == [(ref, Role.NAYSAYER)]
    # idempotent: adding the same watch again does not spawn twice.
    await watcher.add_watch(WatchSpec(thread_ref=ref, role=Role.NAYSAYER), baseline=False)
    assert len(dispatcher.spawned) == 1
