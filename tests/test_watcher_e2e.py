"""End-to-end tests for the Phase 0 watcher.

Stitches together the real watchdog observer, the real dispatcher, and
a fake SDK invoker. The point is to catch wiring regressions — the
unit tests cover each component in isolation, these tests verify they
compose. The Claude SDK and Phanthand are mocked because exercising
either in CI is out of scope.

Feature 2 sub-PR 4 adds 4 full-lifecycle scenarios (decide:
``T-subPR4-integration-test-design`` msg-099):

- (i) Permanent → terminated/validation-failed → operator → resolved → archived
- (ii) Retry exhaustion → terminated/retry-exhausted → operator → resolved → archived
- (iii) Mix startup_scan: terminal-skipped + active-requeued + retrying-requeued
- (iv) Operator-edited transition の watcher restart 反映

No new source-code changes for sub-PR 4 — the implementation under test
all landed in sub-PR 1/2/3; these scenarios verify the composed
end-to-end behavior.
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
from factories import seed_thread_meta, write_message_file

from spirrow_mindwire.claude_code import InvokeResult, InvokeTimeoutError
from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.phanthand import PhanthandClient
from spirrow_mindwire.schema import Participant, ThreadMeta, ThreadStatus
from spirrow_mindwire.watcher.dedup import DedupCache
from spirrow_mindwire.watcher.dispatcher import ThreadDispatcher
from spirrow_mindwire.watcher.events import ThreadEvent
from spirrow_mindwire.watcher.observer import WatcherObserver
from spirrow_mindwire.watcher.startup_scan import startup_full_scan

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ULID_B = "01ARZ3NDEKTSV4RRFFQ69G5FBZ"
ULID_C = "01ARZ3NDEKTSV4RRFFQ69G5FCA"
EVENT_TIMEOUT = 5.0


def _read_meta(layout: ThreadDirLayout) -> ThreadMeta:
    return ThreadMeta.model_validate(yaml.safe_load(layout.meta_path.read_text(encoding="utf-8")))


def _operator_edit_status(
    layout: ThreadDirLayout,
    *,
    new_status: ThreadStatus,
    new_awaiting_from: Participant | None,
) -> None:
    """Simulate §3.6 operator manual transition by editing meta.yaml directly.

    Phase 0 has no CLI / MCP write API for these transitions — operators
    use ``yq`` / an editor. This helper mirrors that flow: load, mutate
    the two fields the operator typically touches, write back. Other
    fields (``terminated_reason`` / ``terminated_at`` / ``retry_count``)
    are preserved as audit trail, matching docs §3.4.
    """
    meta_dict = yaml.safe_load(layout.meta_path.read_text(encoding="utf-8"))
    assert isinstance(meta_dict, dict), "meta.yaml must parse to a dict"
    meta_dict["status"] = new_status
    meta_dict["awaiting_from"] = new_awaiting_from
    layout.meta_path.write_text(
        yaml.safe_dump(meta_dict, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _seed_thread(base: Path) -> ThreadDirLayout:
    layout = ThreadDirLayout(base_dir=base, thread_id=ULID_A)
    seed_thread_meta(layout)
    layout.messages_dir.mkdir(parents=True, exist_ok=True)
    return layout


def _write_message_atomically(
    layout: ThreadDirLayout, seq: int, sender: Participant, body: str
) -> None:
    write_message_file(layout, seq, sender, body, atomic=True)


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
        # Simulate mcp__mindwire__write_reply landing the reply file
        # (PR #40 review: dispatcher's Phase1-Obs1 gate requires the
        # claude-code message at next_seq=3 on disk, not just SDK success).
        write_message_file(layout, 3, "claude-code", "fake reply", atomic=True)
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

    # Event log carries the full start/end pair plus the Phase1-Obs1
    # awaiting_from toggle (success path SOT for awaiting_from).
    log_lines = layout.event_log_path.read_text(encoding="utf-8").splitlines()
    types = [json.loads(line)["type"] for line in log_lines]
    assert types == [
        "claude_code.invoke.start",
        "claude_code.invoke.end",
        "thread.awaiting_from.changed",
    ]
    end_event = json.loads(log_lines[1])
    assert end_event["duration_ms"] == 42
    assert end_event["exit_code"] == 0


@pytest.mark.anyio
async def test_lifecycle_permanent_to_terminated_then_operator_resolves_and_archives(
    tmp_path: Path,
) -> None:
    """Scenario (i) — full lifecycle through a permanent failure.

    active → invoke (permanent error) → terminated/validation-failed
        → operator edit → resolved → restart skip
        → operator edit → archived → restart skip.

    Verifies sub-PR 3 ``_handle_permanent_failure`` path composes with
    sub-PR 1 startup_scan terminal-skip and §3.6 operator manual
    transitions. ``terminated_reason`` and ``terminated_at`` survive the
    terminal-out audit trail across two operator transitions.
    """
    layout = _seed_thread(tmp_path)
    _write_message_atomically(layout, 1, "claude.ai", "trigger")

    invoker_calls: list[dict[str, Any]] = []

    async def permanent_failing_invoker(**kwargs: Any) -> InvokeResult:
        invoker_calls.append(kwargs)
        # Non-allowlist exception ⇒ ``_is_transient(exc) is False`` ⇒
        # safe-by-default permanent → terminated/validation-failed.
        raise ValueError("simulated permanent failure (non-allowlist exception)")

    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=permanent_failing_invoker,
    )
    observer = WatcherObserver(threads_root=tmp_path / "threads", queue=queue, loop=loop)
    observer.start()
    try:
        await asyncio.sleep(0.1)  # let the observer settle
        _write_message_atomically(layout, 2, "claude.ai", "second")
        event = await asyncio.wait_for(queue.get(), timeout=EVENT_TIMEOUT)
        await dispatcher.handle(event)
    finally:
        observer.stop()

    # After invoke: thread terminated with validation-failed.
    assert len(invoker_calls) == 1
    meta_after_invoke = _read_meta(layout)
    assert meta_after_invoke.status == "terminated"
    assert meta_after_invoke.terminated_reason == "validation-failed"
    assert meta_after_invoke.terminated_at is not None
    terminated_at_initial = meta_after_invoke.terminated_at

    # Operator: terminated → resolved.
    _operator_edit_status(layout, new_status="resolved", new_awaiting_from=None)

    # Watcher restart: startup_scan skips resolved (TERMINAL_STATES guard).
    restart_queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    assert startup_full_scan(tmp_path, restart_queue) == 0
    assert restart_queue.empty()

    meta_after_resolve = _read_meta(layout)
    assert meta_after_resolve.status == "resolved"
    # Audit trail preserved across terminal-out transition.
    assert meta_after_resolve.terminated_reason == "validation-failed"
    assert meta_after_resolve.terminated_at == terminated_at_initial

    # Operator: resolved → archived.
    _operator_edit_status(layout, new_status="archived", new_awaiting_from=None)

    # Watcher restart again: archived is terminal.
    restart_queue_2: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    assert startup_full_scan(tmp_path, restart_queue_2) == 0
    assert restart_queue_2.empty()

    meta_after_archive = _read_meta(layout)
    assert meta_after_archive.status == "archived"
    # Audit trail still preserved through resolved → archived.
    assert meta_after_archive.terminated_reason == "validation-failed"
    assert meta_after_archive.terminated_at == terminated_at_initial


@pytest.mark.anyio
async def test_lifecycle_retry_exhaustion_to_terminated_then_operator_path(
    tmp_path: Path,
) -> None:
    """Scenario (ii) — full lifecycle through retry exhaustion.

    active → invoke transient x (max_retries + 1) → terminated/retry-exhausted
        → operator → resolved → restart skip
        → operator → archived → restart skip.

    Verifies sub-PR 3 retry loop + ``_handle_transient_failure``
    exhaustion path composes with sub-PR 1 startup_scan. ``retry_count``
    is preserved through both operator transitions as the audit trail
    framing (D-7 (b), docs §3.5).
    """
    layout = _seed_thread(tmp_path)
    _write_message_atomically(layout, 1, "claude.ai", "trigger")

    invoker_calls: list[dict[str, Any]] = []

    async def always_timeout_invoker(**kwargs: Any) -> InvokeResult:
        invoker_calls.append(kwargs)
        raise InvokeTimeoutError("idle", 0.1)

    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=always_timeout_invoker,
        # ``len(retry_backoff_seconds) == max_retries`` samples the boundary
        # edge of the sub-PR 3 invariant enforced by
        # ``WatcherConfig._retry_backoff_length_covers_max_retries``.
        max_retries=2,
        retry_backoff_seconds=(0.0, 0.0),
        retry_jitter=0.0,
    )
    observer = WatcherObserver(threads_root=tmp_path / "threads", queue=queue, loop=loop)
    observer.start()
    try:
        await asyncio.sleep(0.1)
        _write_message_atomically(layout, 2, "claude.ai", "second")
        event = await asyncio.wait_for(queue.get(), timeout=EVENT_TIMEOUT)
        await dispatcher.handle(event)
    finally:
        observer.stop()

    # Retry loop ran the full ``max_retries + 1`` attempts.
    assert len(invoker_calls) == 3
    meta_after_invoke = _read_meta(layout)
    assert meta_after_invoke.status == "terminated"
    assert meta_after_invoke.terminated_reason == "retry-exhausted"
    # 2 bumps (attempt 0 + attempt 1). attempt 2 = direct exhaust, no bump.
    assert meta_after_invoke.retry_count == 2
    assert meta_after_invoke.terminated_at is not None
    terminated_at_initial = meta_after_invoke.terminated_at

    # Operator: terminated → resolved.
    _operator_edit_status(layout, new_status="resolved", new_awaiting_from=None)
    restart_queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    assert startup_full_scan(tmp_path, restart_queue) == 0

    meta_after_resolve = _read_meta(layout)
    assert meta_after_resolve.status == "resolved"
    assert meta_after_resolve.terminated_reason == "retry-exhausted"
    assert meta_after_resolve.terminated_at == terminated_at_initial
    assert meta_after_resolve.retry_count == 2  # preserved through terminal-out

    # Operator: resolved → archived.
    _operator_edit_status(layout, new_status="archived", new_awaiting_from=None)
    restart_queue_2: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    assert startup_full_scan(tmp_path, restart_queue_2) == 0

    meta_after_archive = _read_meta(layout)
    assert meta_after_archive.status == "archived"
    assert meta_after_archive.terminated_reason == "retry-exhausted"
    assert meta_after_archive.terminated_at == terminated_at_initial
    assert meta_after_archive.retry_count == 2  # preserved end-to-end


@pytest.mark.anyio
async def test_lifecycle_mix_startup_scan_terminal_skipped_active_and_retrying_requeued(
    tmp_path: Path,
) -> None:
    """Scenario (iii) — multi-thread startup_full_scan + dispatch composition.

    Seeds three threads (A active, B retrying with retry_count=2, C
    terminated), runs startup_full_scan, then dispatches the enqueued
    events with a success invoker. Verifies:

    - C (terminated) is never enqueued (sub-PR 1 TERMINAL_STATES guard).
    - A (active) invokes normally and stays active.
    - B (retrying-on-entry) invokes successfully and recovers via
      ``_recover_retrying_to_active`` (= retrying → active, retry_count
      preserved as audit trail).
    """
    layout_a = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    seed_thread_meta(layout_a, status="active", awaiting_from="claude-code", retry_count=0)
    write_message_file(layout_a, 1, "claude.ai", "msg A", atomic=False)

    layout_b = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_B)
    seed_thread_meta(layout_b, status="retrying", awaiting_from="claude-code", retry_count=2)
    write_message_file(layout_b, 1, "claude.ai", "msg B", atomic=False)

    layout_c = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_C)
    seed_thread_meta(
        layout_c,
        status="terminated",
        awaiting_from=None,
        terminated_reason="validation-failed",
        terminated_at="2026-05-07T08:43:07Z",
        retry_count=0,
    )
    write_message_file(layout_c, 1, "claude.ai", "msg C", atomic=False)

    # Startup full scan: active (A) + retrying (B) enqueued, terminated (C) skipped.
    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    enqueued_count = startup_full_scan(tmp_path, queue)
    assert enqueued_count == 2

    # Drain queue with a success invoker.
    invoked_thread_ids: list[str] = []

    async def success_invoker(**kwargs: Any) -> InvokeResult:
        invoked_thread_ids.append(kwargs["cwd"].name)
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
        invoker=success_invoker,
    )
    while not queue.empty():
        event = queue.get_nowait()
        await dispatcher.handle(event)

    # Both A and B were dispatched (queue order is non-deterministic, so
    # sort before comparing).
    assert sorted(invoked_thread_ids) == sorted([ULID_A, ULID_B])

    # A: active → invoke success → still active (no transition needed).
    meta_a = _read_meta(layout_a)
    assert meta_a.status == "active"
    assert meta_a.retry_count == 0

    # B: retrying → invoke success → retrying → active recovery
    # (retry_count preserved at 2 as D-7 (b) audit trail).
    meta_b = _read_meta(layout_b)
    assert meta_b.status == "active"
    assert meta_b.retry_count == 2

    # C: terminated → never invoked → unchanged.
    meta_c = _read_meta(layout_c)
    assert meta_c.status == "terminated"
    assert meta_c.terminated_reason == "validation-failed"
    assert ULID_C not in invoked_thread_ids
    # No event log written for C — startup_scan skipped before any append.
    assert not layout_c.event_log_path.exists()


@pytest.mark.anyio
async def test_lifecycle_operator_edit_active_to_resolved_pre_invoke(tmp_path: Path) -> None:
    """Scenario (iv) — operator transitions active → resolved before any invoke.

    Simulates the §3.6 recommended flow: operator stops the watcher,
    edits meta.yaml directly, restarts. After restart, ``startup_full_scan``
    skips the now-resolved thread; the watcher never invokes. The
    transition is enforced at meta.yaml read time (no race because no
    watcher is running during the edit).
    """
    layout = _seed_thread(tmp_path)
    write_message_file(layout, 1, "claude.ai", "msg", atomic=False)

    # Sanity: pre-edit state is active.
    assert _read_meta(layout).status == "active"

    # Operator edits: active → resolved (skipping terminated since the
    # operator decided no investigation needed — `_ALLOWED_TRANSITIONS`
    # permits active → resolved per §3.3 docs).
    _operator_edit_status(layout, new_status="resolved", new_awaiting_from=None)

    # Watcher "restart": startup_full_scan skips the now-resolved thread.
    # An empty queue is the proof of "no invoke" — no dispatcher run needed.
    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    assert startup_full_scan(tmp_path, queue) == 0
    assert queue.empty()

    meta_after = _read_meta(layout)
    assert meta_after.status == "resolved"
