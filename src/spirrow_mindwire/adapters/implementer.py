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
import contextlib
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
from ..naysayer.adr_index import load_adr_entries
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
from . import _sdk_job_hook
from ._sdk_job_hook import (
    _JOB_HANDLE_CTX,
    JobState,
)
from ._session_isolation import session_isolation_kwargs

# Reuse the SDK session glue (same package, identical reply-drain protocol).
from .claude_code_sdk import (
    SdkTurnTimeoutError,
    _default_client_factory,
    _drain_reply,
    _SdkClient,
    _shutdown,
)

# Default budgets for the two v12 timeouts (T-auto-backgrounded-command-hangs-\
# conductor-4h). Both are conservative — smaller than the Task Scheduler's 4 h
# wall by a wide margin, larger than any healthy turn. Overridable via the
# constructor + env vars for operational tuning.
_DEFAULT_SPAWN_TIMEOUT_SECONDS = 60.0
_DEFAULT_TURN_TIMEOUT_SECONDS = 30 * 60.0  # 30 minutes — a long turn is fine,
# a session that eats hours is what we exist to break.


def _read_float_env(name: str, default: float) -> float:
    """Read a float from ``os.environ``; return ``default`` if unset or malformed.

    A malformed value is logged (would be, if we had a logger here — the
    dispatcher's log capture picks up the ``ValueError`` message) and then
    ignored: the whole point of an env override is to be safe under
    operator error, not to break the daemon at import.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _extract_sdk_pid(client: Any) -> int | None:
    """Best-effort extraction of the SDK subprocess pid for verify (v12).

    The SDK's ``ClaudeSDKClient`` stores its transport at ``._transport``
    and the transport keeps the anyio ``Process`` on ``._process``. This
    helper walks that path defensively — if the SDK's private layout
    changes we return ``None`` rather than crash, and the caller skips the
    verify (the cost is a lost fail-loud check; the alternative would be
    the whole spawn failing on an SDK internal-refactor).
    """
    try:
        transport = getattr(client, "_transport", None) or getattr(client, "transport", None)
        if transport is None:
            return None
        process = getattr(transport, "_process", None) or getattr(transport, "process", None)
        if process is None:
            return None
        pid = getattr(process, "pid", None)
        return int(pid) if pid is not None else None
    except Exception:
        return None


def _default_sdk_executable_path() -> str:
    """Best-effort absolute path to the bundled SDK ``claude`` executable.

    Used by ``lookup_and_assign_leftover`` as the identity check when walking
    the daemon's direct children. Three resolution strategies, tried in
    order:

    1. The SDK's bundled binary (``claude_agent_sdk/_bundled/claude.exe``).
       This is the canonical location for the vendored CLI.
    2. ``shutil.which("claude")`` — resolves the PATH to a real executable
       location. PR-gate #299 round 3 blocker: if the bundled binary is
       missing (system install, refactored SDK layout, or a stub package),
       the previous code returned the bare string ``"claude"``. The belt in
       ``lookup_and_assign_leftover`` then compared ``os.path.abspath\
       ("claude")`` — which does NOT do a PATH lookup — against the child's
       real absolute path, guaranteeing a mismatch and rendering the belt
       useless. ``shutil.which`` fixes this by producing a real absolute
       path.
    3. The bare string ``"claude"``. The belt normalizes both sides and,
       when the target is not a real absolute path, falls back to a
       case-insensitive basename comparison (see
       ``_sdk_job_hook.lookup_and_assign_leftover``). This preserves the
       belt's utility even under total resolution failure.
    """
    try:
        import claude_agent_sdk

        pkg_dir = Path(claude_agent_sdk.__file__).resolve().parent
        candidate = pkg_dir / "_bundled" / "claude.exe"
        if candidate.exists():
            return str(candidate)
    except Exception:
        pass
    # Fallback 2 (PR-gate #299 round 3): try PATH via shutil.which. On
    # Windows shutil.which auto-appends .exe if omitted; on POSIX it uses
    # the bare name.
    import shutil

    resolved = shutil.which("claude") or shutil.which("claude.exe")
    if resolved:
        return resolved
    # Fallback 3: bare name. The belt handles this via basename comparison.
    return "claude"


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


class ImplementerSdkSpawnTimeoutError(ImplementerSdkSpawnError):
    """``spawn`` exceeded its init time budget (v12 B-4).

    Distinct subclass so the dispatcher / conductor can tell "the SDK never
    connected" from "the SDK connected but errored". Error code:
    ``adapter.spawn_timeout``.
    """


class ImplementerSdkDeliveryError(AdapterDeliveryError):
    """``deliver_event`` failure for the implementer adapter (§3.4)."""


class ImplementerSdkTurnTimeoutError(ImplementerSdkDeliveryError):
    """The SDK turn did not finish inside the caller's time budget (v12 B-1).

    Wraps :class:`SdkTurnTimeoutError` from the shared drain helper.
    Error code: ``adapter.turn_timeout``.
    """


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
    # v12 — the Job Object that owns every claude.exe descendant of this
    # session. ``None`` on POSIX (no Job primitive) and until ``spawn`` has
    # created it. ``close_handle`` is idempotent and sets it back to ``None``
    # on close, so a double-cleanup does not attempt to close the same handle
    # twice (which on Windows can destroy a recycled handle).
    job_state: JobState | None = None


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
    reproduces exactly the silent confident invention OBL-DECLARE-UNREADABLE exists to prevent —
    only now the confabulation is better grounded and therefore harder to catch. The rule alone
    leaves it unable to say even which ADR it cannot read.

    Source is the same in-repo manifest the naysayer uses (``spec/adr_index.yaml``); nothing is
    duplicated. An unloadable manifest says so out loud rather than shipping a silent gap.

    The rendered block also carries each entry's ``body:`` locator (a chatroom message, a
    file in this repository, or Drive), added in
    ``T-adr-index-omits-chatroom-body-locator``. The original failure this fixes was that
    the implementer could see an ADR id and title, know it needed the body, and have no
    way to reach it — three turns of the ``T-not-waiting-conclair-contract-assumptions``
    sub-thread misjudged ADR-2026-05-29-12 as "body unreadable" while the body was in
    fact reachable at ``T-embodiment-self-declared#msg-325``.
    """
    entries = load_adr_entries()
    if not entries:
        return (
            "ADR INDEX — UNAVAILABLE. The in-repo ADR manifest could not be loaded, so you do not "
            "even have the list of ADR ids. If a task names an ADR, say that you could not look it "
            "up; do not guess what it requires."
        )
    rows = "\n".join(f"- {e.adr_id} — {e.title} [body: {e.body}]" for e in entries)
    return (
        "ADR INDEX (ids, TITLES, and BODY LOCATORS — the bodies themselves are not inlined):\n"
        f"{rows}\n"
        "Use this to identify an ADR and to avoid attributing to one what belongs to another. It "
        "is NOT the ADRs. A title tells you the subject, never the requirements — so never write "
        "that an ADR 'requires' or 'permits' something on the strength of its title. The `body:` "
        "locator names WHERE the body lives: `chatroom:<project>/<thread>#msg-<n>` is the "
        "decide-close message that carries the ADR body; `repo:<path>` is a file in THIS "
        "repository, so just open it — it is the cheapest of the three; `drive` means "
        "Drive/spirrow-docs, and a bare `drive` (no file pointer) means the specific file is "
        "unknown to this index. If a task needs an ADR's actual content, follow the locator "
        "or say you could not read it — do not guess what it requires."
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
        spawn_timeout_seconds: float | None = None,
        turn_timeout_seconds: float | None = None,
        sdk_executable_path: str | None = None,
        job_module: Any = None,
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
        # v12 timeouts — bound the two async waits that the observed hang
        # exposed. Env overrides let operators tune per host without a code
        # change; the constructor arg takes precedence for tests.
        self._spawn_timeout_seconds = (
            spawn_timeout_seconds
            if spawn_timeout_seconds is not None
            else _read_float_env(
                "MINDWIRE_IMPLEMENTER_SPAWN_TIMEOUT_SECONDS",
                _DEFAULT_SPAWN_TIMEOUT_SECONDS,
            )
        )
        self._turn_timeout_seconds = (
            turn_timeout_seconds
            if turn_timeout_seconds is not None
            else _read_float_env(
                "MINDWIRE_IMPLEMENTER_TURN_TIMEOUT_SECONDS",
                _DEFAULT_TURN_TIMEOUT_SECONDS,
            )
        )
        # v12 — the SDK executable's absolute path. Used by the fallback
        # ``lookup_and_assign_leftover`` to distinguish OUR ``claude.exe``
        # from any other executable a child process might be. Defaults to
        # the bundled SDK executable path, resolvable at import time.
        self._sdk_executable_path = sdk_executable_path or _default_sdk_executable_path()
        # ``job_module`` is dependency-injected so tests can substitute a fake
        # for the Windows-only APIs without patching module globals. Default
        # is the real ``_sdk_job_hook`` module.
        self._job_module = job_module if job_module is not None else _sdk_job_hook

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
            # Isolation (T37 #4) — this role had it first; the definition now lives
            # in ``_session_isolation`` so all three roles cannot drift apart.
            **session_isolation_kwargs(),
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
        """Spawn an SDK client under a Windows Job Object with bounded init (v12).

        The v12 design (see ``T-auto-backgrounded-command-hangs-conductor-4h``)
        wraps the SDK spawn in three layered protections:

        1. A **spawn init timeout** (``asyncio.wait_for`` on ``connect()``).
           A hung SDK subprocess bounds the wait so the conductor is not
           parked behind one spawn (B-4).
        2. A **Windows Job Object** with ``KILL_ON_JOB_CLOSE``. Every
           ``claude.exe`` the SDK spawns is added to the Job via a
           ContextVar-gated proxy on ``anyio.open_process``. When the Job's
           last handle closes, the OS reaps the whole tree.
        3. A **``try/finally``** with a ``session_registered`` flag and an
           idempotent ``close_handle`` helper. Success takes the halt path
           for cleanup; every other exit (timeout, exception, cancel,
           KeyboardInterrupt, SystemExit) hits the finally which first runs
           a fallback ``lookup_and_assign_leftover`` (in case the proxy
           missed a race) and then closes the Job.

        The three together bind the observed hang class to a finite wall
        with an OS-level reaper for anything Python cannot cleanly halt.
        """
        if not self._inference_base_url:
            raise ImplementerSdkSpawnError(
                "no inference base URL configured (set inference_base_url or "
                "MINDWIRE_IMPLEMENTER_BASE_URL): the implementer must route inference "
                "via Lexora, never api.anthropic.com directly (ADR-07 §2.4 / env spec §4)"
            )
        options = self._make_options()

        now = datetime.now(UTC)
        session = _Session(
            # Placeholder — replaced by the real client after connect().
            client=None,  # type: ignore[arg-type]
            ctx=ctx,
            own_role=role,
            state=SessionState.IDLE,
            last_active_at=now,
            options=options,
        )
        session_registered = False
        client: _SdkClient | None = None
        session_id_for_diag = f"pending:{thread_ref.thread_id}"
        try:
            # ── Job Object creation (Windows only) ───────────────────────
            # POSIX raises NotImplementedError from create_job; we let that
            # bubble as an ImplementerSdkSpawnError below. The daemon runs on
            # Windows in production; a POSIX host that reaches here is a
            # deployment error (v12 §POSIX).
            try:
                session.job_state = self._job_module.create_job(session_id_for_diag)
            except NotImplementedError:
                # POSIX — no Job Object. Fall through with job_state=None;
                # the ContextVar stays None, the proxy is a no-op, and the
                # spawn proceeds without the Windows-only protection. The
                # rest of v12 (bounded init timeout, bounded turn timeout)
                # still applies.
                session.job_state = None

            # ── SDK client factory + connect, bounded by wait_for ────────
            client = self._client_factory(options)
            job_handle_for_ctx = session.job_state.handle if session.job_state is not None else None
            token = _JOB_HANDLE_CTX.set(job_handle_for_ctx)
            try:
                await asyncio.wait_for(
                    client.connect(),
                    timeout=self._spawn_timeout_seconds,
                )
            finally:
                _JOB_HANDLE_CTX.reset(token)

            # ── success invariant: verify the proxy caught the child ─────
            # Only meaningful when we actually built a Job (Windows). Miss
            # here is fail-loud: it means our ``claude.exe`` is running
            # OUTSIDE our Job and KILL_ON_JOB_CLOSE will not reap it.
            if session.job_state is not None:
                pid = _extract_sdk_pid(client)
                if pid is not None and not self._job_module.is_process_in_job(
                    pid, session.job_state
                ):
                    raise ImplementerSdkSpawnError(
                        f"adapter.job_assign_missed: claude.exe pid={pid} did "
                        f"not land in our Job — the SDK spawn hook was not "
                        f"installed, or the proxy did not fire. Aborting "
                        f"spawn for {role.value} on thread {thread_ref.thread_id}."
                    )

            session.client = client
            handle = SessionHandle(
                session_id=new_ulid(),
                instance_id=ctx.own_instance_id,
                adapter_id=self.adapter_id,
                thread_ref=thread_ref,
                role=role,
                started_at=now,
            )
            self._sessions[handle] = session
            session_registered = True
            return handle

        except TimeoutError as exc:
            # B-4 spawn init timeout. The disconnect best-effort tries to
            # let the SDK reap its subprocess through its own cancellation
            # path; the finally then handles the Job cleanup and fallback
            # lookup for anything the proxy missed.
            if client is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client.disconnect(), timeout=5.0)
            raise ImplementerSdkSpawnTimeoutError(
                f"adapter.spawn_timeout: SDK spawn did not connect inside "
                f"{self._spawn_timeout_seconds}s for role {role.value} on "
                f"thread {thread_ref.thread_id}"
            ) from exc
        except ImplementerSdkSpawnError:
            # Already a spawn error — pass through so the caller sees our
            # error class; the finally still runs.
            raise
        except Exception as exc:
            raise ImplementerSdkSpawnError(
                f"spawn failed for role {role.value} on thread {thread_ref.thread_id}: {exc}"
            ) from exc
        finally:
            # v12 §finally-cleanup — always runs on every failure exit,
            # including asyncio.CancelledError and KeyboardInterrupt /
            # SystemExit (which bypass ``except Exception``). Skipped on
            # success because the halt path then owns the cleanup.
            if not session_registered and session.job_state is not None:
                # Fallback lookup FIRST — in case the proxy missed a race
                # with the SDK's ``anyio.open_process`` call and left a
                # ``claude.exe`` outside the Job. Silent per-child skip is
                # inside the helper; we still guard the whole call in case
                # psutil itself raises, because losing the ORIGINAL failure
                # reason (the timeout, the cancel) to a diagnostic is
                # strictly worse than a leaked child. ``adapter.lookup_\
                # leftover_failed`` — swallowed so the original spawn
                # failure propagates cleanly.
                with contextlib.suppress(Exception):
                    self._job_module.lookup_and_assign_leftover(
                        session.job_state, self._sdk_executable_path
                    )
                self._job_module.close_handle(session.job_state)

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
        # B-1 bounded turn drain (v12) — hand ``_drain_reply`` the turn
        # budget so a CLI that silently backgrounds a foreground shell
        # command cannot hold the drain open past our wall. A hit here
        # raises ``SdkTurnTimeoutError`` which we wrap as
        # ``adapter.turn_timeout``.
        try:
            await session.client.query(_build_prompt(event, session.own_role))
            body = await _drain_reply(
                session.client,
                turn_timeout_seconds=self._turn_timeout_seconds,
            )
            await session.ctx.on_reply(
                ReplyDraft(
                    body=body,
                    reply_to_msg_id=payload.msg_id,
                    adapter_metadata={"adapter_id": self.adapter_id, "model": self._model},
                )
            )
        except SdkTurnTimeoutError as exc:
            session.state = SessionState.FAILED
            session.error = ErrorInfo(
                code="adapter.turn_timeout",
                message=str(exc),
                raised_at=datetime.now(UTC),
            )
            raise ImplementerSdkTurnTimeoutError(
                f"deliver_event turn timeout for session {handle.session_id}: {exc}"
            ) from exc
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
        """Interrupt+disconnect the SDK; close the Job Object (v12).

        The graceful shutdown is best-effort under ``grace``. Regardless of
        whether it succeeds, times out, or is cancelled, the Windows Job
        handle is closed via the idempotent ``close_handle`` helper in a
        ``finally`` — that is what makes ``KILL_ON_JOB_CLOSE`` fire and
        reaps any ``claude.exe`` (and its descendants) that the SDK could
        not politely disconnect. ``close_handle`` is safe under BaseException
        (asyncio cancel, KeyboardInterrupt), so a cancel during halt still
        closes the Job.
        """
        session = self._sessions.get(handle)
        if session is None or session.state in _SHUTDOWN_STATES:
            return
        session.state = SessionState.HALTING
        halt_error: Exception | None = None
        try:
            try:
                await asyncio.wait_for(_shutdown(session.client), timeout=grace.total_seconds())
            except Exception as exc:
                halt_error = exc
        finally:
            # v12 § halt Job cleanup — MUST run even if shutdown raised
            # or was cancelled. The helper is idempotent (sentinel + guard)
            # so a repeat halt or a concurrent spawn-finally-close is safe.
            if session.job_state is not None:
                # Cleanup diagnostic only — do not override the halt
                # error with a close diagnostic.
                with contextlib.suppress(Exception):
                    self._job_module.close_handle(session.job_state)
        if halt_error is not None:
            session.state = SessionState.FAILED
            session.error = ErrorInfo(
                code="adapter.halt_failed",
                message=str(halt_error),
                raised_at=datetime.now(UTC),
            )
            raise ImplementerSdkHaltError(
                f"halt failed for session {handle.session_id}: {halt_error}"
            ) from halt_error
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
    "ImplementerSdkSpawnTimeoutError",
    "ImplementerSdkTurnTimeoutError",
]
