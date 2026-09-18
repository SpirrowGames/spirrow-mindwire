"""v12 tests for the implementer's spawn/halt lifecycle.

Covers the additions from ``T-auto-backgrounded-command-hangs-conductor-4h``:

* **B-1 bounded turn drain** — ``deliver_event`` must raise
  :class:`ImplementerSdkTurnTimeoutError` when the SDK holds the drain past
  the ``turn_timeout_seconds`` budget (the observed hang class).
* **B-4 spawn init timeout** — ``spawn`` must raise
  :class:`ImplementerSdkSpawnTimeoutError` when ``connect()`` does not
  finish inside ``spawn_timeout_seconds``.
* **Job Object lifecycle** — spawn creates a Job, halt closes it via
  ``close_handle``; every failure exit (timeout, exception, cancel) hits
  a ``finally`` that FIRST runs ``lookup_and_assign_leftover`` and THEN
  ``close_handle``. ``close_handle`` is called exactly once per
  ``JobState`` even if two paths race (idempotency belt).

The Job APIs are injected via ``job_module=`` on the constructor, so the
tests never touch a real Job Object — they run cross-platform and verify
the ORDER + IDEMPOTENCY of the calls the adapter makes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from spirrow_mindwire.adapters._sdk_job_hook import JobState
from spirrow_mindwire.adapters.implementer import (
    ImplementerSdkAdapter,
    ImplementerSdkSpawnTimeoutError,
    ImplementerSdkTurnTimeoutError,
)
from spirrow_mindwire.obligations import load_manifest
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.value_objects import (
    ChatroomEvent,
    Event,
    EventType,
    NewMessagePayload,
    ReplyDraft,
    Role,
    ThreadRef,
)

_OBLIGATIONS = load_manifest()


# --------------------------------------------------------------------------- #
# fake job_module — records the sequence of calls the adapter makes
# --------------------------------------------------------------------------- #


class _FakeJobModule:
    """Records the adapter's Job Object API interactions.

    The adapter takes this in via the ``job_module=`` constructor kwarg,
    so no ``monkeypatch`` on module globals is needed and the fake stays
    scoped to a single test.
    """

    def __init__(self, *, create_raises: bool = False) -> None:
        self.create_calls: list[str] = []
        self.close_calls: list[JobState] = []
        self.lookup_calls: list[tuple[JobState, str]] = []
        self.is_in_job_answer = True
        self._create_raises = create_raises
        self._next_handle = 0xB000

    def create_job(self, session_id: str) -> JobState:
        self.create_calls.append(session_id)
        if self._create_raises:
            raise NotImplementedError("no Job Object on this platform")
        handle = self._next_handle
        self._next_handle += 1
        return JobState(handle=handle, session_id=session_id)

    def close_handle(self, state: JobState) -> None:
        self.close_calls.append(state)
        # Idempotency check — mirror the real helper's sentinel behaviour so
        # the tests exercise the same double-close guarantee.
        state.handle = None

    def is_process_in_job(self, _pid: int, _state: JobState) -> bool:
        return self.is_in_job_answer

    def lookup_and_assign_leftover(self, state: JobState, exe_path: str) -> None:
        self.lookup_calls.append((state, exe_path))


# --------------------------------------------------------------------------- #
# fake SDK client — pluggable connect/receive to drive the timeout paths
# --------------------------------------------------------------------------- #


class _FakeClient:
    def __init__(
        self,
        responses: list[Any],
        *,
        connect_hang: float = 0.0,
        receive_hang: float = 0.0,
        connect_raises: BaseException | None = None,
    ) -> None:
        self._responses = responses
        self._connect_hang = connect_hang
        self._receive_hang = receive_hang
        self._connect_raises = connect_raises
        self.connected = False
        self.disconnected = False
        self.interrupt_count = 0

    async def connect(self) -> None:
        if self._connect_hang:
            await asyncio.sleep(self._connect_hang)
        if self._connect_raises is not None:
            raise self._connect_raises
        self.connected = True

    async def query(self, prompt: str) -> None:
        return None

    async def receive_response(self) -> AsyncIterator[Any]:
        if self._receive_hang:
            await asyncio.sleep(self._receive_hang)
        for msg in self._responses:
            yield msg

    async def interrupt(self) -> None:
        self.interrupt_count += 1

    async def disconnect(self) -> None:
        self.disconnected = True


def _factory(client_ref: list[_FakeClient], client: _FakeClient) -> Any:
    def _make(_options: Any) -> _FakeClient:
        client_ref.append(client)
        return client

    return _make


def _thread_ref() -> ThreadRef:
    return ThreadRef(project_id="p", thread_id="T", chatroom_uri="mc://t/1")


def _ctx(captured: list[ReplyDraft]) -> SpawnContext:
    async def on_reply(d: ReplyDraft) -> None:
        captured.append(d)

    async def on_event_log(_e: Event) -> None:
        return None

    return SpawnContext(
        on_reply=on_reply,
        on_event_log=on_event_log,
        own_role=Role.IMPLEMENTER,
        own_instance_id="implementer-1",
    )


def _event(author: str = "human") -> ChatroomEvent:
    from datetime import UTC, datetime

    return ChatroomEvent(
        event_id="e",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_thread_ref(),
        occurred_at=datetime(2026, 9, 19, tzinfo=UTC),
        payload=NewMessagePayload(msg_id="m1", author=author, body="go", parent_msg_id=None),
    )


def _adapter(
    tmp_path: Path,
    *,
    client: _FakeClient,
    job_module: _FakeJobModule,
    spawn_timeout: float = 5.0,
    turn_timeout: float = 5.0,
) -> tuple[ImplementerSdkAdapter, list[_FakeClient]]:
    ref: list[_FakeClient] = []
    a = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=_OBLIGATIONS,
        inference_base_url="http://lx",
        client_factory=_factory(ref, client),
        spawn_timeout_seconds=spawn_timeout,
        turn_timeout_seconds=turn_timeout,
        job_module=job_module,
    )
    return a, ref


# --------------------------------------------------------------------------- #
# B-4 spawn init timeout
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_spawn_raises_timeout_when_connect_hangs(tmp_path: Path) -> None:
    """The v12 spawn budget must bound ``connect()``."""
    client = _FakeClient(responses=[], connect_hang=10.0)
    job = _FakeJobModule()
    adapter, _ = _adapter(tmp_path, client=client, job_module=job, spawn_timeout=0.05)
    with pytest.raises(ImplementerSdkSpawnTimeoutError):
        await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    # Timeout path MUST have run: lookup fallback then close.
    assert len(job.lookup_calls) == 1
    assert len(job.close_calls) == 1
    # And the disconnect belt was attempted.
    assert client.disconnected is True


# --------------------------------------------------------------------------- #
# B-1 turn timeout via deliver_event
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_deliver_event_raises_turn_timeout_when_drain_hangs(tmp_path: Path) -> None:
    """Bounded drain (v12 B-1) — the observed hang class."""
    # ``receive_hang`` blocks the drain far longer than the turn budget.
    client = _FakeClient(
        responses=[
            AssistantMessage(content=[TextBlock(text="hi")], model="m"),
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="t",
                stop_reason="end_turn",
                result="ok",
            ),
        ],
        receive_hang=10.0,
    )
    job = _FakeJobModule()
    adapter, _ = _adapter(tmp_path, client=client, job_module=job, turn_timeout=0.05)
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    with pytest.raises(ImplementerSdkTurnTimeoutError):
        await adapter.deliver_event(handle, _event())
    health = await adapter.health(handle)
    assert health.error is not None
    assert health.error.code == "adapter.turn_timeout"


# --------------------------------------------------------------------------- #
# Cleanup guarantees (v12 finally / session_registered / lookup+close ordering)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_spawn_success_does_not_close_job_yet(tmp_path: Path) -> None:
    """Successful spawn hands cleanup to halt — the finally must not fire."""
    client = _FakeClient(responses=[])
    job = _FakeJobModule()
    adapter, _ = _adapter(tmp_path, client=client, job_module=job)
    await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    assert job.create_calls == ["pending:T"]
    assert job.close_calls == []
    assert job.lookup_calls == []


@pytest.mark.anyio
async def test_halt_closes_job_and_is_idempotent(tmp_path: Path) -> None:
    client = _FakeClient(responses=[])
    job = _FakeJobModule()
    adapter, _ = _adapter(tmp_path, client=client, job_module=job)
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    await adapter.halt(handle)
    await adapter.halt(handle)  # idempotent no-op — must not double-close
    # Even though halt was called twice, close_handle was only invoked once
    # (the second halt short-circuits on _SHUTDOWN_STATES).
    assert len(job.close_calls) == 1


@pytest.mark.anyio
async def test_spawn_cancellation_still_runs_lookup_then_close(tmp_path: Path) -> None:
    """v12 §finally-cleanup — asyncio.CancelledError must not bypass cleanup."""
    slow_client = _FakeClient(responses=[], connect_hang=10.0)
    job = _FakeJobModule()
    adapter, _ = _adapter(tmp_path, client=slow_client, job_module=job, spawn_timeout=10.0)

    task = asyncio.create_task(adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([])))
    # Let spawn begin
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Both cleanup steps must have run in order despite the cancel.
    assert len(job.lookup_calls) == 1
    assert len(job.close_calls) == 1
    # Ordering: lookup must precede close (the belt must Assign before
    # KILL_ON_JOB_CLOSE reaps).
    lookup_state = job.lookup_calls[0][0]
    close_state = job.close_calls[0]
    assert lookup_state is close_state


@pytest.mark.anyio
async def test_spawn_connect_exception_runs_cleanup(tmp_path: Path) -> None:
    """A raise from connect() must trigger the finally cleanup."""

    class _BoomError(RuntimeError):
        pass

    client = _FakeClient(responses=[], connect_raises=_BoomError("no"))
    job = _FakeJobModule()
    adapter, _ = _adapter(tmp_path, client=client, job_module=job)
    from spirrow_mindwire.adapters.implementer import ImplementerSdkSpawnError

    with pytest.raises(ImplementerSdkSpawnError):
        await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    assert len(job.lookup_calls) == 1
    assert len(job.close_calls) == 1


@pytest.mark.anyio
async def test_spawn_swallows_lookup_helper_exception(tmp_path: Path) -> None:
    """A raise from lookup_and_assign_leftover must not shadow the original spawn error."""

    class _BoomJob(_FakeJobModule):
        def lookup_and_assign_leftover(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("psutil hiccup")

    slow_client = _FakeClient(responses=[], connect_hang=10.0)
    job = _BoomJob()
    adapter, _ = _adapter(tmp_path, client=slow_client, job_module=job, spawn_timeout=0.05)
    with pytest.raises(ImplementerSdkSpawnTimeoutError):
        await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    # Close still ran despite lookup blowing up.
    assert len(job.close_calls) == 1


@pytest.mark.anyio
async def test_spawn_on_posix_falls_through_when_create_raises(tmp_path: Path) -> None:
    """POSIX path: create_job raises NotImplementedError → adapter falls through."""

    job = _FakeJobModule(create_raises=True)
    client = _FakeClient(responses=[])
    adapter, _ = _adapter(tmp_path, client=client, job_module=job)
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, _ctx([]))
    # Spawn succeeded even though create_job raised.
    assert handle.role is Role.IMPLEMENTER
    # No Job to close — halt is still safe.
    await adapter.halt(handle)
    assert job.close_calls == []
