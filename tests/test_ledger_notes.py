"""``mindwire-ledger``: the implementer's two Magickit tools (get / append-only notes).

Thread ``T-silent-stops-need-a-generic-watchdog-and-a-loud-stand-down``: Bohr msg-5296 §5 (tests),
msg-5300 (strategy 3 tests: lock serialises, event carries the prior notes + sha256; every error
has an event). The exact-tool-set pin on the production session lives in
``tests/test_role_tool_surface.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
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
from spirrow_mindwire.magickit.client import MagickitMcpError
from spirrow_mindwire.magickit.ledger_notes import (
    EVENT_KIND_LEDGER_NOTE_APPENDED,
    EVENT_KIND_LEDGER_NOTE_FAILED,
    LedgerError,
    LedgerFailure,
    LedgerHead,
    LedgerNotes,
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
    mcp: _FakeMagickit, events: list[Event], *, head: str = "msg-1", locks: TaskLocks | None = None
) -> LedgerNotes:
    async def sink(event: Event) -> None:
        events.append(event)

    return LedgerNotes(
        mcp=mcp,
        project_id=_PROJECT,
        thread_id=_THREAD,
        on_event=sink,
        head=LedgerHead(msg_id=head),
        locks=locks if locks is not None else TaskLocks(),
        now=lambda: _NOW,
    )


_HEADER = f"[mindwire {_THREAD} msg-1 2026-10-02T03:04:05Z]"


# --- append keeps what is there ------------------------------------------------------------- #


@pytest.mark.anyio
async def test_append_keeps_existing_notes_and_adds_a_provenance_header() -> None:
    mcp = _FakeMagickit({"T42": "## 目的\nexisting"})
    events: list[Event] = []
    result = await _notes(mcp, events).append_note("T42", "語彙: delivery.stalled")

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
async def test_append_to_empty_notes_has_no_leading_blank_line() -> None:
    mcp = _FakeMagickit({"T44": ""})
    await _notes(mcp, []).append_note("T44", "x")
    assert mcp.notes["T44"] == f"{_HEADER}\nx"


@pytest.mark.anyio
async def test_every_magickit_call_is_pinned_to_the_thread_project() -> None:
    mcp = _FakeMagickit({"T42": ""})
    notes = _notes(mcp, [])
    await notes.get_task("T42")
    await notes.append_note("T42", "x")
    assert {args["project"] for _, args in mcp.calls} == {_PROJECT}
    assert {name for name, _ in mcp.calls} == {"get_task", "update_task"}


def test_the_tools_take_no_project_argument() -> None:
    tools = {t.name: t for t in build_ledger_tools(_notes(_FakeMagickit(), []))}
    assert set(tools) == LEDGER_TOOL_NAMES == {"ledger_get_task", "ledger_append_note"}
    get_schema: Any = tools["ledger_get_task"].input_schema
    append_schema: Any = tools["ledger_append_note"].input_schema
    assert set(get_schema) == {"task_id"}
    assert set(append_schema) == {"task_id", "text"}


# --- strategy 3: (a) lock, (d) event --------------------------------------------------------- #


@pytest.mark.anyio
async def test_concurrent_appends_to_one_task_are_serialised_and_both_survive() -> None:
    mcp = _FakeMagickit({"T42": "base"}, yield_between=True)
    locks = TaskLocks()
    events: list[Event] = []
    a = _notes(mcp, events, head="msg-a", locks=locks)
    b = _notes(mcp, events, head="msg-b", locks=locks)

    await asyncio.gather(a.append_note("T42", "from-a"), b.append_note("T42", "from-b"))

    assert "from-a" in mcp.notes["T42"]
    assert "from-b" in mcp.notes["T42"]
    assert mcp.notes["T42"].startswith("base\n\n")
    # Serialised: read, write, read, write — never read, read.
    assert [name for name, _ in mcp.calls] == ["get_task", "update_task"] * 2


@pytest.mark.anyio
async def test_without_the_shared_lock_the_race_is_real() -> None:
    """Control for the test above: the fake does interleave, so the lock is what saved it."""
    mcp = _FakeMagickit({"T42": "base"}, yield_between=True)
    a = _notes(mcp, [], locks=TaskLocks())
    b = _notes(mcp, [], locks=TaskLocks())
    await asyncio.gather(a.append_note("T42", "from-a"), b.append_note("T42", "from-b"))
    assert ("from-a" in mcp.notes["T42"]) != ("from-b" in mcp.notes["T42"])


@pytest.mark.anyio
async def test_appended_event_carries_prior_notes_their_sha256_and_the_text() -> None:
    prior = "## 目的\nexisting"
    mcp = _FakeMagickit({"T42": prior})
    events: list[Event] = []
    await _notes(mcp, events).append_note("T42", "added")

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
async def test_every_append_error_returns_is_error_and_emits_note_failed(case: str) -> None:
    mcp, task, text, reason = _setup_failure(case)
    events: list[Event] = []
    tools = {t.name: t for t in build_ledger_tools(_notes(mcp, events))}

    result = await tools["ledger_append_note"].handler({"task_id": task, "text": text})

    assert result.get("isError") is True
    assert reason.value in result["content"][0]["text"]
    assert [e.kind for e in events] == [EVENT_KIND_LEDGER_NOTE_FAILED]
    assert events[0].fields["reason"] == reason.value
    assert events[0].fields["operation"] == "append"
    assert mcp.notes["T42"] == "x"  # nothing was written


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["get_transport", "not_found", "empty_task"])
async def test_every_get_error_returns_is_error_and_emits_note_failed(case: str) -> None:
    mcp, task, _, reason = _setup_failure(case)
    events: list[Event] = []
    tools = {t.name: t for t in build_ledger_tools(_notes(mcp, events))}

    result = await tools["ledger_get_task"].handler({"task_id": task})

    assert result.get("isError") is True
    assert [e.kind for e in events] == [EVENT_KIND_LEDGER_NOTE_FAILED]
    assert events[0].fields["reason"] == reason.value
    assert events[0].fields["operation"] == "get"


@pytest.mark.anyio
async def test_get_tool_returns_the_task_and_emits_nothing() -> None:
    mcp = _FakeMagickit({"T42": "notes here"})
    events: list[Event] = []
    tools = {t.name: t for t in build_ledger_tools(_notes(mcp, events))}
    result = await tools["ledger_get_task"].handler({"task_id": "T42"})
    assert "isError" not in result
    payload = json.loads(result["content"][0]["text"])
    assert payload["task"]["notes"] == "notes here"
    assert payload["project"] == _PROJECT
    assert events == []


@pytest.mark.anyio
async def test_a_raising_event_sink_does_not_change_the_result() -> None:
    mcp = _FakeMagickit({"T42": ""})

    async def sink(_event: Event) -> None:
        raise RuntimeError("log down")

    notes = LedgerNotes(mcp=mcp, project_id=_PROJECT, thread_id=_THREAD, on_event=sink)
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


def test_an_implementer_without_a_magickit_client_attaches_no_server(tmp_path: Path) -> None:
    adapter = ImplementerSdkAdapter(cwd=tmp_path, obligations=load_manifest())
    assert adapter._make_options().mcp_servers == {}
