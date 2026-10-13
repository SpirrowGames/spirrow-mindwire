"""``claude --bg`` turn adapter (T-spawn-via-claude-bg, PR-B).

Each turn runs as its own Claude Code background session (Agent View):
``claude --bg --agent <identity> --model <choose_model result> --effort high
--dangerously-skip-permissions --name <thread_id>-r<round>-<nonce6> "<prompt>"``
with ``cwd = repo_dir``. One turn is one session: :meth:`ClaudeBgAdapter.deliver_event`
starts a fresh ``--bg`` session for the turn and never appends to an existing one.

The session posts its own handoff to the chatroom, so this adapter never calls
``SpawnContext.on_reply``. It does not post anything into a chatroom thread either:
every failure leaves it as a :class:`ClaudeBgSpawnError` / :class:`AdapterHaltError`
carrying a fixed reason code, and :meth:`ClaudeBgSpawnError.stand_down_body` renders the
text the conductor posts as the stand-down (wired in PR-C).

Spawn failure ladder (Bohr msg-6937). The exit code and stderr of ``claude --bg`` are
always kept and judged first, in this order:

1. non-zero exit, stderr contains ``Workspace not trusted`` -> ``workspace-untrusted``
2. non-zero exit, stderr matches :data:`MODEL_UNAVAILABLE_PATTERN` -> ``model-unavailable``
3. any other non-zero exit -> ``spawn-failed`` (exit code and stderr head in the post)
4. exit 0 -> the session id is resolved from ``claude agents --json --all`` by ``name``

No path relaunches the turn on another model: a Sonnet turn whose model id is refused
stands down loudly instead of silently becoming an Opus turn.

Session id resolution (V-1, msg-6810 §1): the id is never taken from stdout. The
adapter polls the agent list for up to ``resolve_timeout_s`` (10 s) and looks for
background elements whose ``name`` equals the launch name:

- exactly one: adopt its ``id`` and ``sessionId``; a differing stdout id is logged as
  ``bg-id-mismatch`` and is not fatal;
- none by the deadline: ``bg-session-not-listed``;
- two or more: every one of them is stopped and removed, then ``bg-double-spawn``; the
  stand-down names each session that could not be confirmed removed.

Ending a session is always ``stop`` then ``rm`` (V-2), and ``rm`` is confirmed by the id
being absent from a fresh listing afterwards -- on every path (turn end, ``halt``, the
double-spawn cleanup and the orphan sweep). A zero exit from ``rm`` alone is never
reported as a removal.

Pending launches. The launch name is recorded on the session before ``claude --bg`` runs
and cleared only once the session is adopted (one match) or every match is confirmed
removed. If the launch is cancelled, fails, or is not listed in time, the name stays
pending; the next ``deliver_event`` and ``halt`` reconcile it by name (stop -> rm ->
confirm), so a ``--bg`` that detached after its caller gave up is still cleaned up.

A recorded session that has vanished from the list is ``bg-session-lost`` (V-3, no
``respawn``). The state table (V-4): ``done`` is finished; a live session with
``status=idle`` is waiting; anything else is running, and a ``state`` value outside
:data:`KNOWN_BG_STATES` is logged once per value as ``bg-unknown-state`` and still
treated as running so the no-progress timer covers it.

What the launch records (msg-6888, Bohr msg-6935): ``start_head`` and whether the work
tree was clean at launch (the C6 input) -- the two facts the post-turn check needs. The
rollback-era fields (``start_branch``, ``start_local_heads``, ``start_tracking_refs``) are
deliberately not recorded.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from ..conductor.model_policy import ModelChoice
from ..exceptions import (
    AdapterDeliveryError,
    AdapterHaltError,
    AdapterHealthError,
    AdapterSpawnError,
)
from ..ports import SpawnContext
from ..ulid_util import new_ulid
from ..value_objects import (
    Capability,
    ChatroomEvent,
    ErrorInfo,
    Event,
    HealthStatus,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

ADAPTER_ID = "claude-bg"
DEFAULT_EFFORT = "high"
DEFAULT_RESOLVE_TIMEOUT_S = 10.0
DEFAULT_POLL_INTERVAL_S = 0.5
# How long an unsettled launch name is kept while it is NOT listed. One empty listing
# cannot prove a detached launch will never appear (it may surface after the resolve
# deadline), so the name is held, and the next turn / halt() refused, until this much
# time has passed since the launch began. After that it is dropped with a logged
# event; a session that surfaces later still is a managed orphan, and reap_orphans()
# removes it.
DEFAULT_SETTLE_S = 120.0

# Reason codes. Spelled once here; stand-down posts and tests use these names.
REASON_WORKSPACE_UNTRUSTED = "workspace-untrusted"
REASON_MODEL_UNAVAILABLE = "model-unavailable"
REASON_SPAWN_FAILED = "spawn-failed"
REASON_NOT_LISTED = "bg-session-not-listed"
REASON_DOUBLE_SPAWN = "bg-double-spawn"
REASON_SESSION_LOST = "bg-session-lost"
EVENT_ID_MISMATCH = "bg-id-mismatch"
EVENT_UNKNOWN_STATE = "bg-unknown-state"
EVENT_STOP_FAILED = "bg-stop-failed"
EVENT_PENDING_EXPIRED = "bg-pending-expired"

UNTRUSTED_STDERR_MARKER = "Workspace not trusted"
# The single place the "model name refused" wording lives (Bohr msg-6937 step 2).
# PROVISIONAL: the operator checklist records the real stderr of `claude -p --model
# <bogus>` on tomtebo, and this pattern is replaced with that text. Wording that does
# not match falls through to `spawn-failed`, which is just as loud.
MODEL_UNAVAILABLE_PATTERN = re.compile(
    r"model.{0,80}\b(not found|not available|unavailable|invalid|unknown|not supported"
    r"|does not exist|does not support)\b|\b(invalid|unknown) model\b",
    re.IGNORECASE,
)
STDERR_HEAD_CHARS = 500

# `state` values whose meaning is measured (V-4). Others are logged once and run on.
KNOWN_BG_STATES: frozenset[str] = frozenset({"done"})

_STDOUT_ID = re.compile(r"backgrounded\s*\S?\s*([0-9a-f]{8})\b")
# The orphan sweep touches only names this adapter mints: T-...-r<round>-<nonce6>.
MANAGED_NAME = re.compile(r"^T-.+-r\d+-[0-9a-f]{6}$")


# --------------------------------------------------------------------------- #
# Command execution seam
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Runs one command. Tests substitute a fake; production uses :class:`SubprocessRunner`."""

    async def run(self, argv: Sequence[str], *, cwd: str) -> CommandResult: ...


class SubprocessRunner:
    """Runs a command without a shell. A missing executable is exit 127, not an exception."""

    def __init__(self, *, timeout_s: float = 60.0) -> None:
        self._timeout_s = timeout_s

    async def run(self, argv: Sequence[str], *, cwd: str) -> CommandResult:
        exe = shutil.which(argv[0]) or argv[0]
        try:
            proc = await asyncio.create_subprocess_exec(
                exe,
                *argv[1:],
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return CommandResult(127, "", f"{type(exc).__name__}: {exc}")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self._timeout_s)
        except TimeoutError:
            await _terminate(proc)
            return CommandResult(124, "", f"timed out after {self._timeout_s}s: {argv[0]}")
        except asyncio.CancelledError:
            # Cancelling communicate() does not end the child: kill and reap it, then let
            # the cancellation through. A session the CLI already detached survives this;
            # the adapter's pending launch name is what cleans that one up.
            await _terminate(proc)
            raise
        return CommandResult(
            proc.returncode if proc.returncode is not None else -1,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ClaudeBgSpawnError(AdapterSpawnError, AdapterDeliveryError):
    """A turn's ``--bg`` launch failed. ``reason`` is one of the fixed reason codes.

    It is both an :class:`AdapterSpawnError` (msg-6810 names the failures so) and an
    :class:`AdapterDeliveryError`, because the launch happens inside ``deliver_event``
    (one turn, one session) and the dispatcher catches that method's base class.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        returncode: int | None = None,
        stderr: str = "",
        session_ids: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.code = f"adapter.{reason.replace('-', '_')}"
        self.detail = message
        self.returncode = returncode
        self.stderr = stderr
        self.session_ids = session_ids

    def stand_down_body(self) -> str:
        """The stand-down text for the thread: the reason first, then what the operator needs."""
        lines = [f"STAND-DOWN: {self.reason}", "", self.detail]
        if self.returncode is not None:
            lines.append(f"exit code: {self.returncode}")
        head = self.stderr.strip()[:STDERR_HEAD_CHARS]
        if head:
            lines += ["stderr (head):", "```", head, "```"]
        if self.session_ids:
            lines.append("sessions: " + ", ".join(self.session_ids))
        lines.append("The turn was not relaunched on another model.")
        return "\n".join(lines)


def classify_spawn_failure(returncode: int, stderr: str) -> str | None:
    """Steps 1-3 of the ladder: the reason for a failed ``--bg``, or ``None`` on exit 0."""
    if returncode == 0:
        return None
    if UNTRUSTED_STDERR_MARKER in stderr:
        return REASON_WORKSPACE_UNTRUSTED
    if MODEL_UNAVAILABLE_PATTERN.search(stderr):
        return REASON_MODEL_UNAVAILABLE
    return REASON_SPAWN_FAILED


# --------------------------------------------------------------------------- #
# Agent list
# --------------------------------------------------------------------------- #


class AgentListError(Exception):
    """``claude agents --json --all`` failed or did not print a JSON array."""


@dataclass(frozen=True)
class BgAgent:
    """One background element of ``claude agents --json --all`` (interactive ones are dropped)."""

    id: str
    session_id: str | None
    name: str | None
    cwd: str | None
    state: str | None
    status: str | None
    pid: int | None


def parse_agent_list(stdout: str) -> list[BgAgent]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AgentListError(f"agent list is not JSON: {exc}") from exc
    if not isinstance(data, list):
        raise AgentListError("agent list is not a JSON array")
    agents: list[BgAgent] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue  # interactive sessions carry no `id`
        pid = item.get("pid")
        agents.append(
            BgAgent(
                id=item["id"],
                session_id=_opt_str(item.get("sessionId")),
                name=_opt_str(item.get("name")),
                cwd=_opt_str(item.get("cwd")),
                state=_opt_str(item.get("state")),
                status=_opt_str(item.get("status")),
                pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            )
        )
    return agents


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


class BgPhase(StrEnum):
    RUNNING = "running"
    IDLE = "idle"
    DONE = "done"
    LOST = "lost"


def phase_of(agent: BgAgent | None) -> BgPhase:
    """The V-4 table. ``None`` (not listed) is lost."""
    if agent is None:
        return BgPhase.LOST
    if agent.state == "done":
        return BgPhase.DONE
    if agent.pid is not None and agent.status == "idle":
        return BgPhase.IDLE
    return BgPhase.RUNNING


@dataclass(frozen=True)
class BgObservation:
    phase: BgPhase
    agent: BgAgent | None
    reason: str | None  # REASON_SESSION_LOST when phase is LOST


# --------------------------------------------------------------------------- #
# Turn plan and launch record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BgTurnPlan:
    """What the conductor decided for one turn (PR-C builds it from the handoff + Conclair)."""

    identity: str
    round: int
    prompt: str
    choice: ModelChoice


TurnPlanner = Callable[[SessionHandle, ChatroomEvent, bool | None], Awaitable[BgTurnPlan]]
"""``(handle, event, worktree_clean) -> plan``. ``worktree_clean`` feeds ``choose_model``'s C6."""


@dataclass(frozen=True)
class BgLaunchRecord:
    """One launch, as the conductor records it in Conclair (msg-6830 §1 minus msg-6888)."""

    thread_id: str
    round: int
    name: str
    id: str
    session_id: str | None
    model: str
    model_reasons: tuple[str, ...]
    effort: str
    start_head: str | None
    start_worktree_clean: bool | None
    start_ts: datetime


@dataclass(frozen=True)
class ReapReport:
    reaped: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]  # (id, why)


@dataclass
class _Session:
    ctx: SpawnContext
    launch: BgLaunchRecord | None = None
    pending_name: str | None = None
    pending_since: float = 0.0  # self._clock() when pending_name was set
    removed: bool = False
    halted: bool = False
    # Serializes deliver_event and halt on this handle: a halt waits for an in-flight
    # delivery, so whatever that delivery launched is settled by the halt.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_active_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _unsettled(sess: _Session) -> bool:
    """True while halt() still owes cleanup: a pending launch name, or an adopted
    session whose removal has not been confirmed."""
    return sess.pending_name is not None or (sess.launch is not None and not sess.removed)


def mint_name(thread_id: str, round_: int, nonce: str | None = None) -> str:
    return f"{thread_id}-r{round_}-{nonce or secrets.token_hex(3)}"


def _same_dir(a: str | None, b: str) -> bool:
    if a is None:
        return False
    return Path(a).resolve(strict=False) == Path(b).resolve(strict=False)


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #


class ClaudeBgAdapter:
    """``RoleAdapter`` that runs each turn as a fresh ``claude --bg`` session."""

    capabilities: frozenset[Capability] = frozenset(
        {Capability.READ_THREAD, Capability.POST_REPLY, Capability.EXECUTE_CODE}
    )

    def __init__(
        self,
        *,
        repo_dir: str,
        planner: TurnPlanner,
        runner: CommandRunner | None = None,
        adapter_id: str = ADAPTER_ID,
        claude_bin: str = "claude",
        git_bin: str = "git",
        effort: str = DEFAULT_EFFORT,
        resolve_timeout_s: float = DEFAULT_RESOLVE_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        settle_s: float = DEFAULT_SETTLE_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        known_states: frozenset[str] = KNOWN_BG_STATES,
    ) -> None:
        self.adapter_id = adapter_id
        self._repo_dir = repo_dir
        self._planner = planner
        self._runner: CommandRunner = runner or SubprocessRunner()
        self._claude = claude_bin
        self._git = git_bin
        self._effort = effort
        self._resolve_timeout_s = resolve_timeout_s
        self._poll_interval_s = poll_interval_s
        self._settle_s = settle_s
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._known_states = known_states
        self._seen_unknown: set[str] = set()
        self._sessions: dict[SessionHandle, _Session] = {}

    # -- RoleAdapter ------------------------------------------------------ #

    async def spawn(self, thread_ref: ThreadRef, role: Role, ctx: SpawnContext) -> SessionHandle:
        """Registers the session; no process starts until the first turn is delivered."""
        handle = SessionHandle(
            session_id=new_ulid(),
            instance_id=ctx.own_instance_id,
            adapter_id=self.adapter_id,
            thread_ref=thread_ref,
            role=role,
            started_at=self._now(),
        )
        self._sessions[handle] = _Session(ctx=ctx)
        return handle

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        """Launches the turn as a new ``--bg`` session (after ending the previous one)."""
        sess = self._session(handle)
        async with sess.lock:
            self._refuse_if_halted(handle, sess)
            await self._reconcile_pending(sess)
            if sess.launch is not None and not sess.removed:
                await self._end(sess, sess.launch.id)
            sess.launch, sess.removed = None, False
            start_head, worktree_clean = await self._git_start_state()
            plan = await self._planner(handle, event, worktree_clean)
            # halt() sets `halted` without the lock, so a halt that arrived during any
            # await above is seen here; _launch sets the pending name with no await
            # after this check, so the launch is either refused or owed to the halt.
            self._refuse_if_halted(handle, sess)
            # _launch installs sess.launch itself, before anything else is awaited.
            await self._launch(sess, handle.thread_ref, plan, start_head, worktree_clean)
            sess.last_active_at = self._now()

    async def halt(self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)) -> None:
        """``stop`` -> ``rm`` -> confirm absent.

        The handle refuses further turns from the first call on. It is idempotent only
        once nothing is left unsettled: while a launch name is still pending (not yet
        listed, within the settle window) or an adopted session is not yet confirmed
        removed (``rm`` failed, the id stayed listed, or the cleanup was cancelled),
        every call retries and raises :class:`AdapterHaltError` until cleanup is
        confirmed, and health() reports HALTING rather than HALTED.
        """
        sess = self._sessions.get(handle)
        if sess is None or (sess.halted and not _unsettled(sess) and not sess.lock.locked()):
            return
        # Set before waiting for the lock: an in-flight delivery re-checks it before it
        # launches, so it either refuses the launch or leaves one that is cleaned up below.
        sess.halted = True
        async with sess.lock:
            await self._reconcile_pending(sess)
            if sess.launch is not None and not sess.removed:
                await self._end(sess, sess.launch.id)

    async def health(self, handle: SessionHandle) -> HealthStatus:
        sess = self._session_for_health(handle)
        if sess.halted:
            if _unsettled(sess):
                owed: dict[str, str] = {}
                if sess.pending_name is not None:
                    owed["pending"] = sess.pending_name
                if sess.launch is not None and not sess.removed:
                    owed["bg_id"] = sess.launch.id
                return HealthStatus(SessionState.HALTING, sess.last_active_at, None, owed)
            return HealthStatus(SessionState.HALTED, sess.last_active_at, None, {})
        if sess.launch is None or sess.removed:
            return HealthStatus(SessionState.IDLE, sess.last_active_at, None, {})
        try:
            obs = await self.observe(handle)
        except AgentListError as exc:
            raise AdapterHealthError(str(exc)) from exc
        details = {"bg_id": sess.launch.id, "phase": obs.phase.value}
        if obs.phase is BgPhase.LOST:
            err = ErrorInfo(
                code="adapter.bg_session_lost",
                message=f"{REASON_SESSION_LOST}: {sess.launch.id} is no longer listed",
                raised_at=self._now(),
            )
            return HealthStatus(SessionState.FAILED, sess.last_active_at, err, details)
        state = SessionState.PROCESSING if obs.phase is BgPhase.RUNNING else SessionState.IDLE
        return HealthStatus(state, sess.last_active_at, None, details)

    # -- conductor-facing (PR-C) ------------------------------------------ #

    def last_launch(self, handle: SessionHandle) -> BgLaunchRecord | None:
        return self._session(handle).launch

    def pending_launch(self, handle: SessionHandle) -> str | None:
        """The name of a launch not yet adopted or confirmed gone (``None`` when settled)."""
        return self._session(handle).pending_name

    async def observe(self, handle: SessionHandle) -> BgObservation:
        """Where the turn's session is now. Raises :class:`AgentListError` if the list fails."""
        sess = self._session(handle)
        if sess.launch is None:
            raise AdapterHealthError(f"session {handle.session_id} has no launched turn")
        agents = await self._list()
        match = next((a for a in agents if a.id == sess.launch.id), None)
        if match is not None:
            await self._note_state(sess, match)
        phase = phase_of(match)
        return BgObservation(phase, match, REASON_SESSION_LOST if phase is BgPhase.LOST else None)

    async def reap_orphans(self, live_ids: frozenset[str]) -> ReapReport:
        """Stops and removes managed sessions in ``repo_dir`` that are not in ``live_ids``.

        ``live_ids`` is the conductor's set of recorded sessions whose handoff is still
        pending. Only names this adapter mints (``T-*-r*-*``) whose ``cwd`` is ``repo_dir``
        are touched; anyone else's sessions are left alone.

        Sessions this adapter itself still owns are never targets, whatever ``live_ids``
        says: a pending launch name (detached but not yet adopted, or owed to a halt)
        and an adopted session not yet confirmed removed. That protection is read from
        memory after the listing returns, with no await before selection, and it covers
        removal too: a launch name is set pending before ``claude --bg`` runs, so any
        session the listing shows was already protected by then, and a protected name
        stays protected until the adapter itself settles it (adopt keeps it protected
        under ``launch.name``; only confirmed removal or the settle-window expiry drops it).
        """
        agents = await self._list()
        own_names, own_ids = self._owned()
        targets = [
            agent.id
            for agent in agents
            if agent.id not in live_ids
            and agent.id not in own_ids
            and agent.name
            and agent.name not in own_names
            and MANAGED_NAME.match(agent.name)
            and _same_dir(agent.cwd, self._repo_dir)
        ]
        reaped, failed = await self._remove_confirmed(None, targets)
        return ReapReport(reaped, failed)

    def _owned(self) -> tuple[frozenset[str], frozenset[str]]:
        """Names and ids of sessions this adapter still owns (never orphan-sweep targets)."""
        names: set[str] = set()
        ids: set[str] = set()
        for sess in self._sessions.values():
            if sess.pending_name is not None:
                names.add(sess.pending_name)
            if sess.launch is not None and not sess.removed:
                names.add(sess.launch.name)
                ids.add(sess.launch.id)
        return frozenset(names), frozenset(ids)

    # -- internals -------------------------------------------------------- #

    def _session(self, handle: SessionHandle) -> _Session:
        sess = self._sessions.get(handle)
        if sess is None:
            raise AdapterDeliveryError(f"unknown session {handle.session_id}")
        return sess

    @staticmethod
    def _refuse_if_halted(handle: SessionHandle, sess: _Session) -> None:
        if sess.halted:
            raise AdapterDeliveryError(f"session {handle.session_id} is halted")

    def _session_for_health(self, handle: SessionHandle) -> _Session:
        sess = self._sessions.get(handle)
        if sess is None:
            raise AdapterHealthError(f"unknown session {handle.session_id}")
        return sess

    async def _log(self, sess: _Session, kind: str, **fields: Any) -> None:
        # The event log is observational only (I7): a failing sink never breaks the turn.
        with contextlib.suppress(Exception):
            await sess.ctx.on_event_log(Event(new_ulid(), self._now(), kind, fields))

    async def _run_claude(self, *args: str) -> CommandResult:
        return await self._runner.run([self._claude, *args], cwd=self._repo_dir)

    async def _git_start_state(self) -> tuple[str | None, bool | None]:
        head = await self._runner.run([self._git, "rev-parse", "HEAD"], cwd=self._repo_dir)
        status = await self._runner.run([self._git, "status", "--porcelain"], cwd=self._repo_dir)
        start_head = (head.stdout.strip() or None) if head.returncode == 0 else None
        clean = status.stdout.strip() == "" if status.returncode == 0 else None
        return start_head, clean

    async def _list(self) -> list[BgAgent]:
        res = await self._run_claude("agents", "--json", "--all")
        if res.returncode != 0:
            raise AgentListError(
                f"`claude agents --json --all` exited {res.returncode}: "
                f"{res.stderr.strip()[:STDERR_HEAD_CHARS]}"
            )
        return parse_agent_list(res.stdout)

    async def _launch(
        self,
        sess: _Session,
        thread_ref: ThreadRef,
        plan: BgTurnPlan,
        start_head: str | None,
        worktree_clean: bool | None,
    ) -> None:
        """Launches and resolves one turn; on success ``sess.launch`` owns the session.

        Ownership is handed over with no await in between: the record is installed and
        only then is the pending name cleared, so a cancellation at any await leaves at
        least one of the two pointing at the live session for halt() to clean up.
        """
        name = mint_name(thread_ref.thread_id, plan.round)
        start_ts = self._now()
        # Before the CLI runs: a cancelled launch may still detach.
        sess.pending_name, sess.pending_since = name, self._clock()
        res = await self._run_claude(
            "--bg",
            "--agent",
            plan.identity,
            "--model",
            plan.choice.model,
            "--effort",
            self._effort,
            "--dangerously-skip-permissions",
            "--name",
            name,
            plan.prompt,
        )
        reason = classify_spawn_failure(res.returncode, res.stderr)
        if reason is not None:
            raise ClaudeBgSpawnError(
                reason,
                f"`claude --bg` for {name} (model {plan.choice.model}) failed",
                returncode=res.returncode,
                stderr=res.stderr,
            )
        agent = await self._resolve(sess, name)
        sess.launch = BgLaunchRecord(
            thread_id=thread_ref.thread_id,
            round=plan.round,
            name=name,
            id=agent.id,
            session_id=agent.session_id,
            model=plan.choice.model,
            model_reasons=plan.choice.reasons,
            effort=self._effort,
            start_head=start_head,
            start_worktree_clean=worktree_clean,
            start_ts=start_ts,
        )
        sess.removed = False
        sess.pending_name = None  # adopted: the installed launch record owns it now
        stdout_id = _STDOUT_ID.search(res.stdout)
        if stdout_id is not None and stdout_id.group(1) != agent.id:
            await self._log(
                sess, EVENT_ID_MISMATCH, name=name, stdout_id=stdout_id.group(1), id=agent.id
            )

    async def _resolve(self, sess: _Session, name: str) -> BgAgent:
        deadline = self._clock() + self._resolve_timeout_s
        last_error = ""
        while True:
            try:
                matches = [a for a in await self._list() if a.name == name]
            except AgentListError as exc:
                matches, last_error = [], str(exc)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                ids = tuple(a.id for a in matches)
                removed, failed = await self._remove_confirmed(sess, ids)
                if not failed:
                    sess.pending_name = None
                    outcome = "all were stopped and removed"
                else:
                    # The name stays pending so the next turn or halt() retries by name.
                    outcome = "NOT all could be confirmed removed: " + "; ".join(
                        f"{i} ({why})" for i, why in failed
                    )
                    if removed:
                        outcome += "; removed: " + ", ".join(removed)
                raise ClaudeBgSpawnError(
                    REASON_DOUBLE_SPAWN,
                    f"{len(ids)} sessions are named {name}; {outcome}",
                    session_ids=ids,
                )
            if self._clock() >= deadline:
                detail = f"no session named {name} after {self._resolve_timeout_s:g}s"
                raise ClaudeBgSpawnError(
                    REASON_NOT_LISTED, f"{detail} ({last_error})" if last_error else detail
                )
            await self._sleep(self._poll_interval_s)

    async def _end(self, sess: _Session, agent_id: str) -> None:
        _, failed = await self._remove_confirmed(sess, [agent_id])
        if failed:
            raise AdapterHaltError(f"{agent_id}: {failed[0][1]}")
        sess.removed = True

    async def _remove_confirmed(
        self, sess: _Session | None, ids: Sequence[str]
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        """``stop`` -> ``rm`` each id, then one fresh listing decides what was removed.

        Returns ``(removed, failed)``. An id counts as removed only when ``rm`` exited 0
        AND the id is absent from the listing taken afterwards; every other id is in
        ``failed`` with its reason, so no caller can report an unconfirmed removal.
        """
        failed: list[tuple[str, str]] = []
        attempted: list[str] = []
        for agent_id in ids:
            stop = await self._run_claude("stop", agent_id)
            if stop.returncode != 0 and sess is not None:
                await self._log(sess, EVENT_STOP_FAILED, id=agent_id, stderr=stop.stderr[:200])
            rm = await self._run_claude("rm", agent_id)
            if rm.returncode != 0:
                head = rm.stderr.strip()[:STDERR_HEAD_CHARS]
                failed.append((agent_id, f"`claude rm` exited {rm.returncode}: {head}"))
            else:
                attempted.append(agent_id)
        if not attempted:
            return (), tuple(failed)
        try:
            listed = {a.id for a in await self._list()}
        except AgentListError as exc:
            failed += [(i, f"could not confirm removal: {exc}") for i in attempted]
            return (), tuple(failed)
        removed = tuple(i for i in attempted if i not in listed)
        failed += [(i, "still listed after `claude rm`") for i in attempted if i in listed]
        return removed, tuple(failed)

    async def _reconcile_pending(self, sess: _Session) -> None:
        """Cleans up an unsettled launch by name; raises if that cannot be confirmed.

        The name is cleared only on positive evidence: every listed match confirmed
        removed, or no match for longer than the settle window. An empty listing inside
        the window settles nothing (the launch may surface later), so the name is kept
        and this raises, which refuses the next turn and keeps halt() retrying.
        """
        name = sess.pending_name
        if name is None:
            return
        try:
            ids = [a.id for a in await self._list() if a.name == name]
        except AgentListError as exc:
            raise AdapterHaltError(f"could not reconcile pending launch {name}: {exc}") from exc
        if not ids:
            waited = self._clock() - sess.pending_since
            if waited < self._settle_s:
                raise AdapterHaltError(
                    f"pending launch {name} is not listed yet; cannot confirm it never "
                    f"started ({waited:g}s of a {self._settle_s:g}s settle window)"
                )
            sess.pending_name = None
            await self._log(sess, EVENT_PENDING_EXPIRED, name=name, waited_s=waited)
            return
        _, failed = await self._remove_confirmed(sess, ids)
        if failed:
            raise AdapterHaltError(
                f"pending launch {name} not cleaned up: "
                + "; ".join(f"{i} ({why})" for i, why in failed)
            )
        sess.pending_name = None

    async def _note_state(self, sess: _Session, agent: BgAgent) -> None:
        value = agent.state if agent.state is not None else "<absent>"
        if value in self._known_states or value in self._seen_unknown:
            return
        self._seen_unknown.add(value)
        await self._log(sess, EVENT_UNKNOWN_STATE, id=agent.id, state=value)


__all__ = [
    "ADAPTER_ID",
    "KNOWN_BG_STATES",
    "MODEL_UNAVAILABLE_PATTERN",
    "REASON_DOUBLE_SPAWN",
    "REASON_MODEL_UNAVAILABLE",
    "REASON_NOT_LISTED",
    "REASON_SESSION_LOST",
    "REASON_SPAWN_FAILED",
    "REASON_WORKSPACE_UNTRUSTED",
    "AgentListError",
    "BgAgent",
    "BgLaunchRecord",
    "BgObservation",
    "BgPhase",
    "BgTurnPlan",
    "ClaudeBgAdapter",
    "ClaudeBgSpawnError",
    "CommandResult",
    "CommandRunner",
    "ReapReport",
    "SubprocessRunner",
    "classify_spawn_failure",
    "mint_name",
    "parse_agent_list",
    "phase_of",
]
