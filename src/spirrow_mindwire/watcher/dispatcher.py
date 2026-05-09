"""Per-thread async dispatch: queue → invoke → event-log persist.

Phase 0 happy-path only. Robustness (timeout, retry, dead-letter,
validation) lives in Feature 2 (``develop/feat-robustness``).

The dispatcher's loop:

1. Pull a :class:`ThreadEvent` from the queue.
2. Run TTL dedup; skip if seen recently.
3. Acquire a slot from the global semaphore (``max_concurrent_threads``).
4. Read thread state (meta + messages) from disk.
5. Build the prompt, the in-process MindWire MCP server, the
   pass-through MCP servers (left empty in Phase 0 happy path), and
   the allowed_tools list.
6. Call ``invoke_claude_code`` and append events for the invoke
   start / end.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from spirrow_mindwire.claude_code import (
    SYSTEM_PROMPT,
    InvokeResult,
    build_mindwire_mcp_server,
    build_thread_prompt,
    invoke_claude_code,
)
from spirrow_mindwire.filesystem import EventLogWriter, ThreadDirLayout
from spirrow_mindwire.phanthand import PhanthandClient
from spirrow_mindwire.schema import (
    ClaudeCodeInvokeEnd,
    ClaudeCodeInvokeStart,
    Participant,
)
from spirrow_mindwire.ulid_util import new_ulid

from .dedup import DedupCache
from .events import ThreadEvent
from .loader import load_messages, load_thread_meta

logger = logging.getLogger(__name__)

# Tool names the MindWire MCP server exposes; mirrors §6.3.
_ALLOWED_MINDWIRE_TOOLS = [
    "mcp__mindwire__write_reply",
    "mcp__mindwire__read_file",
    "mcp__mindwire__list_dir",
    "mcp__mindwire__search",
    "mcp__mindwire__file_info",
]

# Type of the SDK invoker; defaults to ``invoke_claude_code`` in production
# and tests can substitute a fake to avoid the real CLI.
SdkInvoker = Callable[..., Awaitable[InvokeResult]]


class ThreadDispatcher:
    """Runs one ``invoke_claude_code`` per ``ThreadEvent``, with TTL dedup."""

    def __init__(
        self,
        *,
        base_dir: Path,
        phanthand_client: PhanthandClient,
        dedup: DedupCache,
        max_concurrent: int = 4,
        invoker: SdkInvoker | None = None,
    ) -> None:
        self._base_dir = base_dir
        self._phanthand_client = phanthand_client
        self._dedup = dedup
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._invoker: SdkInvoker = invoker or invoke_claude_code

    async def handle(self, event: ThreadEvent) -> None:
        if self._dedup.seen_recently(event.thread_id, event.seq, event.detected_at):
            logger.debug("deduped event thread_id=%s seq=%d", event.thread_id, event.seq)
            return
        self._dedup.mark(event.thread_id, event.seq, event.detected_at)

        async with self._semaphore:
            await self._run_thread(event)

    async def _run_thread(self, event: ThreadEvent) -> None:
        layout = ThreadDirLayout(base_dir=self._base_dir, thread_id=event.thread_id)
        meta = load_thread_meta(layout)
        messages = load_messages(layout)
        if not messages:
            logger.warning("thread %s has no messages on disk; skipping", event.thread_id)
            return

        latest = messages[-1]
        # Phase 0 happy path: the watcher only invokes claude-code in
        # response to claude.ai messages. Replies authored by claude-code
        # itself end up here too (we just wrote them via write_reply);
        # skip those to avoid an infinite loop.
        if latest.from_ != "claude.ai":
            logger.debug(
                "latest message in %s is from %s; nothing to do",
                event.thread_id,
                latest.from_,
            )
            return

        sender: Participant = "claude-code"
        recipient: Participant = "claude.ai"
        next_seq = latest.seq + 1

        prompt = build_thread_prompt(meta, messages)
        mcp_server = build_mindwire_mcp_server(
            layout=layout,
            next_seq=next_seq,
            sender=sender,
            recipient=recipient,
            phanthand_client=self._phanthand_client,
        )

        log = EventLogWriter(layout.event_log_path)
        log.append(
            ClaudeCodeInvokeStart(
                schema_version=1,
                event_id=new_ulid(),
                ts=datetime.now(UTC),
                thread_id=event.thread_id,
                msg_seq=latest.seq,
            )
        )

        try:
            result = await self._invoker(
                prompt=prompt,
                cwd=layout.thread_dir,
                system_prompt=SYSTEM_PROMPT,
                mcp_servers={"mindwire": mcp_server},
                allowed_tools=_ALLOWED_MINDWIRE_TOOLS,
            )
        except Exception:
            log.append(
                ClaudeCodeInvokeEnd(
                    schema_version=1,
                    event_id=new_ulid(),
                    ts=datetime.now(UTC),
                    thread_id=event.thread_id,
                    msg_seq=latest.seq,
                    duration_ms=0,
                    exit_code=1,
                )
            )
            raise

        log.append(
            ClaudeCodeInvokeEnd(
                schema_version=1,
                event_id=new_ulid(),
                ts=datetime.now(UTC),
                thread_id=event.thread_id,
                msg_seq=latest.seq,
                duration_ms=result.duration_ms or 0,
                exit_code=1 if result.is_error else 0,
            )
        )


__all__ = ["ThreadDispatcher"]
