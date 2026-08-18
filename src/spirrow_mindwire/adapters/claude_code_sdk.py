"""T11 — ``ClaudeCodeSdkAdapter`` (ADR-2026-05-21-06 §3.1 RoleAdapter).

A stateful adapter that runs one role on one thread via a **persistent**
Claude Agent SDK session (:class:`claude_agent_sdk.ClaudeSDKClient`):
``spawn`` connects a client, ``deliver_event`` drives one query→reply
round-trip, ``halt`` disconnects, ``health`` reports the session state.

This is a **new** implementation per ADR-06 §6 — the Phase 0
``claude_code/`` package was a *stateless single-invoke* model
(``invoke_claude_code`` = one fresh ``query()`` per trigger). Only its
SDK glue is reused as knowledge (``ClaudeAgentOptions`` construction,
``tools=[]`` fail-closed, message-stream draining); the session
lifecycle is the ADR-06 §I8 model.

Reply mechanism (Phase 1, design choice — flagged for review): the
adapter wraps the SDK turn's final assistant text into a
:class:`~spirrow_mindwire.value_objects.ReplyDraft` and emits it via
``SpawnContext.on_reply``. A dedicated ``write_reply`` MCP tool (the
Phase 0 protocol) is deferred to Phase 2 if structured/explicit replies
are needed.

Failures map to the §3.4 Port exception catalog via adapter-specific
subclasses (:class:`ClaudeCodeSdkSpawnError` etc.); the failure code is
stored on ``HealthStatus.error.code`` (``adapter.*`` namespace, §3.4
Option (i)), never duplicated into ``HealthStatus.details`` (I2).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from ..conductor.handoff import build_handoff_protocol_block
from ..exceptions import (
    AdapterDeliveryError,
    AdapterHaltError,
    AdapterHealthError,
    AdapterSpawnError,
)
from ..ports import SpawnContext
from ..thread_context import build_turn_prompt
from ..ulid_util import new_ulid
from ..value_objects import (
    Capability,
    ChatroomEvent,
    ErrorInfo,
    EventType,
    HealthStatus,
    ReplyDraft,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

# A session that is halting or terminal (ADR-06 §4 I8). For these states
# halt() is an idempotent no-op, and deliver_event is rejected (no query()
# may race halt()'s interrupt/disconnect; I8 shutdown is one-way).
_SHUTDOWN_STATES: frozenset[SessionState] = frozenset(
    {SessionState.HALTING, SessionState.HALTED, SessionState.FAILED}
)

_BASE_SYSTEM_PROMPT = """\
You are an AI agent participating in a Spirrow MindWire ChatRoom thread \
between multiple agents. Each turn you receive the latest message in the \
thread together with the role you are playing. Respond to that message in \
your assigned role. Your entire response is posted verbatim to the thread \
as your reply, so reply directly with the message body — no preamble, no \
tool calls, no meta-commentary.
"""

# In the Stage 3 loop this adapter plays the proposer (the read-only Stage3ProposerAdapter), so it
# carries the proposer handoff guidance. The conductor reads the trailing NEXT: line to chain the
# design loop (PR-2b-1); the block is advisory — the conductor's routing guards are the enforcement.
_DEFAULT_SYSTEM_PROMPT = f"{_BASE_SYSTEM_PROMPT}\n{build_handoff_protocol_block(Role.PROPOSER)}"


class _SdkClient(Protocol):
    """Structural view of the SDK session methods the adapter drives.

    Satisfied by :class:`claude_agent_sdk.ClaudeSDKClient`; tests inject a
    fake with the same shape via ``client_factory``.
    """

    async def connect(self) -> None: ...

    async def query(self, prompt: str) -> None: ...

    def receive_response(self) -> AsyncIterator[Any]: ...

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


def _default_client_factory(options: Any) -> _SdkClient:
    client: _SdkClient = ClaudeSDKClient(options=options)
    return client


@dataclass
class _Session:
    """Per-``SessionHandle`` adapter state (keyed by handle identity, I2)."""

    client: _SdkClient
    ctx: SpawnContext
    own_role: Role
    state: SessionState
    last_active_at: datetime
    # The exact ``ClaudeAgentOptions`` instance passed to the SDK client on
    # spawn — kept so :meth:`ClaudeCodeSdkAdapter.source_marker_options` can
    # hand the harness that same object (msg-834 §2 (a)). ``Any`` typing so
    # the dataclass file need not import the SDK type where it is already
    # imported at module scope; the runtime value is a real
    # ``ClaudeAgentOptions``.
    options: Any = None
    error: ErrorInfo | None = None


class ClaudeCodeSdkSpawnError(AdapterSpawnError):
    """``spawn`` failure for the Claude Code SDK adapter (§3.4)."""


class ClaudeCodeSdkDeliveryError(AdapterDeliveryError):
    """``deliver_event`` failure for the Claude Code SDK adapter (§3.4)."""


class ClaudeCodeSdkHaltError(AdapterHaltError):
    """``halt`` failure for the Claude Code SDK adapter (§3.4)."""


class ClaudeCodeSdkHealthError(AdapterHealthError):
    """``health`` failure for the Claude Code SDK adapter (§3.4)."""


def _build_prompt(event: ChatroomEvent, own_role: Role) -> str:
    return build_turn_prompt(event, own_role, "Reply to this message in your role.")


async def _drain_reply(client: _SdkClient) -> str:
    """Drain one SDK response, returning the concatenated assistant text.

    Raises ``RuntimeError`` if the SDK reports ``is_error`` on its
    ``ResultMessage`` (mapped to a Port exception by the caller).
    """
    chunks: list[str] = []
    final: Any = None
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
        elif isinstance(msg, ResultMessage):
            final = msg
    if final is None:
        # Incomplete / aborted turn — treat as a protocol error rather than a
        # silent empty reply, so deliver_event can mark the session FAILED.
        raise RuntimeError("SDK session ended without a ResultMessage")
    if getattr(final, "is_error", False):
        raise RuntimeError(getattr(final, "result", None) or "SDK session reported is_error")
    return "".join(chunks)


async def _shutdown(client: _SdkClient) -> None:
    """Interrupt then disconnect an SDK session (bounded by ``grace`` in halt)."""
    await client.interrupt()
    await client.disconnect()


class ClaudeCodeSdkAdapter:
    """RoleAdapter backed by the Claude Agent SDK (ADR-06 §3.1, T11).

    ``capabilities`` deliberately **omits**
    :attr:`~spirrow_mindwire.value_objects.Capability.NAYSAYER_QUALIFIED`:
    claude-code is the same model family as ``main``, so the architecture
    must never assign it the naysayer slot (ADR-05 §5 independence
    enforcement, surfaced through ``AdapterRegistry.qualified_for`` in T13).
    """

    adapter_id: str = "claude-code-sdk"
    capabilities: frozenset[Capability] = frozenset(
        {Capability.READ_THREAD, Capability.POST_REPLY, Capability.EXECUTE_CODE}
    )

    def __init__(
        self,
        *,
        cwd: Path,
        system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
        builtin_tools: Sequence[str] = (),
        allowed_tools: list[str] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        client_factory: Callable[[Any], _SdkClient] | None = None,
    ) -> None:
        self._cwd = cwd
        self._system_prompt = system_prompt
        self._builtin_tools = list(builtin_tools)
        self._allowed_tools = allowed_tools or []
        self._mcp_servers = mcp_servers or {}
        self._client_factory = client_factory or _default_client_factory
        self._sessions: dict[SessionHandle, _Session] = {}

    async def spawn(
        self,
        thread_ref: ThreadRef,
        role: Role,
        ctx: SpawnContext,
    ) -> SessionHandle:
        options = ClaudeAgentOptions(
            cwd=self._cwd,
            system_prompt=self._system_prompt,
            # ``tools=[]`` DISABLES every built-in (SDK 0.1.77 -> ``--tools ""``).
            # It is not "expose only what allowed_tools names" — that reading is
            # what left the implementer with no hands until T37 #1, and it is why
            # a proposer constructed with the default here can read nothing at
            # all. The composition root decides what a role may see; the default
            # stays empty so text-only remains text-only unless asked otherwise.
            tools=self._builtin_tools,
            allowed_tools=self._allowed_tools,
            mcp_servers=self._mcp_servers,
        )
        try:
            client = self._client_factory(options)
            await client.connect()
        except Exception as exc:
            raise ClaudeCodeSdkSpawnError(
                f"spawn failed for role {role.value} on thread {thread_ref.thread_id}: {exc}"
            ) from exc

        now = datetime.now(UTC)
        handle = SessionHandle(
            session_id=new_ulid(),
            instance_id=ctx.own_instance_id,
            adapter_id=self.adapter_id,
            thread_ref=thread_ref,
            role=role,
            started_at=now,
        )
        self._sessions[handle] = _Session(
            client=client,
            ctx=ctx,
            own_role=role,  # == ctx.own_role by dispatcher contract (Gap-2 (b))
            state=SessionState.IDLE,
            last_active_at=now,
            # Store the same ``ClaudeAgentOptions`` object handed to the SDK
            # so the harness can derive the source marker from it on every
            # reply without asking the agent to declare it (D3 / msg-805).
            options=options,
        )
        return handle

    def source_marker_options(self, handle: SessionHandle) -> Any:
        """Return the ``ClaudeAgentOptions`` for ``handle``, or ``None`` if unknown.

        Public so the dispatcher can retrieve the exact options object the
        SDK client was spawned with (msg-834 §2 (a)) and hand it to
        :func:`spirrow_mindwire.source_marker.render_source_marker`. This
        getter is the seam that lets **this** file — the adapter — never
        import the marker builder (msg-834 §2 (c)): the adapter exposes the
        options, and the dispatcher (which does import the builder) reads
        them through the port.
        """
        session = self._sessions.get(handle)
        return None if session is None else session.options

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        session = self._sessions.get(handle)
        if session is None:
            raise ClaudeCodeSdkDeliveryError(f"unknown session {handle.session_id}")
        if session.state in _SHUTDOWN_STATES:
            # No delivery to a halting or terminal session (avoids a query()
            # racing halt()'s interrupt/disconnect; I8 shutdown is one-way).
            raise ClaudeCodeSdkDeliveryError(
                f"session {handle.session_id} is {session.state.value}; cannot deliver"
            )
        if event.event_type is not EventType.NEW_MESSAGE:
            # Phase 1 handles NEW_MESSAGE only; other event types are no-ops here.
            return
        payload = event.payload
        if payload.author == handle.instance_id:
            # instance self-filter (Gap-2 (b), I3 v2.2): drop our own post echoed
            # back — replies are now posted with author = instance_id (e.g.
            # "proposer-1"), so the filter compares against our instance_id, not
            # the bare role. Defends against a dispatcher routing bug causing a
            # self-reply loop.
            return

        session.state = SessionState.PROCESSING
        try:
            await session.client.query(_build_prompt(event, session.own_role))
            body = await _drain_reply(session.client)
            # on_reply is inside the try: a raising dispatcher callback must not
            # leave the session stuck in PROCESSING — it transitions to FAILED.
            await session.ctx.on_reply(
                ReplyDraft(
                    body=body,
                    reply_to_msg_id=payload.msg_id,
                    adapter_metadata={"adapter_id": self.adapter_id},
                )
            )
        except Exception as exc:
            session.state = SessionState.FAILED
            session.error = ErrorInfo(
                code="adapter.delivery_failed",
                message=str(exc),
                raised_at=datetime.now(UTC),
            )
            raise ClaudeCodeSdkDeliveryError(
                f"deliver_event failed for session {handle.session_id}: {exc}"
            ) from exc

        session.last_active_at = datetime.now(UTC)
        session.state = SessionState.IDLE

    async def halt(
        self,
        handle: SessionHandle,
        *,
        grace: timedelta = timedelta(seconds=5),
    ) -> None:
        session = self._sessions.get(handle)
        if session is None or session.state in _SHUTDOWN_STATES:
            # Idempotent no-op on unknown / halting / terminal (I8).
            return
        session.state = SessionState.HALTING
        try:
            # ``grace`` bounds the interrupt+disconnect shutdown; a hang past
            # the budget (asyncio.TimeoutError) fails the halt rather than
            # blocking indefinitely.
            await asyncio.wait_for(_shutdown(session.client), timeout=grace.total_seconds())
        except Exception as exc:
            session.state = SessionState.FAILED
            session.error = ErrorInfo(
                code="adapter.halt_failed",
                message=str(exc),
                raised_at=datetime.now(UTC),
            )
            raise ClaudeCodeSdkHaltError(
                f"halt failed for session {handle.session_id}: {exc}"
            ) from exc
        session.state = SessionState.HALTED

    async def health(self, handle: SessionHandle) -> HealthStatus:
        session = self._sessions.get(handle)
        if session is None:
            raise ClaudeCodeSdkHealthError(f"unknown session {handle.session_id}")
        return HealthStatus(
            state=session.state,
            last_active_at=session.last_active_at,
            error=session.error,
            # observability only (I2): no exception code here — that lives in error.code.
            details={"adapter_id": self.adapter_id, "session_id": handle.session_id},
        )


__all__ = [
    "ClaudeCodeSdkAdapter",
    "ClaudeCodeSdkDeliveryError",
    "ClaudeCodeSdkHaltError",
    "ClaudeCodeSdkHealthError",
    "ClaudeCodeSdkSpawnError",
]
