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

**Environment variable — ``MINDWIRE_NAYSAYER_BASE_URL``** (single source of
validation, T-public-repo-carries-real-infra-values PR #296 pr-gate advisory
msg-3484 + msg-3516). This adapter is the ONE place that validates the
variable; the deploy wrappers deliberately do NOT duplicate the check.

*When*: :meth:`NaysayerSdkAdapter.__init__` **reads** the env into
``self._inference_base_url`` but does **not** validate. The check happens at
:meth:`NaysayerSdkAdapter.spawn` (see the guard on ``self._inference_base_url``
that raises :class:`NaysayerSdkSpawnError`), which runs when a naysayer session
is actually summoned. That is intentional — it lets a daemon that never needs
a naysayer (e.g. an operator-lane conductor working purely on the operator
thread) start without a value.

*Process-exit timing (per ``--mode``)*: the exit story diverges by mode
because different call sites treat spawn failures differently. Producers
forking the deploy wrappers should read this before adding their own preflight
(the wrappers document only the mode they launch and point here for the rest):

  - ``--mode conductor`` (launched by ``deploy/run-conductor.ps1``):
    :meth:`Conductor.run` awaits ``spawn_instance`` sequentially — no
    ``asyncio.create_task`` and no per-turn task-exception swallow — so a
    :class:`NaysayerSdkSpawnError` at the first naysayer summon propagates
    through ``run_conductor`` (unwrapped except a ``finally`` for teardown),
    out to ``asyncio.run``, and the process exits non-zero via the default
    excepthook. Operator-visible: at first naysayer summon on a design thread.
    If the conductor's task_thread hits a stop condition without ever summoning
    a naysayer, an unset value is not surfaced by that run.
  - ``--mode watcher``: :meth:`ChatroomWatcher.run` wraps each ``poll_once()``
    in ``except Exception: logger.exception("chatroom poll failed; continuing")``
    (see ``src/spirrow_mindwire/magickit/watcher.py``). Under that swallow the
    daemon does NOT exit — a :class:`NaysayerSdkSpawnError` from an unset value
    recurs every ``poll_interval_seconds`` in the log while process supervisors
    see a healthy daemon. Making watcher-mode fail loud on this variable would
    require either narrowing the watcher's ``except`` to exclude spawn-time
    config faults or an explicit startup-time validation pass. Out of scope
    for this adapter; called out so the divergence is documented in ONE place.

The :meth:`spawn` body itself (below) contains the older, more detailed
account of ``_run_preflight`` failure — that account is about attestation
outcomes (backend mismatch vs transient 502) rather than env-config faults;
the two accounts are complementary, not duplicative.
"""

from __future__ import annotations

import asyncio
import logging
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
from ._sdk_result import (
    SdkIsErrorSignal,
    capture_is_error_detail,
    emit_sdk_error_marker,
)
from ._session_isolation import session_isolation_kwargs

logger = logging.getLogger(__name__)

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
    """Per-adapter session record for the naysayer.

    **1-turn-1-session lifecycle**
    (T-naysayer-sdk-session-carries-the-whole-conversation-every-turn):
    ``client`` is ``None`` *between* turns; a fresh :class:`_SdkClient`
    (real subprocess in production) is constructed inside
    :meth:`NaysayerSdkAdapter.deliver_event`, published to this field
    under :attr:`client_lock`, and cleared to ``None`` again in the
    same call's ``finally`` block. The SDK subprocess never outlives
    one turn, so it cannot accumulate conversation history in the SDK's
    own session buffer — the O(n²) input growth that motivated this
    design lived exactly in that buffer.

    Consequence for future tool/MCP use: because each turn gets a fresh
    subprocess, the SDK session never carries tool-execution memory
    across turns. If a subsequent design wires tools onto the naysayer,
    any information that must survive turn boundaries has to be
    re-supplied through the rendered prompt (as the ADR index already is,
    ADR-19 N-2), not left in the SDK's tool-result cache. Losing this
    property in exchange for cross-turn tool memory is a real trade the
    caller must state — the docstring exists so the trade is not made
    silently by "just holding the client open".

    **Halt-during-turn concurrency** (PR-gate feedback, pr-review pass on
    PR-338 @ e26288a — "``halt`` no longer interrupts active generations"):
    :meth:`NaysayerSdkAdapter.halt` must be able to interrupt a running
    per-turn client, not just flip a state flag while the subprocess
    keeps burning tokens. The publication is therefore *stealable* under
    :attr:`client_lock`: whichever coroutine (halt or deliver_event's
    finally) reads ``client`` and swaps it to ``None`` under the lock
    *owns* the shutdown of that client. The other coroutine finds
    ``None`` and knows there is nothing left to shut down, so the two
    paths cannot double-shutdown the same subprocess.
    """

    # The live per-turn SDK client. ``None`` between turns AND after the
    # owner-swap in :meth:`deliver_event`'s ``finally`` / :meth:`halt`.
    # Read-and-swap under :attr:`client_lock` — see class docstring for the
    # steal-semantics contract with :meth:`NaysayerSdkAdapter.halt`.
    client: _SdkClient | None
    # Serialises the owner-swap on :attr:`client` between :meth:`deliver_event`'s
    # ``finally`` block and :meth:`NaysayerSdkAdapter.halt`. The lock only
    # covers the swap itself — the actual ``_shutdown`` runs *outside* the
    # lock so a slow ``interrupt``/``disconnect`` never blocks the other
    # party's swap read (which is what would let a second shutdown attempt
    # sneak in against a half-torn-down subprocess).
    client_lock: asyncio.Lock
    ctx: SpawnContext
    own_role: Role
    state: SessionState
    last_active_at: datetime
    # The exact ``ClaudeAgentOptions`` handed to the SDK on every turn — kept
    # so :meth:`NaysayerSdkAdapter.source_marker_options` can hand the same
    # object to the harness (msg-834 §2 (a)). ``Any`` typing on the field so
    # the dataclass loads without touching the SDK type here (already
    # imported at module scope for the constructor).
    options: Any = None
    # The preflight OBSERVATION for the CURRENT turn (D-2 per-verdict). Kept
    # per-session, alongside ``options``, because the two are the pair the
    # dispatcher renders: what the session was configured to do, and what the
    # gateway's accounting said actually happened when we tried it, for THIS
    # turn. Set at the top of :meth:`deliver_event`; the ``spawn``-time dry-run
    # preflight is intentionally discarded (see :meth:`spawn`).
    attestation: AttestationRecord | None = None
    error: ErrorInfo | None = None
    # Set (under :attr:`client_lock`) while :meth:`NaysayerSdkAdapter.deliver_event`'s
    # finally owns an in-flight shutdown of the per-turn client; the event fires
    # when that bounded shutdown has finished (either way). A :meth:`halt` that
    # finds ``client is None`` but ``releasing`` set waits on it rather than
    # reporting a clean halt over a subprocess still being torn down.
    releasing: asyncio.Event | None = None


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

    **Guardrail for future tool/MCP use** (recorded here so it cannot be
    quietly dropped by a later change,
    T-naysayer-sdk-session-carries-the-whole-conversation-every-turn): the
    adapter now runs one SDK subprocess per turn, so the SDK-side session
    never carries tool-execution memory across turns. The naysayer currently
    ships with ``tools=[]`` and no MCP servers (see
    :class:`NaysayerSdkAdapter.__init__`), so this has no runtime effect
    today; but if a future design wires tools onto the naysayer, any state
    that must survive turn boundaries has to be re-supplied through the
    prompt (as the ADR index already is via N-2), not left in the SDK's
    tool cache. Re-fetching per turn is the trade for having bounded
    input-token growth.
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

    **★ ``sdk_session_id`` reads differently since the 1-turn-1-session change**
    (T-naysayer-sdk-session-carries-the-whole-conversation-every-turn): each
    naysayer turn now spawns a fresh :class:`ClaudeSDKClient`, so this value is
    the per-turn subprocess's session ID — it CHANGES on every reply, by
    design. Two consecutive naysayer posts sharing an ``sdk_session_id`` would
    now be a bug (matching the D-2 rule that already held for the per-verdict
    attestation ``probe`` line). ``sdk_num_turns`` is likewise always ``1`` for
    the same reason. To correlate posts belonging to the same adapter-level
    session, read ``handle.session_id`` — the ULID assigned in :meth:`spawn`,
    which persists across all turns of a spawn.
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
        # Same structured capture as claude_code_sdk's drain
        # (T-sdk-is-error-loses-the-reason). Fixing only one adapter would let
        # naysayer failures — the most expensive kind to lose because they burn
        # the paid Lexora/Gemini tier — continue disappearing behind the
        # pre-change constant string.
        detail = capture_is_error_detail(final)
        emit_sdk_error_marker(detail)
        raise SdkIsErrorSignal(detail)
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
        shutdown_grace: timedelta = timedelta(seconds=5),
    ) -> None:
        self._cwd = Path(cwd)
        # Upper bound on the per-turn client shutdown that deliver_event's
        # finally runs itself (halt's own shutdown is bounded by its ``grace``
        # argument). Defaults to halt's default grace.
        self._shutdown_grace = shutdown_grace
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
            # ``tools: []`` disables the built-ins and says nothing about MCP; these
            # keep host connectors out. See ``_session_isolation``.
            **session_isolation_kwargs(),
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
        # **Dry-run since the 1-turn-1-session change**
        # (T-naysayer-sdk-session-carries-the-whole-conversation-every-turn):
        # the observation is DISCARDED here. Each turn now runs its own
        # per-verdict preflight in :meth:`deliver_event` (D-2), so keeping the
        # spawn-time record and stamping it onto later posts is exactly the
        # per-process independence claim D-2 exists to reject. We still run the
        # probe at spawn — the whole point is to fail loud at attach time when
        # ``options`` is unroutable / mis-credentialed, so orchestrator +
        # human get a spawn-time refusal instead of a mysterious first-turn
        # failure — but the record is thrown away and ``session.attestation``
        # stays ``None`` until :meth:`deliver_event` writes its own.
        #
        # We no longer construct or connect the SDK client here; a fresh client
        # is built inside :meth:`deliver_event`. As a beneficial side-effect
        # the "session refused after connecting → leaked subprocess" hazard
        # the older code had to guard against is gone: nothing to leak.
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
            await self._run_preflight()  # dry-run: result deliberately discarded
        except Exception as exc:
            raise NaysayerSdkSpawnError(
                f"preflight attestation failed for role {role.value} on thread "
                f"{thread_ref.thread_id}; refusing to spawn an unattested naysayer "
                f"session: {exc}"
            ) from exc
        options = self._make_options()

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
            client=None,  # per-turn: constructed inside deliver_event
            # Serialises the client-ownership swap between deliver_event's
            # finally and halt (see _Session docstring). Constructed here so
            # every path that reaches for it in :meth:`deliver_event` /
            # :meth:`halt` finds a real lock even on a session that never
            # runs a turn (halt between spawn and first deliver, etc.).
            client_lock=asyncio.Lock(),
            ctx=ctx,
            own_role=role,
            state=SessionState.IDLE,
            last_active_at=now,
            # Retain the exact ``ClaudeAgentOptions`` for the harness marker
            # (msg-805 D3 / msg-834 §2 (a)) — never re-read, never re-declared.
            # Each per-turn ``_client_factory(options)`` call receives THIS
            # object, so the subprocess and the marker cannot drift.
            options=options,
            attestation=None,  # per-turn: written by deliver_event
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
        # Shutdown-state guard. It sits BEFORE the per-turn preflight below with
        # no ``await`` in between, so a session halted between turns refuses
        # delivery without burning a network probe (PR-gate advisory on PR-338
        # @ cc91962; pinned by
        # test_deliver_event_on_a_halted_session_skips_the_preflight). Keep it
        # above the preflight.
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
        #
        # **D-2 amendment**
        # (T-naysayer-sdk-session-carries-the-whole-conversation-every-turn,
        # msg-4096 §1): per-turn attestation is now matched by a per-turn SDK
        # subprocess. Under the old shape ``ClaudeSDKClient`` was held open for
        # the entire adapter session, so D-2's "per-verdict probe" strengthening
        # was contradicted by the subprocess it stamped — one subprocess
        # answered many verdicts, and the SDK session buffer accumulated the
        # whole conversation, causing O(n²) input token growth (msg-3445).
        # The subprocess now lives exactly one turn (see the ``client`` block
        # below), so the process and the probe share a lifetime by design; D-2
        # continues to hold, and the per-process/per-verdict mismatch it
        # highlighted is gone at the code level as well as at the accounting
        # level.
        try:
            session.attestation = await self._run_preflight()
        except Exception as exc:
            # Yield to halt: the preflight is awaited with no client published,
            # so a halt landing here is a pure state transition that may already
            # have reached HALTED. Same "any shutdown state" rule as the body's
            # except blocks below — do not clobber halt's state with FAILED.
            if session.state not in _SHUTDOWN_STATES:
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
        # 1-turn-1-session
        # (T-naysayer-sdk-session-carries-the-whole-conversation-every-turn).
        # A fresh SDK client is constructed, connected, queried, drained, and
        # shut down within this call — nothing persists between turns. This
        # bounds the input tokens sent per turn to what
        # :func:`build_turn_prompt` renders (opener + recent window +
        # system_prompt + ADR index + obligations), eliminating the O(n²)
        # accumulation that came from letting ``ClaudeSDKClient`` keep the
        # conversation on the subprocess side across every ``deliver_event``.
        #
        # ``client`` is pre-declared ``None`` so the ``finally`` block below
        # cannot ``UnboundLocalError`` if the factory itself throws. The
        # ``body_success`` flag tells that finally whether the main try
        # completed — required to know whether a shutdown failure is the
        # only error in flight (propagate) or a secondary error while the
        # main path was already unwinding (log-only, so the original cause
        # is not hidden).
        #
        # **Publish under ``client_lock``** (PR-gate feedback on PR-338,
        # e26288a — "``halt`` no longer interrupts active generations"): the
        # client is written to ``session.client`` under the lock so
        # :meth:`halt` can *steal* it and force-shut down a turn that is
        # currently running. Steal semantics: whichever coroutine reads
        # ``session.client`` and swaps it to ``None`` under the lock owns
        # the shutdown. That is what closes the double-shutdown race the
        # earlier "don't touch session.client from halt" strategy tried to
        # avoid — cheaply and without stripping halt of its cancellation
        # power.
        #
        # Three-way ErrorInfo.code split — same reasoning as
        # ClaudeCodeSdkAdapter.deliver_event (T-sdk-is-error-loses-the-reason
        # D-3(c)): SDK is_error, on_reply raised, and everything else each
        # need a different next hand from the operator. Shutdown failures on
        # a successful main path get their own fourth code,
        # ``adapter.shutdown_failed`` — a leaked per-turn subprocess is
        # operationally distinct from a mid-turn SDK error (dispatcher /
        # operator want to react to it separately, e.g. quarantine harder
        # rather than retry).
        client: _SdkClient | None = None
        body_success = False
        try:
            async with session.client_lock:
                # If ``halt`` won the race between preflight and here, do not
                # construct a subprocess we would immediately have to tear
                # down. Halt has already claimed the session; report that as
                # a delivery failure and let halt finish its transition to
                # ``HALTED``.
                if session.state in _SHUTDOWN_STATES:
                    raise NaysayerSdkDeliveryError(
                        f"session {handle.session_id} was halted before its "
                        f"per-turn client could be published; refusing to spawn "
                        f"an SDK subprocess for a halted session"
                    )
                client = self._client_factory(session.options)
                session.client = client
            await client.connect()
            await client.query(_build_prompt(event, session.own_role))
            body, result = await _drain_reply(client)
            body_success = True
        except SdkIsErrorSignal as sig:
            # If halt won the race, it has set state to HALTING — and, if
            # its shutdown was fast, possibly already to HALTED (or FAILED
            # with ``adapter.halt_failed``) — before this except runs.
            # Overwriting state / session.error from here would clobber the
            # halt path's authoritative diagnosis — halt is the reason the
            # SDK stream died, not an intrinsic SDK failure. The check is
            # therefore "any shutdown state", not "== HALTING" (PR-gate on
            # PR-338 @ 5a3d99b: a fast halt reaching HALTED first was
            # clobbered to FAILED by the narrower check).
            if session.state not in _SHUTDOWN_STATES:
                session.state = SessionState.FAILED
                session.error = ErrorInfo(
                    code="adapter.sdk_is_error",
                    message=str(sig),
                    raised_at=datetime.now(UTC),
                )
            raise NaysayerSdkDeliveryError(
                f"deliver_event failed for session {handle.session_id}: {sig}"
            ) from sig
        except Exception as exc:
            # Same yield-to-halt rule as the SdkIsErrorSignal branch above.
            if session.state not in _SHUTDOWN_STATES:
                session.state = SessionState.FAILED
                session.error = ErrorInfo(
                    code="adapter.delivery_failed",
                    message=str(exc),
                    raised_at=datetime.now(UTC),
                )
            raise NaysayerSdkDeliveryError(
                f"deliver_event failed for session {handle.session_id}: {exc}"
            ) from exc
        finally:
            # Steal the client back from the session under the lock. If halt
            # already stole it (concurrent :meth:`halt`), ``owned`` is ``None``
            # here and this coroutine has no shutdown to do; halt owns it.
            # If halt did not race us, we own the shutdown.
            #
            # When we own the shutdown we also publish a ``releasing`` event
            # under the same lock, so a :meth:`halt` that arrives while this
            # shutdown is in flight (and therefore reads ``client is None``)
            # waits for it instead of reporting a clean halt over a
            # subprocess that is still being torn down (PR-gate on PR-338 @
            # 5a3d99b).
            releasing: asyncio.Event | None = None
            async with session.client_lock:
                owned = session.client
                session.client = None
                if owned is not None:
                    releasing = asyncio.Event()
                    session.releasing = releasing
            if owned is not None:
                try:
                    # Bounded, same as halt's ``grace`` (PR-gate on PR-338 @
                    # 5a3d99b): an unbounded ``await _shutdown`` let a hung
                    # disconnect park deliver_event forever while halt read
                    # ``None`` and reported success. A timeout surfaces here
                    # as ``TimeoutError`` and takes the same branches as any
                    # other shutdown failure.
                    await asyncio.wait_for(
                        _shutdown(owned), timeout=self._shutdown_grace.total_seconds()
                    )
                except Exception as shutdown_exc:
                    # Deliberately NOT gated on halt's state: this client was
                    # never stolen by halt (else ``owned`` would be ``None``),
                    # so halt holds no diagnosis about it. A halt that raced
                    # in after the swap is waiting on ``releasing`` and reads
                    # the FAILED state written here.
                    if body_success:
                        # Main path completed → no other exception is in flight
                        # → the shutdown failure is the sole anomaly. Match the
                        # main ``except`` contract (state=FAILED, session.error,
                        # NaysayerSdkDeliveryError wrap) so the dispatcher's
                        # exception handling and health() give the same answer
                        # they would for any other adapter failure.
                        #
                        # Leaked-subprocess note: a shutdown that raises may
                        # have left the ``claude`` / ``node`` process behind.
                        # Under the Codex Pro migration that follows this
                        # thread, an unnoticed leaked subprocess burns 5-hour
                        # quota. Fail-loud rather than swallowing (Principle 5).
                        session.state = SessionState.FAILED
                        session.error = ErrorInfo(
                            code="adapter.shutdown_failed",
                            message=str(shutdown_exc),
                            raised_at=datetime.now(UTC),
                        )
                        raise NaysayerSdkDeliveryError(
                            f"per-turn client shutdown failed for session "
                            f"{handle.session_id} after a successful turn "
                            f"(subprocess may have leaked): {shutdown_exc}"
                        ) from shutdown_exc
                    # The main path was ALREADY raising when shutdown failed
                    # (state / session.error / the exception itself are all
                    # set by the except blocks above). Re-raising the shutdown
                    # error here would REPLACE the propagating exception.
                    # Log-only; the original failure continues.
                    logger.exception(
                        "naysayer per-turn client shutdown failed while unwinding "
                        "another error; original error will propagate"
                    )
                finally:
                    session.releasing = None
                    if releasing is not None:
                        releasing.set()

        # halt() may have raced between ``body_success = True`` and here
        # (its own ``client_lock`` acquisition can interleave with ours in
        # the ``finally`` above). If it has, halt is authoritative — the
        # verdict is not the naysayer's to post. Return without calling
        # ``on_reply`` so a halted turn does not become a stealth post.
        # PR-gate note (PR-338 @ e26288a): the earlier design flipped
        # ``state = HALTED`` in halt but let deliver_event drain and post
        # anyway; this check is what makes that state read authoritative.
        if session.state in _SHUTDOWN_STATES:
            return

        try:
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
                code="adapter.on_reply_failed",
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
        """Terminate the adapter-level session.

        1-turn-1-session
        (T-naysayer-sdk-session-carries-the-whole-conversation-every-turn):
        no long-lived SDK subprocess exists between turns, so a between-turn
        ``halt`` is a pure state-machine transition.

        **Halt during a live turn** (PR-gate feedback on PR-338 @ e26288a
        — "``halt`` no longer interrupts active generations"): if a turn
        is in flight, :meth:`deliver_event` has published its per-turn
        client to ``session.client`` under :attr:`_Session.client_lock`.
        Halt steals that client (read-and-swap under the same lock), sets
        state to ``HALTING`` while holding the lock, then runs
        :func:`_shutdown` on the stolen client *outside* the lock so a
        slow ``disconnect`` never blocks ``deliver_event``'s finally from
        making its own read-and-swap.

        Steal semantics — the read-and-swap under the lock is what makes
        this safe:

        * If halt reads a non-``None`` client, ``deliver_event``'s finally
          will subsequently read ``None`` and skip shutdown; halt owns it.
        * If halt reads ``None`` (``deliver_event``'s finally already ran,
          or the turn was between events), halt has nothing to shut down;
          ``deliver_event`` owned it (or nothing did).

        Either way exactly one coroutine calls ``_shutdown`` on any given
        client. ``deliver_event`` also checks ``session.state`` after its
        finally and, if halt has flipped it to ``HALTING``/``HALTED``,
        returns without calling ``on_reply`` — a halted turn does not
        become a stealth post.

        ``grace`` bounds the shutdown wait; a subprocess that will not
        interrupt/disconnect within it is reported as ``adapter.halt_failed``
        and reraised as :class:`NaysayerSdkHaltError`, matching the
        original halt contract before the 1-turn-1-session change.

        ``grace`` also bounds a shutdown halt did *not* steal: if
        ``deliver_event``'s finally already swapped the client out and is
        shutting it down (``_Session.releasing`` set), halt waits for that
        shutdown instead of returning a clean ``HALTED`` over it. A timeout
        there is ``adapter.halt_failed``; a shutdown that deliver_event
        itself reported as failed (``adapter.shutdown_failed``, state
        ``FAILED``) makes halt raise :class:`NaysayerSdkHaltError` and keeps
        that diagnosis. The deliver-side shutdown is itself bounded by the
        adapter's ``shutdown_grace``, so neither party can park forever.
        """
        session = self._sessions.get(handle)
        if session is None or session.state in _SHUTDOWN_STATES:
            return
        # Steal the live per-turn client (if any) under the swap lock, and
        # flip state to HALTING under the SAME lock so a concurrent
        # ``deliver_event`` sees the transition atomically with the steal.
        # deliver_event's except blocks check ``state not in _SHUTDOWN_STATES``
        # before overwriting session.state, so this ordering is what stops halt's
        # authority from being clobbered by a "the SDK stream died, must be
        # FAILED" write from the racing deliver.
        async with session.client_lock:
            if session.state in _SHUTDOWN_STATES:
                return  # another halt call won under the lock
            session.state = SessionState.HALTING
            owned = session.client
            session.client = None
            pending = session.releasing
        if owned is None and pending is not None:
            # deliver_event's finally already owns the shutdown of this
            # turn's client and it is still in flight. Do not report a clean
            # halt over it: wait (bounded by ``grace``) and read the outcome
            # deliver_event recorded. PR-gate on PR-338 @ 5a3d99b.
            try:
                await asyncio.wait_for(pending.wait(), timeout=grace.total_seconds())
            except TimeoutError as exc:
                session.state = SessionState.FAILED
                session.error = ErrorInfo(
                    code="adapter.halt_failed",
                    message="per-turn client shutdown owned by deliver_event did not "
                    f"finish within grace={grace}",
                    raised_at=datetime.now(UTC),
                )
                raise NaysayerSdkHaltError(
                    f"halt failed for session {handle.session_id}: in-flight per-turn "
                    f"shutdown did not finish within grace (subprocess may have leaked)"
                ) from exc
            if session.state is SessionState.FAILED:
                # deliver_event recorded ``adapter.shutdown_failed``; keep that
                # diagnosis (it names the cause) and fail the halt loudly.
                raise NaysayerSdkHaltError(
                    f"halt failed for session {handle.session_id}: the per-turn "
                    f"client shutdown failed "
                    f"({session.error.code if session.error else 'unknown'})"
                )
        if owned is not None:
            try:
                await asyncio.wait_for(_shutdown(owned), timeout=grace.total_seconds())
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
