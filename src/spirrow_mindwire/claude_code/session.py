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

Feature 2 sub-PR 2 adds two optional timeouts:

- ``idle_timeout_seconds``: max wait between successive SDK messages.
  Fires :class:`InvokeTimeoutError` (kind=``"idle"``) if the iterator
  produces no output for that long.
- ``absolute_timeout_seconds``: cap on total wall-clock from invoke
  start. Fires :class:`InvokeTimeoutError` (kind=``"absolute"``).

Both default to ``None`` (= no timeout) so existing callers are
unchanged. The watcher passes the values from
:class:`spirrow_mindwire.config.WatcherConfig`.

D-4 (SDK subprocess cleanup on cancellation): the SDK's ``query()``
``async for`` is presumed to clean up its own subprocess / file
handles when the iterator is cancelled (= ``GeneratorExit`` propagates
through the ``async for``). We rely on that contract; observed leaks
should be filed as a separate issue (see PR #19 D-4 design draft).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

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

TimeoutKind = Literal["idle", "absolute"]


class InvokeTimeoutError(asyncio.TimeoutError):
    """Raised by :func:`invoke_claude_code` when a timeout fires.

    Subclasses :class:`asyncio.TimeoutError` so naive callers using
    ``except asyncio.TimeoutError`` still catch it; the watcher catches
    this specific class to distinguish ``kind`` (``"idle"`` vs
    ``"absolute"``) and to read ``elapsed_seconds`` for event-log
    entries.
    """

    def __init__(self, kind: TimeoutKind, elapsed_seconds: float) -> None:
        super().__init__(f"invoke_claude_code: {kind} timeout fired after {elapsed_seconds:.1f}s")
        self.kind: TimeoutKind = kind
        self.elapsed_seconds = elapsed_seconds


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
    idle_timeout_seconds: float | None = None,
    absolute_timeout_seconds: float | None = None,
) -> InvokeResult:
    """Run one SDK session to completion and collect the result.

    The session is hard-configured per architecture.md §6.2:

    - ``tools=[]``: every built-in tool is disabled. claude-code only
      sees what is explicitly listed in ``allowed_tools``. Combined with
      ``allowed_tools or []`` below this is fail-closed: callers that
      forget to pass ``allowed_tools`` get a session that can read but
      cannot write or call any MCP tool.
    - ``cwd`` is set to the thread directory the watcher hands in.
    - ``mcp_servers`` carries the in-process MindWire tools and any
      config-driven pass-through servers; this layer doesn't construct
      them — that's the watcher's job.

    *runner* defaults to :func:`claude_agent_sdk.query`; tests inject a
    fake async iterator instead of patching the import.

    Stream-event handling:

    - ``RateLimitEvent`` and ``StreamEvent`` are kept in
      :attr:`InvokeResult.raw_messages` but not surfaced as fields.
      Callers that need to react to rate limiting (e.g. the watcher's
      retry layer in Feature 2) walk ``raw_messages`` themselves.
    - If multiple ``ResultMessage`` instances arrive (the SDK contract
      promises one, but we don't enforce it), the *last* wins.

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
    started = time.monotonic()

    async def _drain() -> InvokeResult:
        raw: list[SdkMessage] = []
        text_chunks: list[str] = []
        final: ResultMessage | None = None

        iterator = run(prompt=prompt, options=options).__aiter__()
        while True:
            try:
                if idle_timeout_seconds is not None:
                    msg = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout_seconds)
                else:
                    msg = await iterator.__anext__()
            except StopAsyncIteration:
                break
            except TimeoutError as exc:
                # ``wait_for`` raises asyncio.TimeoutError with no further
                # context; rewrap with kind + elapsed for the watcher.
                raise InvokeTimeoutError("idle", time.monotonic() - started) from exc

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

    if absolute_timeout_seconds is None:
        return await _drain()
    try:
        return await asyncio.wait_for(_drain(), timeout=absolute_timeout_seconds)
    except TimeoutError as exc:
        # The inner ``_drain`` may already have raised InvokeTimeoutError
        # (= idle); re-raising the outer ``asyncio.TimeoutError`` would
        # mask that. ``except`` matches subclasses, so an ``InvokeTimeoutError``
        # short-circuits before this branch and propagates as-is.
        raise InvokeTimeoutError("absolute", time.monotonic() - started) from exc


__all__ = [
    "InvokeResult",
    "InvokeTimeoutError",
    "TimeoutKind",
    "invoke_claude_code",
]
