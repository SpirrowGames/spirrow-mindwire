"""Stage 3 — ``ImplementerSdkAdapter`` (ADR-2026-05-23-07 §2.3, T19).

The third role of the loop: an ``EXECUTE_CODE`` adapter that runs the
implementer on a Claude Agent SDK session **gated by the operation-based
allow-list** (:mod:`spirrow_mindwire.allowlist`,
MINDWIRE_STAGE3_WIRING_ALLOWLIST_SPEC §B). Unlike
:class:`~spirrow_mindwire.adapters.claude_code_sdk.ClaudeCodeSdkAdapter` (the
proposer, ``tools=[]`` text-only), this adapter lets the SDK session use code /
git / fs tools — but every tool call passes through a ``can_use_tool`` guard
that classifies it into an :class:`~spirrow_mindwire.allowlist.Operation` and
checks the allow-list **before execution**. A denied call is *fail-loud*: the
guard interrupts the turn and the adapter halts the session into ``FAILED``
(Stage 2 ``fail-loud no-fallback`` inherited, ADR-07 §2.3 / §2.6).

Inference routing (ADR-07 §2.4 / env spec §3-§4): the implementer PC holds no
Anthropic key — inference goes **via Lexora** (which routes to cloud Claude).
This adapter therefore **never** lets the SDK reach ``api.anthropic.com``
directly: it requires an explicit ``inference_base_url`` (or
``MINDWIRE_IMPLEMENTER_BASE_URL``), wired into the SDK as ``ANTHROPIC_BASE_URL``,
and **refuses to spawn** if none is configured (no silent fallback to the
default endpoint).

Enforcement scope (mirrors :mod:`spirrow_mindwire.allowlist`): the hard
guarantee is that the six Tier C operations are denied. The guard resolves the
*effective* branch from ``.git/HEAD`` (so a bare ``git push`` / ``git commit`` /
``git merge`` while on ``main`` is denied, and an undeterminable branch fails
closed → ``UNKNOWN`` → deny). Shell *indirection* (``bash -c`` / ``eval`` /
``$(...)`` / backticks) hides the inner command from tokenization, so the raw
string is additionally scanned for Tier C patterns. Both remain best-effort over
a single command — defence-in-depth, not the only line. The blast-radius
backstops are: (1) the **environment** (Tailscale ACL + egress default-deny +
scoped token, ADR-07 §2.4 / env spec); and (2) **Takahito's manual merge** — the
Tier C human pre-GO is the authoritative guard that changes never reach ``main``
(GitHub main branch protection is a *planned* hardening, env spec §7, deferred on
the current plan). The loop-level push-to-main / merge-to-main denials here
reduce noise but do not solely carry that guarantee.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from ..allowlist import Allowlist, AllowlistDecision, ClassifiedAction, Operation, default_allowlist
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

_DEFAULT_IMPLEMENTER_SYSTEM_PROMPT = """\
You are the implementer in a Spirrow MindWire ChatRoom thread. You write and \
run code to carry out the agreed proposal. You operate under a strict, \
fail-loud allow-list (Stage 3 autonomy gating):

ALLOWED (Tier A): edit files inside the repository, run tests/builds/code, \
commit and push on feature/* and develop branches, merge feature/* into \
develop, open pull requests. read and search freely.

FORBIDDEN (Tier C — never attempt; they will be denied and halt you): merging \
to or pushing to main, force-push, history rewrite (rebase / reset --hard / \
filter-branch), deleting files, writing to Drive, any external publish/post/send.

Work on a feature/* branch, commit your changes, and (when ready) open a PR to \
develop. When you reply in the thread, reply directly with the message body — \
no preamble, no meta-commentary; your response is posted verbatim.
"""


class ImplementerSdkSpawnError(AdapterSpawnError):
    """``spawn`` failure for the implementer adapter (§3.4)."""


class ImplementerSdkDeliveryError(AdapterDeliveryError):
    """``deliver_event`` failure for the implementer adapter (§3.4)."""


class ImplementerAllowlistError(ImplementerSdkDeliveryError):
    """A tool call was denied by the allow-list → fail-loud halt (ADR-07 §2.3)."""

    def __init__(self, message: str, *, decision: AllowlistDecision) -> None:
        super().__init__(message)
        self.decision = decision


class ImplementerSdkHaltError(AdapterHaltError):
    """``halt`` failure for the implementer adapter (§3.4)."""


class ImplementerSdkHealthError(AdapterHealthError):
    """``health`` failure for the implementer adapter (§3.4)."""


# --------------------------------------------------------------------------- #
# SDK-tool → Operation classifier (the safety-critical mapping)
# --------------------------------------------------------------------------- #

# Bash command separators we split a compound command on before classifying.
_BASH_SEP = re.compile(r"&&|\|\||;|\||\n")

_DELETE_CMDS = {"rm", "rmdir", "unlink", "shred", "del", "erase"}
_PUBLISH_PATTERNS = (
    ("npm", "publish"),
    ("yarn", "publish"),
    ("pnpm", "publish"),
    ("twine", "upload"),
    ("poetry", "publish"),
    ("cargo", "publish"),
    ("gem", "push"),
    ("docker", "push"),
)
# Leading tokens that wrap the real command (skip to classify the inner one).
_CMD_PREFIXES = {"sudo", "command", "time", "env", "nice", "nohup", "xargs"}


def _strip_prefixes(tokens: list[str]) -> list[str]:
    """Drop wrapper prefixes and leading ``VAR=value`` assignments."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _CMD_PREFIXES:
            i += 1
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok):
            i += 1
            continue
        break
    return tokens[i:]


def _git_subcommand(tokens: list[str]) -> tuple[str | None, list[str]]:
    """Return (subcommand, remaining-args) for a ``git`` token list.

    Skips global options (``-C path`` / ``-c k=v`` / other ``-x``) before the
    subcommand.
    """
    rest = tokens[1:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok, rest[i + 1 :]
    return None, []


def _push_target_and_force(args: list[str]) -> tuple[str | None, bool]:
    """Best-effort extract the pushed destination branch + force flag.

    Refspecs: ``branch`` / ``src:dst`` / ``HEAD:dst`` / ``+branch`` (force).
    Destination ``main``/``master`` is surfaced so the allow-list denies a
    direct push to the canonical branch (the loop-level backstop).
    """
    force = any(
        a in ("--force", "-f", "--force-with-lease", "--force-if-includes")
        or a.startswith("--force-with-lease=")
        for a in args
    )
    dest: str | None = None
    positional = [a for a in args if not a.startswith("-")]
    # Drop the remote (first positional) when there is a refspec after it.
    refspecs = positional[1:] if len(positional) >= 2 else []
    for spec in refspecs:
        if spec.startswith("+"):
            force = True
            spec = spec[1:]
        target = spec.split(":", 1)[1] if ":" in spec else spec
        target = target.removeprefix("refs/heads/")
        if target in ("main", "master"):
            return "main", force
        if dest is None and target not in ("HEAD", ""):
            dest = target
    return dest, force


def _classify_git(tokens: list[str]) -> ClassifiedAction:
    sub, args = _git_subcommand(tokens)
    detail = " ".join(tokens)
    if sub == "push":
        dest, force = _push_target_and_force(args)
        if force:
            return ClassifiedAction(Operation.FORCE_PUSH, branch=dest, force=True, detail=detail)
        return ClassifiedAction(Operation.GIT_PUSH, branch=dest, force=False, detail=detail)
    if sub == "commit":
        return ClassifiedAction(Operation.GIT_COMMIT, detail=detail)
    if sub == "merge":
        positional = [a for a in args if not a.startswith("-")]
        source = positional[0] if positional else None
        return ClassifiedAction(Operation.GIT_MERGE, source=source, detail=detail)
    if sub in ("rebase", "filter-branch", "filter-repo"):
        return ClassifiedAction(Operation.HISTORY_REWRITE, detail=detail)
    if sub == "reset" and "--hard" in args:
        return ClassifiedAction(Operation.HISTORY_REWRITE, detail=detail)
    # Other git subcommands (status/log/diff/add/checkout/switch/fetch/pull/
    # branch/stash/...) are non-Tier-C and run as ordinary code execution.
    return ClassifiedAction(Operation.EXEC_CODE, detail=detail)


def _classify_gh(tokens: list[str]) -> ClassifiedAction:
    detail = " ".join(tokens)
    rest = [t for t in tokens[1:] if not t.startswith("-")]
    group = rest[0] if rest else None
    sub = rest[1] if len(rest) > 1 else None
    if group == "pr" and sub == "merge":
        # The implementer never merges PRs (develop merges are local self-review;
        # main is Takahito/Tier C). A PR merge is a canonical promotion.
        return ClassifiedAction(Operation.GIT_MERGE_TO_MAIN, detail=detail)
    if group == "pr" and sub in ("create", "new"):
        base = _flag_value(tokens, "--base") or _flag_value(tokens, "-B")
        return ClassifiedAction(Operation.GITHUB_PR_OPEN, target=base, detail=detail)
    if group == "release" or (group == "repo" and sub in ("delete", "archive")):
        return ClassifiedAction(Operation.EXTERNAL_PUBLISH, detail=detail)
    # pr view/list/diff/checks, issue, api GET, etc. → read via the scoped token.
    return ClassifiedAction(Operation.GITHUB_READ, detail=detail)


def _flag_value(tokens: list[str], flag: str) -> str | None:
    for i, tok in enumerate(tokens):
        if tok == flag and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
    return None


def _classify_single_bash(cmd: str) -> ClassifiedAction:
    cmd = cmd.strip()
    if not cmd:
        return ClassifiedAction(Operation.EXEC_CODE, detail=cmd)
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        tokens = cmd.split()
    tokens = _strip_prefixes(tokens)
    if not tokens:
        return ClassifiedAction(Operation.EXEC_CODE, detail=cmd)
    head = tokens[0]
    base = os.path.basename(head)
    if base in _DELETE_CMDS or "-delete" in tokens or base == "Remove-Item":
        return ClassifiedAction(Operation.FS_DELETE, detail=cmd)
    for prog, sub in _PUBLISH_PATTERNS:
        if base == prog and sub in tokens:
            return ClassifiedAction(Operation.EXTERNAL_PUBLISH, detail=cmd)
    if base == "git":
        return _classify_git(tokens)
    if base == "gh":
        return _classify_gh(tokens)
    return ClassifiedAction(Operation.EXEC_CODE, detail=cmd)


# Shell indirection that defeats token-level classification (the real command is
# a quoted string / substitution we don't tokenize, e.g. ``bash -c "rm -rf x"``,
# ``eval ...``, ``$(...)``, backticks). When present we additionally scan the RAW
# command for Tier C patterns and fail-loud on a match (deny-safe; a false match
# only over-denies an indirection command, which is rare).
_INDIRECTION_RE = re.compile(r"\b(?:bash|sh|zsh|dash)\b\s+-\w*c\b|\beval\b|\$\(|`")
_RAW_FORBIDDEN: tuple[tuple[re.Pattern[str], Operation], ...] = (
    (re.compile(r"\b(?:rm|rmdir|shred|unlink)\b|-delete\b|\bRemove-Item\b"), Operation.FS_DELETE),
    (
        re.compile(r"\bgit\b.*\bpush\b.*(?:--force\b|--force-with-lease\b|\s-f\b)"),
        Operation.FORCE_PUSH,
    ),
    (re.compile(r"\bgit\b.*\b(?:rebase|filter-branch|filter-repo)\b"), Operation.HISTORY_REWRITE),
    (re.compile(r"\bgit\b.*\breset\b.*--hard\b"), Operation.HISTORY_REWRITE),
)


def _scan_raw_forbidden(command: str) -> ClassifiedAction | None:
    """Scan the raw command string for Tier C patterns (indirection backstop)."""
    for pattern, op in _RAW_FORBIDDEN:
        if pattern.search(command):
            return ClassifiedAction(op, detail=command)
    return None


# Ranking so a compound command is judged by its most dangerous sub-command.
_DANGER_RANK: dict[Operation, int] = {
    Operation.GIT_MERGE_TO_MAIN: 100,
    Operation.FORCE_PUSH: 100,
    Operation.HISTORY_REWRITE: 100,
    Operation.FS_DELETE: 100,
    Operation.DRIVE_WRITE: 100,
    Operation.EXTERNAL_PUBLISH: 100,
    Operation.UNKNOWN: 90,
    Operation.GIT_PUSH: 50,
    Operation.GIT_MERGE: 50,
    Operation.GIT_COMMIT: 40,
    Operation.GITHUB_PR_OPEN: 30,
    Operation.FS_WRITE: 20,
    Operation.GITHUB_READ: 5,
    Operation.FS_READ: 1,
    Operation.SEARCH: 1,
    Operation.EXEC_CODE: 0,
}


def _classify_bash(command: str) -> ClassifiedAction:
    """Classify a (possibly compound) bash command by its most dangerous part.

    A one-liner that checks out main and then merges (``git checkout main &&
    git merge develop``) is surfaced as ``git.merge_to_main`` — the static
    catch for the obvious main-merge; cross-tool-call sequences are backed by
    the push-to-main denial + environment containment.
    """
    parts = [p.strip() for p in _BASH_SEP.split(command) if p.strip()]
    if not parts:
        return ClassifiedAction(Operation.EXEC_CODE, detail=command)
    actions = [_classify_single_bash(p) for p in parts]

    switches_to_main = any(
        a.operation is Operation.EXEC_CODE
        and re.search(r"\bgit\s+(checkout|switch)\b.*\b(main|master)\b", a.detail)
        for a in actions
    )
    if switches_to_main and any(a.operation is Operation.GIT_MERGE for a in actions):
        candidate = ClassifiedAction(Operation.GIT_MERGE_TO_MAIN, detail=command)
    else:
        candidate = max(actions, key=lambda a: _DANGER_RANK.get(a.operation, 0))

    # Indirection backstop: a wrapper hides the inner command from tokenization,
    # so scan the raw string and take the result if it is at least as dangerous.
    if _INDIRECTION_RE.search(command):
        scanned = _scan_raw_forbidden(command)
        if scanned is not None and _DANGER_RANK.get(scanned.operation, 0) >= _DANGER_RANK.get(
            candidate.operation, 0
        ):
            return scanned
    return candidate


# MCP read operations are POSITIVELY enumerated (§B.2 default-deny): anything not
# matched as a known write below AND not in this set falls through to UNKNOWN →
# deny. A substring read-allow (e.g. "list_") would let a `list_and_delete`
# variant pass, so reads must be an exact whitelist.
_MCP_READ_OPS = frozenset(
    {
        "get_file_contents",
        "get_commit",
        "list_commits",
        "list_branches",
        "list_tags",
        "get_tag",
        "search_code",
        "search_repositories",
        "search_issues",
        "search_pull_requests",
        "list_pull_requests",
        "pull_request_read",
        "list_issues",
        "issue_read",
        "list_releases",
        "get_latest_release",
        "get_release_by_tag",
        "get_label",
    }
)


def _classify_mcp(tool_name: str, tool_input: dict[str, Any]) -> ClassifiedAction:
    op = tool_name.split("__")[-1].lower()
    # Dangerous writes first (explicit). "delete" anywhere → fs.delete (deny).
    if "merge_pull_request" in op:
        return ClassifiedAction(Operation.GIT_MERGE_TO_MAIN, detail=tool_name)
    if "create_pull_request" in op:
        return ClassifiedAction(
            Operation.GITHUB_PR_OPEN, target=tool_input.get("base"), detail=tool_name
        )
    if "delete" in op:
        return ClassifiedAction(Operation.FS_DELETE, detail=tool_name)
    if "create_or_update_file" in op or "push_files" in op:
        return ClassifiedAction(
            Operation.GIT_PUSH, branch=tool_input.get("branch"), detail=tool_name
        )
    if "smart_create_document" in op or "smart_update_document" in op or op.startswith("drive"):
        return ClassifiedAction(Operation.DRIVE_WRITE, detail=tool_name)
    # Reads are a positive whitelist; everything else is UNKNOWN → default-deny.
    if op in _MCP_READ_OPS:
        return ClassifiedAction(Operation.GITHUB_READ, detail=tool_name)
    return ClassifiedAction(Operation.UNKNOWN, detail=tool_name)


def classify_tool_call(tool_name: str, tool_input: dict[str, Any]) -> ClassifiedAction:
    """Map one SDK tool call to a classified allow-list :class:`ClassifiedAction`.

    Unknown tools map to :attr:`Operation.UNKNOWN`, which the default-deny
    allow-list rejects (fail-loud).
    """
    if tool_name in ("Write", "Edit", "MultiEdit"):
        path = tool_input.get("file_path") or tool_input.get("path")
        return ClassifiedAction(Operation.FS_WRITE, path=path, detail=str(path))
    if tool_name == "NotebookEdit":
        path = tool_input.get("notebook_path") or tool_input.get("file_path")
        return ClassifiedAction(Operation.FS_WRITE, path=path, detail=str(path))
    if tool_name == "Read":
        return ClassifiedAction(Operation.FS_READ, path=tool_input.get("file_path"))
    if tool_name in ("Glob", "Grep"):
        return ClassifiedAction(Operation.SEARCH, detail=tool_name)
    if tool_name == "Bash":
        return _classify_bash(str(tool_input.get("command", "")))
    if tool_name.startswith("mcp__"):
        return _classify_mcp(tool_name, tool_input)
    return ClassifiedAction(Operation.UNKNOWN, detail=tool_name)


# --------------------------------------------------------------------------- #
# can_use_tool guard
# --------------------------------------------------------------------------- #


def _current_branch(repo_root: Path) -> str | None:
    """Read the repo's current branch from ``.git/HEAD``; None if undeterminable.

    Returns None for a detached HEAD (raw sha) or an unreadable / worktree-file
    ``.git`` — the guard treats None as fail-closed (the action is downgraded to
    UNKNOWN → deny) rather than letting a missing branch pass a constraint.
    """
    try:
        head = (repo_root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        return head.split(":", 1)[1].strip().removeprefix("refs/heads/")
    return None  # detached HEAD


class _AllowlistGuard:
    """Per-spawn ``can_use_tool`` callback: classify → enrich → check → allow/deny.

    A deny is recorded (so ``deliver_event`` can fail-loud) and returned with
    ``interrupt=True`` so the SDK aborts the turn immediately.
    """

    def __init__(self, allowlist: Allowlist) -> None:
        self._allowlist = allowlist
        self.violations: list[AllowlistDecision] = []

    def _enrich(self, action: ClassifiedAction) -> ClassifiedAction:
        """Fill an unparsed branch/target from the repo's current branch.

        ``git commit`` / bare ``git push`` carry no branch and ``git merge`` no
        target on the command line, but the *effective* branch is the checked-out
        one. Resolving it from ``.git/HEAD`` closes the fail-open where a commit/
        push/merge while on ``main`` would otherwise pass (None == "no constraint
        to check"). If the branch can't be determined, downgrade to UNKNOWN so
        ``default: deny`` blocks it (fail-closed).
        """
        repo_root = self._allowlist.repo_root
        if action.operation in (Operation.GIT_COMMIT, Operation.GIT_PUSH) and action.branch is None:
            cur = _current_branch(repo_root)
            return (
                replace(action, operation=Operation.UNKNOWN)
                if cur is None
                else replace(action, branch=cur)
            )
        if action.operation is Operation.GIT_MERGE and action.target is None:
            cur = _current_branch(repo_root)
            return (
                replace(action, operation=Operation.UNKNOWN)
                if cur is None
                else replace(action, target=cur)
            )
        return action

    async def __call__(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        action = self._enrich(classify_tool_call(tool_name, tool_input))
        decision = self._allowlist.check(action)
        if decision.allowed:
            return PermissionResultAllow()
        self.violations.append(decision)
        return PermissionResultDeny(message=decision.reason, interrupt=True)


@dataclass
class _Session:
    client: _SdkClient
    ctx: SpawnContext
    own_role: Role
    guard: _AllowlistGuard
    state: SessionState
    last_active_at: datetime
    error: ErrorInfo | None = None


def _build_prompt(event: ChatroomEvent, own_role: Role) -> str:
    payload = event.payload
    return (
        f"You are acting as the {own_role.value} role in thread "
        f"{event.thread_ref.thread_id}.\n\n"
        f"New message from {payload.author}:\n\n{payload.body}\n\n"
        f"Carry out the work in your role, then reply in the thread."
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
        allowlist: Allowlist | None = None,
        inference_base_url: str | None = None,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        system_prompt: str = _DEFAULT_IMPLEMENTER_SYSTEM_PROMPT,
        extra_env: dict[str, str] | None = None,
        client_factory: Any = None,
    ) -> None:
        self._cwd = Path(cwd)
        self._allowlist = allowlist if allowlist is not None else default_allowlist(self._cwd)
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
        self._system_prompt = system_prompt
        self._extra_env = dict(extra_env or {})
        self._client_factory = client_factory or _default_client_factory
        self._sessions: dict[SessionHandle, _Session] = {}

    def _make_options(self, guard: _AllowlistGuard) -> ClaudeAgentOptions:
        env = {"ANTHROPIC_BASE_URL": self._inference_base_url, **self._extra_env}
        kwargs: dict[str, Any] = {
            "cwd": self._cwd,
            "system_prompt": self._system_prompt,
            "tools": [],
            "allowed_tools": self._allowed_tools,
            "mcp_servers": self._mcp_servers,
            "can_use_tool": guard,
            "permission_mode": "default",  # NOT bypassPermissions — the guard must run
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
        guard = _AllowlistGuard(self._allowlist)
        try:
            client = self._client_factory(self._make_options(guard))
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
            guard=guard,
            state=SessionState.IDLE,
            last_active_at=now,
        )
        return handle

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

        guard = session.guard
        session.state = SessionState.PROCESSING
        try:
            await session.client.query(_build_prompt(event, session.own_role))
            body = await _drain_reply(session.client)
            if guard.violations:
                # A denied tool call → fail-loud halt (ADR-07 §2.3), even if the
                # turn otherwise produced text.
                raise _violation(guard.violations[-1])
            await session.ctx.on_reply(
                ReplyDraft(
                    body=body,
                    reply_to_msg_id=payload.msg_id,
                    adapter_metadata={"adapter_id": self.adapter_id, "model": self._model},
                )
            )
        except ImplementerAllowlistError as violation:
            session.state = SessionState.FAILED
            session.error = ErrorInfo(
                code="adapter.allowlist_violation",
                message=violation.decision.reason,
                raised_at=datetime.now(UTC),
            )
            raise
        except Exception as exc:
            session.state = SessionState.FAILED
            # A guard denial may have driven the SDK abort (interrupt=True);
            # surface the allow-list reason as the cause rather than a generic error.
            if guard.violations:
                decision = guard.violations[-1]
                session.error = ErrorInfo(
                    code="adapter.allowlist_violation",
                    message=decision.reason,
                    raised_at=datetime.now(UTC),
                )
                raise ImplementerAllowlistError(
                    f"allow-list denied {decision.operation.value}: {decision.reason}",
                    decision=decision,
                ) from exc
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


def _violation(decision: AllowlistDecision) -> ImplementerAllowlistError:
    return ImplementerAllowlistError(
        f"allow-list denied {decision.operation.value}: {decision.reason}", decision=decision
    )


__all__ = [
    "ImplementerAllowlistError",
    "ImplementerSdkAdapter",
    "ImplementerSdkDeliveryError",
    "ImplementerSdkHaltError",
    "ImplementerSdkHealthError",
    "ImplementerSdkSpawnError",
    "classify_tool_call",
]
