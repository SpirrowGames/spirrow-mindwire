"""``mindwire-ledger``: the implementer's two Magickit tools (get / append-only notes).

Thread ``T-silent-stops-need-a-generic-watchdog-and-a-loud-stand-down``: Bohr msg-5296 §5 (tests),
msg-5300 (strategy 3 tests: lock serialises, event carries the prior notes + sha256; every error
has an event), msg-5884 / msg-5886 (the recovery record: one file per append, written before
Magickit, ``journal_unavailable`` / ``invalid_identifier``). The exact-tool-set pin on the
production session lives in ``tests/test_role_tool_surface.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from spirrow_mindwire.adapters.implementer import ImplementerSdkAdapter
from spirrow_mindwire.claude_code.tools.ledger_server import (
    LEDGER_TOOL_NAMES,
    build_ledger_tools,
)
from spirrow_mindwire.loop_runner import _LEDGER_LOG_FIELDS, _log_event_sink
from spirrow_mindwire.magickit.client import MagickitMcpError
from spirrow_mindwire.magickit.ledger_notes import (
    EVENT_KIND_LEDGER_NOTE_APPENDED,
    EVENT_KIND_LEDGER_NOTE_FAILED,
    LedgerError,
    LedgerFailure,
    LedgerHead,
    LedgerJournal,
    LedgerNotes,
    MonotonicUlids,
    TaskLocks,
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

_NOW = datetime(2026, 10, 2, 3, 4, 5, tzinfo=UTC)
_PROJECT = "spirrow-mindwire"
_THREAD = "T-ledger"


@pytest.fixture
def journal(tmp_path: Path) -> LedgerJournal:
    return LedgerJournal(tmp_path / "ledger")


def _records(journal: LedgerJournal, task_id: str = "T42") -> list[dict[str, Any]]:
    """Every recovery record for ``task_id``, in file-name (= event_id) order."""
    directory = journal.root / _PROJECT / task_id
    if not directory.exists():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))
    ]


class _FakeMagickit:
    """``get_task`` / ``update_task`` over an in-memory notes store, recording every call.

    ``yield_between`` makes the read and the write of one append separate scheduling points, so
    two concurrent appends would interleave if nothing serialised them.
    """

    def __init__(self, notes: dict[str, str] | None = None, *, yield_between: bool = False) -> None:
        self.notes = dict(notes or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.yield_between = yield_between
        self.fail_get: BaseException | None = None
        self.fail_update: BaseException | None = None
        self.update_result: Any = None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, dict(arguments)))
        if self.yield_between:
            await asyncio.sleep(0)
        if name == "get_task":
            if self.fail_get is not None:
                raise self.fail_get
            task_id = arguments["task_id"]
            if task_id not in self.notes:
                return {"success": False, "task": None, "phase": "", "project": _PROJECT}
            return {
                "success": True,
                "task": {"task_id": task_id, "name": "n", "notes": self.notes[task_id]},
                "phase": "Phase 2",
                "project": arguments["project"],
            }
        if name == "update_task":
            if self.fail_update is not None:
                raise self.fail_update
            if self.update_result is not None:
                return self.update_result
            self.notes[arguments["task_id"]] = arguments["description"]
            return {"success": True}
        raise AssertionError(f"ledger called a tool it must never call: {name}")


def _notes(
    mcp: _FakeMagickit,
    events: list[Event],
    journal: LedgerJournal,
    *,
    head: str = "msg-1",
    locks: TaskLocks | None = None,
) -> LedgerNotes:
    async def sink(event: Event) -> None:
        events.append(event)

    return LedgerNotes(
        mcp=mcp,
        project_id=_PROJECT,
        thread_id=_THREAD,
        on_event=sink,
        journal=journal,
        head=LedgerHead(msg_id=head),
        locks=locks if locks is not None else TaskLocks(),
        now=lambda: _NOW,
    )


_HEADER = f"[mindwire {_THREAD} msg-1 2026-10-02T03:04:05Z]"


# --- append keeps what is there ------------------------------------------------------------- #


@pytest.mark.anyio
async def test_append_keeps_existing_notes_and_adds_a_provenance_header(
    journal: LedgerJournal,
) -> None:
    mcp = _FakeMagickit({"T42": "## 目的\nexisting"})
    events: list[Event] = []
    result = await _notes(mcp, events, journal).append_note("T42", "語彙: delivery.stalled")

    assert mcp.notes["T42"] == f"## 目的\nexisting\n\n{_HEADER}\n語彙: delivery.stalled"
    assert result["header"] == _HEADER
    # The write goes to the phase Magickit resolved, in the pinned project, through notes.
    name, args = mcp.calls[-1]
    assert name == "update_task"
    assert args == {
        "task_id": "T42",
        "phase": "Phase 2",
        "description": mcp.notes["T42"],
        "project": _PROJECT,
    }


@pytest.mark.anyio
async def test_append_to_empty_notes_has_no_leading_blank_line(journal: LedgerJournal) -> None:
    mcp = _FakeMagickit({"T44": ""})
    await _notes(mcp, [], journal).append_note("T44", "x")
    assert mcp.notes["T44"] == f"{_HEADER}\nx"


@pytest.mark.anyio
async def test_every_magickit_call_is_pinned_to_the_thread_project(journal: LedgerJournal) -> None:
    mcp = _FakeMagickit({"T42": ""})
    notes = _notes(mcp, [], journal)
    await notes.get_task("T42")
    await notes.append_note("T42", "x")
    assert {args["project"] for _, args in mcp.calls} == {_PROJECT}
    assert {name for name, _ in mcp.calls} == {"get_task", "update_task"}


def test_the_tools_take_no_project_argument(journal: LedgerJournal) -> None:
    tools = {t.name: t for t in build_ledger_tools(_notes(_FakeMagickit(), [], journal))}
    assert set(tools) == LEDGER_TOOL_NAMES == {"ledger_get_task", "ledger_append_note"}
    get_schema: Any = tools["ledger_get_task"].input_schema
    append_schema: Any = tools["ledger_append_note"].input_schema
    assert set(get_schema) == {"task_id"}
    assert set(append_schema) == {"task_id", "text"}


# --- strategy 3: (a) lock, (d) event --------------------------------------------------------- #


@pytest.mark.anyio
async def test_concurrent_appends_to_one_task_are_serialised_and_both_survive(
    journal: LedgerJournal,
) -> None:
    mcp = _FakeMagickit({"T42": "base"}, yield_between=True)
    locks = TaskLocks()
    events: list[Event] = []
    a = _notes(mcp, events, journal, head="msg-a", locks=locks)
    b = _notes(mcp, events, journal, head="msg-b", locks=locks)

    await asyncio.gather(a.append_note("T42", "from-a"), b.append_note("T42", "from-b"))

    assert "from-a" in mcp.notes["T42"]
    assert "from-b" in mcp.notes["T42"]
    assert mcp.notes["T42"].startswith("base\n\n")
    # Serialised: read, write, read, write — never read, read.
    assert [name for name, _ in mcp.calls] == ["get_task", "update_task"] * 2
    assert len(locks) == 0  # evicted once the last holder left


@pytest.mark.anyio
async def test_task_lock_is_evicted_only_after_the_last_waiter_leaves() -> None:
    """PR #409 advisory: no per-task lock outlives its users, and eviction never splits a queue."""
    locks = TaskLocks()
    order: list[str] = []
    release_first = asyncio.Event()

    async def first() -> None:
        async with locks.hold("p", "T42"):
            order.append("first-in")
            await release_first.wait()
            order.append("first-out")

    async def later(name: str) -> None:
        async with locks.hold("p", "T42"):
            order.append(f"{name}-in")
            await asyncio.sleep(0)
            order.append(f"{name}-out")

    t1 = asyncio.create_task(first())
    await asyncio.sleep(0)
    t2 = asyncio.create_task(later("second"))
    t3 = asyncio.create_task(later("third"))
    await asyncio.sleep(0)
    assert len(locks) == 1
    release_first.set()
    await asyncio.gather(t1, t2, t3)

    assert order == ["first-in", "first-out", "second-in", "second-out", "third-in", "third-out"]
    assert len(locks) == 0


@pytest.mark.anyio
async def test_task_lock_is_evicted_when_the_holder_raises() -> None:
    locks = TaskLocks()
    with pytest.raises(RuntimeError):
        async with locks.hold("p", "T42"):
            raise RuntimeError("boom")
    assert len(locks) == 0


@pytest.mark.anyio
async def test_without_the_shared_lock_the_race_is_real(journal: LedgerJournal) -> None:
    """Control for the test above: the fake does interleave, so the lock is what saved it."""
    mcp = _FakeMagickit({"T42": "base"}, yield_between=True)
    a = _notes(mcp, [], journal, locks=TaskLocks())
    b = _notes(mcp, [], journal, locks=TaskLocks())
    await asyncio.gather(a.append_note("T42", "from-a"), b.append_note("T42", "from-b"))
    assert ("from-a" in mcp.notes["T42"]) != ("from-b" in mcp.notes["T42"])


@pytest.mark.anyio
async def test_appended_event_carries_prior_notes_their_sha256_and_the_text(
    journal: LedgerJournal,
) -> None:
    prior = "## 目的\nexisting"
    mcp = _FakeMagickit({"T42": prior})
    events: list[Event] = []
    await _notes(mcp, events, journal).append_note("T42", "added")

    assert [e.kind for e in events] == [EVENT_KIND_LEDGER_NOTE_APPENDED]
    fields = events[0].fields
    assert fields["prior_notes"] == prior
    assert fields["prior_notes_sha256"] == hashlib.sha256(prior.encode("utf-8")).hexdigest()
    assert fields["appended_text"] == "added"
    assert fields["header"] == _HEADER
    assert fields["task_id"] == "T42"
    assert fields["project_id"] == _PROJECT
    assert fields["thread_id"] == _THREAD
    assert fields["appended_bytes"] == len(f"{_HEADER}\nadded".encode())


# --- nothing fails silently ------------------------------------------------------------------ #


def _setup_failure(case: str) -> tuple[_FakeMagickit, str, str, LedgerFailure]:
    mcp = _FakeMagickit({"T42": "x"})
    task, text = "T42", "t"
    if case == "get_transport":
        mcp.fail_get = MagickitMcpError("connect refused")
        reason = LedgerFailure.MAGICKIT_UNREACHABLE
    elif case == "update_transport":
        mcp.fail_update = MagickitMcpError("connect refused")
        reason = LedgerFailure.MAGICKIT_UNREACHABLE
    elif case == "not_found":
        task = "T9999"
        reason = LedgerFailure.TASK_NOT_FOUND
    elif case == "rejected":
        mcp.update_result = {"success": False, "message": "no"}
        reason = LedgerFailure.WRITE_REJECTED
    elif case == "empty_task":
        task = " "
        reason = LedgerFailure.INVALID_ARGUMENT
    elif case == "empty_text":
        text = ""
        reason = LedgerFailure.INVALID_ARGUMENT
    elif case == "unexpected":
        mcp.fail_update = ValueError("bug")
        reason = LedgerFailure.UNEXPECTED_ERROR
    else:  # pragma: no cover
        raise AssertionError(case)
    return mcp, task, text, reason


_FAILURES = (
    "get_transport",
    "update_transport",
    "not_found",
    "rejected",
    "empty_task",
    "empty_text",
    "unexpected",
)


@pytest.mark.anyio
@pytest.mark.parametrize("case", _FAILURES)
async def test_every_append_error_returns_is_error_and_emits_note_failed(
    case: str, journal: LedgerJournal
) -> None:
    mcp, task, text, reason = _setup_failure(case)
    events: list[Event] = []
    tools = {t.name: t for t in build_ledger_tools(_notes(mcp, events, journal))}

    result = await tools["ledger_append_note"].handler({"task_id": task, "text": text})

    assert result.get("isError") is True
    assert reason.value in result["content"][0]["text"]
    assert [e.kind for e in events] == [EVENT_KIND_LEDGER_NOTE_FAILED]
    assert events[0].fields["reason"] == reason.value
    assert events[0].fields["operation"] == "append"
    assert mcp.notes["T42"] == "x"  # nothing was written


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["get_transport", "not_found", "empty_task"])
async def test_every_get_error_returns_is_error_and_emits_note_failed(
    case: str, journal: LedgerJournal
) -> None:
    mcp, task, _, reason = _setup_failure(case)
    events: list[Event] = []
    tools = {t.name: t for t in build_ledger_tools(_notes(mcp, events, journal))}

    result = await tools["ledger_get_task"].handler({"task_id": task})

    assert result.get("isError") is True
    assert [e.kind for e in events] == [EVENT_KIND_LEDGER_NOTE_FAILED]
    assert events[0].fields["reason"] == reason.value
    assert events[0].fields["operation"] == "get"


@pytest.mark.anyio
async def test_get_tool_returns_the_task_and_emits_nothing(journal: LedgerJournal) -> None:
    mcp = _FakeMagickit({"T42": "notes here"})
    events: list[Event] = []
    tools = {t.name: t for t in build_ledger_tools(_notes(mcp, events, journal))}
    result = await tools["ledger_get_task"].handler({"task_id": "T42"})
    assert "isError" not in result
    payload = json.loads(result["content"][0]["text"])
    assert payload["task"]["notes"] == "notes here"
    assert payload["project"] == _PROJECT
    assert events == []


@pytest.mark.anyio
async def test_a_raising_event_sink_does_not_change_the_result(journal: LedgerJournal) -> None:
    mcp = _FakeMagickit({"T42": ""})

    async def sink(_event: Event) -> None:
        raise RuntimeError("log down")

    notes = LedgerNotes(
        mcp=mcp, project_id=_PROJECT, thread_id=_THREAD, on_event=sink, journal=journal
    )
    await notes.append_note("T42", "x")
    assert "x" in mcp.notes["T42"]
    with pytest.raises(LedgerError) as info:
        await notes.append_note("T9999", "x")
    assert info.value.reason is LedgerFailure.TASK_NOT_FOUND


# --- adapter wiring: project from the ThreadRef, head from the delivered message ------------- #


def _sdk_factory(capture: list[Any]) -> Callable[[Any], Any]:
    class _Client:
        def __init__(self, options: Any) -> None:
            self.options = options

        async def connect(self) -> None:
            return None

        async def query(self, prompt: str) -> None:
            return None

        async def receive_response(self) -> AsyncIterator[Any]:
            yield AssistantMessage(content=[TextBlock(text="ok")], model="m")
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s",
                stop_reason="end_turn",
                result="ok",
            )

        async def interrupt(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    def make(options: Any) -> _Client:
        capture.append(options)
        return _Client(options)

    return make


@pytest.mark.anyio
async def test_implementer_session_ledger_uses_thread_project_and_delivered_head(
    tmp_path: Path,
) -> None:
    mcp = _FakeMagickit({"T42": ""})
    captured: list[Any] = []
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=load_manifest(),
        inference_base_url="http://lx",
        client_factory=_sdk_factory(captured),
        ledger_mcp=mcp,
        ledger_locks=TaskLocks(),
        ledger_journal=LedgerJournal(tmp_path / "ledger"),
    )
    events: list[Event] = []

    async def on_reply(_draft: ReplyDraft) -> None:
        return None

    async def on_event(event: Event) -> None:
        events.append(event)

    ref = ThreadRef(project_id="proj-x", thread_id="T-x", chatroom_uri="mc://x")
    ctx = SpawnContext(
        on_reply=on_reply,
        on_event_log=on_event,
        own_role=Role.IMPLEMENTER,
        own_instance_id="implementer-1",
    )
    handle = await adapter.spawn(ref, Role.IMPLEMENTER, ctx)
    await adapter.deliver_event(
        handle,
        ChatroomEvent(
            event_id="e1",
            event_type=EventType.NEW_MESSAGE,
            thread_ref=ref,
            occurred_at=_NOW,
            payload=NewMessagePayload(
                msg_id="msg-77", author="human", body="go", parent_msg_id=None
            ),
        ),
    )

    server = captured[0].mcp_servers["mindwire-ledger"]
    assert server["type"] == "sdk"
    # Call the tool the session would call, through the server the session was given.
    from mcp.types import CallToolRequest, CallToolRequestParams

    handler = server["instance"].request_handlers[CallToolRequest]
    await handler(
        CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="ledger_append_note", arguments={"task_id": "T42", "text": "hello"}
            ),
        )
    )
    assert {args["project"] for _, args in mcp.calls} == {"proj-x"}
    assert mcp.notes["T42"].startswith("[mindwire T-x msg-77 ")
    assert [e.kind for e in events if e.kind.startswith("ledger.")] == [
        EVENT_KIND_LEDGER_NOTE_APPENDED
    ]
    assert len(list((tmp_path / "ledger" / "proj-x" / "T42").glob("*.json"))) == 1


def test_an_implementer_without_a_magickit_client_attaches_no_server(tmp_path: Path) -> None:
    adapter = ImplementerSdkAdapter(cwd=tmp_path, obligations=load_manifest())
    assert adapter._make_options().mcp_servers == {}


def test_a_magickit_client_without_a_journal_is_refused(tmp_path: Path) -> None:
    """No ledger without its recovery record (msg-5884): the wiring error is loud at build time."""
    with pytest.raises(ValueError, match="ledger_journal"):
        ImplementerSdkAdapter(cwd=tmp_path, obligations=load_manifest(), ledger_mcp=_FakeMagickit())


# --- the recovery record (Bohr msg-5884 / msg-5886) ------------------------------------------- #


@pytest.mark.anyio
async def test_an_append_writes_one_record_with_the_event_id_of_its_event(
    journal: LedgerJournal,
) -> None:
    prior = "## 目的\nexisting"
    mcp = _FakeMagickit({"T42": prior})
    events: list[Event] = []
    result = await _notes(mcp, events, journal).append_note("T42", "added")

    [record] = _records(journal)
    [event] = events
    assert record == {
        "event_id": event.event_id,
        "project_id": _PROJECT,
        "thread_id": _THREAD,
        "task_id": "T42",
        "header": _HEADER,
        "appended_text": "added",
        "prior_notes": prior,
        "prior_notes_sha256": hashlib.sha256(prior.encode("utf-8")).hexdigest(),
    }
    assert result["event_id"] == event.event_id
    assert (journal.root / _PROJECT / "T42" / f"{event.event_id}.json").is_file()
    assert event.fields["prior_notes_sha256"] == record["prior_notes_sha256"]


@pytest.mark.anyio
async def test_the_record_is_written_before_magickit_is(journal: LedgerJournal) -> None:
    mcp = _FakeMagickit({"T42": "x"})
    seen_at_update: list[int] = []
    original = mcp.call_tool

    async def spy(name: str, arguments: dict[str, Any]) -> Any:
        if name == "update_task":
            seen_at_update.append(len(_records(journal)))
        return await original(name, arguments)

    mcp.call_tool = spy  # type: ignore[method-assign]
    await _notes(mcp, [], journal).append_note("T42", "y")
    assert seen_at_update == [1]


@pytest.mark.anyio
async def test_the_record_is_written_off_the_event_loop_thread(
    journal: LedgerJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fsync latency must not stall the loop (PR #439 gate advisory): the write runs in a worker."""
    import threading

    loop_thread = threading.get_ident()
    write_threads: list[int] = []
    original = LedgerJournal.write

    def spy(self: LedgerJournal, record: dict[str, Any]) -> Path:
        write_threads.append(threading.get_ident())
        return original(self, record)

    monkeypatch.setattr(LedgerJournal, "write", spy)
    await _notes(_FakeMagickit({"T42": "x"}), [], journal).append_note("T42", "y")
    assert len(write_threads) == 1
    assert write_threads[0] != loop_thread
    assert len(_records(journal)) == 1


@pytest.mark.anyio
async def test_an_unwritable_record_means_no_append_and_no_tmp_left(
    journal: LedgerJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The I7 exception: no recovery record, no Magickit write; the failure is loud."""

    def broken_fsync(_fd: int) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr("spirrow_mindwire.filesystem.atomic.os.fsync", broken_fsync)
    mcp = _FakeMagickit({"T42": "x"})
    events: list[Event] = []
    tools = {t.name: t for t in build_ledger_tools(_notes(mcp, events, journal))}

    result = await tools["ledger_append_note"].handler({"task_id": "T42", "text": "y"})

    assert result.get("isError") is True
    assert LedgerFailure.JOURNAL_UNAVAILABLE.value in result["content"][0]["text"]
    assert [name for name, _ in mcp.calls] == ["get_task"]  # update_task never called
    assert mcp.notes["T42"] == "x"
    assert [e.kind for e in events] == [EVENT_KIND_LEDGER_NOTE_FAILED]
    assert events[0].fields["reason"] == LedgerFailure.JOURNAL_UNAVAILABLE.value
    assert "journal_event_id" not in events[0].fields
    leftovers = list(journal.root.rglob("*")) if journal.root.exists() else []
    assert [p for p in leftovers if p.is_file()] == []  # neither .json nor .json.tmp


@pytest.mark.anyio
async def test_a_record_whose_magickit_write_failed_is_named_in_the_failure(
    journal: LedgerJournal,
) -> None:
    mcp = _FakeMagickit({"T42": "x"})
    mcp.update_result = {"success": False}
    events: list[Event] = []
    with pytest.raises(LedgerError):
        await _notes(mcp, events, journal).append_note("T42", "y")
    [record] = _records(journal)
    assert events[0].fields["reason"] == LedgerFailure.WRITE_REJECTED.value
    assert events[0].fields["journal_event_id"] == record["event_id"]


@pytest.mark.anyio
@pytest.mark.parametrize("task_id", ["../x", "T42/../../y", "a b", "C:x", "T42.json"])
async def test_a_path_unsafe_task_id_is_refused_before_anything_is_touched(
    journal: LedgerJournal, tmp_path: Path, task_id: str
) -> None:
    mcp = _FakeMagickit({"T42": "x"})
    events: list[Event] = []
    tools = {t.name: t for t in build_ledger_tools(_notes(mcp, events, journal))}

    result = await tools["ledger_append_note"].handler({"task_id": task_id, "text": "y"})

    assert result.get("isError") is True
    assert [e.fields["reason"] for e in events] == [LedgerFailure.INVALID_IDENTIFIER.value]
    assert mcp.calls == []
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


@pytest.mark.anyio
async def test_a_path_unsafe_project_id_is_refused(journal: LedgerJournal) -> None:
    mcp = _FakeMagickit({"T42": "x"})

    async def sink(_event: Event) -> None:
        return None

    notes = LedgerNotes(
        mcp=mcp, project_id="../evil", thread_id=_THREAD, on_event=sink, journal=journal
    )
    with pytest.raises(LedgerError) as info:
        await notes.append_note("T42", "y")
    assert info.value.reason is LedgerFailure.INVALID_IDENTIFIER
    assert mcp.calls == []


@pytest.mark.anyio
async def test_concurrent_appends_leave_two_whole_records_in_write_order(
    journal: LedgerJournal,
) -> None:
    mcp = _FakeMagickit({"T42": "base"}, yield_between=True)
    locks = TaskLocks()
    events: list[Event] = []
    a = _notes(mcp, events, journal, head="msg-a", locks=locks)
    b = _notes(mcp, events, journal, head="msg-b", locks=locks)

    await asyncio.gather(a.append_note("T42", "from-a"), b.append_note("T42", "from-b"))

    records = _records(journal)  # each parsed as JSON; sorted by file name = event_id
    assert [r["appended_text"] for r in records] == ["from-a", "from-b"]
    # The order of the records is the order the writes landed in Magickit.
    final = mcp.notes["T42"]
    assert final.index("from-a") < final.index("from-b")
    # Each record holds the notes its own write replaced, so each is recoverable.
    assert records[0]["prior_notes"] == "base"
    assert "from-a" in records[1]["prior_notes"]


class _SecondCallerFirstLocks(TaskLocks):
    """Admits the second caller before the first, as a lock may when acquisition interleaves.

    The first caller to ask waits until the second has left its critical section; only then does
    it take the real lock. That is the ordering PR #439's gate showed can happen.
    """

    def __init__(self) -> None:
        super().__init__()
        self._callers = 0
        self._second_done = asyncio.Event()

    @asynccontextmanager
    async def hold(self, project_id: str, task_id: str) -> AsyncIterator[None]:
        self._callers += 1
        first = self._callers == 1
        if first:
            await self._second_done.wait()
        try:
            async with super().hold(project_id, task_id):
                yield
        finally:
            if not first:
                self._second_done.set()


@pytest.mark.anyio
async def test_record_order_follows_lock_order_not_call_order(journal: LedgerJournal) -> None:
    """PR #439 gate (REQUEST_CHANGES): the event id is issued under the lock, not before it."""
    mcp = _FakeMagickit({"T42": "base"}, yield_between=True)
    locks = _SecondCallerFirstLocks()
    events: list[Event] = []
    a = _notes(mcp, events, journal, head="msg-a", locks=locks)
    b = _notes(mcp, events, journal, head="msg-b", locks=locks)

    # a asks first, b is admitted first.
    await asyncio.gather(a.append_note("T42", "from-a"), b.append_note("T42", "from-b"))

    final = mcp.notes["T42"]
    assert final.index("from-b") < final.index("from-a")  # b really wrote first
    records = _records(journal)  # sorted by file name = event_id
    assert [r["appended_text"] for r in records] == ["from-b", "from-a"]
    # No temporal inversion: the later record's prior notes hold the earlier record's text.
    assert records[0]["prior_notes"] == "base"
    assert "from-b" in records[1]["prior_notes"]


def test_monotonic_ulids_strictly_increase_within_one_millisecond() -> None:
    ids = MonotonicUlids()
    issued = [ids() for _ in range(2000)]
    assert issued == sorted(issued)
    assert len(set(issued)) == len(issued)


# --- the log line carries the keys that find the record --------------------------------------- #


@pytest.mark.anyio
async def test_the_log_line_for_a_ledger_event_carries_its_matching_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sha = "ab" * 32
    appended = Event(
        event_id="01J00000000000000000000000",
        occurred_at=_NOW,
        kind=EVENT_KIND_LEDGER_NOTE_APPENDED,
        fields={
            "project_id": "spirrow-mindwire",
            "task_id": "T42",
            "prior_notes_sha256": sha,
            "prior_notes": "SECRET-BODY",
            "appended_text": "SECRET-TEXT",
        },
    )
    failed = Event(
        event_id="01J00000000000000000000001",
        occurred_at=_NOW,
        kind=EVENT_KIND_LEDGER_NOTE_FAILED,
        fields={
            "project_id": "spirrow-mindwire",
            "task_id": "T44",
            "reason": "journal_unavailable",
            "appended_text": "SECRET-TEXT",
        },
    )
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.loop_runner"):
        await _log_event_sink(appended)
        await _log_event_sink(failed)

    appended_line, failed_line = [r.getMessage() for r in caplog.records]
    # Exact lines, not substrings: each key appears once (project_id is rendered by the fixed
    # format, and the extras loop is an allow-list that does not contain it), and nothing
    # outside the allow-list -- in particular never the notes themselves -- reaches the line.
    assert appended_line == (
        f"loop event {EVENT_KIND_LEDGER_NOTE_APPENDED} event_id=01J00000000000000000000000"
        f" project_id=spirrow-mindwire task_id=T42 prior_notes_sha256={sha}"
    )
    assert failed_line == (
        f"loop event {EVENT_KIND_LEDGER_NOTE_FAILED} event_id=01J00000000000000000000001"
        " project_id=spirrow-mindwire task_id=T44 reason=journal_unavailable"
    )
    for line in (appended_line, failed_line):
        assert line.count("project_id=") == 1
        assert "SECRET" not in line


@pytest.mark.anyio
async def test_a_ledger_event_without_its_keys_logs_a_placeholder(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bare = Event(
        event_id="01J00000000000000000000002",
        occurred_at=_NOW,
        kind=EVENT_KIND_LEDGER_NOTE_FAILED,
        fields={},
    )
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.loop_runner"):
        await _log_event_sink(bare)

    (line,) = [r.getMessage() for r in caplog.records]
    assert line == (
        f"loop event {EVENT_KIND_LEDGER_NOTE_FAILED} event_id=01J00000000000000000000002"
        " project_id=? task_id=?"
    )


def test_the_extras_allow_list_never_repeats_a_key_the_fixed_format_renders() -> None:
    # project_id and task_id are rendered by the fixed format; listing either in the extras
    # allow-list too would print it twice on every ledger line.
    assert not {"event_id", "project_id", "task_id"} & set(_LEDGER_LOG_FIELDS)
