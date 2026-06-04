"""Tests for the PR-review orchestrator (WIRING_ALLOWLIST_SPEC §A.2 → ADR-19 driver-化).

A fake :class:`McpToolCaller` + a fake :class:`NaysayerPrReviewDriver` exercise thread creation,
number derivation, and that ``fire_pr_review`` **drives the driver directly** (parses the PR ref,
posts the critique to the thread, returns the outcome). ``require_ci_success`` (the L2 merge gate)
and ``ChatroomWatcher.add_watch`` (the standing role watches) are tested directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from spirrow_mindwire.github.client import CiState, CiStatus, PrRef, ReviewEvent
from spirrow_mindwire.magickit.watcher import ChatroomWatcher, WatchSpec
from spirrow_mindwire.naysayer.pr_review import PostCritique, PrReviewOutcome
from spirrow_mindwire.orchestrator import (
    MergeBlockedError,
    PrReviewOrchestrator,
    require_ci_success,
)
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


class _FakeDriver:
    """Records the reviewed PR + posts a critique via the callback; returns a fixed outcome."""

    def __init__(self, outcome: PrReviewOutcome | None = None) -> None:
        self.reviewed: list[PrRef] = []
        self._outcome = outcome or PrReviewOutcome(
            verdict=ReviewEvent.APPROVE,
            body="LGTM\n\nVERDICT: APPROVE",
            ci_state=CiState.SUCCESS,
            head_sha="sha1",
        )

    async def review(self, pr: PrRef, *, post_critique: PostCritique) -> PrReviewOutcome:
        self.reviewed.append(pr)
        await post_critique(self._outcome.body)
        return self._outcome


@pytest.mark.anyio
async def test_fire_pr_review_opens_thread_with_explicit_number() -> None:
    mcp = _FakeMcp()
    driver = _FakeDriver()
    orch = PrReviewOrchestrator(mcp, driver=driver)  # type: ignore[arg-type]
    ref, outcome = await orch.fire_pr_review(
        project="spirrow-mindwire", pr_ref="org/repo#5", number=3
    )
    assert ref.thread_id == "T-pr-review-3"
    assert ref.project_id == "spirrow-mindwire"
    args = mcp.args_for("chatroom_open_thread")
    assert args["thread_id"] == "T-pr-review-3"
    assert args["owner"] == "orchestrator"
    assert "org/repo#5" in args["propose_content"]
    assert "pr-review" in args["tags"]
    # The driver was driven with the parsed PR ref, and its critique was posted to the thread.
    assert driver.reviewed == [PrRef("org", "repo", 5)]
    post = mcp.args_for("chatroom_post_message")
    assert post["thread_id"] == "T-pr-review-3"
    assert post["content"] == outcome.body
    assert post["author"] == "naysayer-pr-review"


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
    orch = PrReviewOrchestrator(mcp, driver=_FakeDriver())  # type: ignore[arg-type]
    ref, _outcome = await orch.fire_pr_review(project="p", pr_ref="o/r#1")
    assert ref.thread_id == "T-pr-review-5"


@pytest.mark.anyio
async def test_fire_pr_review_first_number_when_none_exist() -> None:
    mcp = _FakeMcp({"chatroom_list_threads": {"items": []}})
    orch = PrReviewOrchestrator(mcp, driver=_FakeDriver())  # type: ignore[arg-type]
    ref, _outcome = await orch.fire_pr_review(project="p", pr_ref="o/r#1")
    assert ref.thread_id == "T-pr-review-1"


@pytest.mark.anyio
async def test_fire_pr_review_returns_driver_outcome() -> None:
    outcome = PrReviewOutcome(
        verdict=ReviewEvent.REQUEST_CHANGES,
        body="bug\n\nVERDICT: REQUEST_CHANGES",
        ci_state=CiState.SUCCESS,
        head_sha="sha9",
    )
    orch = PrReviewOrchestrator(_FakeMcp(), driver=_FakeDriver(outcome))  # type: ignore[arg-type]
    _ref, got = await orch.fire_pr_review(project="p", pr_ref="o/r#7", number=1)
    assert got is outcome


@pytest.mark.anyio
async def test_fire_pr_review_unparseable_ref_raises() -> None:
    orch = PrReviewOrchestrator(_FakeMcp(), driver=_FakeDriver())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unparseable PR ref"):
        await orch.fire_pr_review(project="p", pr_ref="not a pr ref", number=1)


class _RaisingDriver:
    """A driver whose review fails before any critique (transient remote error)."""

    async def review(self, pr: PrRef, *, post_critique: PostCritique) -> PrReviewOutcome:
        raise RuntimeError("transient github/lexora error")


@pytest.mark.anyio
async def test_fire_pr_review_does_not_leak_thread_on_review_error() -> None:
    # Tier B msg-453: the thread is opened lazily inside post_critique, so a review that raises
    # before producing a critique must NOT leave an abandoned empty T-pr-review-<n> behind.
    mcp = _FakeMcp()
    orch = PrReviewOrchestrator(mcp, driver=_RaisingDriver())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await orch.fire_pr_review(project="p", pr_ref="o/r#1", number=1)
    assert all(name != "chatroom_open_thread" for name, _ in mcp.calls)
    assert all(name != "chatroom_post_message" for name, _ in mcp.calls)


@pytest.mark.anyio
async def test_fire_pr_review_opens_thread_then_posts_on_success() -> None:
    # On a successful review the thread is opened (with the request) and the critique posted —
    # open precedes post (lazy-open happens inside the single post_critique call).
    mcp = _FakeMcp()
    orch = PrReviewOrchestrator(mcp, driver=_FakeDriver())  # type: ignore[arg-type]
    await orch.fire_pr_review(project="p", pr_ref="o/r#1", number=1)
    names = [name for name, _ in mcp.calls]
    assert names == ["chatroom_open_thread", "chatroom_post_message"]


# ---------- L2 merge gate: require_ci_success (ADR-2026-06-03-16 D-3) ------ #


class _FakeGitHubCi:
    """Minimal GitHubReviewClient stub returning a fixed CI status."""

    def __init__(self, ci: CiStatus) -> None:
        self._ci = ci

    async def fetch_pr_diff(self, pr: PrRef) -> str:
        raise NotImplementedError

    async def fetch_ci_status(self, pr: PrRef) -> CiStatus:
        return self._ci

    async def submit_review(self, pr: PrRef, *, event: ReviewEvent, body: str) -> dict[str, Any]:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


@pytest.mark.anyio
async def test_require_ci_success_returns_status_when_green() -> None:
    gh = _FakeGitHubCi(CiStatus(CiState.SUCCESS, "sha1", []))
    status = await require_ci_success(gh, PrRef("o", "r", 1))
    assert status.state is CiState.SUCCESS


@pytest.mark.anyio
@pytest.mark.parametrize(
    "ci",
    [
        CiStatus(CiState.FAILURE, "sha1", ["test"]),
        CiStatus(CiState.PENDING, "sha1", []),
        CiStatus(CiState.UNKNOWN, None, []),
    ],
)
async def test_require_ci_success_blocks_when_not_green(ci: CiStatus) -> None:
    # L2 is the deterministic merge gate — it must block on anything but SUCCESS, independently of
    # any naysayer APPROVE (fail-closed).
    gh = _FakeGitHubCi(ci)
    with pytest.raises(MergeBlockedError):
        await require_ci_success(gh, PrRef("o", "r", 1))


# ---------- ChatroomWatcher.add_watch (standing role watches) ------------- #


class _FakeDispatcher:
    def __init__(self) -> None:
        self.spawned: list[tuple[ThreadRef, Role]] = []
        self.dispatched: list[Any] = []

    async def spawn_instance(
        self, thread_ref: ThreadRef, role: Role, instance_id: str
    ) -> SessionHandle:
        self.spawned.append((thread_ref, role))
        return SessionHandle(
            session_id=new_ulid(),
            instance_id=instance_id,
            adapter_id="fake",
            thread_ref=thread_ref,
            role=role,
            started_at=_TS,
        )

    async def dispatch(self, handle: SessionHandle, event: Any) -> None:
        self.dispatched.append(event)

    async def halt(self, handle: SessionHandle) -> None:
        return None


class _StubMcp:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self._messages = messages or []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return {"messages": self._messages}


@pytest.mark.anyio
async def test_add_watch_spawns_and_registers_for_polling() -> None:
    dispatcher = _FakeDispatcher()
    watcher = ChatroomWatcher(_StubMcp(), dispatcher, watches=[])  # type: ignore[arg-type]
    ref = ThreadRef(project_id="p", thread_id="T-x-1", chatroom_uri="mc://t")
    spec = WatchSpec(thread_ref=ref, role=Role.PROPOSER)
    await watcher.add_watch(spec, baseline=False)
    assert dispatcher.spawned == [(ref, Role.PROPOSER)]
    # C1 regression guard: the watch must be registered for polling, not only spawned —
    # otherwise poll_once() (which iterates _watches) never polls it.
    assert spec in watcher._watches
    # idempotent: adding the same watch again does not spawn or register twice.
    await watcher.add_watch(spec, baseline=False)
    assert len(dispatcher.spawned) == 1
    assert watcher._watches.count(spec) == 1


@pytest.mark.anyio
async def test_add_watch_then_poll_dispatches() -> None:
    dispatcher = _FakeDispatcher()
    mcp = _StubMcp([{"msg_id": "m1", "author": "proposer", "content": "hello", "timestamp": ""}])
    watcher = ChatroomWatcher(mcp, dispatcher, watches=[])  # type: ignore[arg-type]
    ref = ThreadRef(project_id="p", thread_id="T-x-1", chatroom_uri="mc://t")
    await watcher.add_watch(WatchSpec(thread_ref=ref, role=Role.PROPOSER), baseline=False)
    dispatched = await watcher.poll_once()
    assert dispatched == 1
    assert len(dispatcher.dispatched) == 1
