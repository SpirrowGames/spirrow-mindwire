"""Stage 3 — ``ImplementerSdkAdapter`` (ADR-2026-05-23-07 §2.3, T19).

The third role of the loop: an ``EXECUTE_CODE`` adapter that runs the
implementer on a Claude Agent SDK session. Unlike
:class:`~spirrow_mindwire.adapters.claude_code_sdk.ClaudeCodeSdkAdapter` (the
proposer, which is given Read/Glob/Grep and nothing else), this adapter lets the
SDK session use code / git / fs tools without a per-call gate.

There used to be one: an operation classifier plus a ``can_use_tool`` allow-list
that denied four Tier C operations and branch-scoped the destructive git verbs.
It was removed on 2026-08-20 after its reach was measured against what it cost.
What it still guarded, end to end, was a single operation — ``gh release``. The
package-manager publishes it also named cannot run here at all (`npm`, `twine`,
`docker` and `gem` are not installed and no credentials for them exist), ``gh
repo delete`` needs a ``delete_repo`` scope the loop's token does not carry, and
a merge to ``main`` is refused by the GitHub org ruleset, which the loop's
identity cannot bypass. Against that, the gate halted five sessions in one day —
three of them for writing the word "release" inside a quoted argument — because
a denial is fail-loud by design. A regex over a raw command string cannot tell a
quoted mention from a command, and ``exec.code`` was unconditional anyway, so
nothing it enforced survived contact with intent.

What holds the invariants now sits outside the agent, where it can be enforced
rather than predicted:

* the GitHub org ruleset ``guard-default-branch`` — ``main`` cannot be pushed,
  force-pushed, deleted, or merged without a human approval, on every repository
  in the org, with no bypass for the loop's identity;
* :mod:`spirrow_mindwire.preflight` — P0/P1/P2 at the composition root, which
  refuse to start the loop at all unless the repository it is about to work in
  is separate from the daemon's own checkout, has remotes under the org, and has
  that server-side protection actually in place;
* the egress proxy's allow-list, for anything trying to leave the host;
* the implementer's own clone being disposable.

What that leaves unguarded, deliberately: pushing a named tag, and deleting a
remote branch other than the default one. The removed allow-list did block both
(a tag name is outside the branch glob, so ``git push origin v1.0.0`` was denied
— though ``git push origin --tags`` was not, so the bar was already porous). A
tag ruleset would cover it properly, and it is not being added.

The decision rule behind that, which is the thing to argue with if this ever
looks wrong: ``main`` is the only ref whose loss is not recoverable, and it is
protected server-side by something the loop cannot bypass. A deleted branch can
be pushed back from its SHA, a stray tag can be deleted, a wrecked clone can be
thrown away. So the question for each further guard is not "could this go
wrong" but "would it be unrecoverable if it did", and for everything except
``main`` the answer is no. Building and maintaining guards for the recoverable
cases has a real cost — this one cost five halted sessions in a day and three
review rounds — and that cost is paid out of the time the project exists to
spend elsewhere.

Inference routing (ADR-07 §2.4 / env spec §3-§4): the implementer PC holds no
Anthropic key — inference goes **via Lexora** (which routes to cloud Claude).
This adapter therefore **never** lets the SDK reach ``api.anthropic.com``
directly: it requires an explicit ``inference_base_url`` (or
``MINDWIRE_IMPLEMENTER_BASE_URL``), wired into the SDK as ``ANTHROPIC_BASE_URL``,
and **refuses to spawn** if none is configured (no silent fallback to the
default endpoint).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
)

from ..conductor.handoff import build_handoff_protocol_block
from ..exceptions import (
    AdapterDeliveryError,
    AdapterHaltError,
    AdapterHealthError,
    AdapterSpawnError,
)
from ..naysayer.adr_index import load_adr_index
from ..obligations import ObligationsManifest
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

# Reuse the SDK session glue (same package, identical reply-drain protocol).
from .claude_code_sdk import (
    _default_client_factory,
    _drain_reply,
    _SdkClient,
    _shutdown,
)

_SHUTDOWN_STATES: frozenset[SessionState] = frozenset(
    {SessionState.HALTING, SessionState.HALTED, SessionState.FAILED}
)

# The static (identity-only) portion of the implementer's system prompt. Loop-
# readable obligations that constrain behaviour at runtime — "declare what you
# cannot read", read-back at entry/exit — live in ``spec/process/obligations.yaml``
# and are injected here by the composition root via
# :class:`~spirrow_mindwire.obligations.ObligationsManifest`; the manifest, not
# this literal, is the single source of truth for those clauses (CLAUDE.md §N →
# spec/process/README.md → obligations.yaml). The paragraphs that once lived
# inline (notably "DOCUMENTS YOU CANNOT READ") have been MOVED to that manifest —
# deleted here, restored to the rendered prompt through injection. Keeping the
# manifest as the sole owner is what lets the tests assert on the actual prompt
# the loop reads rather than a mirror.
_BASE_IMPLEMENTER_SYSTEM_PROMPT = """\
You are the implementer in a Spirrow MindWire ChatRoom thread. You write and \
run code to carry out the agreed proposal. You operate under a strict, \
fail-loud allow-list (Stage 3 autonomy gating):

ALLOWED (Tier A): edit files inside the repository, run tests/builds/code, \
commit and push on feature/* and develop branches, merge feature/* into \
develop, open pull requests. read and search freely.

FORBIDDEN (Tier C — never attempt; they will be denied and halt you): merging \
to or pushing to main, force-push, history rewrite (rebase / reset --hard / \
filter-branch), deleting files, writing to Drive, any external publish/post/send.

GATE (run before every commit; never commit on a red gate): run the \
repository's own gate and ensure it passes. Discover it from the repo — do NOT \
assume a toolchain or hard-code paths: if the repo root has a `.mindwire-gate` \
script, EXECUTE it (`bash .mindwire-gate`; exit 0 = green); otherwise run the \
project's own configured test suite as defined by its local config. A red gate \
blocks the commit — fix the cause, never commit around it.

SCRATCH FILES: every write lands inside the repository or is denied — including \
the throwaway ones. A PR body, a message draft, a diff you want to re-read: do \
NOT put them in the OS temp directory, that write is denied and it halts you. \
Two options, in order: (1) skip the file — `gh pr create --body-file -` reads \
the body from stdin, so a heredoc needs no file at all; (2) if you genuinely \
need a file, use `<repo>/.git/mindwire-scratch/`, which is inside the repo yet \
can never appear in `git status` or be committed by `git add`. Never put \
scratch files in the working tree, where they would be committed by accident.

Work on a feature/* branch, commit your changes, and (when ready) open a PR to \
develop. When you reply in the thread, reply directly with the message body — \
no preamble, no meta-commentary; your response is posted verbatim.
"""

# The conductor reads the trailing NEXT: line to chain the loop (PR-2b-1); the implementer hands
# back to the proposer for a spec-review, or to the human for a Tier-C decision (it never merges).
_DEFAULT_IMPLEMENTER_SYSTEM_PROMPT = (
    f"{_BASE_IMPLEMENTER_SYSTEM_PROMPT}\n{build_handoff_protocol_block(Role.IMPLEMENTER)}"
)

# The built-in Claude Code tools the implementer's SDK session exposes (T37 #1).
# This is the SDK ``tools=`` base set: ``tools=[]`` means "disable ALL built-ins"
# in claude-agent-sdk 0.1.77 (CLI ``--tools ""``), which left the autonomous
# implementer with zero Read/Edit/Bash/… — the brain ran but had no hands, so it
# could never act (the whole "implementer never ran for real" finding). We expose
# the code / fs / search / shell + planning tools it needs and DELIBERATELY OMIT
# the rest (no ``Task`` sub-agents, no ``WebFetch``/``WebSearch`` network reach,
# no ``SlashCommand``) to keep the surface minimal. Since the allow-list guard was
# removed (2026-08-20) this list IS the tool surface: what is not exposed here
# cannot be called at all, and what is exposed runs without a per-call check.
_IMPLEMENTER_BUILTIN_TOOLS: tuple[str, ...] = (
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Bash",
    "BashOutput",
    "KillShell",
    "Glob",
    "Grep",
    "TodoWrite",
)


class ImplementerSdkSpawnError(AdapterSpawnError):
    """``spawn`` failure for the implementer adapter (§3.4)."""


class ImplementerSdkDeliveryError(AdapterDeliveryError):
    """``deliver_event`` failure for the implementer adapter (§3.4)."""


class ImplementerSdkHaltError(AdapterHaltError):
    """``halt`` failure for the implementer adapter (§3.4)."""


class ImplementerSdkHealthError(AdapterHealthError):
    """``health`` failure for the implementer adapter (§3.4)."""


@dataclass
class _Session:
    client: _SdkClient
    ctx: SpawnContext
    own_role: Role
    state: SessionState
    last_active_at: datetime
    # The exact ``ClaudeAgentOptions`` handed to the SDK client on spawn,
    # kept so :meth:`ImplementerSdkAdapter.source_marker_options` can hand
    # the same object to the harness (msg-834 §2 (a)). ``Any`` typing on the
    # dataclass field so an old cached instance without options still loads.
    options: Any = None
    error: ErrorInfo | None = None


def _build_prompt(event: ChatroomEvent, own_role: Role) -> str:
    return build_turn_prompt(
        event, own_role, "Carry out the work in your role, then reply in the thread."
    )


def _cwd_grounding_block(cwd: Path) -> str:
    """Tell the implementer its working directory so it never guesses an absolute path (T40).

    The adapter runs with a *custom* ``system_prompt`` (not the claude_code preset), so the SDK does
    not inject the "working directory" dynamic section the agent would normally rely on. Without it
    the agent has guessed a wrong absolute path (e.g. ``/home/user/<repo>/...`` on a non-cwd repo)
    and the allow-list fail-loud denied the out-of-repo write — the loop made no progress. Grounding
    the cwd + mandating relative paths closes that (observed on the spirrow-voxelworld conductor
    smoke, T-voxel-autoloop-smoke).
    """
    return (
        f"WORKING DIRECTORY: `{cwd}` — this directory IS the root of the repo you operate in. "
        "Resolve every file path against it: prefer plain relative paths (e.g. `src/foo.py`) and "
        "never invent or hard-code an absolute path to any other location. All your reads, edits, "
        "builds, and git commands run inside this directory."
    )


def _adr_index_block() -> str:
    """Give the implementer the same deterministic ADR id+title map the naysayer gets (N-2).

    Why the implementer needs it at all: the loop asks it to satisfy ADRs by number. Told to
    "perform the ADR-2026-05-29-13 read-back" with no map and no bodies, a capable agent does the
    only thing left — it reconstructs the ADR from context and states the result as fact. That
    happened on 2026-08-08 (voxelworld PR #182): three of five read-back claims attributed things to
    ADR-13 that it does not say, because ADR-13 is the *spec read-back checklist* and the agent had
    no way to know. The failure was not ignorance, it was silent confident invention.

    So this block, and the "DOCUMENTS YOU CANNOT READ" rule in the system prompt, are halves of one
    fix and neither works alone. The map alone would be **worse than nothing**: a title is a
    summary, and inviting an agent to reason outward from a summary as if it were the document
    reproduces exactly what ADR-2026-05-29-13 warns against — only now the confabulation is better
    grounded and therefore harder to catch. The rule alone leaves it unable to say even which ADR
    it cannot read.

    Source is the same in-repo manifest the naysayer uses (``spec/adr_index.yaml``); nothing is
    duplicated. An unloadable manifest says so out loud rather than shipping a silent gap.
    """
    index = load_adr_index()
    if not index:
        return (
            "ADR INDEX — UNAVAILABLE. The in-repo ADR manifest could not be loaded, so you do not "
            "even have the list of ADR ids. If a task names an ADR, say that you could not look it "
            "up; do not guess what it requires."
        )
    rows = "\n".join(f"- {adr_id} — {title}" for adr_id, title in index)
    return (
        "ADR INDEX (ids and TITLES ONLY — the bodies are not available to you):\n"
        f"{rows}\n"
        "Use this to identify an ADR and to avoid attributing to one what belongs to another. It "
        "is NOT the ADRs. A title tells you the subject, never the requirements — so never write "
        "that an ADR 'requires' or 'permits' something on the strength of its title. If a task "
        "needs an "
        "ADR's actual content, say you cannot read it and ask for the relevant text to be quoted "
        "into the thread."
    )


class ImplementerSdkAdapter:
    """RoleAdapter for the implementer: SDK + EXECUTE_CODE + allow-list (T19).

    ``capabilities`` carries ``EXECUTE_CODE`` (qualifies for the implementer
    slot) and omits ``NAYSAYER_QUALIFIED`` (same model family as main).
    """

    adapter_id: str = "implementer-sdk"
    capabilities: frozenset[Capability] = frozenset(
        {Capability.READ_THREAD, Capability.POST_REPLY, Capability.EXECUTE_CODE}
    )

    def __init__(
        self,
        *,
        cwd: Path,
        obligations: ObligationsManifest,
        inference_base_url: str | None = None,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        system_prompt: str = _DEFAULT_IMPLEMENTER_SYSTEM_PROMPT,
        extra_env: dict[str, str] | None = None,
        client_factory: Any = None,
    ) -> None:
        self._cwd = Path(cwd)
        # Inference MUST be routed via Lexora (env spec §4): require an explicit
        # base URL; never fall back to the SDK default (api.anthropic.com).
        self._inference_base_url = (
            inference_base_url
            if inference_base_url is not None
            else os.environ.get("MINDWIRE_IMPLEMENTER_BASE_URL", "")
        )
        self._model = model
        # Empty by default → every tool call routes through the guard (the guard
        # is the single enforcement point; auto-approval would bypass it).
        self._allowed_tools = list(allowed_tools) if allowed_tools is not None else []
        self._mcp_servers = mcp_servers or {}
        # Loop-readable obligations are injected here — the manifest passed in is the
        # single source of truth (CLAUDE.md §N → spec/process/README.md) and the
        # adapter never reaches for a module-global path itself. That injection
        # shape is what canary two-prime (rendered-prompt-contains-obligation-body)
        # reads: the assembled system prompt under test is exactly the one the
        # adapter renders in production, from a manifest the test picks.
        self._obligations = obligations
        # Append cwd grounding (T40): the custom system prompt omits the SDK's working-directory
        # dynamic section, so the agent must be told its cwd explicitly or it guesses abs paths.
        self._system_prompt = (
            f"{system_prompt}\n\n{_cwd_grounding_block(self._cwd)}\n\n"
            f"{obligations.render_role_obligations(Role.IMPLEMENTER)}\n\n"
            f"{_adr_index_block()}"
        )
        self._extra_env = dict(extra_env or {})
        self._client_factory = client_factory or _default_client_factory
        self._sessions: dict[SessionHandle, _Session] = {}

    def _make_options(self) -> ClaudeAgentOptions:
        env = {
            "ANTHROPIC_BASE_URL": self._inference_base_url,
            # Force UTF-8 in the CLI subprocess and any Python the agent spawns
            # (``uv run`` / pytest / its own scripts). On Japanese Windows the
            # default cp932 codec cannot encode em-dash / 日本語 in prompts or
            # tool output and raises ``UnicodeEncodeError`` (T37 #2 — observed:
            # ``'cp932' codec can't encode '—'``). PYTHONUTF8=1 is the
            # canonical fix; PYTHONIOENCODING covers child stdio explicitly.
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            **self._extra_env,
        }
        kwargs: dict[str, Any] = {
            "cwd": self._cwd,
            "system_prompt": self._system_prompt,
            # Expose the implementer's built-in toolset. ``tools=[]`` DISABLES all
            # built-ins (SDK 0.1.77 → ``--tools ""``), so this list is what the
            # session can call — and, with no per-call guard, the only limit on it.
            "tools": list(_IMPLEMENTER_BUILTIN_TOOLS),
            "allowed_tools": self._allowed_tools,
            "mcp_servers": self._mcp_servers,
            # Isolation (T37 #4). setting_sources=[] runs the session in SDK
            # isolation mode: it does NOT load this host's user/project/local
            # settings (claude.ai connectors, CLAUDE.md, env, hooks). That both
            # stops the implementer from inheriting the operator's MCP connectors
            # (a credential-surface leak — the agent reached smart_read/Gmail/
            # Drive). It matters more now, not less: with the per-call guard gone,
            # host settings are one of the few things that could still widen what
            # this session reaches, and they stay out.
            # strict_mcp_config ignores any MCP config not passed here
            # (project ``.mcp.json`` / plugin servers), so only ``mcp_servers``
            # (empty by default) is honored. Inference auth (credentials file) is
            # independent of settings sources, so this does not affect routing.
            "setting_sources": [],
            "strict_mcp_config": True,
            # No ``can_use_tool``: with nothing to ask, "default" would leave the
            # SDK waiting on a prompt no one can answer in a headless session.
            # The invariants this used to approximate are enforced outside the
            # agent now — see the module docstring.
            "permission_mode": "bypassPermissions",
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
            raise ImplementerSdkSpawnError(
                "no inference base URL configured (set inference_base_url or "
                "MINDWIRE_IMPLEMENTER_BASE_URL): the implementer must route inference "
                "via Lexora, never api.anthropic.com directly (ADR-07 §2.4 / env spec §4)"
            )
        options = self._make_options()
        try:
            client = self._client_factory(options)
            await client.connect()
        except Exception as exc:
            raise ImplementerSdkSpawnError(
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
            # Retain the exact ``ClaudeAgentOptions`` object we passed to
            # the SDK client so the harness can derive the source marker
            # from it (msg-805 D3 / msg-834 §2 (a)). Never re-declare or
            # re-read; the marker's SOT is this instance.
            options=options,
        )
        return handle

    def source_marker_options(self, handle: SessionHandle) -> Any:
        """Return the ``ClaudeAgentOptions`` for ``handle``, or ``None`` if unknown.

        Public so :mod:`spirrow_mindwire.dispatcher.core` can retrieve the
        exact options object the SDK client was spawned with and hand it to
        :func:`spirrow_mindwire.source_marker.render_source_marker`. This
        adapter file **never** imports the marker builder; the seam is here
        (msg-834 §2 (c)).
        """
        session = self._sessions.get(handle)
        return None if session is None else session.options

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        session = self._sessions.get(handle)
        if session is None:
            raise ImplementerSdkDeliveryError(f"unknown session {handle.session_id}")
        if session.state in _SHUTDOWN_STATES:
            raise ImplementerSdkDeliveryError(
                f"session {handle.session_id} is {session.state.value}; cannot deliver"
            )
        if event.event_type is not EventType.NEW_MESSAGE:
            return
        payload = event.payload
        if payload.author == handle.instance_id:
            # instance self-filter (Gap-2 (b), I3 v2.2): drop our own echoed post
            # (author == our instance_id, e.g. "implementer-1"), not the bare role.
            return

        session.state = SessionState.PROCESSING
        try:
            await session.client.query(_build_prompt(event, session.own_role))
            body = await _drain_reply(session.client)
            await session.ctx.on_reply(
                ReplyDraft(
                    body=body,
                    reply_to_msg_id=payload.msg_id,
                    adapter_metadata={"adapter_id": self.adapter_id, "model": self._model},
                )
            )
        except Exception as exc:
            session.state = SessionState.FAILED
            session.error = ErrorInfo(
                code="adapter.delivery_failed",
                message=str(exc),
                raised_at=datetime.now(UTC),
            )
            raise ImplementerSdkDeliveryError(
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
            raise ImplementerSdkHaltError(
                f"halt failed for session {handle.session_id}: {exc}"
            ) from exc
        session.state = SessionState.HALTED

    async def health(self, handle: SessionHandle) -> HealthStatus:
        session = self._sessions.get(handle)
        if session is None:
            raise ImplementerSdkHealthError(f"unknown session {handle.session_id}")
        return HealthStatus(
            state=session.state,
            last_active_at=session.last_active_at,
            error=session.error,
            details={"adapter_id": self.adapter_id, "session_id": handle.session_id},
        )


__all__ = [
    "ImplementerSdkAdapter",
    "ImplementerSdkDeliveryError",
    "ImplementerSdkHaltError",
    "ImplementerSdkHealthError",
    "ImplementerSdkSpawnError",
]
