"""Tests for :func:`spirrow_mindwire.claude_code.invoke_claude_code`.

The Claude Agent SDK is replaced by a fake async iterator passed via
the ``runner`` parameter, so these tests exercise the option wiring,
message-stream draining, and result aggregation without spinning up
the real CLI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
)

from spirrow_mindwire.claude_code import invoke_claude_code
from spirrow_mindwire.claude_code.session import SdkMessage


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="test-model")


def _result(
    *,
    is_error: bool = False,
    duration_ms: int = 100,
    result: str | None = "ok",
    stop_reason: str | None = "end_turn",
) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=duration_ms,
        duration_api_ms=duration_ms,
        is_error=is_error,
        num_turns=1,
        session_id="test-session",
        stop_reason=stop_reason,
        result=result,
    )


def _runner(
    captured: dict[str, Any],
    yields: list[SdkMessage],
) -> Any:
    async def fake(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[SdkMessage]:
        captured["prompt"] = prompt
        captured["options"] = options
        for m in yields:
            yield m

    return fake


@pytest.mark.anyio
async def test_invoke_collects_text_and_result(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    runner = _runner(
        captured,
        [
            _assistant("hello "),
            _assistant("world"),
            _result(duration_ms=200, result="done"),
        ],
    )

    out = await invoke_claude_code(
        prompt="what's up?",
        cwd=tmp_path,
        system_prompt="role",
        runner=runner,
    )

    assert out.is_error is False
    assert out.duration_ms == 200
    assert out.text_output == "hello world"
    assert out.result_text == "done"
    assert out.stop_reason == "end_turn"
    assert len(out.raw_messages) == 3


@pytest.mark.anyio
async def test_invoke_hardcodes_tools_empty_and_passes_cwd(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    runner = _runner(captured, [_result()])

    await invoke_claude_code(
        prompt="x",
        cwd=tmp_path,
        system_prompt="role",
        runner=runner,
    )

    options = captured["options"]
    assert options.tools == []  # built-in tools disabled (architecture.md §6.2)
    assert options.cwd == tmp_path
    assert options.system_prompt == "role"


@pytest.mark.anyio
async def test_invoke_passes_mcp_servers_and_allowed_tools(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    runner = _runner(captured, [_result()])

    await invoke_claude_code(
        prompt="x",
        cwd=tmp_path,
        system_prompt="role",
        mcp_servers={"mindwire": {"type": "stdio", "command": "echo"}},
        allowed_tools=["mcp__mindwire__write_reply"],
        runner=runner,
    )

    options = captured["options"]
    assert options.allowed_tools == ["mcp__mindwire__write_reply"]
    assert "mindwire" in options.mcp_servers


@pytest.mark.anyio
async def test_invoke_propagates_is_error(tmp_path: Path) -> None:
    runner = _runner({}, [_result(is_error=True)])
    out = await invoke_claude_code(
        prompt="x",
        cwd=tmp_path,
        system_prompt="role",
        runner=runner,
    )
    assert out.is_error is True


@pytest.mark.anyio
async def test_invoke_raises_when_no_result_message(tmp_path: Path) -> None:
    runner = _runner({}, [_assistant("orphan")])
    with pytest.raises(RuntimeError, match="ResultMessage"):
        await invoke_claude_code(
            prompt="x",
            cwd=tmp_path,
            system_prompt="role",
            runner=runner,
        )


@pytest.mark.anyio
async def test_invoke_handles_missing_optional_result_fields(tmp_path: Path) -> None:
    runner = _runner({}, [_result(result=None, stop_reason=None)])
    out = await invoke_claude_code(
        prompt="x",
        cwd=tmp_path,
        system_prompt="role",
        runner=runner,
    )
    assert out.result_text is None
    assert out.stop_reason is None


@pytest.mark.anyio
async def test_invoke_passes_max_turns_when_provided(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    runner = _runner(captured, [_result()])
    await invoke_claude_code(
        prompt="x",
        cwd=tmp_path,
        system_prompt="role",
        max_turns=5,
        runner=runner,
    )
    assert captured["options"].max_turns == 5
