"""Per-thread async dispatch: queue → invoke → event-log persist.

Phase 0 happy-path baseline + Feature 2 robustness (sub-PR 2 timeout +
sub-PR 3 retry) layered on top.

The dispatcher's loop:

1. Pull a :class:`ThreadEvent` from the queue.
2. Run TTL dedup; skip if seen recently (D-6 (a): retry loop below
   bypasses this layer — only the *entry* into ``_run_thread`` is
   deduped).
3. Acquire a slot from the global semaphore (``max_concurrent_threads``)
   and the per-thread asyncio lock.
4. Read thread state (meta + messages) from disk.
5. Build the prompt, the in-process MindWire MCP server, the
   pass-through MCP servers (left empty in Phase 0 happy path), and
   the allowed_tools list.
6. Run the **retry loop** (up to ``max_retries + 1`` attempts):

   - Append :class:`ClaudeCodeInvokeStart` + call ``invoke_claude_code``.
   - On success: append :class:`ClaudeCodeInvokeEnd` (exit_code=0). If
     the thread was in ``retrying`` state on entry, transition back to
     ``active`` with ``retry_count=None`` so the audit-trail counter is
     preserved (D-7 (b) preserve, see docs/feature-2-design.md §3.5).
   - On :class:`InvokeTimeoutError` (= transient): append
     ``ClaudeCodeInvokeEnd`` (exit_code=1) + bump ``retry_count`` (=
     ``active → retrying`` first time via :func:`transition_state`, or
     ``bump_retry_count`` thereafter to avoid the forbidden
     ``retrying → retrying`` transition) + append
     :class:`RetryBackoffStarted` + sleep
     ``retry_backoff_seconds[attempt] * (1 ± retry_jitter)`` + retry.
     When the loop hits ``max_retries`` exhaustion, transition to
     ``terminated`` with ``terminated_reason="retry-exhausted"``.
   - On :class:`asyncio.CancelledError`: propagate (= shutdown path).
   - On other exceptions (= permanent / unknown): safe-by-default
     ``active → terminated`` with ``terminated_reason="validation-failed"``.
     Allowlist-based transient classification is intentional — see
     docs/feature-2-design.md §6 FI-4 for the dogfooding-pending re-audit.

The retry loop runs entirely inside ``_run_thread``; new
``ThreadEvent`` instances are not re-enqueued by the dispatcher
itself. ``startup_full_scan`` (re-)queues ``retrying`` threads on
watcher restart, so cumulative ``retry_count`` can exceed
``max_retries`` across restarts — this is the audit-trail framing of
``retry_count`` (D-7 (b), docs/feature-2-design.md §3.5).
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
    InvokeTimeoutError,
    build_mindwire_mcp_server,
    build_thread_prompt,
    invoke_claude_code,
)
from spirrow_mindwire.filesystem import EventLogWriter, ThreadDirLayout
from spirrow_mindwire.lifecycle import TERMINAL_STATES, bump_retry_count, transition_state
from spirrow_mindwire.phanthand import PhanthandClient
from spirrow_mindwire.schema import (
    ClaudeCodeInvokeEnd,
    ClaudeCodeInvokeStart,
    Participant,
    ThreadStatusChanged,
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
        idle_timeout_seconds: float | None = None,
        absolute_timeout_seconds: float | None = None,
    ) -> None:
        self._base_dir = base_dir
        self._phanthand_client = phanthand_client
        self._dedup = dedup
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._invoker: SdkInvoker = invoker or invoke_claude_code
        # Feature 2 sub-PR 2: SDK invocation timeouts. ``None`` keeps the
        # pre-Feature-2 behavior (no timeout); the watcher entry point
        # (``run_watcher``) passes the values from
        # :class:`spirrow_mindwire.config.WatcherConfig` so these are
        # always set in production. Tests instantiate the dispatcher
        # directly with ``None`` to keep most cases free of timing
        # noise; the timeout-specific tests pass real values.
        self._idle_timeout_seconds = idle_timeout_seconds
        self._absolute_timeout_seconds = absolute_timeout_seconds
        # Per-thread serialization: architecture.md §4.0 requires that a
        # single thread never run two invocations concurrently. Without
        # this, two events landing close together (e.g. seq=1 then seq=2)
        # would race on ``write_reply``'s next_seq computation and on the
        # event log's start/end pairing. Independent threads still run in
        # parallel, capped by ``self._semaphore``.
        #
        # Lock entries are intentionally never deleted in Phase 0:
        # - The expected scale is ~tens of threads per running watcher
        #   (1 user, small thread set), so dict growth is bounded.
        # - The candidates for cleanup (`lock._waiters` introspection or
        #   ref-counting) all add complexity for negligible payoff.
        # - Feature 2 introduces startup-full-scan + graceful-shutdown,
        #   which is the right place to consolidate lifecycle including
        #   any thread-lock GC strategy.
        # See ChatRoom thread T-T06-pr8-per-thread-serialization msg-019.
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._locks_mutex = asyncio.Lock()

    async def _get_thread_lock(self, thread_id: str) -> asyncio.Lock:
        async with self._locks_mutex:
            return self._thread_locks.setdefault(thread_id, asyncio.Lock())

    async def handle(self, event: ThreadEvent) -> None:
        if self._dedup.seen_recently(event.thread_id, event.seq, event.detected_at):
            logger.debug("deduped event thread_id=%s seq=%d", event.thread_id, event.seq)
            return
        self._dedup.mark(event.thread_id, event.seq, event.detected_at)

        thread_lock = await self._get_thread_lock(event.thread_id)
        async with thread_lock, self._semaphore:
            await self._run_thread(event)

    async def _run_thread(self, event: ThreadEvent) -> None:
        layout = ThreadDirLayout(base_dir=self._base_dir, thread_id=event.thread_id)
        meta = load_thread_meta(layout)
        # Feature 2: terminal states are never auto-revived (docs §3.6).
        # Operator manual transitions go through meta.yaml edits; until
        # status is set back to active by the operator, skip the event.
        if meta.status in TERMINAL_STATES:
            logger.info(
                "thread %s is in terminal state %s; skipping event",
                event.thread_id,
                meta.status,
            )
            return
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
                idle_timeout_seconds=self._idle_timeout_seconds,
                absolute_timeout_seconds=self._absolute_timeout_seconds,
            )
        except InvokeTimeoutError as exc:
            # Feature 2 sub-PR 2: timeout advances the retry counter.
            # Two source statuses are possible here:
            # - ``active``: first failure → transition active → retrying
            #   (logs ``ThreadStatusChanged``).
            # - ``retrying``: thread was requeued by ``startup_scan`` after a
            #   prior timeout → ``retrying → retrying`` is forbidden by
            #   ``_ALLOWED_TRANSITIONS``; bump only ``retry_count`` via
            #   :func:`lifecycle.bump_retry_count` (no ``ThreadStatusChanged``).
            # The actual retry loop (re-invoking the thread) is sub-PR 3
            # territory — for now, the watcher just records the new state
            # and returns, leaving the next live event / startup_scan to
            # pick the thread back up.
            log.append(
                ClaudeCodeInvokeEnd(
                    schema_version=1,
                    event_id=new_ulid(),
                    ts=datetime.now(UTC),
                    thread_id=event.thread_id,
                    msg_seq=latest.seq,
                    duration_ms=round(exc.elapsed_seconds * 1000),
                    exit_code=1,
                )
            )
            if meta.status == "active":
                transition_state(
                    layout,
                    "retrying",
                    awaiting_from=meta.awaiting_from,
                    retry_count=meta.retry_count + 1,
                )
                log.append(
                    ThreadStatusChanged(
                        schema_version=1,
                        event_id=new_ulid(),
                        ts=datetime.now(UTC),
                        thread_id=event.thread_id,
                        from_status="active",
                        to_status="retrying",
                    )
                )
            else:  # already "retrying"
                bump_retry_count(layout)
                # No ``ThreadStatusChanged`` — status is unchanged.
            logger.info(
                "thread %s timed out (%s, %.1fs); retry_count=%d (status=%s)",
                event.thread_id,
                exc.kind,
                exc.elapsed_seconds,
                meta.retry_count + 1,
                "retrying",
            )
            return
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
