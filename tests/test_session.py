"""Tests for :func:`spirrow_mindwire.claude_code.invoke_claude_code`.

The Claude Agent SDK is replaced by a fake async iterator passed via
the ``runner`` parameter, so these tests exercise the option wiring,
message-stream draining, and result aggregation without spinning up
the real CLI.
"""

from __future__ import annotations

import asyncio
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

from spirrow_mindwire.claude_code import InvokeTimeoutError, invoke_claude_code
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


# ----- Feature 2 sub-PR 2: timeout tests ------------------------------------


@pytest.mark.anyio
async def test_invoke_idle_timeout_fires(tmp_path: Path) -> None:
    """``idle_timeout_seconds`` 内に SDK が次の message を yield しない場合に
    ``InvokeTimeoutError(kind="idle")`` が raise される."""

    async def slow_first_message(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[SdkMessage]:
        # Sleep longer than idle_timeout before any yield → fires idle.
        await asyncio.sleep(10.0)
        yield _result()

    idle_timeout = 0.05
    with pytest.raises(InvokeTimeoutError) as exc:
        await invoke_claude_code(
            prompt="x",
            cwd=tmp_path,
            system_prompt="role",
            runner=slow_first_message,
            idle_timeout_seconds=idle_timeout,
        )
    assert exc.value.kind == "idle"
    # Lower bound tolerates a small precision/scheduling drift — on Windows
    # ``time.monotonic()`` resolution + ``asyncio.wait_for`` event-loop wake-up
    # have been observed to land a few ms before the nominal boundary
    # (e.g. 0.047s < 0.05s on a 50ms timeout). The 0.9x factor mirrors the
    # spirit of ``test_invoke_absolute_timeout_fires``'s ``> 0`` lower bound
    # (= what matters is ``kind == "idle"`` and that the wait actually happened);
    # see Issue #35 for the original reproductions.
    assert exc.value.elapsed_seconds >= idle_timeout * 0.9


@pytest.mark.anyio
async def test_invoke_absolute_timeout_fires(tmp_path: Path) -> None:
    """``absolute_timeout_seconds`` を超えると ``InvokeTimeoutError(kind="absolute")``.

    Each per-message wait stays under ``idle_timeout_seconds``, but cumulative
    wall-clock exceeds ``absolute_timeout_seconds`` → absolute timeout fires.
    """

    async def steady_chatter(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[SdkMessage]:
        # One real gap (0.1s) already exceeds the 0.05s absolute cap while staying
        # under the 0.5s idle cap, so the absolute timeout fires deterministically.
        # (The old form — twenty 0.02s sleeps — raced on Windows: under load the
        # event loop could coalesce them so the body finished before the cap, a
        # pre-existing flake. A single 0.1s gap cannot collapse below 0.05s.)
        for _ in range(5):
            yield _assistant("...")
            await asyncio.sleep(0.1)
        yield _result()

    with pytest.raises(InvokeTimeoutError) as exc:
        await invoke_claude_code(
            prompt="x",
            cwd=tmp_path,
            system_prompt="role",
            runner=steady_chatter,
            idle_timeout_seconds=0.5,
            absolute_timeout_seconds=0.05,
        )
    assert exc.value.kind == "absolute"
    # Lower bound is generous because asyncio.wait_for can fire a few ms early
    # depending on event-loop scheduling; what matters here is "kind == absolute".
    assert exc.value.elapsed_seconds > 0


@pytest.mark.anyio
async def test_invoke_returns_normally_within_timeouts(tmp_path: Path) -> None:
    """Timeout 引数を渡しても、 期限内に完了する run は正常 return (= 既存挙動 regression なし)."""
    captured: dict[str, Any] = {}
    runner = _runner(captured, [_assistant("quick"), _result(result="ok")])

    out = await invoke_claude_code(
        prompt="x",
        cwd=tmp_path,
        system_prompt="role",
        runner=runner,
        idle_timeout_seconds=5.0,
        absolute_timeout_seconds=5.0,
    )
    assert out.text_output == "quick"
    assert out.result_text == "ok"


@pytest.mark.anyio
async def test_invoke_no_timeout_default_preserves_existing_behavior(
    tmp_path: Path,
) -> None:
    """両 timeout を None のままにすると既存 path をそのまま走る (defaults backward compat)."""
    runner = _runner({}, [_assistant("hello"), _result()])
    out = await invoke_claude_code(
        prompt="x",
        cwd=tmp_path,
        system_prompt="role",
        runner=runner,
    )
    # No timeout-related kwargs, no timeout fires.
    assert out.text_output == "hello"


@pytest.mark.anyio
async def test_invoke_idle_propagates_unchanged_when_both_timeouts_set(
    tmp_path: Path,
) -> None:
    """idle + absolute 両 set 時、 idle が先 fire したら kind="idle" のまま propagate.

    Without the explicit ``except InvokeTimeoutError: raise`` ahead of the
    outer ``except TimeoutError``, the outer wait_for catches the inner
    InvokeTimeoutError (subclass match) and re-wraps it as kind="absolute".
    This regression test covers that idle classification survives both-set.
    """

    async def slow_first_message(
        *, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[SdkMessage]:
        # Sleep > idle threshold but < absolute threshold → idle fires first.
        await asyncio.sleep(10.0)
        yield _result()

    with pytest.raises(InvokeTimeoutError) as exc:
        await invoke_claude_code(
            prompt="x",
            cwd=tmp_path,
            system_prompt="role",
            runner=slow_first_message,
            idle_timeout_seconds=0.05,
            absolute_timeout_seconds=5.0,  # comfortably larger than idle
        )
    # The crucial assertion: kind stays "idle"; nothing re-wraps it as absolute.
    assert exc.value.kind == "idle"
