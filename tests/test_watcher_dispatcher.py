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
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest
import yaml
from factories import seed_thread_meta, write_message_file

from spirrow_mindwire.claude_code import InvokeResult, InvokeTimeoutError
from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.phanthand import PhanthandClient
from spirrow_mindwire.schema import Participant, ThreadMeta
from spirrow_mindwire.watcher.dedup import DedupCache
from spirrow_mindwire.watcher.dispatcher import ThreadDispatcher
from spirrow_mindwire.watcher.events import ThreadEvent

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
NOW = datetime(2026, 5, 7, 8, 43, 7, tzinfo=UTC)


def _seed_thread(base: Path, sender: Participant = "claude.ai", seq: int = 1) -> ThreadDirLayout:
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


def _write_message(
    layout: ThreadDirLayout, seq: int, sender: Participant, body: str = "hello"
) -> None:
    write_message_file(layout, seq, sender, body, atomic=False)


def _invoker(captured: dict[str, Any], result: InvokeResult) -> Any:
    async def fake(**kwargs: Any) -> InvokeResult:
        captured.update(kwargs)
        return result

    return fake


def _timeout_invoker(
    kind: Literal["idle", "absolute"] = "idle", elapsed_seconds: float = 0.5
) -> Any:
    """Fake invoker that raises :class:`InvokeTimeoutError`.

    Used by the timeout-handling tests (Feature 2 sub-PR 2 step 4) to drive
    the dispatcher's ``except InvokeTimeoutError`` branch without spinning up
    a real SDK session or relying on real-time sleep.
    """

    async def fake(**kwargs: Any) -> InvokeResult:
        raise InvokeTimeoutError(kind, elapsed_seconds)

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
    """Non-transient exceptions terminate the thread (safe-by-default permanent).

    Feature 2 sub-PR 3 reframes 「invoke raises」 — instead of propagating the
    exception up to ``run_watcher._safe_handle``, the dispatcher classifies
    the exception via :func:`_is_transient` (allowlist = :class:`InvokeTimeoutError`),
    and any non-transient exception goes directly to
    ``terminated/validation-failed``. The traceback is captured in the
    watcher log (logger.warning + exc_info=True) instead of being re-raised.
    """
    layout = _seed_thread(tmp_path)

    async def failing_invoker(**kwargs: Any) -> InvokeResult:
        raise RuntimeError("SDK boom")

    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=failing_invoker,
    )

    # No raise: dispatcher swallows and terminates.
    await dispatcher.handle(_event())

    log_lines = layout.event_log_path.read_text(encoding="utf-8").splitlines()
    types = [json.loads(line)["type"] for line in log_lines]
    assert types == [
        "claude_code.invoke.start",
        "claude_code.invoke.end",
        "thread.status.changed",
    ]
    end = json.loads(log_lines[1])
    assert end["exit_code"] == 1
    status_changed = json.loads(log_lines[2])
    assert status_changed["from_status"] == "active"
    assert status_changed["to_status"] == "terminated"

    new_meta = ThreadMeta.model_validate(
        yaml.safe_load(layout.meta_path.read_text(encoding="utf-8"))
    )
    assert new_meta.status == "terminated"
    assert new_meta.terminated_reason == "validation-failed"


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


# ----- Feature 2 sub-PR 2 + sub-PR 3: timeout / retry handling -------------
#
# These tests adapt the original sub-PR 2 "transition only" assertions to
# the sub-PR 3 retry loop. ``max_retries=0`` is the default in
# ``ThreadDispatcher.__init__`` (=  single transient failure goes straight
# to ``terminated/retry-exhausted`` without sleeping), so unit tests that
# don't exercise the retry loop itself can rely on the default. The full
# retry-success / retry-exhausted / bump scenarios live in commit 5.


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["idle", "absolute"])
async def test_dispatcher_timeout_exhausts_to_terminated_with_max_retries_zero(
    tmp_path: Path, kind: Literal["idle", "absolute"]
) -> None:
    """``max_retries=0`` + idle / absolute timeout → terminated/retry-exhausted."""
    layout = _seed_thread(tmp_path)

    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_timeout_invoker(kind, elapsed_seconds=0.5),
    )

    await dispatcher.handle(_event())

    new_meta = ThreadMeta.model_validate(
        yaml.safe_load(layout.meta_path.read_text(encoding="utf-8"))
    )
    assert new_meta.status == "terminated"
    assert new_meta.terminated_reason == "retry-exhausted"
    # retry_count not bumped on direct exhaustion (attempt==max_retries=0).
    assert new_meta.retry_count == 0
    assert new_meta.awaiting_from is None

    log_lines = layout.event_log_path.read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 3
    start = json.loads(log_lines[0])
    end = json.loads(log_lines[1])
    status_changed = json.loads(log_lines[2])
    assert start["type"] == "claude_code.invoke.start"
    assert end["type"] == "claude_code.invoke.end"
    assert end["exit_code"] == 1
    assert end["duration_ms"] == 500  # 0.5s * 1000
    assert status_changed["type"] == "thread.status.changed"
    assert status_changed["from_status"] == "active"
    assert status_changed["to_status"] == "terminated"


@pytest.mark.anyio
async def test_dispatcher_timeout_preserves_retry_count_on_direct_exhaustion(
    tmp_path: Path,
) -> None:
    """既存 retry_count=2 + ``max_retries=0`` で timeout → status terminated、
    retry_count は bump せず 2 のまま (= attempt==max_retries=0 で direct path)."""
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    seed_thread_meta(layout, status="active", awaiting_from="claude-code", retry_count=2)
    write_message_file(layout, 1, "claude.ai", atomic=False)

    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_timeout_invoker("idle", elapsed_seconds=1.0),
    )

    await dispatcher.handle(_event())

    new_meta = ThreadMeta.model_validate(
        yaml.safe_load(layout.meta_path.read_text(encoding="utf-8"))
    )
    assert new_meta.status == "terminated"
    assert new_meta.terminated_reason == "retry-exhausted"
    assert new_meta.retry_count == 2  # unchanged (no bump on direct exhaustion)


@pytest.mark.anyio
async def test_dispatcher_timeout_from_retrying_terminates_directly_with_max_retries_zero(
    tmp_path: Path,
) -> None:
    """retrying status の thread で timeout + ``max_retries=0`` →
    ``retrying → terminated`` direct transition、 retry_count は preserve."""
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    seed_thread_meta(layout, status="retrying", awaiting_from="claude-code", retry_count=1)
    write_message_file(layout, 1, "claude.ai", atomic=False)

    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_timeout_invoker("idle", elapsed_seconds=0.5),
    )

    await dispatcher.handle(_event())

    new_meta = ThreadMeta.model_validate(
        yaml.safe_load(layout.meta_path.read_text(encoding="utf-8"))
    )
    assert new_meta.status == "terminated"
    assert new_meta.terminated_reason == "retry-exhausted"
    assert new_meta.retry_count == 1  # preserved

    log_lines = layout.event_log_path.read_text(encoding="utf-8").splitlines()
    types = [json.loads(line)["type"] for line in log_lines]
    assert types == [
        "claude_code.invoke.start",
        "claude_code.invoke.end",
        "thread.status.changed",
    ]
    status_changed = json.loads(log_lines[-1])
    assert status_changed["from_status"] == "retrying"
    assert status_changed["to_status"] == "terminated"


@pytest.mark.anyio
async def test_dispatcher_timeout_dedups_subsequent_event_within_ttl(
    tmp_path: Path,
) -> None:
    """timeout 直後 (= terminated state with max_retries=0) の同 (thread_id, seq)
    event は ``TERMINAL_STATES`` short-circuit でも DedupCache でも skip される.

    The early dedup short-circuit fires first (DedupCache.seen_recently is
    checked in ``handle`` before ``_run_thread``); the terminal-state
    short-circuit inside ``_run_thread`` is a defense in depth for events
    that bypass dedup (e.g. across watcher restarts).
    """
    layout = _seed_thread(tmp_path)
    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_timeout_invoker(),
    )

    # 1 回目: timeout fire → terminated (max_retries=0 default).
    await dispatcher.handle(_event(seq=1, when=NOW))
    meta_after_first = ThreadMeta.model_validate(
        yaml.safe_load(layout.meta_path.read_text(encoding="utf-8"))
    )
    assert meta_after_first.status == "terminated"

    # 2 回目: 同じ (thread_id, seq) は dedup_ttl 内なら DedupCache で skip。
    # invoker / event log とも変化なし。
    log_size_before = layout.event_log_path.stat().st_size
    await dispatcher.handle(_event(seq=1, when=NOW + timedelta(seconds=2)))
    log_size_after = layout.event_log_path.stat().st_size
    assert log_size_after == log_size_before  # 再呼出されない


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "extra"),
    [
        (
            "terminated",
            {
                "terminated_reason": "retry-exhausted",
                "terminated_at": "2026-05-07T08:43:07Z",
            },
        ),
        ("resolved", {}),
        ("archived", {}),
    ],
)
async def test_dispatcher_skips_terminal_state(
    tmp_path: Path, status: str, extra: dict[str, Any]
) -> None:
    """Terminal-state threads must not auto-revive on live observer events.

    Complements ``test_startup_scan.py::test_terminal_states_are_skipped``
    (which covers the startup-scan path). This test exercises the
    dispatcher's own ``meta.status in TERMINAL_STATES`` guard for events
    that bypass startup_scan (e.g. operator wrote a message file after
    terminating the thread, or the live observer got a delayed event).
    """
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    seed_thread_meta(layout, status=status, awaiting_from=None, **extra)
    write_message_file(layout, 1, "claude.ai", atomic=False)

    captured: dict[str, Any] = {}
    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_invoker(captured, _ok_result()),
    )

    await dispatcher.handle(_event())

    # Invoker NOT called: skipped due to terminal status.
    assert captured == {}
    # No event log written either (early return is before any log.append).
    assert not layout.event_log_path.exists()


# ----- Feature 2 sub-PR 3: retry loop --------------------------------------


def _flaky_timeout_invoker(
    failures: int,
    success_result: InvokeResult,
    kind: Literal["idle", "absolute"] = "idle",
    elapsed_seconds: float = 0.1,
    call_counter: list[int] | None = None,
) -> Any:
    """Invoker that times out *failures* times, then returns *success_result*.

    ``call_counter`` (if provided) is appended-to on each call so tests can
    introspect how many attempts were made.
    """
    state = {"count": 0}

    async def fake(**kwargs: Any) -> InvokeResult:
        state["count"] += 1
        if call_counter is not None:
            call_counter.append(state["count"])
        if state["count"] <= failures:
            raise InvokeTimeoutError(kind, elapsed_seconds)
        return success_result

    return fake


def _cancelled_invoker() -> Any:
    async def fake(**kwargs: Any) -> InvokeResult:
        raise asyncio.CancelledError()

    return fake


@pytest.mark.anyio
async def test_dispatcher_retry_succeeds_after_transient_failures(tmp_path: Path) -> None:
    """N transient failures then success → status=active, retry_count preserved.

    Verifies the D-7 (b) preserve semantic: retry_count grows during the
    retry session (1 bump per failure for the active→retrying first
    transition), and stays at that value after the retrying→active
    recovery (= the audit trail of "this thread needed N retries to
    succeed" is persisted).
    """
    layout = _seed_thread(tmp_path)
    call_counter: list[int] = []
    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_flaky_timeout_invoker(
            failures=1, success_result=_ok_result(), call_counter=call_counter
        ),
        max_retries=2,
        retry_backoff_seconds=(0.0, 0.0),
        retry_jitter=0.0,
    )

    await dispatcher.handle(_event())

    # Invoker called twice (= 1 failure + 1 success).
    assert call_counter == [1, 2]

    new_meta = ThreadMeta.model_validate(
        yaml.safe_load(layout.meta_path.read_text(encoding="utf-8"))
    )
    assert new_meta.status == "active"
    assert new_meta.retry_count == 1  # 1 bump during retry, preserved after success
    assert new_meta.awaiting_from == "claude-code"

    log_lines = [
        json.loads(line) for line in layout.event_log_path.read_text(encoding="utf-8").splitlines()
    ]
    types = [entry["type"] for entry in log_lines]
    assert types == [
        "claude_code.invoke.start",
        "claude_code.invoke.end",
        "thread.status.changed",  # active → retrying
        "thread.retry.backoff_started",
        "claude_code.invoke.start",
        "claude_code.invoke.end",
        "thread.status.changed",  # retrying → active (recovery)
    ]
    sc_active_to_retrying = log_lines[2]
    assert sc_active_to_retrying["from_status"] == "active"
    assert sc_active_to_retrying["to_status"] == "retrying"
    assert sc_active_to_retrying["retry_count"] == 1
    assert log_lines[3]["attempt_num"] == 1
    assert log_lines[3]["backoff_seconds"] == 0.0
    sc_recovery = log_lines[6]
    assert sc_recovery["from_status"] == "retrying"
    assert sc_recovery["to_status"] == "active"
    assert sc_recovery["retry_count"] == 1


@pytest.mark.anyio
async def test_dispatcher_retry_exhausts_to_terminated_with_bumps(tmp_path: Path) -> None:
    """All retry attempts fail → terminated/retry-exhausted, retry_count = max bumps.

    With ``max_retries=2`` + ``retry_count=0`` initial: 3 invoke attempts,
    2 bumps (attempt 0 + attempt 1 are non-exhaustion paths), final attempt
    skips bump and goes direct to terminated → retry_count ends at 2.
    """
    layout = _seed_thread(tmp_path)
    call_counter: list[int] = []
    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_flaky_timeout_invoker(
            failures=99,  # always fail
            success_result=_ok_result(),
            call_counter=call_counter,
        ),
        max_retries=2,
        retry_backoff_seconds=(0.0, 0.0),
        retry_jitter=0.0,
    )

    await dispatcher.handle(_event())

    # 3 invocations total (max_retries=2 → 3 attempts).
    assert call_counter == [1, 2, 3]

    new_meta = ThreadMeta.model_validate(
        yaml.safe_load(layout.meta_path.read_text(encoding="utf-8"))
    )
    assert new_meta.status == "terminated"
    assert new_meta.terminated_reason == "retry-exhausted"
    # 2 bumps (attempts 0 and 1); attempt 2 is direct exhaustion without bump.
    assert new_meta.retry_count == 2
    assert new_meta.awaiting_from is None

    log_lines = [
        json.loads(line) for line in layout.event_log_path.read_text(encoding="utf-8").splitlines()
    ]
    types = [entry["type"] for entry in log_lines]
    assert types == [
        "claude_code.invoke.start",
        "claude_code.invoke.end",
        "thread.status.changed",  # active → retrying (bump 1)
        "thread.retry.backoff_started",
        "claude_code.invoke.start",
        "claude_code.invoke.end",
        # bump_retry_count: no ThreadStatusChanged on retrying → retrying
        "thread.retry.backoff_started",
        "claude_code.invoke.start",
        "claude_code.invoke.end",
        "thread.status.changed",  # retrying → terminated (retry-exhausted)
    ]
    backoff_events = [e for e in log_lines if e["type"] == "thread.retry.backoff_started"]
    assert [e["attempt_num"] for e in backoff_events] == [1, 2]
    final_status = log_lines[-1]
    assert final_status["from_status"] == "retrying"
    assert final_status["to_status"] == "terminated"
    assert final_status["retry_count"] == 2


@pytest.mark.anyio
async def test_dispatcher_retry_bypasses_dedup_between_attempts(tmp_path: Path) -> None:
    """D-6 (a): the retry loop calls the invoker multiple times within a single
    ``dispatcher.handle()`` call, regardless of DedupCache state.

    DedupCache gates the *entry* into ``_run_thread`` (= which event spawns
    a retry session) but not the attempts within a session. With
    ``max_retries=2`` and an always-failing invoker, the invoker is hit 3
    times even though the same (thread_id, seq) is being processed.
    """
    _seed_thread(tmp_path)
    call_counter: list[int] = []
    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=60)),  # long TTL irrelevant
        invoker=_flaky_timeout_invoker(
            failures=99, success_result=_ok_result(), call_counter=call_counter
        ),
        max_retries=2,
        retry_backoff_seconds=(0.0, 0.0),
        retry_jitter=0.0,
    )

    await dispatcher.handle(_event())

    assert call_counter == [1, 2, 3], "retry loop must bypass dedup between attempts"


@pytest.mark.anyio
async def test_dispatcher_retry_count_cumulative_from_existing(tmp_path: Path) -> None:
    """retry_count is cumulative across thread lifetime (D-7 (b) audit trail).

    Seed retry_count=2 (= a prior retry session under a hypothetical earlier
    watcher run). New retry session bumps to 2+max_retries (one bump per
    non-exhaust attempt). Persistent counter grows; only ``max_retries`` is
    the per-_run_thread gate.
    """
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    seed_thread_meta(layout, status="active", awaiting_from="claude-code", retry_count=2)
    write_message_file(layout, 1, "claude.ai", atomic=False)

    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_timeout_invoker(),
        max_retries=2,
        retry_backoff_seconds=(0.0, 0.0),
        retry_jitter=0.0,
    )

    await dispatcher.handle(_event())

    new_meta = ThreadMeta.model_validate(
        yaml.safe_load(layout.meta_path.read_text(encoding="utf-8"))
    )
    assert new_meta.status == "terminated"
    assert new_meta.retry_count == 4  # 2 initial + 2 bumps (attempts 0 + 1)


@pytest.mark.anyio
async def test_dispatcher_retry_uses_bump_when_entry_status_is_retrying(
    tmp_path: Path,
) -> None:
    """retrying-on-entry (= startup_full_scan requeue scenario): first
    transient failure uses ``bump_retry_count`` (not ``transition_state``)
    because ``retrying → retrying`` is forbidden by ``_ALLOWED_TRANSITIONS``.

    ``ThreadStatusChanged`` for the first attempt is **not** emitted (status
    unchanged); on retry exhaustion the final ``retrying → terminated``
    is emitted normally.
    """
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    seed_thread_meta(layout, status="retrying", awaiting_from="claude-code", retry_count=1)
    write_message_file(layout, 1, "claude.ai", atomic=False)

    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_timeout_invoker(),
        max_retries=1,
        retry_backoff_seconds=(0.0,),
        retry_jitter=0.0,
    )

    await dispatcher.handle(_event())

    new_meta = ThreadMeta.model_validate(
        yaml.safe_load(layout.meta_path.read_text(encoding="utf-8"))
    )
    assert new_meta.status == "terminated"
    assert new_meta.retry_count == 2  # 1 initial + 1 bump (attempt 0)

    log_lines = [
        json.loads(line) for line in layout.event_log_path.read_text(encoding="utf-8").splitlines()
    ]
    types = [entry["type"] for entry in log_lines]
    # First attempt: bump-only, no ThreadStatusChanged.
    # Second attempt: retry-exhausted, retrying → terminated.
    assert types == [
        "claude_code.invoke.start",
        "claude_code.invoke.end",
        "thread.retry.backoff_started",
        "claude_code.invoke.start",
        "claude_code.invoke.end",
        "thread.status.changed",
    ]
    final_status = log_lines[-1]
    assert final_status["from_status"] == "retrying"
    assert final_status["to_status"] == "terminated"


@pytest.mark.anyio
async def test_dispatcher_propagates_cancelled_error(tmp_path: Path) -> None:
    """``asyncio.CancelledError`` is shutdown signal: propagate, don't retry."""
    layout = _seed_thread(tmp_path)
    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=_cancelled_invoker(),
        max_retries=3,
        retry_backoff_seconds=(0.0, 0.0, 0.0),
        retry_jitter=0.0,
    )

    with pytest.raises(asyncio.CancelledError):
        await dispatcher.handle(_event())

    # Status is unchanged (still active) — we propagated before any transition.
    new_meta = ThreadMeta.model_validate(
        yaml.safe_load(layout.meta_path.read_text(encoding="utf-8"))
    )
    assert new_meta.status == "active"
    assert new_meta.retry_count == 0

    # InvokeEnd was logged before the raise (for traceability), but no retry / transition.
    log_lines = [
        json.loads(line) for line in layout.event_log_path.read_text(encoding="utf-8").splitlines()
    ]
    types = [entry["type"] for entry in log_lines]
    assert types == ["claude_code.invoke.start", "claude_code.invoke.end"]


@pytest.mark.anyio
async def test_dispatcher_permanent_error_terminates_without_retry(tmp_path: Path) -> None:
    """Non-allowlisted exception → terminated/validation-failed, no retry attempts."""
    layout = _seed_thread(tmp_path)
    call_counter: list[int] = []

    async def permanent_failing_invoker(**kwargs: Any) -> InvokeResult:
        call_counter.append(1)
        raise ValueError("malformed thread data — not a retry-worthy error")

    dispatcher = ThreadDispatcher(
        base_dir=tmp_path,
        phanthand_client=AsyncMock(spec=PhanthandClient),
        dedup=DedupCache(ttl=timedelta(seconds=5)),
        invoker=permanent_failing_invoker,
        max_retries=3,
        retry_backoff_seconds=(0.0, 0.0, 0.0),
        retry_jitter=0.0,
    )

    # Permanent error is swallowed (= safe-by-default).
    await dispatcher.handle(_event())

    # Invoker called once only — no retries on permanent errors.
    assert len(call_counter) == 1

    new_meta = ThreadMeta.model_validate(
        yaml.safe_load(layout.meta_path.read_text(encoding="utf-8"))
    )
    assert new_meta.status == "terminated"
    assert new_meta.terminated_reason == "validation-failed"
    assert new_meta.retry_count == 0  # never bumped, went direct to terminated
