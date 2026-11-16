"""D-1' turn telemetry (T-implementer-context-drop-unexplained, Bohr msg-8398 / msg-3295).

The load-bearing test here is the Point-4 key-set invariant: a PreCompact line carries
exactly ``{session_id, trigger, uuid, timestamp, upstream_msg_id}``. The SDK's
``PreCompactHookInput`` also carries ``custom_instructions`` and ``transcript_path``,
and the Tier A boundary is that neither ever reaches a log (Einstein msg-3296).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from spirrow_mindwire.adapters._turn_telemetry import (
    PRECOMPACT_LOG_KEYS,
    TurnTelemetryState,
    log_context_usage,
    log_prompt,
    precompact_fields,
    precompact_hooks,
)
from spirrow_mindwire.adapters.claude_code_sdk import ClaudeCodeSdkAdapter
from spirrow_mindwire.adapters.implementer import ImplementerSdkAdapter
from spirrow_mindwire.obligations import load_manifest
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.thread_context import (
    build_thread_context,
    build_turn_prompt,
    with_appended_trigger,
)
from spirrow_mindwire.value_objects import (
    ChatroomEvent,
    Event,
    EventType,
    NewMessagePayload,
    ReplyDraft,
    Role,
    ThreadRef,
)

_TS = datetime(2026, 11, 9, tzinfo=UTC)
_SECRET = "SECRET-BODY-TEXT"
_PRECOMPACT_INPUT: dict[str, Any] = {
    "hook_event_name": "PreCompact",
    "session_id": "cc-sess-1",
    "transcript_path": "C:/secret/transcript.jsonl",
    "cwd": "C:/repo",
    "trigger": "auto",
    "custom_instructions": _SECRET,
}


def _msgs(n: int) -> list[dict[str, Any]]:
    return [
        {"msg_id": f"msg-{i}", "author": "a", "content": f"{_SECRET} {i}"} for i in range(1, n + 1)
    ]


def _thread_ref() -> ThreadRef:
    return ThreadRef(project_id="p", thread_id="T-x", chatroom_uri="mc://t/1")


def _event(messages: list[dict[str, Any]], trigger: str) -> ChatroomEvent:
    return ChatroomEvent(
        event_id=f"T-x:{trigger}",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id=trigger, author="human", body="go", parent_msg_id=None),
        thread_context=build_thread_context(messages, trigger_msg_id=trigger),
    )


def _line_fields(line: str, prefix: str) -> dict[str, str]:
    assert line.startswith(prefix), line
    return dict(tok.split("=", 1) for tok in line[len(prefix) :].split())


# --------------------------------------------------------------------------- #
# Point-4 — the CI-asserted invariant
# --------------------------------------------------------------------------- #


def test_the_precompact_key_set_is_exactly_the_five_identity_fields() -> None:
    expected = {"session_id", "trigger", "uuid", "timestamp", "upstream_msg_id"}
    assert expected == PRECOMPACT_LOG_KEYS
    fields = precompact_fields(_PRECOMPACT_INPUT, upstream_msg_id="msg-7")
    assert set(fields) == PRECOMPACT_LOG_KEYS


@pytest.mark.anyio
async def test_the_logged_precompact_line_carries_exactly_the_five_keys_and_no_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = TurnTelemetryState(upstream_msg_id="msg-7")
    hooks = precompact_hooks(state)
    [matcher] = hooks["PreCompact"]
    [callback] = matcher.hooks
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.adapters._turn_telemetry"):
        out = await callback(_PRECOMPACT_INPUT, None, {"signal": None})  # type: ignore[arg-type]
    assert out == {}  # observe only: never blocks or alters the compaction
    [line] = [r.getMessage() for r in caplog.records]
    fields = _line_fields(line, "turn-telemetry precompact ")
    assert set(fields) == PRECOMPACT_LOG_KEYS
    assert fields["session_id"] == "cc-sess-1"
    assert fields["trigger"] == "auto"
    assert fields["upstream_msg_id"] == "msg-7"
    assert _SECRET not in line
    assert "transcript" not in line


def test_each_firing_gets_its_own_uuid() -> None:
    a = precompact_fields(_PRECOMPACT_INPUT, upstream_msg_id=None)
    b = precompact_fields(_PRECOMPACT_INPUT, upstream_msg_id=None)
    assert a["uuid"] != b["uuid"]


# --------------------------------------------------------------------------- #
# Point-1 — inside build_thread_context, one place for both call sites
# --------------------------------------------------------------------------- #


def test_the_builder_logs_identity_only(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.thread_context"):
        build_thread_context(_msgs(5), trigger_msg_id="msg-5", max_messages=2)
    [line] = [r.getMessage() for r in caplog.records]
    assert _line_fields(line, "thread-context built ") == {
        "trigger_msg_id": "msg-5",
        "recent_ids": "msg-3,msg-4",
        "omitted_count": "1",
        "total_count": "5",
        "trigger_appended": "false",
    }
    assert _SECRET not in line


def test_the_builder_reports_a_trigger_the_caller_appended(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fetched = _msgs(3)
    trigger = {"msg_id": "msg-4", "author": "conductor-relay", "content": "relay"}
    thread = with_appended_trigger(fetched, trigger)
    assert len(fetched) == 3  # non-destructive (R-1b)
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.thread_context"):
        ctx = build_thread_context(thread, trigger_msg_id="msg-4")
    assert [m.msg_id for m in ctx.recent] == ["msg-2", "msg-3"]
    fields = _line_fields(caplog.records[-1].getMessage(), "thread-context built ")
    assert fields["trigger_appended"] == "true"


def test_the_builder_logs_an_empty_context_too(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.thread_context"):
        build_thread_context(_msgs(1), trigger_msg_id="msg-1")
    fields = _line_fields(caplog.records[-1].getMessage(), "thread-context built ")
    assert fields["recent_ids"] == ""
    assert fields["total_count"] == "1"


# --------------------------------------------------------------------------- #
# Point-2 / Point-3
# --------------------------------------------------------------------------- #


def test_the_prompt_line_checks_every_selected_message_header(
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = _event(_msgs(4), "msg-4")
    prompt = build_turn_prompt(event, Role.IMPLEMENTER, "close")
    state = TurnTelemetryState()
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.adapters._turn_telemetry"):
        log_prompt(event, prompt, state)
        # A prompt that lost one selected message must say so.
        log_prompt(event, prompt.replace("--- msg-3 · ", "--- gone · "), state)
    first, second = (_line_fields(r.getMessage(), "turn-telemetry prompt ") for r in caplog.records)
    assert first["trigger_msg_id"] == "msg-4"
    assert first["contains_all_recent_ids"] == "true"
    assert first["prompt_len"] == str(len(prompt))
    assert len(first["prompt_sha256"]) == 8
    assert second["contains_all_recent_ids"] == "false"
    assert int(second["session_cumulative_bytes"]) > int(first["session_cumulative_bytes"])
    assert state.upstream_msg_id == "msg-4"
    assert all(_SECRET not in r.getMessage() for r in caplog.records)


class _UsageClient:
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self._result, self._exc = result, exc

    async def get_context_usage(self) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.mark.anyio
async def test_the_context_usage_line(caplog: pytest.LogCaptureFixture) -> None:
    state = TurnTelemetryState(upstream_msg_id="msg-9")
    usage = {"totalTokens": 1200, "maxTokens": 200000, "percentage": 0.6, "categories": []}
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.adapters._turn_telemetry"):
        await log_context_usage(_UsageClient(usage), state)
    assert _line_fields(caplog.records[-1].getMessage(), "turn-telemetry context-usage ") == {
        "trigger_msg_id": "msg-9",
        "total_tokens": "1200",
        "max_tokens": "200000",
        "percentage": "0.6",
    }


@pytest.mark.anyio
async def test_context_usage_never_fails_the_turn(caplog: pytest.LogCaptureFixture) -> None:
    state = TurnTelemetryState(upstream_msg_id="msg-9")
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.adapters._turn_telemetry"):
        await log_context_usage(_UsageClient(exc=RuntimeError("boom")), state)
        await log_context_usage(object(), state)
    lines = [r.getMessage() for r in caplog.records]
    assert "unavailable=RuntimeError" in lines[0]
    assert "unavailable=no-control-call" in lines[1]


# --------------------------------------------------------------------------- #
# Wiring — both Claude adapters register the hook and emit Point-2/3
# --------------------------------------------------------------------------- #


class _Client:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def connect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

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

    async def get_context_usage(self) -> dict[str, Any]:
        return {"totalTokens": 10, "maxTokens": 100, "percentage": 10.0}


def _ctx() -> SpawnContext:
    async def on_reply(_d: ReplyDraft) -> None:
        return None

    async def on_event_log(_e: Event) -> None:
        return None

    return SpawnContext(
        on_reply=on_reply,
        on_event_log=on_event_log,
        own_role=Role.PROPOSER,
        own_instance_id="proposer-1",
    )


@pytest.mark.anyio
async def test_the_proposer_adapter_wires_all_three_adapter_points(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = _Client()
    seen: list[Any] = []

    def factory(options: Any) -> _Client:
        seen.append(options)
        return client

    make: Callable[[Any], Any] = factory
    adapter = ClaudeCodeSdkAdapter(cwd=tmp_path, client_factory=make)
    handle = await adapter.spawn(_thread_ref(), Role.PROPOSER, _ctx())
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.adapters._turn_telemetry"):
        await adapter.deliver_event(handle, _event(_msgs(3), "msg-3"))
        [matcher] = seen[0].hooks["PreCompact"]
        await matcher.hooks[0](_PRECOMPACT_INPUT, None, {"signal": None})
    lines = [r.getMessage() for r in caplog.records]
    assert lines[0].startswith("turn-telemetry prompt trigger_msg_id=msg-3 ")
    assert "contains_all_recent_ids=true" in lines[0]
    assert lines[1].startswith("turn-telemetry context-usage trigger_msg_id=msg-3 ")
    assert "upstream_msg_id=msg-3" in lines[2]


def test_the_implementer_adapter_registers_the_precompact_hook(tmp_path: Path) -> None:
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=load_manifest(),
        inference_base_url="https://api.anthropic.com",
    )
    options = adapter._make_options(telemetry=TurnTelemetryState())
    assert options.hooks is not None
    assert set(options.hooks) == {"PreCompact"}
    assert adapter._make_options().hooks is None


class _NoJobModule:
    """The POSIX path of the Job API: no Job Object, so the test is host-independent."""

    def create_job(self, _session_id: str) -> Any:
        raise NotImplementedError("no Job Object in this test")


@pytest.mark.anyio
async def test_the_implementer_spawn_path_wires_the_precompact_hook_to_the_delivered_trigger(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # rev12 P-B (Bohr msg-8938; Einstein go on rev12): the test above calls _make_options()
    # directly, so nothing pinned that ImplementerSdkAdapter.spawn() itself hands telemetry
    # through. Mock only: this says the spawn path registers the callback and the callback
    # logs against the delivered trigger — not that the SDK/CLI fires it, nor anything of prod.
    client = _Client()
    seen: list[Any] = []

    def factory(options: Any) -> _Client:
        seen.append(options)
        return client

    async def on_reply(_d: ReplyDraft) -> None:
        return None

    async def on_event_log(_e: Event) -> None:
        return None

    ctx = SpawnContext(
        on_reply=on_reply,
        on_event_log=on_event_log,
        own_role=Role.IMPLEMENTER,
        own_instance_id="implementer-1",
    )
    adapter = ImplementerSdkAdapter(
        cwd=tmp_path,
        obligations=load_manifest(),
        inference_base_url="https://api.anthropic.com",
        client_factory=factory,
        job_module=_NoJobModule(),
    )
    handle = await adapter.spawn(_thread_ref(), Role.IMPLEMENTER, ctx)
    [options] = seen
    assert options.hooks is not None
    [matcher] = options.hooks["PreCompact"]
    [callback] = matcher.hooks
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.adapters._turn_telemetry"):
        await adapter.deliver_event(handle, _event(_msgs(3), "msg-3"))
        out = await callback(_PRECOMPACT_INPUT, None, {"signal": None})
    assert out == {}
    precompact = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("turn-telemetry precompact ")
    ]
    [line] = precompact
    fields = _line_fields(line, "turn-telemetry precompact ")
    assert set(fields) == PRECOMPACT_LOG_KEYS
    assert fields["upstream_msg_id"] == "msg-3"
    assert _SECRET not in line
