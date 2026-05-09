"""Single-invoke Claude Agent SDK session orchestration.

architecture.md §6.1-§6.2: every watcher invocation creates a fresh
SDK session with the thread directory as ``cwd`` and the SDK's
built-in tools (Read / Write / Bash / WebFetch / etc.) disabled. The
watcher composes the in-process MindWire custom-tool MCP server and
any pass-through MCP servers from the config and hands them to this
function via ``mcp_servers`` + ``allowed_tools``.

This module is intentionally thin — it owns the SDK glue (option
construction, message-stream draining, result aggregation) and
nothing else. The watcher is responsible for serializing per-thread
invocations and for translating :class:`InvokeResult` into event-log
entries.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    McpServerConfig,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    UserMessage,
    query,
)

SdkMessage = (
    UserMessage | AssistantMessage | SystemMessage | ResultMessage | StreamEvent | RateLimitEvent
)


@dataclass(frozen=True)
class InvokeResult:
    """Aggregated outcome of one SDK invocation."""

    is_error: bool
    duration_ms: int | None
    text_output: str
    """Concatenation of every ``TextBlock`` from every ``AssistantMessage``."""
    result_text: str | None
    """The final ``ResultMessage.result`` if the SDK provided one."""
    stop_reason: str | None
    raw_messages: tuple[SdkMessage, ...] = field(default_factory=tuple)


async def invoke_claude_code(
    prompt: str,
    *,
    cwd: Path,
    system_prompt: str,
    mcp_servers: dict[str, McpServerConfig] | None = None,
    allowed_tools: list[str] | None = None,
    max_turns: int | None = None,
    runner: Callable[..., AsyncIterator[SdkMessage]] | None = None,
) -> InvokeResult:
    """Run one SDK session to completion and collect the result.

    The session is hard-configured per architecture.md §6.2:

    - ``tools=[]``: every built-in tool is disabled. claude-code only
      sees what is explicitly listed in ``allowed_tools``.
    - ``cwd`` is set to the thread directory the watcher hands in.
    - ``mcp_servers`` carries the in-process MindWire tools and any
      config-driven pass-through servers; this layer doesn't construct
      them — that's the watcher's job.

    *runner* defaults to :func:`claude_agent_sdk.query`; tests inject a
    fake async iterator instead of patching the import.

    Raises :class:`RuntimeError` if the stream ends without a
    ``ResultMessage``. Network / SDK errors propagate out unchanged so
    the watcher's retry / dead-letter layer can decide what to do.
    """

    options = ClaudeAgentOptions(
        cwd=cwd,
        system_prompt=system_prompt,
        tools=[],
        allowed_tools=allowed_tools or [],
        mcp_servers=mcp_servers or {},
        max_turns=max_turns,
    )

    run = runner if runner is not None else query

    raw: list[SdkMessage] = []
    text_chunks: list[str] = []
    final: ResultMessage | None = None

    async for msg in run(prompt=prompt, options=options):
        raw.append(msg)
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_chunks.append(block.text)
        elif isinstance(msg, ResultMessage):
            final = msg

    if final is None:
        raise RuntimeError("SDK session ended without a ResultMessage")

    return InvokeResult(
        is_error=final.is_error,
        duration_ms=final.duration_ms,
        text_output="".join(text_chunks),
        result_text=final.result,
        stop_reason=final.stop_reason,
        raw_messages=tuple(raw),
    )


__all__ = ["InvokeResult", "invoke_claude_code"]
