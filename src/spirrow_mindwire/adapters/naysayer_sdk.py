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
from collections.abc import AsyncIterator, Awaitable, Callable
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
from ..naysayer.preflight import PreflightError, attest_backend
from ..naysayer.principles import (
    NAYSAYER_EXPECTED_BACKEND,
    NAYSAYER_MODEL_TIER,
    build_preamble,
)
from ..obligations import ObligationsManifest
from ..ports import SpawnContext
from ..thread_context import build_turn_prompt
from ..ulid_util import new_ulid
from ..value_objects import (
    AttestationRecord,
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
# ``spec/process/obligations.yaml`` (OBL-VERDICT-CONSTRAINT, CLAUDE.md §N →
# spec/process/README.md) and is injected here from the manifest passed by the
# composition root — deleted from this literal, restored to the rendered prompt
# through injection. Keeping the manifest as the sole owner is what lets the
# tests assert on the actual prompt the loop reads rather than a mirror.
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
    # The exact ``ClaudeAgentOptions`` handed to the SDK on spawn — kept so
    # :meth:`NaysayerSdkAdapter.source_marker_options` can hand the same
    # object to the harness (msg-834 §2 (a)). ``Any`` typing on the field so
    # the dataclass loads without touching the SDK type here (already
    # imported at module scope for the constructor).
    options: Any = None
    # The preflight OBSERVATION made when this session was spawned (P-2). Kept
    # per-session, alongside ``options``, because the two are the pair the
    # dispatcher renders: what the session was configured to do, and what the
    # gateway's accounting said actually happened when we tried it once.
    attestation: AttestationRecord | None = None
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
    verbatim (CLAUDE.md §N → spec/process/README.md → obligations.yaml) — the
    naysayer verdict-constraint clause lives in that manifest, not in this
    module. Requiring the caller to pass it (no default) is deliberate: the
    adapter must never reach for a module-global path itself, so canary two-prime
    can assert on exactly the prompt the loop renders in production from a
    manifest the test picks.

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
    return build_turn_prompt(event, own_role, "Reply to this message in your role.")


# Session facts retained from the SDK ``ResultMessage`` (P-1c). Prefixed
# ``sdk_`` in ``adapter_metadata`` so their provenance — the SDK result object,
# not the model's prose — is legible at the point of use.
_RETAINED_RESULT_FIELDS: tuple[str, ...] = ("session_id", "duration_ms", "num_turns")


def _session_facts(result: Any) -> dict[str, Any]:
    """Extract the retained session facts from a ``ResultMessage`` (P-1c).

    **★ ``model`` is deliberately excluded, and that exclusion is the point.**
    Tier-C msg-954 §3: "``.model`` は tier のエコーなので provenance に使わ
    ない". Lexora answers an Anthropic-compatible request by echoing back the
    *tier alias* (``"naysayer"``), never the concrete backend model — measured
    in msg-950 §2. Retaining it would drop a value that LOOKS like provenance
    into the event log's ``model_id`` field
    (:func:`~spirrow_mindwire.dispatcher.event_log.reply_sent_event` reads that
    key), manufacturing precisely the overclaim this arc exists to remove.

    So: the value of P-1c is **operational observability** — how long the turn
    took, which SDK session it was, how many turns it burned. It is not, and
    must not be presented as, evidence of which distribution answered. That
    evidence can only come from the server side (P-2's accounting-row read-back
    / P-5's per-request streaming record).
    """
    facts: dict[str, Any] = {}
    for name in _RETAINED_RESULT_FIELDS:
        value = getattr(result, name, None)
        if value is not None:
            facts[f"sdk_{name}"] = value
    return facts


async def _drain_reply(client: _SdkClient) -> tuple[str, Any]:
    """Drain one SDK response, returning ``(text, ResultMessage)``.

    P-1c (msg-953 §2 / Tier-C msg-954 §3): this used to read ``is_error`` off
    the ``ResultMessage`` and then **throw the object away**, so a naysayer
    turn left no ``session_id``, no duration, no turn count — nothing an
    operator could correlate against anything afterwards. Returning it lets
    :meth:`NaysayerSdkAdapter.deliver_event` put those facts on the reply's
    ``adapter_metadata`` (and thus into the ``reply.sent`` event log).
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
        raise RuntimeError("SDK session ended without a ResultMessage")
    if getattr(final, "is_error", False):
        raise RuntimeError(getattr(final, "result", None) or "SDK session reported is_error")
    return "".join(chunks), final


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
        expected_backend: str = NAYSAYER_EXPECTED_BACKEND,
        preflight: Callable[[], Awaitable[AttestationRecord]] | None = None,
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
        # single source of truth (CLAUDE.md §N → spec/process/README.md) and the
        # adapter never reaches for a module-global path itself. The verdict-
        # constraint clause (OBL-VERDICT-CONSTRAINT) lives in that manifest, not
        # in _NAYSAYER_ROLE_PROMPT.
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
        self._expected_backend = expected_backend
        # Injectable so tests never touch the network. Unset, the default runs
        # the real probe against **this adapter's own** inference base URL — not
        # ``MINDWIRE_LEXORA_URL``, which is a different variable pointing at a
        # possibly different host. Attesting a host the session will not use
        # would be an attestation of the wrong thing, stated with the same
        # confidence as a true one.
        self._preflight = preflight
        self._sessions: dict[SessionHandle, _Session] = {}

    async def _run_preflight(self) -> AttestationRecord:
        if self._preflight is not None:
            return await self._preflight()
        if not self._model:
            # Nothing to attest: with no model kwarg the session does not name a
            # tier, so there is no tier→backend resolution to observe. Refusing
            # is the honest answer; probing with an empty model would fail with
            # a confusing gateway error and imply the check ran.
            raise PreflightError(
                "cannot attest a session that pins no model tier (model=None): "
                "there is no tier→backend resolution to observe"
            )
        return await attest_backend(
            base_url=self._inference_base_url,
            tier=self._model,
            expected=self._expected_backend,
        )

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
        # P-2 (msg-953 §3 / Tier-C msg-954 §3 / msg-965). Until now the check
        # above was the ONLY thing standing behind ``docs/deploy.md:73``'s claim
        # that the naysayer "refuses to spawn" without its independent route:
        # a non-empty string was accepted as proof that the string pointed at
        # Gemini. This asks the gateway instead — one non-streaming probe, then
        # its own accounting row read back — and refuses the spawn if the tier
        # did not resolve to the expected backend.
        #
        # Ordered BEFORE the SDK client is constructed and connected. The client
        # is a real subprocess in production, and a session refused after
        # connecting is a session nobody will ever ``halt`` — one leaked
        # subprocess per refused spawn.
        #
        # A transient fault does not reach this fail-closed path on the first
        # blip. ``attest_backend`` retries transport failures up to
        # ``PREFLIGHT_ATTEMPTS`` (= 3) with a fresh baseline each time, sized
        # from the T36 learning that Gemini 502s are frequent and self-resolving
        # (msg-954 §3; ``test_transient_http_failure_is_retried_and_can_succeed``,
        # ``test_attempts_are_bounded_at_three``). A backend MISMATCH is the one
        # thing never retried — it is a verdict, not a transient, and re-rolling
        # it until the gateway says what we wanted is the same as not checking.
        # So what propagates from here is a sustained outage or a wrong route,
        # not a two-second 502.
        #
        # Fail-closed here means the turn is not posted. It does not mean the
        # loop parks politely: the error propagates out of ``Conductor.run()``
        # and the daemon exits non-zero (verified by reading
        # ``loop_runner.run_conductor`` — the call is not wrapped).
        #
        # That exit is DECLARED, not silent. ``deploy/run-conductor-scheduled.ps1``
        # treats any non-zero exit as a candidate failure: it writes a quarantine
        # record and pushes a Discord notification, then continues the sweep to
        # the next candidate (``if ($code -ne 0)`` → ``New-QuarantineRecord``,
        # re-read 2026-08-13; the wrapper gained this on 2026-08-11 in
        # T-sweep-failure-isolation). So downstream threads do not starve behind
        # a failing one, and a human learns about it without reading logs — which
        # is what makes fail-loud the right choice here rather than a degradation
        # that would let an un-attested naysayer post (Tier-C msg-970 §2).
        #
        # The quarantine holds until a human clears it (``Clear-Quarantine.ps1``);
        # ticks are 5 minutes apart, so a transient outage that resolves itself
        # still needs that clear.
        try:
            attestation = await self._run_preflight()
        except Exception as exc:
            raise NaysayerSdkSpawnError(
                f"preflight attestation failed for role {role.value} on thread "
                f"{thread_ref.thread_id}; refusing to spawn an unattested naysayer "
                f"session: {exc}"
            ) from exc
        options = self._make_options()
        try:
            client = self._client_factory(options)
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
            # Retain the exact ``ClaudeAgentOptions`` for the harness marker
            # (msg-805 D3 / msg-834 §2 (a)) — never re-read, never re-declared.
            options=options,
            attestation=attestation,
        )
        return handle

    def attestation_record(self, handle: SessionHandle) -> AttestationRecord | None:
        """Return the attestation for ``handle``'s current turn, or ``None`` if unknown.

        The duck-typed seam P-1 opened in
        :meth:`spirrow_mindwire.dispatcher.core.Dispatcher._handle_reply`; this
        is the first adapter to define it, so the ``attest:`` line starts
        appearing on naysayer posts with this change.

        **What the line says, exactly.** Since D-2
        (``T-dispatched-turn-gets-one-message``) the observation is re-taken at the
        top of every :meth:`deliver_event`, so the record read here — the dispatcher
        reads it after ``on_reply``, which fires synchronously inside that same
        ``deliver_event`` — is the one probed for **this** turn. Two consecutive
        verdicts therefore carry two different ``probe`` values, and a reader who
        finds the same probe on two posts is looking at a bug rather than at the
        design.

        It previously said the opposite, and the correction is the point of D-2: the
        probe used to be taken once at spawn and re-stamped on every post of the
        session (measured: 39 attested posts, 24 distinct probes, worst case one
        probe across 5 posts spanning 8m59s), which meant a stamp could not
        distinguish "each turn was independently attested" from "one turn was, and
        the rest inherited its evidence".

        **Still not** a claim about the streaming turn itself. The probe is a
        separate non-streaming request made immediately before it, so it evidences
        the route and the tier→backend resolution at that instant, not the tokens
        that came back. ``at=`` is the observation's timestamp precisely so a reader
        can check the distance. Closing that last gap needs streaming request records
        on the gateway side (P-5, different repo, not done).
        """
        session = self._sessions.get(handle)
        return None if session is None else session.attestation

    def source_marker_options(self, handle: SessionHandle) -> Any:
        """Return the ``ClaudeAgentOptions`` for ``handle``, or ``None`` if unknown.

        Public seam so the dispatcher can derive the source marker without
        this file importing :mod:`spirrow_mindwire.source_marker`
        (msg-834 §2 (c)).
        """
        session = self._sessions.get(handle)
        return None if session is None else session.options

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
        # D-2 (T-dispatched-turn-gets-one-message). Re-attest for THIS turn before
        # asking the model anything. Until now the probe was taken once, at spawn,
        # and re-stamped on every post of that session — so the loop measured
        # independence per *process*, not per verdict, and a stamp said nothing
        # about the post it sat on beyond "some earlier request on this session
        # resolved to gemini". Measured on the live corpus (2026-08-16): 39
        # attested posts carried 24 distinct probes; one probe was stamped on 5
        # posts spanning 8m59s, so 38% of attested verdicts were evidenced by an
        # observation made for a different verdict.
        #
        # What this buys, exactly: the stamp now records an observation taken
        # immediately before this turn's inference, on this session's own route.
        # What it still does NOT buy: proof that the streaming turn itself resolved
        # to that backend. The probe is a separate non-streaming request; closing
        # that gap needs streaming request records on the gateway side (P-5,
        # different repo, not done). "Different probe per post" is a real strengthening
        # and is not the same claim as "this post's backend".
        #
        # Fail-closed, same as spawn: a failed re-attest raises out of deliver_event
        # BEFORE ``on_reply``, so no unattested verdict is posted. The turn is lost
        # loudly (conductor run fails → daemon non-zero → quarantine record +
        # Discord), which is the behaviour spawn already had for the same fault.
        try:
            session.attestation = await self._run_preflight()
        except Exception as exc:
            session.state = SessionState.FAILED
            session.error = ErrorInfo(
                code="adapter.delivery_failed",
                message=str(exc),
                raised_at=datetime.now(UTC),
            )
            raise NaysayerSdkDeliveryError(
                f"per-turn preflight attestation failed for session {handle.session_id}; "
                f"refusing to post an unattested naysayer verdict: {exc}"
            ) from exc
        try:
            await session.client.query(_build_prompt(event, session.own_role))
            body, result = await _drain_reply(session.client)
            await session.ctx.on_reply(
                ReplyDraft(
                    body=body,
                    reply_to_msg_id=payload.msg_id,
                    adapter_metadata={
                        "adapter_id": self.adapter_id,
                        # P-1c: session facts, NOT provenance. See _session_facts.
                        **_session_facts(result),
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
