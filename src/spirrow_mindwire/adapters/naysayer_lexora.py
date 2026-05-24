"""Stage 2 — ``NaysayerLexoraAdapter`` (ADR-06 §3.1 RoleAdapter, ADR-05 §5).

The **independent critic** of the three-role loop. It runs the naysayer
role through Lexora's ``model="naysayer"`` tier (DeepSeek V4-Flash), a
*different model family* from ``main`` / claude-code so the architecture
itself guarantees the naysayer's independence (ADR-05 §5). That guarantee
is mechanical, not advisory:
:attr:`capabilities` carries
:attr:`~spirrow_mindwire.value_objects.Capability.NAYSAYER_QUALIFIED`,
which the :class:`~spirrow_mindwire.dispatcher.registry.InMemoryAdapterRegistry`
requires for the naysayer slot and which
:class:`~spirrow_mindwire.adapters.claude_code_sdk.ClaudeCodeSdkAdapter`
deliberately omits. It does **not** carry ``EXECUTE_CODE``: Stage 2 is
advice-only / side-effect-free (the fork-2 staged-capability release).

Lifecycle (stateless HTTP, contrast with the persistent SDK subprocess of
``ClaudeCodeSdkAdapter``): Lexora is a stateless chat-completions gateway,
so each ``deliver_event`` is one independent completion carrying no
conversation history (per msg-215, multi-round memory is Phase 3). A
single :class:`~spirrow_mindwire.lexora.client.LexoraClient` (one httpx
pool) is **shared** across sessions and closed once at adapter teardown
via :meth:`aclose` — ``halt`` only marks the per-session record, it does
not tear down the shared transport.

Reasoning-model handling (msg-210 / msg-215): the reply is
``choices[0].message.content``; the sibling ``reasoning_content``
(deliberation) is captured for trace but never posted. An empty reply —
e.g. ``finish_reason="length"`` with the whole budget spent on
reasoning — is a **fail-loud** :class:`NaysayerLexoraDeliveryError`, not a
silent empty post. ``max_tokens`` defaults to 4096 (well above the
reasoning-model ``>=1500`` floor) and the timeout matches Lexora's 900s
backend.

Failures map to the §3.4 Port exception catalog via adapter-specific
subclasses (:class:`NaysayerLexoraSpawnError` etc.); the failure code is
stored on ``HealthStatus.error.code`` (``adapter.*`` namespace), never
duplicated into ``HealthStatus.details`` (I2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..exceptions import (
    AdapterDeliveryError,
    AdapterHaltError,
    AdapterHealthError,
    AdapterSpawnError,
)
from ..lexora.client import ChatMessage, LexoraChatClient, LexoraClient
from ..ports import SpawnContext
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
# halt() is an idempotent no-op and deliver_event is rejected.
_SHUTDOWN_STATES: frozenset[SessionState] = frozenset(
    {SessionState.HALTING, SessionState.HALTED, SessionState.FAILED}
)

_DEFAULT_MODEL = "naysayer"
_DEFAULT_MAX_TOKENS = 4096  # >= the reasoning-model 1500 floor, with content headroom
_DEFAULT_TIMEOUT_SECONDS = 900.0  # matches the Lexora backend timeout

# main order #2 (msg-215): the naysayer must disagree by default and
# critique by *quoting the specific passage* it objects to (citation-type
# adversarial review, not fabricated dissent).
_NAYSAYER_SYSTEM_PROMPT = """\
You are the independent naysayer in a Spirrow MindWire ChatRoom thread \
between multiple AI agents. You are a different model from the proposer, and \
your job is adversarial review: do NOT agree by default. Assume the proposal \
is flawed until proven otherwise and find the weaknesses.

For every objection you raise, quote the specific passage you are objecting \
to (verbatim), then explain the concrete flaw: a hidden assumption, a missing \
case, an unstated risk, a contradiction, or an unjustified leap. Do not \
fabricate problems and do not pad with generic caveats — every point must be \
anchored to a quoted span of the message under review. If, after a genuine \
search, you find no substantive flaw, say so plainly and name the single \
weakest remaining point.

Your entire response is posted verbatim to the thread as your reply, so reply \
directly with the critique — no preamble, no meta-commentary.
"""


class NaysayerLexoraSpawnError(AdapterSpawnError):
    """``spawn`` failure for the naysayer Lexora adapter (§3.4)."""


class NaysayerLexoraDeliveryError(AdapterDeliveryError):
    """``deliver_event`` failure for the naysayer Lexora adapter (§3.4)."""


class NaysayerLexoraHaltError(AdapterHaltError):
    """``halt`` failure for the naysayer Lexora adapter (§3.4)."""


class NaysayerLexoraHealthError(AdapterHealthError):
    """``health`` failure for the naysayer Lexora adapter (§3.4)."""


@dataclass
class _Session:
    """Per-``SessionHandle`` adapter state (keyed by handle identity, I2)."""

    ctx: SpawnContext
    own_role: Role
    state: SessionState
    last_active_at: datetime
    error: ErrorInfo | None = None


def _build_messages(event: ChatroomEvent, own_role: Role, system_prompt: str) -> list[ChatMessage]:
    payload = event.payload
    user = (
        f"You are acting as the {own_role.value} role in thread "
        f"{event.thread_ref.thread_id}.\n\n"
        f"The {payload.author} posted the following message. Critique it, "
        f"quoting the specific passages you object to:\n\n"
        f"---\n{payload.body}\n---"
    )
    return [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user),
    ]


class NaysayerLexoraAdapter:
    """RoleAdapter backed by Lexora's naysayer tier (ADR-06 §3.1, Stage 2).

    ``capabilities`` carries
    :attr:`~spirrow_mindwire.value_objects.Capability.NAYSAYER_QUALIFIED`
    (independent model → may fill the naysayer slot) and **omits**
    :attr:`~spirrow_mindwire.value_objects.Capability.EXECUTE_CODE`
    (Stage 2 is advice-only).
    """

    adapter_id: str = "naysayer-lexora"
    capabilities: frozenset[Capability] = frozenset(
        {Capability.READ_THREAD, Capability.POST_REPLY, Capability.NAYSAYER_QUALIFIED}
    )

    def __init__(
        self,
        *,
        url: str | None = None,
        model: str = _DEFAULT_MODEL,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        system_prompt: str = _NAYSAYER_SYSTEM_PROMPT,
        health_check_on_spawn: bool = False,
        client: LexoraChatClient | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt
        self._health_check_on_spawn = health_check_on_spawn
        # One shared client (httpx pool) across sessions; tests inject a fake.
        self._client: LexoraChatClient = (
            client if client is not None else LexoraClient(url, timeout_seconds=timeout_seconds)
        )
        self._sessions: dict[SessionHandle, _Session] = {}

    async def spawn(
        self,
        thread_ref: ThreadRef,
        role: Role,
        ctx: SpawnContext,
    ) -> SessionHandle:
        if self._health_check_on_spawn:
            # Optional liveness gate: a dead gateway fails the spawn loudly
            # rather than deferring the failure to the first deliver_event.
            try:
                await self._client.health()
            except Exception as exc:
                raise NaysayerLexoraSpawnError(
                    f"spawn failed for role {role.value} on thread "
                    f"{thread_ref.thread_id}: Lexora health check failed: {exc}"
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
            ctx=ctx,
            own_role=role,  # == ctx.own_role by dispatcher contract (Gap-2 (b))
            state=SessionState.IDLE,
            last_active_at=now,
        )
        return handle

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        session = self._sessions.get(handle)
        if session is None:
            raise NaysayerLexoraDeliveryError(f"unknown session {handle.session_id}")
        if session.state in _SHUTDOWN_STATES:
            raise NaysayerLexoraDeliveryError(
                f"session {handle.session_id} is {session.state.value}; cannot deliver"
            )
        if event.event_type is not EventType.NEW_MESSAGE:
            # Phase 1/2 handle NEW_MESSAGE only; other event types are no-ops.
            return
        payload = event.payload
        if payload.author == handle.instance_id:
            # instance self-filter (Gap-2 (b), I3 v2.2): drop our own post echoed
            # back — replies are now posted with author = instance_id (e.g.
            # "naysayer-1"), so the filter compares against our instance_id, not
            # the bare role. Defends against a dispatcher routing bug causing a
            # self-reply loop.
            return

        session.state = SessionState.PROCESSING
        try:
            completion = await self._client.chat_completion(
                model=self._model,
                messages=_build_messages(event, session.own_role, self._system_prompt),
                max_tokens=self._max_tokens,
            )
            # Reply is content; reasoning_content (deliberation) is never posted.
            body = (completion.content or "").strip()
            if not body:
                # Fail-loud: a reasoning model that spent the whole budget on
                # reasoning_content (finish_reason="length") yields no answer.
                # An empty post would silently break the critique loop.
                raise NaysayerLexoraDeliveryError(
                    f"naysayer returned empty content "
                    f"(finish_reason={completion.finish_reason!r}) for session "
                    f"{handle.session_id}; refusing to post an empty reply"
                )
            # on_reply is inside the try: a raising dispatcher callback must not
            # leave the session stuck in PROCESSING — it transitions to FAILED.
            await session.ctx.on_reply(
                ReplyDraft(
                    body=body,
                    reply_to_msg_id=payload.msg_id,
                    adapter_metadata={
                        "adapter_id": self.adapter_id,
                        "model": completion.model or self._model,
                        "finish_reason": completion.finish_reason,
                        "usage": completion.usage,
                    },
                )
            )
        except Exception as exc:
            session.state = SessionState.FAILED
            session.error = ErrorInfo(
                code="adapter.delivery_failed",
                message=str(exc),
                raised_at=datetime.now(UTC),
            )
            raise NaysayerLexoraDeliveryError(
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
        # Stateless transport: there is no per-session remote resource to tear
        # down (the shared httpx client is closed once via aclose, not here),
        # so the halt is a synchronous state transition. ``grace`` is accepted
        # for Port-signature parity but unused.
        session.state = SessionState.HALTED

    async def health(self, handle: SessionHandle) -> HealthStatus:
        session = self._sessions.get(handle)
        if session is None:
            raise NaysayerLexoraHealthError(f"unknown session {handle.session_id}")
        # health = session state + Lexora reachability (msg-214). The gateway
        # ping is best-effort observability (I2 details); a Lexora outage does
        # not make the *session* state undeterminable, so it is recorded in
        # details rather than raised — health() is exactly what you call when
        # things may be broken and must not itself explode.
        details: dict[str, Any] = {"adapter_id": self.adapter_id, "session_id": handle.session_id}
        try:
            lexora_health = await self._client.health()
        except Exception as exc:
            details["lexora_health"] = f"unreachable: {exc}"
        else:
            details["lexora_health"] = lexora_health.get("status", lexora_health)
        return HealthStatus(
            state=session.state,
            last_active_at=session.last_active_at,
            error=session.error,
            # observability only (I2): no exception code here — that lives in error.code.
            details=details,
        )

    async def aclose(self) -> None:
        """Close the shared Lexora client (adapter teardown).

        Not part of the :class:`~spirrow_mindwire.ports.RoleAdapter` Port —
        the shared httpx pool outlives individual sessions and is released
        once here when the adapter is retired (msg-214).
        """
        await self._client.aclose()


__all__ = [
    "NaysayerLexoraAdapter",
    "NaysayerLexoraDeliveryError",
    "NaysayerLexoraHaltError",
    "NaysayerLexoraHealthError",
    "NaysayerLexoraSpawnError",
]
