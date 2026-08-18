"""Stage 3 — ``ImplementerSdkAdapter`` (ADR-2026-05-23-07 §2.3, T19).

The third role of the loop: an ``EXECUTE_CODE`` adapter that runs the
implementer on a Claude Agent SDK session **gated by the operation-based
allow-list** (:mod:`spirrow_mindwire.allowlist`,
MINDWIRE_STAGE3_WIRING_ALLOWLIST_SPEC §B). Unlike
:class:`~spirrow_mindwire.adapters.claude_code_sdk.ClaudeCodeSdkAdapter` (the
proposer, which is given Read/Glob/Grep and nothing else), this adapter lets
the SDK session use code / git / fs tools — but every tool call passes through
a ``can_use_tool`` guard that classifies it into an
:class:`~spirrow_mindwire.allowlist.Operation` and checks the allow-list
**before execution**. A denied call is *fail-loud*: the
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
guarantee is that four Tier C operations are denied unconditionally
(``git.merge_to_main`` / ``fs.delete`` / ``drive.write`` / ``external.publish``),
and that ``force_push`` / ``history_rewrite`` are denied on any branch outside
``feature/*`` / ``develop`` (branch-scoped Tier A since 2026-08-15,
T-branch-scoped-implementer-permissions). The guard resolves the *effective*
branch from ``.git/HEAD`` (so a bare ``git push`` / ``git commit`` /
``git merge`` / ``git rebase`` / ``git reset --hard`` while on ``main`` is
denied, and an undeterminable branch fails closed → ``UNKNOWN`` → deny).
Shell *indirection* (``bash -c`` / ``eval`` / ``$(...)`` / backticks)
hides the inner command from tokenization, so the inner
command is extracted and re-classified through the **same** structural classifier
(a wrapped Tier C command is judged identically to its direct form — T27), with a
coarse keyword scan as a defence-in-depth floor for input that defeats the
tokenizer. Both remain best-effort over a single command — defence-in-depth, not
the only line. The blast-radius
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
import subprocess
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
from ..conductor.handoff import build_handoff_protocol_block
from ..denial_record import build_denial_record
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
# no ``SlashCommand``) to keep the surface minimal. Exposure is NOT auto-approval:
# every call still routes through the ``can_use_tool`` allow-list guard
# (``allowed_tools=[]`` + ``permission_mode="default"``). ``BashOutput`` /
# ``KillShell`` / ``TodoWrite`` are benign and whitelisted in the classifier
# (``_BENIGN_BUILTIN_TOOLS``); the others map to their fs/git/exec operations.
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


class ImplementerAllowlistError(ImplementerSdkDeliveryError):
    """A tool call was denied by the allow-list → fail-loud halt (ADR-07 §2.3).

    ``denial_record`` carries the structured description of *what was attempted*
    (``spec/design/T-denial-detail-and-overdeny.md``). It stays optional so the
    error can still be raised on paths that have a decision but no action; a
    ``None`` record degrades the log line, it does not break the halt.
    """

    def __init__(self, message: str, *, decision: AllowlistDecision) -> None:
        super().__init__(message)
        self.decision = decision
        self.denial_record: dict[str, object] | None = None


class ImplementerSdkHaltError(AdapterHaltError):
    """``halt`` failure for the implementer adapter (§3.4)."""


class ImplementerSdkHealthError(AdapterHealthError):
    """``health`` failure for the implementer adapter (§3.4)."""


# --------------------------------------------------------------------------- #
# SDK-tool → Operation classifier (the safety-critical mapping)
# --------------------------------------------------------------------------- #

# Bash command separators we split a compound command on before classifying.
# This plain regex does NOT honor quoting, so it can OVER-split a quoted body
# (``bash -c "git push origin main; rm -rf /"`` splits at the inner ``;``). That is
# fail-safe by construction: each fragment is classified independently and
# ``_classify_bash`` takes the MOST dangerous verdict (max over ``_DANGER_RANK``),
# so a stray split can only surface an extra dangerous fragment — never downgrade
# one. The clean path for wrapped commands is the ``_indirection_inner`` recursion;
# this split is the compound-command catch.
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
# Leading no-arg launchers that wrap the real command (skip to classify the inner
# one) — used by _strip_prefixes for the DIRECT path (e.g. `exec rm -rf x`).
_CMD_PREFIXES = {
    "sudo",
    "command",
    "time",
    "env",
    "nice",
    "nohup",
    "xargs",
    "exec",
    "setsid",
    "doas",
}

# Launchers that may precede a `<shell> -c` wrapper — a superset of _CMD_PREFIXES
# that also covers value-taking ones (`timeout 5` / `nice -n 10` / `stdbuf -oL`).
# Used by _find_shell_index to locate the wrapped shell across a leading launcher
# run, so a launcher before `bash -c` does not hide the inner command from the
# structural classifier (#74 naysayer MUST-1 / Copilot).
_LAUNCHER_CMDS = _CMD_PREFIXES | {
    "timeout",
    "stdbuf",
    "ionice",
    "taskset",
    "chrt",
    "setarch",
    "flock",
}

# bash options that consume a following argument; the argument must be skipped when
# scanning for `-c`, else `bash --rcfile myrc -c "<x>"` stops at `myrc` and never
# reaches `-c` (#74 naysayer MUST-1 / Copilot).
_BASH_VALUE_OPTS = {"--rcfile", "--init-file", "-O", "+O"}


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


class _Undecidable:
    """Sentinel type: this command rewrites something, and we cannot say what."""


_UNDECIDABLE = _Undecidable()

#: ``git rebase`` flags that take no value. Anything outside this set — including
#: every value-taking flag — makes the positional count untrustworthy, because an
#: unconsumed value looks exactly like a branch name. ``--onto`` is handled
#: separately since its value must be consumed to count the rest correctly.
_REBASE_NO_VALUE_FLAGS: frozenset[str] = frozenset(
    {
        "-i",
        "--interactive",
        "--continue",
        "--abort",
        "--skip",
        "--quit",
        "--autostash",
        "--no-autostash",
        "--autosquash",
        "--no-autosquash",
        "--fork-point",
        "--no-fork-point",
        "--keep-empty",
        "--no-keep-empty",
        "--rebase-merges",
        "--no-verify",
        "--verify",
        "--committer-date-is-author-date",
        "--ignore-date",
        "--force-rebase",
        "-f",
        "-q",
        "--quiet",
        "-v",
        "--verbose",
        "-n",
        "--no-stat",
        "--stat",
    }
)


def _rebase_target(args: list[str]) -> str | None | _Undecidable:
    """Which branch does this ``git rebase`` rewrite?

    ``None`` means HEAD, and the guard's enrichment resolves it. A string is an
    explicitly named branch. :data:`_UNDECIDABLE` means refuse.

    This exists because ``git rebase <upstream> <branch>`` **checks out and
    rewrites <branch>**, whatever is currently checked out. Measured: on
    ``feature/x``, ``git rebase develop main`` moved ``main`` and left HEAD on
    it. Reading every rebase as "targets HEAD" and filling the current branch in
    therefore let a rewrite of ``main`` borrow ``feature/x``'s permission — the
    bypass Tier B found on PR #158.

    The refusal is deliberately broad. An unrecognised flag is refused rather
    than skipped, because a value-taking flag whose value is not consumed looks
    like a positional: ``git rebase --exec make develop`` would read as two
    positionals and name ``develop`` as the target while the real target is
    HEAD, which is fail-OPEN when HEAD is ``main``. Recognising less costs a
    denial the implementer can work around by writing the simple form; guessing
    costs ``main``.
    """
    positionals: list[str] = []
    options_done = False
    index = 0
    while index < len(args):
        arg = args[index]
        if options_done:
            # git stops parsing options at `--`; everything after it is a
            # positional even when it starts with a dash. Skipping `--` without
            # latching that put this parser out of step with git — the desync
            # Tier B found on PR #158 round 2. Measured, the two shapes it named
            # (`git rebase -- -i main`, `git rebase -- --onto main`) are refused
            # by git itself with `fatal: invalid upstream`, so neither rewrote
            # anything; `git rebase -- develop main` DID rewrite `main`, and was
            # already denied here. The latch is kept anyway: agreeing with git is
            # cheaper to hold than an argument about which of its errors save us.
            positionals.append(arg)
            index += 1
            continue
        if arg == "--":
            options_done = True
            index += 1
            continue
        if arg == "--onto":
            index += 2  # its value is a commit-ish, not the rewritten branch
            continue
        if arg.startswith("-"):
            if arg not in _REBASE_NO_VALUE_FLAGS:
                return _UNDECIDABLE
            index += 1
            continue
        positionals.append(arg)
        index += 1

    if len(positionals) <= 1:
        # `git rebase`, `git rebase --continue`, `git rebase <upstream>`: the
        # rewritten branch is the checked-out one.
        return None
    if len(positionals) == 2:
        # `git rebase <upstream> <branch>` — the second is checked out and
        # rewritten.
        return positionals[1]
    return _UNDECIDABLE


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
    if sub == "rebase":
        target = _rebase_target(args)
        if isinstance(target, _Undecidable):
            return ClassifiedAction(Operation.UNKNOWN, detail=detail)
        return ClassifiedAction(Operation.HISTORY_REWRITE, branch=target, detail=detail)
    if sub in ("filter-branch", "filter-repo"):
        # Left where it was: these take rev-list arguments, so any of them can
        # name a branch, and they are rare enough that recognising their shapes
        # would be cost without a caller. UNKNOWN → default-deny, i.e. exactly
        # the Tier C denial they had before this widening.
        return ClassifiedAction(Operation.UNKNOWN, detail=detail)
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
        # `gh release` is treated as publish for ALL subcommands — including reads
        # (list/view/download). Deliberate over-deny on the safe side; the
        # _RAW_FORBIDDEN indirection pattern mirrors it so direct == wrapped (T23).
        return ClassifiedAction(Operation.EXTERNAL_PUBLISH, detail=detail)
    if group == "api" and _gh_api_is_mutation(tokens):
        # A mutating `gh api` (-X/--method write verb, or field flags that make gh
        # default to POST) bypasses the gh pr/release checks above — e.g.
        # `gh api repos/o/r/merges -X PUT` or `... -f base=main`. Legit implementer
        # mutations go via `gh pr create` / MCP create_pull_request, so any raw
        # mutating gh api is UNKNOWN → default-deny (T23). A read (GET / no fields)
        # falls through to GITHUB_READ below.
        return ClassifiedAction(Operation.UNKNOWN, detail=detail)
    # pr view/list/diff/checks, issue, api GET, etc. → read via the scoped token.
    return ClassifiedAction(Operation.GITHUB_READ, detail=detail)


def _flag_value(tokens: list[str], flag: str) -> str | None:
    for i, tok in enumerate(tokens):
        if tok == flag and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
    return None


_GH_API_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _gh_api_is_mutation(tokens: list[str]) -> bool:
    """True if a ``gh api`` call mutates rather than reads (T23).

    Mutating = an explicit write method (``-X`` / ``--method`` =
    POST/PUT/PATCH/DELETE), or field flags (``-f`` / ``-F`` / ``--field`` /
    ``--raw-field`` / ``--input``) which make ``gh api`` default to POST. An
    explicit GET/HEAD method, or no fields at all, reads.
    """
    method = (_flag_value(tokens, "-X") or _flag_value(tokens, "--method") or "").upper()
    if not method:
        # Short flag with the value concatenated, e.g. `-XPUT` / `-Xdelete` (gh
        # accepts this; _flag_value only catches `-X PUT` and `-X=PUT`).
        for tok in tokens:
            concat = re.fullmatch(r"-X([A-Za-z]+)", tok)
            if concat:
                method = concat.group(1).upper()
                break
    if method:
        return method in _GH_API_MUTATING_METHODS
    for tok in tokens:
        if tok in ("-f", "-F", "--field", "--raw-field", "--input"):
            return True
        if tok.startswith(("--field=", "--raw-field=", "--input=")) or re.match(r"-[fF].*=", tok):
            return True
    return False


_SHELL_CMDS = {"bash", "sh", "zsh", "dash"}

# Bound the recursion when indirection nests (``bash -c "bash -c ..."`` /
# ``$( $(...) )``). Past this we cannot meaningfully analyze the inner command, so
# we fail closed (UNKNOWN → deny) rather than spin. Legit commands nest 0-1
# levels; 5 is generous. The cap's residual value is mainly for verbs the coarse
# floor cannot see when nested (e.g. gh api mutation) — a dangerous verb the floor
# DOES know (rm / force-push / publish) is caught at a shallow layer by the raw
# scan before the cap is reached (#74 naysayer MINOR-3).
_MAX_INDIRECTION_DEPTH = 5


def _find_shell_index(tokens: list[str]) -> int | None:
    """Index of the wrapped shell (``bash``/``sh``/``zsh``/``dash``), if any.

    Allows a leading run of launcher tokens before the shell: a launcher name
    (``_LAUNCHER_CMDS``), an option (``-x`` / ``--x`` / ``+x``), a ``VAR=val``
    assignment, or — once a launcher has been seen — a bare operand such as
    ``timeout``'s duration. Stops (returns None) at a non-launcher command in head
    position, so a genuine command like ``rm -rf x`` is never mistaken for a
    wrapper. Deny-safe: at worst it locates a shell token that is actually an
    argument, but only a following ``-c`` (see :func:`_extract_dash_c`) turns that
    into an extracted inner — and over-extraction can only over-deny.
    """
    seen_launcher = False
    for idx, tok in enumerate(tokens):
        base = os.path.basename(tok)
        if base in _SHELL_CMDS:
            return idx
        if base in _LAUNCHER_CMDS:
            seen_launcher = True
            continue
        if tok.startswith(("-", "+")):
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok):
            continue
        if seen_launcher:
            continue  # an operand of a launcher (e.g. timeout's duration)
        return None  # a non-launcher command in head position → not a wrapper
    return None


def _extract_dash_c(tokens: list[str], start: int) -> str | None:
    """From ``start``, find a ``-c``-style flag and return its argument (the inner).

    Skips value-taking bash options (``_BASH_VALUE_OPTS``) and their arguments, so
    ``bash --rcfile myrc -c "<x>"`` / ``bash -O extglob -c "<x>"`` still reach
    ``-c``. Returns None for ``bash script.sh`` (a bare token before any ``-c`` =
    script/file form — nothing to recurse into).

    Lookahead for value-taking options (#74 main Round-2 MINOR): if the next token
    is itself a ``-c``-style flag, treat the value-taking option as *arg-less* and
    let ``-c`` be parsed normally (so ``bash -O -c "<x>"`` classifies on the
    inner). Real bash 5.x errors out on this form rather than running ``<x>`` (the
    production runtime would not execute it), but classifying on *intent* — and so
    denying the form regardless — costs nothing and is robust against a future
    bash relaxing the requirement or a non-bash shell with different semantics.
    """
    i = start
    while i < len(tokens):
        tok = tokens[i]
        if re.fullmatch(r"-\w*c", tok):  # -c / -lc / -xc … (flag ending in c)
            return tokens[i + 1] if i + 1 < len(tokens) else None
        if tok in _BASH_VALUE_OPTS:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt is not None and re.fullmatch(r"-\w*c", nxt):
                i += 1  # option used without its argument; -c is the next token
            else:
                i += 2  # option WITH arg, skip both
            continue
        if not tok.startswith(("-", "+")):
            return None  # bare token before -c → script/file form
        i += 1
    return None


def _indirection_inner(tokens: list[str]) -> str | None:
    """Return the inner command string for a shell ``-c`` / ``eval`` wrapper.

    Locates the wrapped shell across any leading launcher run
    (``env``/``exec``/``timeout 5``/… — :func:`_find_shell_index`) and extracts its
    ``-c`` argument past value-taking options (:func:`_extract_dash_c`); ``eval
    <args...>`` → the joined args. Returns None when there is no such wrapper (e.g.
    ``bash script.sh`` runs a file). The caller recurses the result through the
    *same* classifier, so a wrapped command is judged identically to the direct
    form — the T27 unification, no regex mirror. (For ``bash -c X Y Z`` only ``X``
    is the command; ``Y``/``Z`` become ``$0``/``$1`` and are not executed.)
    """
    shell_idx = _find_shell_index(tokens)
    if shell_idx is not None:
        return _extract_dash_c(tokens, shell_idx + 1)
    if tokens and os.path.basename(tokens[0]) == "eval":
        rest = tokens[1:]
        return " ".join(rest) if rest else None
    return None


def _extract_substitutions(command: str) -> list[str]:
    """Extract inner command strings from ``$(...)`` and backtick substitutions.

    ``$(...)`` is matched with balanced-paren scanning so nesting (``$( $(...) )``)
    works: the outer body is returned and the caller recurses into it (finding the
    next level). Backticks (which don't nest) are matched pairwise; an unclosed
    trailing backtick takes the remainder as its body. Deny-safe on a missing close
    paren / backtick: the remainder of the string is returned as the body so its
    content is still classified.
    """
    inners: list[str] = []
    i, n = 0, len(command)
    while i < n:
        # `i + 1 < n` guard so a trailing `$` (or `$(` at the very end) is handled
        # rather than skipped — detection-miss is under-deny, so stay inclusive
        # (#74 naysayer SHOULD-2).
        if command[i] == "$" and i + 1 < n and command[i + 1] == "(":
            depth, j = 1, i + 2
            while j < n and depth > 0:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                j += 1
            inners.append(command[i + 2 : j - 1] if depth == 0 else command[i + 2 : j])
            i = j
        else:
            i += 1
    # Backticks (no nesting): each `...` body; an unclosed trailing backtick takes
    # the remainder as its body, mirroring the $( deny-safe rule above.
    bt = command.find("`")
    while bt != -1:
        close = command.find("`", bt + 1)
        if close == -1:
            inners.append(command[bt + 1 :])
            break
        inners.append(command[bt + 1 : close])
        bt = command.find("`", close + 1)
    return inners


def _classify_single_bash(cmd: str, _depth: int = 0) -> ClassifiedAction:
    cmd = cmd.strip()
    if not cmd:
        return ClassifiedAction(Operation.EXEC_CODE, detail=cmd)
    try:
        raw_tokens = shlex.split(cmd, posix=True)
    except ValueError:
        raw_tokens = cmd.split()
    if not raw_tokens:
        return ClassifiedAction(Operation.EXEC_CODE, detail=cmd)
    # Shell indirection (<launcher…> <shell> -c / eval): extract the inner command
    # and recurse through the SAME classifier, so `bash -c "<x>"` is judged exactly
    # as `<x>` (T27: one classifier, no regex mirror to drift). Run on the RAW
    # tokens — before _strip_prefixes — so a launcher (env/exec/timeout) keeps its
    # name for _find_shell_index. The wrapper is plain code execution; the verdict
    # is the more dangerous of wrapper vs. inner.
    inner = _indirection_inner(raw_tokens)
    if inner is not None:
        wrapper = ClassifiedAction(Operation.EXEC_CODE, detail=cmd)
        nested = _classify_bash(inner, _depth + 1)
        return max((wrapper, nested), key=lambda a: _DANGER_RANK.get(a.operation, 0))
    # Direct command: strip wrapper prefixes, then classify the real command.
    tokens = _strip_prefixes(raw_tokens)
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


# Detects the *presence* of indirection (shell -c / eval / $(...) / backticks).
# Used only to gate the COARSE scan below — precise classification is the
# recursive structural pass (_classify_single_bash / _extract_substitutions),
# not this regex. The shell branch allows option/arg tokens between the shell and
# `-c` (``bash --rcfile x -c`` / ``bash -l -c``) so the coarse floor still fires
# when the inner is tokenizer-defeating (ANSI-C ``$'...'``) — #74 naysayer / Copilot.
_INDIRECTION_RE = re.compile(r"\b(?:bash|sh|zsh|dash)\b(?:\s+\S+)*?\s+-\w*c\b|\beval\b|\$\(|`")

# COARSE defence-in-depth floor (T27). A deliberately broad keyword scan over the
# raw string, applied ONLY when indirection is present and ONLY to ADD a denial —
# it is never the sole classifier. The recursive structural pass above is the
# single source of truth and already covers every *tokenizable* wrapped command
# (so direct == wrapped); this floor only fails closed on input that defeats the
# tokenizer (ANSI-C ``$'...'`` quoting, a Tier C verb smuggled into a non-executed
# ``bash -c X Y`` slot, etc.).
#
# It is intentionally NOT a per-operation mirror of the structural checks: in
# particular it carries NO ``gh api`` method/field logic — that precision lives
# solely in ``_gh_api_is_mutation`` (applied via recursion), which removes the
# #71 (T23) structured-vs-regex drift class that motivated this task. A gap here
# can no longer cause an *under*-deny, because the structural classifier runs on
# the extracted inner regardless; at worst this floor over-denies, which is safe.
# Consequence (scope): a `gh api` mutation that ALSO defeats the tokenizer (e.g.
# ANSI-C `$'gh api ... -XPUT'`) is caught by NEITHER the recursion nor this floor —
# that residue is out of scope (environment containment + human merge carry it),
# unlike rm / force-push / publish, which the floor still catches in the raw text.
_RAW_COARSE: tuple[tuple[re.Pattern[str], Operation], ...] = (
    (re.compile(r"\b(?:rm|rmdir|shred|unlink)\b|-delete\b|\bRemove-Item\b"), Operation.FS_DELETE),
    (
        re.compile(r"\bgit\b.*\bpush\b.*(?:--force\b|--force-with-lease\b|\s-f\b)"),
        Operation.FORCE_PUSH,
    ),
    (re.compile(r"\bgit\b.*\b(?:rebase|filter-branch|filter-repo)\b"), Operation.HISTORY_REWRITE),
    (re.compile(r"\bgit\b.*\breset\b.*--hard\b"), Operation.HISTORY_REWRITE),
    (
        re.compile(
            r"\b(?:npm|yarn|pnpm|poetry|cargo)\s+publish\b"
            r"|\btwine\s+upload\b|\bgem\s+push\b|\bdocker\s+push\b|\bgh\b.*\brelease\b"
        ),
        Operation.EXTERNAL_PUBLISH,
    ),
)


# Commands whose stdin is DATA, never a script. Deliberately an ALLOW-list of
# sinks rather than a DENY-list of interpreters, because the two fail in opposite
# directions: forgetting an interpreter (``perl`` / ``node`` / ``xargs sh``) would
# hide a real command from the floor, while forgetting a sink only leaves that
# sink as over-strict as it is today. Only the safe omission is possible here.
#
# Matched against the LEADING tokens of the command that owns the heredoc, so
# ``gh pr create`` does not also license ``gh pr merge``.
_HEREDOC_DATA_SINKS: tuple[tuple[str, ...], ...] = (
    ("git", "commit"),
    ("git", "tag"),
    ("git", "notes"),
    ("gh", "pr", "create"),
    ("gh", "pr", "edit"),
    ("gh", "pr", "comment"),
    ("gh", "issue", "create"),
    ("gh", "issue", "comment"),
    ("gh", "issue", "edit"),
    ("gh", "release", "create"),
)

# The opener line of the ONE command shape whose heredoc body is treated as data:
# a data-sink invocation, then a single quoted heredoc opener, and nothing else.
#
# The owner is never masked, so nothing hidden in it can escape the floor. The
# charset therefore has exactly one job: refuse every character that could move
# where the body BEGINS, because blanking a line bash would run is the fail-OPEN
# direction that four earlier rounds fell into. Measured, not reasoned — each
# shape below was run under bash with ``touch <marker>`` as line 1:
#
#   ``\``            a line continuation, so the body starts later;
#   ``;`` ``&``      a trailing operator leaves the list open, or introduces a
#   ``|``            second opener whose body comes first;
#   ``$`` backtick   an unclosed substitution bash keeps reading past the newline;
#   ``<`` ``>``      process substitution ``<(`` / ``>(``, and ``<<`` itself.
#
# Ordinary punctuation is admitted, because it measured inert: ``: , + @ ! % ^ ~
# * ? ( ) [ ] { }`` all left line 1 as data. So did the naysayer's own example,
# ``--title "feat(ui): fix layout, update docs (#12)"`` — refusing those broke
# the feature for Conventional Commits and issue refs, which is what round 6
# reported. Non-ASCII is admitted for the same reason: no bash operator is
# non-ASCII, and this loop's commit messages are frequently Japanese.
#
# Quotes are admitted because the owner is handed to ``shlex.split(posix=True)``,
# which raises on an unclosed one — the only way a quote could push the line's
# end past this newline (verified: ``--title "x`` raises ``No closing quotation``).
# A quoted metacharacter is inert to bash and stays visible to the floor anyway.
#
# ``#`` is the one the round-6 critique got wrong, and the measurement is the
# reason it is handled separately rather than admitted: see below.
_HEREDOC_OWNER_CHARS = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 \t._/=-:,+@!%^~*?()[]{}<>'\"#"
)
# The delimiter is any run of characters that is neither whitespace nor a quote.
# It used to be a word-character run, which has no hyphen, so ``<<'EOF-1'`` and
# ``<<'EOF-MARKER'`` — both valid, both measured — stopped being recognised and
# their prose went to the coarse floor. Widening it cannot loosen anything: the
# terminator has to equal the delimiter exactly, on this side and in bash alike,
# so admitting more spellings only lets more real heredocs be seen. Quotes are
# excluded because one would end the opener early, whitespace because bash's own
# delimiter cannot contain any once the quotes come off. (Tier B, PR #156 r14.)
_NON_ASCII_RANGE = "\u0080-\U0010ffff"
"""Every code point above ASCII, as one class range. No bash operator lives
here, so admitting the lot cannot introduce one, and it keeps a Japanese
commit title from silently falling out of the recognised shape."""

_SINK_HEREDOC_OPENER_LINE_RE = re.compile(
    r"^(?P<owner>[" + re.escape(_HEREDOC_OWNER_CHARS) + _NON_ASCII_RANGE + r"]+?)"
    r"<<(?P<dash>-?)[ \t]*(?P<q>['\"])(?P<delim>[^\s'\"]+)(?P=q)[ \t]*$"
)

# ``#`` is a comment only where it BEGINS a word — bash's own rule, and the whole
# difference between the two halves of the round-6 critique. Mid-word it is an
# ordinary character (``a#b``, ``"closes (#12)"``), which is the form real PR
# titles use. At the start of a word it comments out the rest of the line —
# including the ``<<'A'`` opener — so there is no heredoc at all and every line
# after it is live shell. Measured: ``true # <<'A'`` runs line 1. Admitting ``#``
# unconditionally, as round 6 asked, would have masked that line.
#
# Whitespace inside quotes is not word separation, so ``--title "a # b"`` is
# refused here although bash would keep it literal. That direction is the safe
# one: the command is left whole for the floor to read.
#
# A word also begins after a METACHARACTER, not only after whitespace, and ``)``
# is a metacharacter that this charset admits. Round 8 turned that into a real
# bypass, measured::
#
#     (
#     git commit -F - )#<<'EOF'
#     rm -rf /tmp/x
#     EOF
#
# bash closes the subshell at ``)``, treats ``#<<'EOF'`` as a comment — so there
# is no heredoc — and runs the deletion. ``shlex`` splits on whitespace only, so
# it hands back ``)#`` as one token and the sink prefix still matches. The other
# metacharacters (``|`` ``&`` ``;`` ``<`` ``>``) cannot appear: the charset has
# never admitted them. Measured over every admitted character, in five bash
# contexts, ``)`` was the only miss.
#
# ``(`` is deliberately NOT in this class, though it is a metacharacter too. It
# cannot open a comment anywhere this pattern is used: the opener line must begin
# with a sink token, so ``(`` can only land in argument position, where bash
# raises a syntax error and runs nothing. Adding it "to be safe" cost the round-6
# fix instead — ``--title "… (#12)"`` contains ``(#``, so every Conventional
# Commit with an issue ref stopped being masked and went back to killing the
# conductor. Round 9 caught that, and only caught it because the test payload
# was made live; ``says git rm`` is inert to the floor and hid the regression.
# ``<`` and ``>`` used to be banned from the owner outright, which refused
# ``--author="Name <email>"`` and ``--title "fix: <Button> layout"`` —
# ordinary flags whose angle brackets are inside quotes and inert. Round 15 asked
# for them back, arguing the owner is never masked so nothing in it can hide.
# That argument does not hold: an unquoted ``<<`` opens a SECOND heredoc, and an
# unquoted delimiter means bash EXPANDS that body. Measured::
#
#     git commit -F - <<X <<'A'
#     $(rm -rf /tmp/x)
#     X
#     prose
#     A
#
# the substitution runs, and it sits inside the span this function blanks.
#
# So the question is not whether the character is present but whether it is
# QUOTED, and shlex answers that instead of reasoning: with ``punctuation_chars``
# on, an unquoted ``<< < > ( ) ; | &`` comes back as its own token while a quoted
# one stays inside its word. Verified both ways.
_SHELL_PUNCTUATION = frozenset("();<>|&")


def _has_unquoted_punctuation(text: str) -> bool:
    """Does an unquoted shell metacharacter appear in ``text``?"""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        # Unbalanced quoting: we cannot say, so say yes.
        return True
    return any(token and set(token) <= _SHELL_PUNCTUATION for token in tokens)


_HASH_BEGINS_A_WORD_RE = re.compile(r"(?:^|[ \t)])#")

# An ordinary command line: the same charset as the owner, over the whole line.
# It is what lets the scan step over ``git add .`` on its way to the commit,
# which is the false negative round 7 reported. Empty lines qualify.
_PLAIN_COMMAND_LINE_RE = re.compile(
    r"^[" + re.escape(_HEREDOC_OWNER_CHARS) + _NON_ASCII_RANGE + r"]*$"
)

# Stepping over a line asserts more than "the next line starts a new command".
# It also asserts that a sink invocation FURTHER DOWN still means what it says —
# and a line the scan walks past can take that away. Measured (round 12)::
#
#     git() {
#     bash
#     }
#     git commit -F - <<'EOF'
#     rm -rf /
#     EOF
#
# Every one of those three lines is ordinary text with balanced quotes, so the
# scan stepped over all three, recognised the sink, and blanked the body. bash
# then ran `git commit -F -` as the FUNCTION, which execs `bash`, which inherits
# stdin — the heredoc — and executes the deletion. Seven more spellings did the
# same: `git ()  {`, `function git {`, `PATH=/tmp`, `export PATH=/tmp`,
# `source f`, `. f`, and `shopt -s expand_aliases` + `alias git=bash`.
#
# The first answer to that was "no ``( ) { }``, and the first word must name an
# external program, because a subprocess cannot touch the calling shell". The
# second half of that was wrong, and round 13 said so: a command need not touch
# the shell to change what the sink does, it can write to the filesystem. The
# reported route (a `pre-commit` hook eating the heredoc from git's stdin) does
# NOT reproduce — measured, the hook's stdin is empty and git records the body as
# the message — but the class is real by another route::
#
#     cp evil .git/hooks/commit-msg     # the hook is `sh "$1"`
#     git commit -F - <<'EOF'
#     touch /tmp/pwned
#     EOF
#
# `commit-msg` is handed the path of the message FILE, and the message is the
# masked body. Measured: it executes. `git config core.hooksPath /tmp` gets there
# too, and `git` was on the old list.
#
# So the rule is not "external" but "cannot change what the sink does", and only
# two kinds of line qualify:
#
#   * lines that run nothing at all — blank, or a comment;
#   * a short list of git/gh subcommands that only read or stage, plus a few
#     inert utilities. Anything that can write a file, change git's
#     configuration, or change directory is off it, ``cd`` included: a different
#     working directory is a different repository, with different hooks.
#
# ``( ) { }`` stay banned outright — every spelling of a function definition or
# a command group needs one — and a bare ``NAME=value`` is an assignment, not a
# command, so it never matches a prefix here.
_COMPOUND_COMMAND_CHARS = frozenset("(){}")

_STEPPABLE_INVOCATIONS: tuple[tuple[str, ...], ...] = (
    # Staging and inspection, so a batch may prepare before it writes. This is
    # the false negative round 7 reported, and `git add .` is its example.
    ("git", "add"),
    ("git", "status"),
    ("git", "diff"),
    ("git", "log"),
    ("git", "show"),
    ("git", "rev-parse"),
    ("git", "ls-files"),
    ("gh", "pr", "view"),
    ("gh", "pr", "list"),
    ("gh", "issue", "view"),
    ("gh", "issue", "list"),
    ("gh", "run", "view"),
    ("gh", "run", "list"),
    # Inert utilities. `echo` cannot redirect — `>` is not in the charset.
    ("ls",),
    ("pwd",),
    ("echo",),
    ("true",),
)


def _without_trailing_cr(line: str) -> str:
    """Drop one trailing ``\\r``, so a CRLF command is read like an LF one.

    Splitting on ``\\n`` leaves a ``\\r`` at the end of every line of a CRLF
    payload. ``\\r`` is not in :data:`_HEREDOC_OWNER_CHARS` and both patterns
    anchor to end-of-string, so without this the scan would stop at line 0 and
    mask nothing at all. That fails closed — but "closed" here means the prose
    goes to the coarse floor, which is the conductor death this whole function
    exists to prevent, arriving silently and only on Windows-authored payloads.

    Stripping it cannot fail OPEN. Measured on git-bash: a CRLF script parses and
    the heredoc body is data exactly as with LF, so masking it is right. Where a
    shell instead treats the ``\\r`` as part of the delimiter, the terminator
    never matches, the heredoc runs to EOF and the script dies on a syntax error
    — nothing runs, so nothing was hidden. ``\\r`` is not a shell operator in
    either case, so a line that was a whole command still is one.
    """
    return line[:-1] if line.endswith("\r") else line


def _is_a_plain_command_line(line: str) -> bool:
    """May the scan walk past this line?

    Two things have to hold, and the second was learnt the hard way. The line
    must not reach the NEXT line, so that the line after it really does start a
    new command. And it must not change what a sink invocation further down
    MEANS — see :data:`_STEPPABLE_INVOCATIONS` for the bypasses that obligation
    exists to close.

    Needed so the scan can walk PAST an ordinary command — ``git add .`` before
    the commit — and still know that the line after it starts a new command
    rather than continuing this one. Every character of the code must come from
    :data:`_HEREDOC_OWNER_CHARS` and its quotes must balance. That excludes
    ``\\`` (continuation), ``;`` ``&`` ``|`` (an operator whose right side may be
    on the next line), ``$`` and backtick (a substitution bash reads across
    newlines), and ``<`` ``>`` (any redirect — so a line bearing a heredoc opener
    we do NOT recognise can never be walked past as if it were ordinary).

    A trailing comment is cut off first, because a comment cannot reach the next
    line under any circumstance. Measured: ``# note`` before a sink leaves the
    heredoc body as data, and so do ``# note &&``, ``# git commit -F - <<'X'``
    and even ``# note \\`` — a backslash loses its power inside a comment. This
    is the one place the ``#`` rule differs from the opener line, where a comment
    kills the heredoc and must stop everything.

    Cutting matters for more than comment-only lines: ``shlex`` is handed the raw
    string, so ``# don't delete`` raises ``No closing quotation`` and ``# note \\``
    raises ``No escaped character``. Apostrophes in English prose are common
    enough that leaving them in would keep most of the false negative round 11
    reported. The cut is made at a word-initial ``#`` only — bash's own rule —
    so a mid-word ``#`` still leaves any unbalanced quote after it visible to
    ``shlex``, which is what makes ``--title a#b'c`` stop the scan.
    """
    comment = _HASH_BEGINS_A_WORD_RE.search(line)
    if comment is not None:
        # The match starts at the character BEFORE the `#` (a space, tab or
        # `)`), and that character is still code, so cut at the `#` itself.
        line = line[: comment.end() - 1]
    if _PLAIN_COMMAND_LINE_RE.match(line) is None:
        return False
    if _COMPOUND_COMMAND_CHARS.intersection(line):
        return False
    if _has_unquoted_punctuation(line):
        # A redirect here could be a heredoc we do not recognise, and its body
        # would be the lines the scan is about to account for. Unlike the same
        # test on an opener's owner, no exploit was found for this half — the
        # lines after such a redirect are that heredoc's body, so blanking them
        # blanks data. It is kept because stepping over a line the scan has not
        # understood is the mistake every earlier round was made of, not because
        # a measurement forced it. Stated plainly so nobody reads it as pinned.
        return False
    try:
        tokens = shlex.split(line, posix=True)
    except ValueError:
        return False
    if not tokens:
        # Nothing to run: a blank line, or one that was only a comment.
        return True
    return any(
        len(tokens) >= len(prefix) and tuple(tokens[: len(prefix)]) == prefix
        for prefix in _STEPPABLE_INVOCATIONS
    )


def _mask_quoted_heredoc_payloads(command: str) -> str:
    """Blank quoted-heredoc bodies that are DATA for a known sink.

    Returns ``command`` with those body characters replaced by spaces. Length and
    newline positions are preserved so ``match_offset`` / ``match_line`` on any
    surviving denial still point at the real place in the real command.

    This is what separates "the command deletes" from "the command writes a file
    that mentions deleting" — the distinction :func:`_scan_raw_coarse` was already
    instrumented to record. It exists because the system prompt tells the
    implementer to write commit messages and PR bodies through a heredoc, those
    bodies are Markdown, a Markdown code span is a backtick, and a lone backtick
    opened the coarse floor over the prose (measured 2026-08-17: four conductor
    runs died on messages that merely *mentioned* deleting, one of them quoting
    the human's own instruction not to delete).

    The scan walks the command line by line and **accounts for every line it
    passes**, which is the property that makes it safe. At each step the line is
    one of exactly three things:

    * a data sink carrying one quoted opener and nothing that could move where
      the body begins (:data:`_HEREDOC_OWNER_CHARS`) — its body is blanked up to
      the **first** line that is exactly the delimiter, because that is where
      bash ends it, and the scan resumes after that line;
    * an ordinary whole command (:func:`_is_a_plain_command_line`) — stepped
      over, so a batch may put ``git add .`` before the commit. "Ordinary" means
      it neither reaches the next line nor rebinds anything the sink depends on;
    * anything else — the scan stops there and leaves the remainder untouched.

    Only bodies are blanked; every other line, before or after, stays shell for
    the floor to read.

    Accounting is what separates this from "find any line that looks like an
    opener", which is what the round-7 critique prescribed and what round 1
    already broke. A line matching the opener template is only an opener if it
    is at a command position, and it is only at a command position if every line
    before it was one of the three above. In::

        bash <<'ZZ'
        git commit -F - <<'X'
        ZZ
        rm -rf /tmp/a
        X

    line 1 matches the template perfectly, but bash ends ``ZZ``'s body at line 2
    and runs line 3. The unrecognised ``bash <<'ZZ'`` stops the scan, so line 3
    stays visible; treating line 1 as an opener would blank it.

    Six review rounds found defect after defect in cleverer attempts to locate a
    heredoc body, and running each under bash separates them into two kinds:

    * **three that really do run the line the mask blanked** — a fake opener
      inside a body, a backslash continuation, and a regex whose end anchor let
      a non-greedy body swallow the real terminator and the command behind it.
      These are fail-OPEN, and they are the reason for the conservatism.
    * **the rest, which bash refuses to parse at all** — a trailing ``&&``,
      ``$(`` left open, a bare backtick. Measured: ``bash`` exits 2 and runs
      nothing. Declining these buys no safety over what bash already does; they
      are declined because the parse is ambiguous, not because a deletion would
      otherwise execute. Saying so keeps the record honest about which of the
      cases in ``tests/test_implementer_heredoc_mask.py`` are load-bearing.

    The lesson taken is not "model bash better" but "recognise less, and never
    step over a line without knowing what it is".
    """
    lines = command.split("\n")
    out = list(lines)
    index = 0
    while index < len(lines):
        opener = _SINK_HEREDOC_OPENER_LINE_RE.match(_without_trailing_cr(lines[index]))
        owner = opener.group("owner") if opener is not None else ""
        if (
            opener is not None
            and not _HASH_BEGINS_A_WORD_RE.search(owner)
            and not _has_unquoted_punctuation(owner)
        ):
            try:
                tokens = shlex.split(owner, posix=True)
            except ValueError:
                tokens = []
            if any(
                len(tokens) >= len(sink) and tuple(tokens[: len(sink)]) == sink
                for sink in _HEREDOC_DATA_SINKS
            ):
                delim = opener.group("delim")
                strip_tabs = opener.group("dash") == "-"
                end = None
                for candidate in range(index + 1, len(lines)):
                    line = _without_trailing_cr(lines[candidate])
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        end = candidate
                        break
                if end is None:
                    # No terminator: we cannot say where this body stops, so we
                    # stop too and leave the rest of the command as shell.
                    break
                for body in range(index + 1, end):
                    # A trailing `\r` is kept so the masked view stays the same
                    # shape as the command it stands for; every offset a denial
                    # reports still lands on the real character.
                    keep_cr = "\r" if lines[body].endswith("\r") else ""
                    out[body] = " " * (len(lines[body]) - len(keep_cr)) + keep_cr
                index = end + 1
                continue

        if _is_a_plain_command_line(_without_trailing_cr(lines[index])):
            index += 1
            continue

        # Not an opener we recognise and not a line we can safely step over.
        break

    return "\n".join(out)


def _scan_raw_coarse(command: str) -> ClassifiedAction | None:
    """Coarse raw-string scan for Tier C verbs (defence-in-depth, deny-only).

    ``match_offset`` records where the keyword was found. It does not participate in
    the verdict — it exists so a denial can say *where* in the command the floor
    matched, which is what distinguishes "the command deletes" from "the command
    writes a file that mentions deleting".
    """
    for pattern, op in _RAW_COARSE:
        hit = pattern.search(command)
        if hit is not None:
            return ClassifiedAction(op, detail=command, match_offset=hit.start())
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


def _classify_bash(command: str, _depth: int = 0) -> ClassifiedAction:
    """Classify a (possibly compound / wrapped) bash command by its most dangerous part.

    Indirection (``bash -c`` / ``eval`` / ``$(...)`` / backticks) is unwound by
    extracting the inner command and recursing through the *same* classifier, so a
    wrapped Tier C command is judged identically to its direct form (T27: direct ==
    wrapped, one source of truth — no regex mirror to drift out of sync). A
    one-liner that checks out main then merges (``git checkout main && git merge
    develop``) is surfaced as ``git.merge_to_main``; cross-tool-call sequences are
    backed by the push-to-main denial + environment containment.

    Recursion is bounded by :data:`_MAX_INDIRECTION_DEPTH`; nesting past it fails
    closed (UNKNOWN → deny) rather than risk an unbounded / unanalyzable parse.
    """
    if _depth > _MAX_INDIRECTION_DEPTH:
        return ClassifiedAction(Operation.UNKNOWN, detail=command)
    # A quoted heredoc body bound for a data sink is text the shell hands over
    # verbatim; it is never executed. Blank it before ANY pass reads it, because
    # every pass otherwise mis-reads prose as shell: the structural split treats a
    # body line as its own command, ``_extract_substitutions`` reads a Markdown
    # code span as command substitution, and the coarse floor keyword-matches the
    # message text. Masking once here keeps all three consistent. Only at depth 0:
    # an extracted inner command is already shell, and masking it again could hide
    # a real command inside a crafted payload.
    scanned = _mask_quoted_heredoc_payloads(command) if _depth == 0 else command
    parts = [p.strip() for p in _BASH_SEP.split(scanned) if p.strip()]
    if not parts:
        return ClassifiedAction(Operation.EXEC_CODE, detail=command)
    actions = [_classify_single_bash(p, _depth) for p in parts]
    # Command substitutions ($(...) / backticks) anywhere in the raw command are
    # inner commands too — recurse into each (same classifier, depth-bounded).
    actions += [_classify_bash(inner, _depth + 1) for inner in _extract_substitutions(scanned)]

    switches_to_main = any(
        a.operation is Operation.EXEC_CODE
        and re.search(r"\bgit\s+(checkout|switch)\b.*\b(main|master)\b", a.detail)
        for a in actions
    )
    if switches_to_main and any(a.operation is Operation.GIT_MERGE for a in actions):
        candidate = ClassifiedAction(Operation.GIT_MERGE_TO_MAIN, detail=command)
    else:
        candidate = max(actions, key=lambda a: _DANGER_RANK.get(a.operation, 0))

    # Coarse defence-in-depth floor: only when indirection is present, and only if
    # at least as dangerous as the structural verdict — so it can ADD a denial the
    # tokenizer missed, never downgrade one (see _RAW_COARSE).
    #
    # Two separate questions, deliberately not sharing an input:
    #
    #   may the floor run?  -> the ORIGINAL command. The gate is an eligibility
    #       test and the floor can only ADD a denial, so opening it more often is
    #       the safe direction. Masking it instead cost a real denial: in
    #       ``git rm -r foo && git commit -F - <<'EOF' ...`x`... EOF`` the only
    #       indirection is the Markdown backtick in the body, so a masked gate
    #       shuts and the `git rm` on the command line goes unseen.
    #   what may it read?   -> the MASKED command, because a quoted heredoc
    #       payload is data the shell hands to a sink verbatim. Measured
    #       2026-08-17: reading it killed four conductor runs on commit messages
    #       that merely *mentioned* deleting — one quoting the human's own
    #       instruction not to delete.
    gate_open = bool(_INDIRECTION_RE.search(command))
    if gate_open:
        coarse = _scan_raw_coarse(scanned)
        if coarse is not None and _DANGER_RANK.get(coarse.operation, 0) >= _DANGER_RANK.get(
            candidate.operation, 0
        ):
            # Report the command that ran, not the masked copy the floor read. The
            # mask is length-preserving, so ``match_offset`` still indexes into it.
            coarse = replace(coarse, detail=command)
            return _with_provenance(coarse, command, candidate, gate_open, _depth)
    return _with_provenance(candidate, command, candidate, gate_open, _depth)


def _with_provenance(
    chosen: ClassifiedAction,
    command: str,
    structural: ClassifiedAction,
    gate_open: bool,
    depth: int,
) -> ClassifiedAction:
    """Attach provenance to the verdict of a top-level classify (spec A3/A4/A5/A10).

    Returns ``chosen`` untouched at recursion depth > 0: only the outermost call
    describes "how this command was judged", and stamping inner results would make
    the reported rule refer to a fragment rather than to the command that was denied.

    Provenance is derived from values the classifier has already computed — nothing
    here re-runs a matcher or can change ``chosen.operation``.
    """
    if depth > 0:
        return chosen
    from_floor = chosen is not structural
    if not from_floor:
        # No floor verdict to corroborate; the structural pass stands alone.
        corroborated = "unknown"
    elif _tokenizer_degraded(command):
        # The structural pass fell back to a naive split, so "the structure agrees"
        # is not a claim we can make either way.
        corroborated = "unknown"
    else:
        corroborated = "yes" if structural.operation is chosen.operation else "no"
    return replace(
        chosen,
        rule_id="raw_coarse" if from_floor else "structural",
        corroborated=corroborated,
        indirection_gate=gate_open,
    )


def _tokenizer_degraded(command: str) -> bool:
    """True when ``shlex`` cannot tokenise ``command`` (unbalanced quotes etc.).

    Mirrors the fallback in :func:`_classify_single_bash`; used only to decide
    whether ``corroborated`` can be asserted, never to classify.
    """
    try:
        shlex.split(command, posix=True)
    except ValueError:
        return True
    return False


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


# Benign built-in Claude Code tools with NO filesystem / git / external side
# effect: in-context planning (``TodoWrite``) and management of background shells
# the agent itself spawned (``BashOutput`` reads a shell's output, ``KillShell``
# stops one). They have no allow-list :class:`Operation` of their own, so without
# this they would fall through to ``UNKNOWN`` → default-deny and halt the agent on
# its first planning step (T37 #3 — observed: ``TodoWrite`` denied → initial halt).
# Mapped to ``EXEC_CODE`` (Tier A allow). Kept deliberately small: a tool with any
# real effect must be classified explicitly above, never added here.
_BENIGN_BUILTIN_TOOLS = frozenset({"TodoWrite", "BashOutput", "KillShell"})


def classify_tool_call(tool_name: str, tool_input: dict[str, Any]) -> ClassifiedAction:
    """Map one SDK tool call to a classified allow-list :class:`ClassifiedAction`.

    Unknown tools map to :attr:`Operation.UNKNOWN`, which the default-deny
    allow-list rejects (fail-loud).

    Every branch except ``Bash`` decides from the tool name and its declared
    arguments, so their ``rule_id`` is stamped here; ``Bash`` is the only path that
    parses free text and it stamps its own (``structural`` vs ``raw_coarse``) inside
    :func:`_classify_bash`.
    """
    if tool_name == "Bash":
        return _classify_bash(str(tool_input.get("command", "")))
    action = _classify_non_bash(tool_name, tool_input)
    # `_classify_mcp` stamps its own; everything else here decided from the path /
    # tool name, so fill that in without clobbering a more specific value.
    return action if action.rule_id else replace(action, rule_id="path")


def _classify_non_bash(tool_name: str, tool_input: dict[str, Any]) -> ClassifiedAction:
    """Tool-name / argument based classification (everything except ``Bash``)."""
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
    if tool_name in _BENIGN_BUILTIN_TOOLS:
        return ClassifiedAction(Operation.EXEC_CODE, detail=tool_name)
    if tool_name.startswith("mcp__"):
        return replace(_classify_mcp(tool_name, tool_input), rule_id="mcp")
    return ClassifiedAction(Operation.UNKNOWN, detail=tool_name)


# --------------------------------------------------------------------------- #
# can_use_tool guard
# --------------------------------------------------------------------------- #


def _current_branch(repo_root: Path) -> str | None:
    """Return the repo's current branch via ``git rev-parse``; None if undeterminable.

    Uses ``git rev-parse --abbrev-ref HEAD`` rather than reading ``.git/HEAD``
    directly so it resolves correctly for **worktrees** (``.git`` is a file, not a
    dir) and **packed-refs**, where a raw ``.git/HEAD`` read would fail and force
    an over-strict deny (T23). Returns None for a detached HEAD (``rev-parse``
    prints ``HEAD``), a non-repo, or any git failure — the guard treats None as
    fail-closed (the action is downgraded to UNKNOWN → deny) rather than letting a
    missing branch pass a constraint.

    Assumes ``repo_root`` is implementer-managed (its own clone): running ``git``
    there trusts that repo's ``.git/config`` (aliases / hooks). Acceptable in
    Phase 1 (self-clone); flagged for later if untrusted repos are ever gated.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":  # detached HEAD or empty output
        return None
    return branch


#: Operations whose branch the command line does not carry, so the guard fills it
#: from HEAD — or downgrades to ``UNKNOWN`` when HEAD is undecidable.
#:
#: One list, not one ``if`` per operation: the lookup, the replacement and the
#: fail-closed downgrade are the same for all of them, and a maintainer adding a
#: fifth should not have to pick which copy to extend. (Tier B, PR #158 round 2.)
#:
#: Membership is load-bearing. ``allowlist._constraints_pass`` returns True when
#: ``branch is None``, so an operation carrying a ``branch_glob`` that is NOT in
#: this list skips its glob entirely and passes. Nothing static catches that; the
#: detached-HEAD and non-repo tests are what does.
_HEAD_ENRICHED_OPERATIONS: tuple[Operation, ...] = (
    Operation.GIT_COMMIT,
    Operation.GIT_PUSH,
    Operation.HISTORY_REWRITE,
    Operation.FORCE_PUSH,
)


class _AllowlistGuard:
    """Per-spawn ``can_use_tool`` callback: classify → enrich → check → allow/deny.

    A deny is recorded (so ``deliver_event`` can fail-loud) and returned with
    ``interrupt=True`` so the SDK aborts the turn immediately.
    """

    def __init__(self, allowlist: Allowlist) -> None:
        self._allowlist = allowlist
        self.violations: list[AllowlistDecision] = []
        # The action alongside each violation. Kept in step with `violations` (same
        # index) rather than replacing it, so existing readers of `violations` are
        # untouched. The decision says which rule fired; only the action knows what
        # was attempted, and dropping it is the defect this record exists to fix.
        self.violation_actions: list[ClassifiedAction] = []

    def _enrich(self, action: ClassifiedAction) -> ClassifiedAction:
        """Fill an unparsed branch/target from the repo's current branch.

        ``git commit`` / bare ``git push`` carry no branch and ``git merge`` no
        target on the command line, but the *effective* branch is the checked-out
        one. Resolving it from ``.git/HEAD`` closes the fail-open where a commit/
        push/merge while on ``main`` would otherwise pass (None == "no constraint
        to check"). If the branch can't be determined, downgrade to UNKNOWN so
        ``default: deny`` blocks it (fail-closed).

        ``git rebase`` / ``git reset --hard`` / ``git filter-branch`` (=
        :attr:`Operation.HISTORY_REWRITE`) mutate the *checked-out* branch and
        carry no branch argument, so the classifier always emits ``branch=None``;
        the enrichment fills the HEAD in for them too, or downgrades to UNKNOWN
        when HEAD is undecidable. Same shape for ``git push --force`` on a bare
        push (``branch=None`` from the classifier) — a refspec-form force push
        (``origin main`` / ``+feature/x:main``) already carries the destination
        via :func:`_push_target_and_force` and enrichment leaves it alone.

        The whole reason this list has to enumerate operations (rather than
        "fill the branch if any allow rule for this op has a ``branch_glob``")
        is that the allow-list's ``branch is None`` case is fail-**open**
        (``allowlist._constraints_pass`` returns ``True, ""`` on None so the
        commit / push structural rule works with an unparseable command line).
        An operation whose branch reaches the check as ``None`` therefore
        SKIPS ``branch_glob`` and slips through. Missing an operation from
        this enumeration is silent in every static check we have, so the
        detached-HEAD / non-repo regression tests below are the only guard
        against a future op being widened via ``branch_glob`` without also
        being added here (T-branch-scoped-implementer-permissions §6-1).
        """
        repo_root = self._allowlist.repo_root
        if action.operation in _HEAD_ENRICHED_OPERATIONS and action.branch is None:
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
        self.violation_actions.append(action)
        return PermissionResultDeny(message=decision.reason, interrupt=True)


@dataclass
class _Session:
    client: _SdkClient
    ctx: SpawnContext
    own_role: Role
    guard: _AllowlistGuard
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

    def _make_options(self, guard: _AllowlistGuard) -> ClaudeAgentOptions:
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
            # built-ins (SDK 0.1.77 → ``--tools ""``); the guard below — not the
            # exposure list — is the enforcement point (T37 #1).
            "tools": list(_IMPLEMENTER_BUILTIN_TOOLS),
            # Empty → nothing is auto-approved, so EVERY tool call routes through
            # the can_use_tool guard (the single enforcement point).
            "allowed_tools": self._allowed_tools,
            "mcp_servers": self._mcp_servers,
            # Isolation (T37 #4). setting_sources=[] runs the session in SDK
            # isolation mode: it does NOT load this host's user/project/local
            # settings (claude.ai connectors, CLAUDE.md, env, hooks). That both
            # stops the implementer from inheriting the operator's MCP connectors
            # (a credential-surface leak — the agent reached smart_read/Gmail/
            # Drive) AND closes a guard bypass: a ``permissions.allow`` rule in
            # host settings would auto-approve a tool so it never reaches
            # can_use_tool (the SDK only invokes can_use_tool on an "ask"
            # verdict). strict_mcp_config ignores any MCP config not passed here
            # (project ``.mcp.json`` / plugin servers), so only ``mcp_servers``
            # (empty by default) is honored. Inference auth (credentials file) is
            # independent of settings sources, so this does not affect routing.
            "setting_sources": [],
            "strict_mcp_config": True,
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
        options = self._make_options(guard)
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
            guard=guard,
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
                raise _violation(
                    decision,
                    guard.violation_actions[-1] if guard.violation_actions else None,
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


def _violation(
    decision: AllowlistDecision, action: ClassifiedAction | None = None
) -> ImplementerAllowlistError:
    """Build the halt error for a denial, carrying the structured record (spec S3).

    The message text is deliberately **unchanged** — the sink for the record is the
    `delivery.failed` event's structured fields, so nothing that reads or matches on
    this string needs to know about the record (spec S1, "sink has fields" branch).
    """
    err = ImplementerAllowlistError(
        f"allow-list denied {decision.operation.value}: {decision.reason}", decision=decision
    )
    if action is not None:
        err.denial_record = build_denial_record(decision, action)
    return err


__all__ = [
    "ImplementerAllowlistError",
    "ImplementerSdkAdapter",
    "ImplementerSdkDeliveryError",
    "ImplementerSdkHaltError",
    "ImplementerSdkHealthError",
    "ImplementerSdkSpawnError",
    "classify_tool_call",
]
