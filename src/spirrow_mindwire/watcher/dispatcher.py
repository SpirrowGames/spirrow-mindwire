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
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from spirrow_mindwire.awaiting_from_toggle import toggle_awaiting_from
from spirrow_mindwire.claude_code import (
    SYSTEM_PROMPT,
    InvokeResult,
    InvokeTimeoutError,
    build_mindwire_mcp_server,
    build_thread_prompt,
    invoke_claude_code,
)
from spirrow_mindwire.filesystem import EventLogWriter, ThreadDirLayout
from spirrow_mindwire.lifecycle import (
    TERMINAL_STATES,
    bump_retry_count,
    transition_state,
)
from spirrow_mindwire.phanthand import PhanthandClient
from spirrow_mindwire.schema import (
    ClaudeCodeInvokeEnd,
    ClaudeCodeInvokeStart,
    Participant,
    RetryBackoffStarted,
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


_TRANSIENT_ERROR_TYPES: tuple[type[BaseException], ...] = (InvokeTimeoutError,)
"""Exception types treated as transient by the retry loop (allowlist).

Safe-by-default permanent for unknown errors: anything not in this
allowlist hits the ``else`` branch in ``_run_thread`` and goes to
``terminated`` with ``terminated_reason='validation-failed'``. Add new
transient types (e.g. an SDK rate-limit class once one exists upstream)
by appending here. See docs/feature-2-design.md §6 FI-4 for the
dogfooding-pending re-audit policy and Issue #32 for tracking.

Phase 0 stance: module-level constant (= operator override unsupported,
all changes happen in this module). Phase 1+ migration candidates if
dogfooding shows the allowlist is too rigid for specific deployments
(T-decide-error-classification msg-074 §2-2):

- ``WatcherConfig.transient_error_types: frozenset[str] = ...`` —
  operator declares additional transient classes by qualified name,
  validated against an importable allowlist at config load.
- ``lifecycle/error_classification.py`` extract — share the allowlist
  + ``_is_transient`` helper with sub-PR 4 terminate path so the
  ``transient`` / ``permanent`` taxonomy has a single source of truth
  outside of the dispatcher.

Until either trigger fires, the safe-by-default constant lives here.
"""


def _is_transient(exc: BaseException) -> bool:
    """Whether *exc* drives the retry loop (vs direct terminate)."""
    return isinstance(exc, _TRANSIENT_ERROR_TYPES)


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
        max_retries: int = 0,
        retry_backoff_seconds: tuple[float, ...] = (),
        retry_jitter: float = 0.0,
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
        # Feature 2 sub-PR 3: retry tuning. Defaults disable retry
        # (``max_retries=0`` ⇒ a single transient failure goes straight
        # to ``terminated/retry-exhausted``) so unit tests that exercise
        # the dispatcher without explicit retry config don't pay the
        # 155s backoff cost. Production code paths go through
        # :func:`run_watcher`, which forwards
        # :class:`spirrow_mindwire.config.WatcherConfig` values (and
        # the config validators in O-2 (b) enforce
        # ``len(retry_backoff_seconds) >= max_retries`` so the loop
        # never indexes past the end).
        if max_retries > 0 and len(retry_backoff_seconds) < max_retries:
            # Same invariant as the config-level model validator, but
            # also enforced for callers that bypass MindwireSettings
            # (tests).
            raise ValueError(
                f"retry_backoff_seconds length ({len(retry_backoff_seconds)}) "
                f"must be >= max_retries ({max_retries})"
            )
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._retry_jitter = retry_jitter
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
        log = EventLogWriter(layout.event_log_path)

        # Feature 2 sub-PR 3 (Copilot review C-1 follow-up): the retry loop
        # reloads meta + messages and rebuilds prompt + MCP server at the
        # top of each attempt. This makes the dispatcher consistent with
        # docs §3.4 D-3 on-disk truth / commit-and-forward semantics and
        # protects three otherwise-silent edge cases:
        #
        #  (a) status staleness — after a transient failure transitions
        #      meta to ``retrying``, attempt N>0 was previously sending a
        #      prompt with ``status="active"`` baked in (build_thread_prompt
        #      renders meta.status as an attribute).
        #  (b) partial write_reply overwrite — claude-code can land a reply
        #      via mcp__mindwire__write_reply just before a timeout fires;
        #      reusing the original ``next_seq`` on retry would silently
        #      overwrite that reply (next_seq is captured at MCP server
        #      construction). On reload, ``next_seq = latest.seq + 1`` is
        #      recomputed, and if the new latest is from claude-code we
        #      treat the session as effectively succeeded and recover.
        #  (c) operator manual terminate mid-retry — operator can edit
        #      meta.yaml to ``terminated`` between attempts (docs §3.6);
        #      reloading lets us honour that promptly instead of running
        #      one more invoke we don't want.
        #
        # The reload is two disk reads per attempt — cheap relative to a
        # real invoke. ``max_retries=0`` collapses to the original single-
        # attempt behaviour.
        for attempt in range(self._max_retries + 1):
            current_meta = load_thread_meta(layout)
            # Edge (c): terminal states are never auto-revived (docs §3.6).
            if current_meta.status in TERMINAL_STATES:
                logger.info(
                    "thread %s is in terminal state %s; skipping event",
                    event.thread_id,
                    current_meta.status,
                )
                return
            messages = load_messages(layout)
            if not messages:
                logger.warning("thread %s has no messages on disk; skipping", event.thread_id)
                return

            latest = messages[-1]
            if latest.from_ != "claude.ai":
                # Phase 0 happy path: the watcher only invokes claude-code in
                # response to claude.ai messages.
                if current_meta.status == "retrying":
                    # Edge (b): partial write_reply landed during a prior
                    # attempt's invoke window. Recover to active so the
                    # thread isn't stuck in ``retrying`` forever.
                    # Same Phase1-Obs1 class: the partial write_reply was a
                    # successful reply, so awaiting_from must also toggle.
                    # Recovery first, toggle after (PR #40 review C-4):
                    # if the toggle's event-log append fails, recovery has
                    # already happened and the thread is no longer stuck in
                    # retrying. Meta-write order doesn't depend on this
                    # ordering because ``_recover_retrying_to_active`` and
                    # ``toggle_awaiting_from`` write disjoint meta fields
                    # (status / retry_count vs awaiting_from).
                    self._recover_retrying_to_active(
                        layout=layout,
                        event=event,
                        log=log,
                        attempt=attempt,
                        reason="partial_write_reply",
                    )
                    toggle_awaiting_from(
                        layout=layout,
                        log=log,
                        from_participant=latest.from_,
                    )
                else:
                    logger.debug(
                        "latest message in %s is from %s; nothing to do",
                        event.thread_id,
                        latest.from_,
                    )
                return

            replier: Participant = "claude-code"
            recipient: Participant = "claude.ai"
            next_seq = latest.seq + 1
            prompt = build_thread_prompt(current_meta, messages)
            mcp_server = build_mindwire_mcp_server(
                layout=layout,
                next_seq=next_seq,
                # ``sender=`` is build_mindwire_mcp_server's public param
                # (out of PR #51 C1 scope: the rename is dispatcher-local);
                # only the argument value follows the ``replier`` rename.
                sender=replier,
                recipient=recipient,
                phanthand_client=self._phanthand_client,
            )

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
            except asyncio.CancelledError:
                # Shutdown path — never swallow.
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
            except Exception as exc:
                duration_ms = (
                    round(exc.elapsed_seconds * 1000) if isinstance(exc, InvokeTimeoutError) else 0
                )
                log.append(
                    ClaudeCodeInvokeEnd(
                        schema_version=1,
                        event_id=new_ulid(),
                        ts=datetime.now(UTC),
                        thread_id=event.thread_id,
                        msg_seq=latest.seq,
                        duration_ms=duration_ms,
                        exit_code=1,
                    )
                )
                if _is_transient(exc):
                    self._handle_transient_failure(
                        layout=layout,
                        event=event,
                        attempt=attempt,
                        exc=exc,
                        log=log,
                    )
                    if attempt == self._max_retries:
                        return
                    # backoff sleep before the next iteration
                    backoff = self._compute_backoff(attempt)
                    log.append(
                        RetryBackoffStarted(
                            schema_version=1,
                            event_id=new_ulid(),
                            ts=datetime.now(UTC),
                            thread_id=event.thread_id,
                            # attempt_num is 1-based and refers to the
                            # upcoming retry attempt (= attempt that runs
                            # after this backoff); the 0-based ``attempt``
                            # loop counter becomes 1-based here. See
                            # ``RetryBackoffStarted`` docstring for the full
                            # semantic.
                            attempt_num=attempt + 1,
                            backoff_seconds=backoff,
                        )
                    )
                    logger.info(
                        "thread %s transient failure (attempt %d/%d, %s); "
                        "sleeping %.2fs before retry",
                        event.thread_id,
                        attempt + 1,
                        self._max_retries + 1,
                        type(exc).__name__,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                # Permanent (safe-by-default) → terminated/validation-failed.
                self._handle_permanent_failure(layout=layout, event=event, exc=exc, log=log)
                return

            # Success path.
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
            # If we entered ``retrying`` earlier in the loop, transition back
            # first. Doing recovery before the awaiting_from toggle keeps the
            # retrying→active transition atomic regardless of whether the
            # subsequent toggle's event append succeeds (PR #40 review C-4:
            # toggle's event log failure must not strand the thread in
            # ``retrying``).
            self._recover_retrying_to_active(
                layout=layout,
                event=event,
                log=log,
                attempt=attempt,
                reason="invoke_success",
            )
            # write_reply 成功完了時を SOT として awaiting_from を相手側に toggle
            # (docs/feature-2-design.md §3.5、 Phase1-Obs1 fix)。 meta の
            # atomic write を先に終え、 best-effort で AwaitingFromChanged
            # event を append (= FI-2 resolution、 meta は SOT)。
            #
            # ``result.is_error=False`` だけでは write_reply 実行の保証にならない
            # (PR #40 review Copilot-1〜3、 claude.ai C2): SDK ResultMessage が
            # success でも model が tool を呼ばずに turn 終了することがある
            # (= system_prompt は advisory、 invocation can finish with no reply
            # file)。 production-correct な gate は **post-invoke message reload
            # で claude-code の新 message が next_seq に存在することを verify**。
            # partial_write_reply 経路は別途 ``latest.from_ == "claude-code"``
            # の早期 return guard で同等 verify 済みのため、 同 check 不要。
            if self._write_reply_completed(layout=layout, replier=replier, next_seq=next_seq):
                toggle_awaiting_from(
                    layout=layout,
                    log=log,
                    from_participant=replier,
                )
            else:
                logger.info(
                    "thread %s: SDK invoke succeeded but no claude-code reply "
                    "landed at seq %d; awaiting_from kept (model finished turn "
                    "without write_reply, or write_reply failed silently)",
                    event.thread_id,
                    next_seq,
                )
            return

    def _handle_transient_failure(
        self,
        *,
        layout: ThreadDirLayout,
        event: ThreadEvent,
        attempt: int,
        exc: BaseException,
        log: EventLogWriter,
    ) -> None:
        """Advance retry_count and (if needed) status after a transient failure.

        Branches:

        - If the on-disk status is ``active``: transition ``active →
          retrying`` with ``retry_count += 1`` and emit a
          :class:`ThreadStatusChanged` snapshot event.
        - If the status is already ``retrying`` (e.g. requeued by
          startup_full_scan): use ``bump_retry_count`` because
          ``retrying → retrying`` is forbidden by
          ``_ALLOWED_TRANSITIONS``; no status-change event is emitted.
        - If ``attempt == max_retries`` (= retry exhaustion): transition
          to ``terminated/retry-exhausted`` regardless of the source
          status and emit the snapshot event.

        The function does *not* sleep or log the
        :class:`RetryBackoffStarted` occurrence — those belong to the
        caller's loop because they only happen between attempts.
        """
        current_meta = load_thread_meta(layout)

        if attempt == self._max_retries:
            new_meta = transition_state(
                layout,
                "terminated",
                awaiting_from=None,
                terminated_reason="retry-exhausted",
            )
            log.append(
                ThreadStatusChanged(
                    schema_version=1,
                    event_id=new_ulid(),
                    ts=datetime.now(UTC),
                    thread_id=event.thread_id,
                    from_status=current_meta.status,
                    to_status="terminated",
                    retry_count=new_meta.retry_count,
                )
            )
            logger.info(
                "thread %s retry-exhausted after %d attempt(s); terminating (%s)",
                event.thread_id,
                attempt + 1,
                type(exc).__name__,
            )
            return

        if current_meta.status == "active":
            new_meta = transition_state(
                layout,
                "retrying",
                awaiting_from=current_meta.awaiting_from,
                retry_count=current_meta.retry_count + 1,
            )
            log.append(
                ThreadStatusChanged(
                    schema_version=1,
                    event_id=new_ulid(),
                    ts=datetime.now(UTC),
                    thread_id=event.thread_id,
                    from_status="active",
                    to_status="retrying",
                    retry_count=new_meta.retry_count,
                )
            )
        else:  # current_meta.status == "retrying"
            bump_retry_count(layout)
            # No ThreadStatusChanged — status is unchanged (retrying → retrying
            # self-loop is forbidden, see _ALLOWED_TRANSITIONS).

    def _handle_permanent_failure(
        self,
        *,
        layout: ThreadDirLayout,
        event: ThreadEvent,
        exc: BaseException,
        log: EventLogWriter,
    ) -> None:
        """Transition the thread to ``terminated/validation-failed``.

        Symmetric counterpart of :meth:`_handle_transient_failure`: when
        ``_is_transient(exc)`` is False (= allowlist miss = safe-by-default
        permanent), the retry loop calls this helper instead of bumping
        ``retry_count``. The traceback is captured at WARNING via
        ``exc_info=True`` so the watcher log keeps a stack trace even
        though the exception is intentionally swallowed (= contained at
        the dispatcher boundary so a single bad invoke doesn't bring
        down the watcher).

        Allowlist policy and dogfooding re-audit triggers: see
        docs/feature-2-design.md §6 FI-4 / Issue #32 and the
        :data:`_TRANSIENT_ERROR_TYPES` docstring.
        """
        logger.warning(
            "thread %s permanent failure (%s); terminating",
            event.thread_id,
            type(exc).__name__,
            exc_info=True,
        )
        current_meta = load_thread_meta(layout)
        new_meta = transition_state(
            layout,
            "terminated",
            awaiting_from=None,
            terminated_reason="validation-failed",
        )
        log.append(
            ThreadStatusChanged(
                schema_version=1,
                event_id=new_ulid(),
                ts=datetime.now(UTC),
                thread_id=event.thread_id,
                from_status=current_meta.status,
                to_status="terminated",
                retry_count=new_meta.retry_count,
            )
        )

    def _write_reply_completed(
        self,
        *,
        layout: ThreadDirLayout,
        replier: Participant,
        next_seq: int,
    ) -> bool:
        """Verify a new claude-code reply landed at ``next_seq``.

        Returns True iff the latest message on disk matches the expected
        post-invoke shape (= same ``replier`` + ``next_seq`` we computed
        before invoking the SDK). Used by the success path as the
        production-correct gate for the ``awaiting_from`` toggle: SDK
        ``result.is_error=False`` is **not** proof that
        ``mcp__mindwire__write_reply`` actually ran (PR #40 review
        Copilot inline-1〜3 / claude.ai C2、 system_prompt は advisory
        instruction、 model can finish a turn without calling the tool).
        Reading the on-disk state matches the
        ``docs/feature-2-design.md`` §3.5 「``write_reply`` 成功完了時
        SOT」 wording verbatim (= reality on disk, not SDK reply).
        """
        messages_after = load_messages(layout)
        if not messages_after:
            return False
        latest_after = messages_after[-1]
        return latest_after.from_ == replier and latest_after.seq == next_seq

    def _recover_retrying_to_active(
        self,
        *,
        layout: ThreadDirLayout,
        event: ThreadEvent,
        log: EventLogWriter,
        attempt: int,
        reason: Literal["invoke_success", "partial_write_reply"],
    ) -> None:
        """If meta is in ``retrying``, transition back to ``active``.

        Shared between two retry-recovery code paths:

        - ``invoke_success``: the SDK invoke returned normally, so the
          thread is no longer waiting on a transient error.
        - ``partial_write_reply``: a prior attempt's invoke timed out
          *after* claude-code already wrote its reply via
          ``mcp__mindwire__write_reply``; the per-attempt reload (Copilot
          review C-1) detected the new reply and we treat the session as
          effectively succeeded.

        Either way ``retry_count`` is preserved (D-7 (b) audit-trail
        framing; ``transition_state(retry_count=None)`` keeps the on-disk
        value). No-op if the thread is not in ``retrying`` (e.g. attempt 0
        succeeded without ever bumping).
        """
        current_meta = load_thread_meta(layout)
        if current_meta.status != "retrying":
            return
        new_meta = transition_state(
            layout,
            "active",
            awaiting_from=current_meta.awaiting_from,
            # retry_count=None preserves on-disk value.
        )
        log.append(
            ThreadStatusChanged(
                schema_version=1,
                event_id=new_ulid(),
                ts=datetime.now(UTC),
                thread_id=event.thread_id,
                from_status="retrying",
                to_status="active",
                retry_count=new_meta.retry_count,
            )
        )
        logger.info(
            "thread %s recovered (%s) after attempt %d; retry_count preserved at %d",
            event.thread_id,
            reason,
            attempt,
            new_meta.retry_count,
        )

    def _compute_backoff(self, attempt: int) -> float:
        """Return the post-attempt backoff sleep, with optional jitter.

        ``retry_backoff_seconds[attempt]`` is the base value; jitter is
        symmetric around it (``base * (1 ± retry_jitter)``) and clamped
        at zero so ``retry_jitter > 1`` never produces negative sleep.

        NOTE on the ``max(0.0, ...)`` clamp: when ``ThreadDispatcher`` is
        constructed via :class:`MindwireSettings` (= production path),
        ``WatcherConfig.retry_jitter`` is validated to ``[0, 1]``
        (``Field(default=0.2, ge=0, le=1)``), which makes the clamp dead
        code — ``base + uniform(-base, base)`` is non-negative. The clamp
        remains as **defense-in-depth** for unit tests / future paths
        that instantiate the dispatcher directly with a ``retry_jitter``
        outside the config-validated range (= MindwireSettings bypass).
        Removing the clamp would silently produce negative sleeps in
        that case, so we keep it.
        """
        base = self._retry_backoff_seconds[attempt]
        if self._retry_jitter == 0:
            return base
        amount = base * self._retry_jitter
        return max(0.0, base + random.uniform(-amount, amount))


__all__ = ["ThreadDispatcher"]
