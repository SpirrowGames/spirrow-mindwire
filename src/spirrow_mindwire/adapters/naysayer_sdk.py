"""``NaysayerSdkAdapter`` — the independent naysayer as a first-class loop **agent**.

ADR-2026-06-04 (supersedes ADR-2026-06-03-17's relay/bundle design): the
governance gate that forced the Gemini naysayer surface to be *tool-less /
one-shot* has been lifted, so the naysayer can run as a proper agent — the same
:class:`~claude_agent_sdk.ClaudeSDKClient` runtime as the proposer/implementer,
just with a **different model backend**. Independence (ADR-05 §5) is preserved by
distribution: ``ANTHROPIC_BASE_URL`` points at the naysayer (Gemini) tier of the
Lexora Anthropic-Messages-compatible gateway, never ``api.anthropic.com``.

This superseded the bespoke ``scripts/design_review.py`` relay + context bundle
(both removed in the ADR-19 N-4 cleanup): the naysayer now participates in the
ordinary loop (watcher → dispatcher → adapter → gateway), so the chatroom
gather/post is handled by the loop infrastructure exactly as for the other roles
— no hand-built relay, no gatherer/relay session.

ADR-17 **D-1 is retained**: the 5-principles SOT (``spec/NAYSAYER_PRINCIPLES.md``)
is injected verbatim into the system prompt via the single
:func:`~spirrow_mindwire.naysayer.principles.build_preamble` entry point, so the
agent always reasons under the current, versioned principles.

``capabilities`` carries ``NAYSAYER_QUALIFIED`` (independent model → may fill the
naysayer slot) and omits ``EXECUTE_CODE`` (design-time review is advice, not repo
mutation — advisory, not a veto, ADR-17 D-5).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
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
from ..naysayer.adr_index import build_adr_index_block
from ..naysayer.principles import NAYSAYER_MODEL_TIER, build_preamble
from ..obligations import ObligationsManifest
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

_SHUTDOWN_STATES: frozenset[SessionState] = frozenset(
    {SessionState.HALTING, SessionState.HALTED, SessionState.FAILED}
)

_ENV_BASE_URL = "MINDWIRE_NAYSAYER_BASE_URL"

# The naysayer's role instructions; the 5-principles SOT is prepended verbatim by
# build_preamble() so the agent always reasons under the current principles (D-1).
# The verdict-constraint clause ("advisory, not a veto") has been MOVED to
# ``spec/obligations.yaml`` (OBL-VERDICT-CONSTRAINT, CLAUDE.md §N.4) and is
# injected here from the manifest passed by the composition root — deleted from
# this literal, restored to the rendered prompt through injection. Keeping the
# manifest as the sole owner is what lets the tests assert on the actual prompt
# the loop reads rather than a mirror.
_NAYSAYER_ROLE_PROMPT = """\
You are the independent naysayer in a Spirrow MindWire ChatRoom design thread. \
You run on a different model family from the proposer/implementer; your job is \
to apply the 5 principles above as an adversarial reviewer of the design under \
discussion. Each turn you receive the latest message in the thread; respond with \
your critique of it — explicitly endorse what is sound (principle 4), object with \
a concrete basis (principle 3), and never stay silent about a real concern \
(principle 5). Your entire response is posted verbatim to the thread as your \
reply, so reply directly with the critique — no preamble, no meta-commentary.
"""


class NaysayerSdkSpawnError(AdapterSpawnError):
    """``spawn`` failure for the naysayer SDK agent (§3.4)."""


class NaysayerSdkDeliveryError(AdapterDeliveryError):
    """``deliver_event`` failure for the naysayer SDK agent (§3.4)."""


class NaysayerSdkHaltError(AdapterHaltError):
    """``halt`` failure for the naysayer SDK agent (§3.4)."""


class NaysayerSdkHealthError(AdapterHealthError):
    """``health`` failure for the naysayer SDK agent (§3.4)."""


class _SdkClient(Protocol):
    """Structural view of the SDK session methods the adapter drives."""

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
    client: _SdkClient
    ctx: SpawnContext
    own_role: Role
    state: SessionState
    last_active_at: datetime
    error: ErrorInfo | None = None


def build_naysayer_system_prompt(
    *,
    obligations: ObligationsManifest,
    repo_root: Path | None = None,
) -> str:
    """Naysayer system prompt: 5-principles SOT (verbatim, D-1) + role instructions
    + injected obligations (OBL-VERDICT-CONSTRAINT) + the deterministic ADR index
    (N-2) + the conductor handoff protocol (PR-2b-1).

    The ``obligations`` manifest is passed by the composition root and injected
    verbatim (CLAUDE.md §N.4) — the naysayer verdict-constraint clause lives in
    that manifest, not in this module. Requiring the caller to pass it (no
    default) is deliberate: the adapter must never reach for a module-global path
    itself, so canary two-prime can assert on exactly the prompt the loop renders in
    production from a manifest the test picks.

    The ADR index is read from **MindWire's own** ``spec/adr_index.yaml`` and injected
    on every summon so the agent's worldview is not bounded by what the thread happens
    to cite — it cannot search for an ADR it does not know exists (ADR-19 N-2; the
    manifest is the complete in-repo derived view, replacing the retired §M /
    context-bundle source).

    ``repo_root`` therefore defaults to **this** repo and callers should leave it
    unset. It exists only so tests can point at a fixture manifest. Passing the
    *reviewed* repo silently disables the whole feature: that repo has no
    ``spec/adr_index.yaml``, ``load_adr_index`` fails open to ``()``, and every review
    runs with the "ADR index — UNAVAILABLE" block. That is exactly what happened —
    the adapter passed its ``cwd`` here, so the design-time naysayer had never once
    seen the index in the conductor path (found 2026-08-02 via a naysayer critique
    that reported it could not cross-check against the ADR set).

    The handoff-protocol block teaches the agent to end each critique with a ``NEXT:``
    line so the conductor can chain the loop (hand back to the proposer for a
    disposition, or to the human when the design is clean).
    """
    return (
        f"{build_preamble()}\n\n{_NAYSAYER_ROLE_PROMPT}\n\n"
        f"{obligations.render_role_obligations(Role.NAYSAYER)}\n\n"
        f"{build_adr_index_block(repo_root)}\n\n{build_handoff_protocol_block(Role.NAYSAYER)}"
    )


def _build_prompt(event: ChatroomEvent, own_role: Role) -> str:
    payload = event.payload
    return (
        f"You are acting as the {own_role.value} role in thread "
        f"{event.thread_ref.thread_id}.\n\n"
        f"New message from {payload.author}:\n\n{payload.body}\n\n"
        f"Reply to this message in your role."
    )


async def _drain_reply(client: _SdkClient) -> str:
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
        raise RuntimeError("SDK session ended without a ResultMessage")
    if getattr(final, "is_error", False):
        raise RuntimeError(getattr(final, "result", None) or "SDK session reported is_error")
    return "".join(chunks)


async def _shutdown(client: _SdkClient) -> None:
    await client.interrupt()
    await client.disconnect()


class NaysayerSdkAdapter:
    """RoleAdapter: the independent naysayer as a Gemini-backed Claude Agent SDK agent."""

    adapter_id: str = "naysayer-sdk"
    capabilities: frozenset[Capability] = frozenset(
        {Capability.READ_THREAD, Capability.POST_REPLY, Capability.NAYSAYER_QUALIFIED}
    )

    def __init__(
        self,
        *,
        cwd: Path,
        obligations: ObligationsManifest,
        inference_base_url: str | None = None,
        model: str | None = NAYSAYER_MODEL_TIER,
        system_prompt: str | None = None,
        allowed_tools: list[str] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        extra_env: dict[str, str] | None = None,
        client_factory: Callable[[Any], _SdkClient] | None = None,
    ) -> None:
        self._cwd = Path(cwd)
        # Independence (ADR-05 §5): inference MUST route to the naysayer (Gemini)
        # tier, never the SDK default (api.anthropic.com). Require an explicit URL.
        self._inference_base_url = (
            inference_base_url
            if inference_base_url is not None
            else os.environ.get(_ENV_BASE_URL, "")
        )
        self._model = model
        # Loop-readable obligations are injected — the manifest passed in is the
        # single source of truth (CLAUDE.md §N.4) and the adapter never reaches for
        # a module-global path itself. The verdict-constraint clause
        # (OBL-VERDICT-CONSTRAINT) lives in that manifest, not in _NAYSAYER_ROLE_PROMPT.
        self._obligations = obligations
        # No repo_root: the ADR manifest is MindWire's, not the reviewed repo's. Passing
        # ``self._cwd`` here is what disabled the N-2 index in the conductor path.
        self._system_prompt = (
            system_prompt
            if system_prompt is not None
            else build_naysayer_system_prompt(obligations=obligations)
        )
        self._allowed_tools = list(allowed_tools) if allowed_tools is not None else []
        self._mcp_servers = mcp_servers or {}
        self._extra_env = dict(extra_env or {})
        self._client_factory = client_factory or _default_client_factory
        self._sessions: dict[SessionHandle, _Session] = {}

    def _make_options(self) -> ClaudeAgentOptions:
        env = {"ANTHROPIC_BASE_URL": self._inference_base_url, **self._extra_env}
        kwargs: dict[str, Any] = {
            "cwd": self._cwd,
            "system_prompt": self._system_prompt,
            "tools": [],
            "allowed_tools": self._allowed_tools,
            "mcp_servers": self._mcp_servers,
            "env": env,
        }
        if self._model is not None:
            kwargs["model"] = self._model
        return ClaudeAgentOptions(**kwargs)

    async def spawn(
        self,
        thread_ref: ThreadRef,
        role: Role,
        ctx: SpawnContext,
    ) -> SessionHandle:
        if not self._inference_base_url:
            raise NaysayerSdkSpawnError(
                f"no inference base URL configured (set inference_base_url or {_ENV_BASE_URL}): "
                "the naysayer must route inference via the Lexora Gemini tier, never "
                "api.anthropic.com directly (ADR-05 §5 independence)"
            )
        try:
            client = self._client_factory(self._make_options())
            await client.connect()
        except Exception as exc:
            raise NaysayerSdkSpawnError(
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
            own_role=role,
            state=SessionState.IDLE,
            last_active_at=now,
        )
        return handle

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        session = self._sessions.get(handle)
        if session is None:
            raise NaysayerSdkDeliveryError(f"unknown session {handle.session_id}")
        if session.state in _SHUTDOWN_STATES:
            raise NaysayerSdkDeliveryError(
                f"session {handle.session_id} is {session.state.value}; cannot deliver"
            )
        if event.event_type is not EventType.NEW_MESSAGE:
            return
        payload = event.payload
        if payload.author == handle.instance_id:
            # instance self-filter: drop our own post echoed back (no self-reply loop).
            return

        session.state = SessionState.PROCESSING
        try:
            await session.client.query(_build_prompt(event, session.own_role))
            body = await _drain_reply(session.client)
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
            raise NaysayerSdkDeliveryError(
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
            return
        session.state = SessionState.HALTING
        try:
            await asyncio.wait_for(_shutdown(session.client), timeout=grace.total_seconds())
        except Exception as exc:
            session.state = SessionState.FAILED
            session.error = ErrorInfo(
                code="adapter.halt_failed",
                message=str(exc),
                raised_at=datetime.now(UTC),
            )
            raise NaysayerSdkHaltError(
                f"halt failed for session {handle.session_id}: {exc}"
            ) from exc
        session.state = SessionState.HALTED

    async def health(self, handle: SessionHandle) -> HealthStatus:
        session = self._sessions.get(handle)
        if session is None:
            raise NaysayerSdkHealthError(f"unknown session {handle.session_id}")
        return HealthStatus(
            state=session.state,
            last_active_at=session.last_active_at,
            error=session.error,
            details={"adapter_id": self.adapter_id, "session_id": handle.session_id},
        )


__all__ = [
    "NaysayerSdkAdapter",
    "NaysayerSdkDeliveryError",
    "NaysayerSdkHaltError",
    "NaysayerSdkHealthError",
    "NaysayerSdkSpawnError",
    "build_naysayer_system_prompt",
]
