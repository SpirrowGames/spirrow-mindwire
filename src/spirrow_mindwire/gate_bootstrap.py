"""New-project ``.mindwire-gate`` bootstrap — sweeper's system-alert opener/closer.

Design source: chatroom thread ``T-new-project-gate-bootstrap``
(msg-1962 request; msg-1963/1965/1967 design; msg-1964/1966/1968 naysayer).
This module is the deterministic, LLM-free piece that runs on every sweep tick.

The problem: a project without ``.mindwire-gate`` at its repo root triggers no
failure and no notification. The implementer's system prompt falls back to
"whatever the project's own tests are", which for a brand-new repo means
"there is no declared green" and any commit-time judgement is silent. The
current mechanism relies on a human noticing the gate is missing and adding it
by hand.

The mechanism this module gives it:

  1. On every sweep tick, for every distinct ``project`` in the sweep list that
     carries a ``repo_dir``, evaluate the 4-branch predicate in
     :func:`inspect_gate`.
  2. If the predicate says "not declared" (``MISSING`` or ``NEW_REPO``): open
     ``T-gate-bootstrap-<project>`` idempotently, with owner = the sweeper's
     machine identity and ``tags = ["system-alert", "gate-bootstrap"]``. The
     ``propose_content`` is a fixed template — no LLM call — that names the
     constraint (**declare a green predicate**, don't drift from CI, don't
     be stricter than PR judgement).
  3. If the predicate says "declared" (``DECLARED`` or ``STALE_WORKTREE``):
     the alert is resolved; attempt to close the fixed-id thread. Not-found /
     already-resolved envelopes are swallowed as success (idempotent no-op).
  4. If the ``repo_dir`` is not a git working tree at all (``UNUSABLE``):
     fail-closed — log and stay silent. That is a sweep-config problem, not an
     onboarding one.

The 4-branch predicate lives in :func:`inspect_gate`. Its point is that the
"upstream ref is unresolvable" case is not "cannot decide" but "nothing is
declared yet" — the exact state a brand-new repo starts in, and the state that
most needs the alert. Falling closed there would silently exclude every new
repo, which is the failure this file exists to fix (msg-1965 N-1).

**Closing semantics (msg-1967 N-5-B) — "system-alert taken down by its opener".**
The close call names the same owner that opened the thread and carries no role
claim. That is the correct semantic form (fact-sync, not a judgement), and the
one the ADR-2026-05-29-10 role registry can accept without inventing a fake
"integrator" role for the sweeper. Whether the far end (magickit's
``chatroom_close_thread``) currently enforces ``closeable_roles`` **before** the
owner check is a magickit-side fact this module cannot introspect from client
code. If it does, the close will fail with a permission-shaped envelope; this
module raises :class:`GateBootstrapCloseError` with the payload so the caller
can log it, and the follow-up work (a carve-out on the server for
``system-alert`` + owner-close, per msg-1968) surfaces loudly rather than being
swallowed. Under no circumstance does this module attribute a role to the
sweeper to "get around" the check — that is exactly the ADR-10 hack the design
rejects (msg-1965 N-4).
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .magickit.client import MagickitMcpError, McpToolCaller

# Fixed identity for the sweeper's system-alert traffic. Reuses the string the
# PR-review orchestrator already writes as ``owner`` (see
# ``spirrow_mindwire.orchestrator._DEFAULT_OWNER``) so both mechanical writers
# share one identity in magickit's registry — no new ADR-10 role needs to be
# claimed by this file (msg-1965 N-4, msg-1967 N-5-B).
DEFAULT_SWEEPER_OWNER = "orchestrator"

# Fixed thread-id prefix. The full id is ``T-gate-bootstrap-<project>``. Fixed
# because it IS the idempotency key: the same predicate opens and closes it, so
# a second open on the same project resolves to "already exists" and the sweep
# stays cost-flat (msg-1963 D-4).
THREAD_ID_PREFIX = "T-gate-bootstrap-"

# Tag set. ``system-alert`` names the kind of thread this is (msg-1967 N-5-B —
# not a design proposal, and not to be treated as one by the review pipeline);
# ``gate-bootstrap`` is the specific alert. Kept as literals here rather than
# an enum because tag-vocabulary sprawl is a project-level decision and this
# file has no opinion on it.
ALERT_TAGS = ("system-alert", "gate-bootstrap")

# The upstream refs the predicate consults, in order. Try ``origin/HEAD`` first
# (whatever the remote points at) and fall back to the two names actually seen
# in the sweep list today — see msg-1965 N-1. Every entry that resolves lets
# the "stale worktree" branch fire; if none resolve, the predicate falls into
# NEW_REPO instead of falling closed (which would silently exclude the exact
# repos most needing the alert).
_UPSTREAM_REF_FALLBACKS: tuple[str, ...] = ("origin/HEAD", "origin/main", "origin/develop")

# The template the sweeper posts as the thread's ``propose_content``. It is a
# **fixed string** — no LLM call, no repo-specific interpolation beyond the
# project name — because the *question* is universal ("declare a green") and
# only the *answer* is repo-specific (msg-1963 D-3). The implementer that picks
# this up reads the actual CI config and writes the gate script itself.
_PROPOSE_TEMPLATE = """\
[This thread was opened automatically by the sweeper. It is a system alert, not \
a design proposal — the machine only asks the question; the answer is written \
by an implementer that reads the target repository.]

Project: {project}

The predicate `gate_missing({project})` fired this tick: neither the working \
tree nor any resolvable upstream ref (`origin/HEAD` / `origin/main` / \
`origin/develop`) carries `.mindwire-gate` at the repository root. Until this \
repo declares a green predicate, the implementer's system-prompt fallback is \
"whatever the project's own tests are", which for a brand-new project means \
"nothing declared", and the commit-time gate becomes silent.

**Ask (fixed template, applies to every project):**

1. Declare this repository's green predicate as a single `.mindwire-gate` \
script at the repo root.
2. It MUST run the same command the CI configuration for this repo runs — \
   the SOT is one script, not two. State in the PR description which CI \
   config file(s) you read (`.github/workflows/*.yml`, `azure-pipelines.yml`, \
   `Jenkinsfile`, …) as the source of the command.
3. It MUST NOT be stricter than the criteria that repo's PRs are actually \
   judged by. If you add checks not in CI, you have made two SOTs — undo it.
4. Open the PR against an integration branch (never `main` directly). The \
   review path is: implementer PR → naysayer PR-gate (independent judge) → \
   human Tier-C merge.

**Design refs (this thread's design is msg-1962..msg-1968 on this thread):**
- ADR-2026-05-29-10 — role registry / `close_reason` enum. This close is the \
  sweeper taking down its own alert as a fact-sync (not a judgement); do not \
  claim `integrator` or any other role for the sweeper.
- ADR-2026-06-03-16 — naysayer CI-gate. The PR-gate covers the review of the \
  gate's contents. No separate design loop is opened for this bootstrap \
  thread.

**Once the PR is merged**, this thread will close itself on the next tick \
(the predicate becomes false; the same sweeper that opened it takes it \
down). If the PR is closed WITHOUT merging, this thread is not re-opened — \
it remains as a stalled record, per msg-1967 N-5-C (a known residual: the \
general "PR stopped, thread stranded" problem is out of scope for this \
mechanism)."""

# Fixed title. Short enough for the chatroom UI and fixed for the same reason
# the thread id is fixed — this is one thread per project, not a series.
_TITLE_TEMPLATE = "System alert: {project} has no `.mindwire-gate` — declare a green"


class GateStatus(StrEnum):
    """The four verdicts the predicate produces (msg-1965 N-1 / msg-1967 D-1).

    Only two are "alertable" (:func:`should_alert` returns ``True``): the
    other two describe states the sweeper leaves alone. ``UNUSABLE`` is the
    fail-closed branch — a broken ``repo_dir`` is a sweep-config problem, and
    guessing "alert" there would spend money in the direction the design
    explicitly forbids (msg-1963 D-6).
    """

    DECLARED = "declared"
    """`.mindwire-gate` is present in the working tree. Alert is not needed."""

    STALE_WORKTREE = "stale_worktree"
    """
    Not in the working tree, but present in an upstream ref (main / develop /
    ``origin/HEAD``). The gate IS declared; this checkout is on an old branch.
    Alert is not needed — leaving the thread closed matches reality.
    """

    MISSING = "missing"
    """
    Not in the working tree AND not in any resolvable upstream ref. The gate
    is genuinely undeclared. **Fire the alert.**
    """

    NEW_REPO = "new_repo"
    """
    Not in the working tree AND no upstream ref resolves at all
    (``origin/HEAD`` / ``origin/main`` / ``origin/develop`` all fail). This
    is the brand-new-repo case: nothing has ever been declared upstream. **Fire
    the alert.** msg-1965 N-1 exists precisely so this branch is not silently
    swallowed as "cannot decide".
    """

    UNUSABLE = "unusable"
    """
    ``repo_dir`` is not an absolute path (empty, whitespace-only, relative,
    or otherwise ambiguous), is absent from disk, is not a git repository,
    or is otherwise unreadable. Fail-closed: log only, no alert. This is a
    sweep-config problem, not an onboarding one (msg-1963 D-6). The
    non-absolute branch was added after the PR-gate review of PR #192
    identified a CWD-fallback attack path: ``Path("") / ".mindwire-gate"``
    silently probes the caller's current working directory.
    """


def should_alert(status: GateStatus) -> bool:
    """Whether ``status`` means "open the T-gate-bootstrap-<project> thread"."""
    return status in (GateStatus.MISSING, GateStatus.NEW_REPO)


@dataclass(frozen=True)
class RepoInspection:
    """The output of :func:`inspect_gate` — a decision plus the evidence for it.

    ``upstream_ref`` names which ref resolved (if any); useful for the log line
    the sweeper writes on every tick. ``reason`` is a human-readable summary,
    kept as a single field so the log site never has to reconstruct which
    branch fired.
    """

    project: str
    repo_dir: Path
    status: GateStatus
    upstream_ref: str | None
    reason: str


class _GitRunner:
    """Runs a git command against ``repo_dir``. Only the two verbs this file uses.

    Split out so tests can substitute a fake without a real git working tree.
    Production instantiates the default and passes it through; tests hand in
    a class with the same two methods returning canned answers.
    """

    def is_git_repo(self, repo_dir: Path) -> bool:
        """True iff ``git -C <repo_dir> rev-parse --git-dir`` succeeds."""
        return self._run(repo_dir, ["rev-parse", "--git-dir"]).returncode == 0

    def resolve_upstream_ref(self, repo_dir: Path) -> str | None:
        """The first of the fallback refs that resolves, or ``None`` if none do.

        Uses ``rev-parse -q --verify <ref>`` for the non-``HEAD`` fallbacks (a
        concrete branch tip) and ``symbolic-ref -q refs/remotes/origin/HEAD``
        for the first, which resolves to whatever the remote HEAD is aliased
        to. Either way, a successful resolution means "there IS an upstream ref
        to consult", which is the branching question the predicate needs.
        """
        for candidate in _UPSTREAM_REF_FALLBACKS:
            if candidate == "origin/HEAD":
                result = self._run(repo_dir, ["symbolic-ref", "-q", "refs/remotes/origin/HEAD"])
            else:
                result = self._run(repo_dir, ["rev-parse", "-q", "--verify", candidate])
            if result.returncode == 0:
                return candidate
        return None

    def ref_carries_gate(self, repo_dir: Path, ref: str) -> bool:
        """True iff ``.mindwire-gate`` is committed at ``ref``.

        ``git cat-file -e <ref>:.mindwire-gate`` is the cheapest question. It
        touches no working tree, hits no network (uses only what has been
        fetched), and answers exactly the question the predicate asks: does
        the blob exist at that path in that tree?
        """
        return self._run(repo_dir, ["cat-file", "-e", f"{ref}:.mindwire-gate"]).returncode == 0

    def _run(self, repo_dir: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            check=False,
        )


def inspect_gate(
    project: str,
    repo_dir: Path,
    *,
    git: _GitRunner | None = None,
) -> RepoInspection:
    """Evaluate the 4-branch predicate on ``repo_dir``.

    Branches, in the order they are evaluated:

    0. ``repo_dir`` is not an absolute path (empty, whitespace-only, ``.``,
       relative) ⇒ :attr:`GateStatus.UNUSABLE` (fail-closed, log only).
       Refuses to probe the caller's current working directory — added
       after the PR-gate review of PR #192 identified that
       ``Path("") / ".mindwire-gate"`` normalises to a relative
       ``.mindwire-gate`` and silently resolves against CWD.
    1. ``.mindwire-gate`` in the working tree ⇒ :attr:`GateStatus.DECLARED`
       (nothing to do).
    2. Not in the working tree and ``repo_dir`` is not a git working tree ⇒
       :attr:`GateStatus.UNUSABLE` (fail-closed, log only).
    3. In the working tree of ``repo_dir`` is a git repo AND an upstream ref
       resolves AND that ref carries the gate ⇒ :attr:`GateStatus.STALE_WORKTREE`
       (nothing to do — checkout is on an old branch).
    4. Same as (3) but the ref does NOT carry the gate ⇒
       :attr:`GateStatus.MISSING` (fire alert — genuinely undeclared).
    5. ``repo_dir`` is a git repo but no upstream ref resolves ⇒
       :attr:`GateStatus.NEW_REPO` (fire alert — brand-new project).

    The single load-bearing choice is #5. An earlier version of this file
    (msg-1963 D-1) collapsed #5 into UNUSABLE and fell closed there, which
    silently excluded every brand-new repo — the exact class the alert is for
    (msg-1964 N-1 / msg-1965 correction).
    """
    git = git or _GitRunner()

    # Reject any non-absolute ``repo_dir`` up front. This is the load-bearing
    # defence identified by the PR-gate review of PR #192. Without it, an
    # empty ``--repo-dir`` from the sweep wrapper (``$c.repo_dir`` coerced to
    # ``""`` by PowerShell) parses through ``argparse``'s ``type=Path`` into
    # ``Path("")``, which normalises to ``Path(".")`` — a RELATIVE path.
    # Then ``Path(".") / ".mindwire-gate"`` is the bare relative
    # ``.mindwire-gate``, and ``.is_file()`` below silently resolves it
    # against the caller's current working directory. In production the
    # sweep wraps the tick in ``Push-Location $repoRoot``, so CWD is the
    # MindWire host repo, which HAS a gate — every candidate with a broken
    # ``repo_dir`` would falsely report DECLARED and fire spurious
    # ``close_alert`` MCP traffic on every tick.
    #
    # The sweep contract is absolute paths (see ``sweep.json.example``).
    # A relative path is either a broken sweep entry or a hostile caller;
    # in both cases the correct verdict is UNUSABLE (fail-closed, log only —
    # msg-1963 D-6). The PowerShell wrapper filters
    # ``[string]::IsNullOrWhiteSpace($c.repo_dir)`` for the same reason,
    # but the Python side must be safe against ANY caller.
    if not repo_dir.is_absolute():
        return RepoInspection(
            project=project,
            repo_dir=repo_dir,
            status=GateStatus.UNUSABLE,
            upstream_ref=None,
            reason=(
                f"repo_dir {str(repo_dir)!r} is not an absolute path — "
                "sweep-config problem, refusing to probe the current working "
                "directory; alert suppressed"
            ),
        )

    gate_path = repo_dir / ".mindwire-gate"
    if gate_path.is_file():
        return RepoInspection(
            project=project,
            repo_dir=repo_dir,
            status=GateStatus.DECLARED,
            upstream_ref=None,
            reason=f".mindwire-gate present in worktree ({gate_path})",
        )

    if not repo_dir.is_dir() or not git.is_git_repo(repo_dir):
        return RepoInspection(
            project=project,
            repo_dir=repo_dir,
            status=GateStatus.UNUSABLE,
            upstream_ref=None,
            reason=(
                f"repo_dir {repo_dir!s} is not a git working tree — "
                "sweep-config problem, not an onboarding one; alert suppressed"
            ),
        )

    ref = git.resolve_upstream_ref(repo_dir)
    if ref is None:
        return RepoInspection(
            project=project,
            repo_dir=repo_dir,
            status=GateStatus.NEW_REPO,
            upstream_ref=None,
            reason=(
                "no upstream ref resolves (origin/HEAD, origin/main, origin/develop all "
                "unavailable) — brand-new repo; alert fires"
            ),
        )

    if git.ref_carries_gate(repo_dir, ref):
        return RepoInspection(
            project=project,
            repo_dir=repo_dir,
            status=GateStatus.STALE_WORKTREE,
            upstream_ref=ref,
            reason=(
                f".mindwire-gate present at {ref}:.mindwire-gate but not in worktree — "
                "checkout is on an older branch; alert suppressed"
            ),
        )

    return RepoInspection(
        project=project,
        repo_dir=repo_dir,
        status=GateStatus.MISSING,
        upstream_ref=ref,
        reason=(
            f".mindwire-gate absent in worktree and at {ref} — genuinely undeclared; alert fires"
        ),
    )


def thread_id_for(project: str) -> str:
    """The fixed idempotency-key thread id for ``project``'s bootstrap alert."""
    return f"{THREAD_ID_PREFIX}{project}"


@dataclass(frozen=True)
class OpenResult:
    """Outcome of an :func:`open_alert` call.

    ``already_exists`` distinguishes "we opened it this tick" from "it was
    already open from a previous tick" so the caller's log line can be honest
    about which happened — both are success from the mechanism's point of view.
    """

    thread_id: str
    already_exists: bool


@dataclass(frozen=True)
class CloseResult:
    """Outcome of an :func:`close_alert` call.

    ``was_open`` is ``True`` when we actually closed a thread this call, and
    ``False`` when the thread was already absent / already resolved (idempotent
    no-op — this is by design, msg-1963 D-4).
    """

    thread_id: str
    was_open: bool


class GateBootstrapCloseError(RuntimeError):
    """Raised when ``chatroom_close_thread`` refuses for a reason we do not swallow.

    The two responses we DO swallow are "thread not found" and "already
    resolved" — both mean the desired end-state (thread not open) is already
    the case. Everything else is surfaced as this exception so a magickit-side
    permission fault (msg-1968: ``closeable_roles`` enforced before the owner
    check) becomes a loud log line, not a silent stall. The follow-up work
    tracked by msg-1968 — a server-side carve-out for ``system-alert`` +
    owner-close — surfaces here, exactly where the operator will see it.
    """


async def open_alert(
    mcp: McpToolCaller,
    *,
    project: str,
    owner: str = DEFAULT_SWEEPER_OWNER,
    title_template: str = _TITLE_TEMPLATE,
    propose_template: str = _PROPOSE_TEMPLATE,
) -> OpenResult:
    """Open ``T-gate-bootstrap-<project>`` idempotently.

    "Already exists" is swallowed and reported (``already_exists=True``): the
    same thread id is the whole idempotency mechanism, and the second call
    within one project's lifetime is the *expected* second call, not a bug. Any
    other envelope re-raises as :class:`MagickitMcpError` — the caller logs it
    and next tick tries again (msg-1963 D-6, open-side is fail-closed on
    everything except the deliberate collision).
    """
    thread_id = thread_id_for(project)
    title = title_template.format(project=project)
    propose = propose_template.format(project=project)
    try:
        await mcp.call_tool(
            "chatroom_open_thread",
            {
                "project": project,
                "thread_id": thread_id,
                "title": title,
                "owner": owner,
                "propose_content": propose,
                "tags": list(ALERT_TAGS),
            },
        )
    except MagickitMcpError as exc:
        if "already exists" in str(exc).lower():
            return OpenResult(thread_id=thread_id, already_exists=True)
        raise
    return OpenResult(thread_id=thread_id, already_exists=False)


# The sweeper's honest ``embodiment`` self-declaration for the ``decide`` msg
# that ``chatroom_close_thread`` emits (embodiment is required at that boundary
# by ADR-2026-05-29-12's mandatory-on-state-transition rule, verified against
# the live magickit schema in T-new-project-gate-bootstrap msg-2083; humans are
# exempt but the sweeper is not human). This is a scheduled Python script — it
# is not a terminal coding agent, not a web chat client, and not any of the
# other named embodiments in the ADR-12 enum. Writing one of those named values
# would make the audit ledger say something false about who acted. ``unknown``
# is what the ADR-12 enum uses when no name applies (T-new-project-gate-bootstrap
# Bohr msg-2084 §5(2) recommends this value on the same reasoning). If a
# scheduled-script embodiment name is added to the enum later, replace here.
_SWEEPER_EMBODIMENT = "unknown"


# Structural discriminator for the benign "thread does not exist" envelope.
#
# Rationale (T-new-project-gate-bootstrap Bohr msg-2139 §5, PR-gate on head
# 9b023fe): magickit has ONE Python exception class — ``MagickitMcpError`` —
# not typed subclasses per envelope kind (verified in magickit/client.py:40;
# there is no ``ChatroomNotFoundError`` or ``ChatroomPermissionError`` Python
# class). The only structural signal available to the caller is the envelope's
# ``error_type`` field, which ``_elevation_message`` embeds verbatim in the
# exception's ``str`` as ``error_type='<Name>'`` (client.py:286, formed by
# ``f"error_type={type_snippet!r}"`` where ``type_snippet`` is the ``str``
# value of the payload's ``error_type`` key).
#
# The prior discriminator was ``"not found" in str(exc).lower()`` — a
# natural-language substring in the envelope's free-form ``error`` field. That
# substring is not machine-owned: a genuine ``ChatroomPermissionError`` whose
# text says "role 'orchestrator' not found in closeable_roles", or a
# transport error mentioning a project name like ``spirrow-not-found``, would
# match. The naysayer's PR-gate finding on head 9b023fe pinned exactly this
# hazard, and Bohr §5's "(b) 文字列弁別 → 型弁別に替えられるなら替える"
# asks for a structural match.
#
# The substring below matches the machine-owned enum tag as ``_elevation_message``
# writes it, not the natural-language ``error`` text. ``ChatroomNotFoundError``
# is 20 characters, well below the 200-char ``_ELEVATION_VALUE_LIMIT`` truncation
# threshold, so the substring is guaranteed to appear in full for a real not-found
# envelope. Any envelope whose ``error_type`` is anything else (e.g.
# ``ChatroomPermissionError``) will not match, so the surfacing invariant holds
# even when the free-form ``error`` text happens to contain "not found".
#
# The two swallows — precheck (:func:`_alert_thread_exists`) and close-boundary
# race (:func:`close_alert`) — share this one predicate on purpose (Bohr §5's
# "併せて 1 点: 二つの swallow が二つの述語を持つと片方だけが直る形の
# divergence を作る"). Any future change to the discriminator affects both
# boundaries by construction. The already-exists / already-resolved swallows
# at :func:`open_alert` / :func:`close_alert`'s close boundary use a separate
# free-form substring; those are unaffected by this rationale and left as-is
# because their misclassification hazard is different (already-* semantics
# cannot masquerade as a benign no-op the way not-found can — they either
# succeed as idempotent or surface as an unrelated failure the operator has to
# see anyway).
_ENVELOPE_NOT_FOUND_MARKER = "error_type='ChatroomNotFoundError'"


def _is_thread_not_found_envelope(exc: MagickitMcpError) -> bool:
    """``True`` iff ``exc`` carries the benign ``ChatroomNotFoundError`` envelope tag.

    Structural discriminator — matches the machine-owned ``error_type``
    field verbatim as embedded by :func:`_elevation_message`, NOT a
    substring in the envelope's free-form ``error`` text. Docstring of
    :data:`_ENVELOPE_NOT_FOUND_MARKER` carries the full rationale; both
    swallows in this module route through this one call so they cannot
    drift apart.
    """
    return _ENVELOPE_NOT_FOUND_MARKER in str(exc)


async def _alert_thread_exists(
    mcp: McpToolCaller,
    *,
    project: str,
    thread_id: str,
) -> bool:
    """Cheap read-side precheck: does the sweeper's alert thread exist?

    Reads ``chatroom_get_thread`` (``mode='summary'``) — one MCP round-trip
    against a read-shaped tool. Returns ``True`` if the thread is present,
    ``False`` if magickit reports it as not found. Any other envelope
    re-raises unchanged so the caller sees genuine transport / permission
    faults instead of swallowing them here.

    Split from :func:`close_alert` so ``close_alert`` never issues a
    write-shaped request against a thread that does not exist (T-new-
    project-gate-bootstrap msg-2027 §設計側の指摘 (ii): 「閉じる対象が
    無いなら呼ばない」). Before this precheck, DECLARED projects with no
    T-gate-bootstrap-<project> thread ever opened would still issue
    ``chatroom_close_thread`` on every tick — 153 close_refused/day in
    the measurement window of msg-2024 — and rely on the not-found
    swallow at the close boundary. That works but leaves a write-shaped
    call in the ledger every 5 minutes for a state that could be read.

    Cost balance is neutral or slightly better: the previous behaviour
    was one write per tick (close → not-found → swallow); this is one
    read per tick (get_thread → not-found). In the exists-and-close case
    it costs one extra read, which fires at most once per gate-lifecycle
    transition per project — rare enough to not be worth optimising.
    """
    try:
        await mcp.call_tool(
            "chatroom_get_thread",
            {"project": project, "thread_id": thread_id, "mode": "summary"},
        )
    except MagickitMcpError as exc:
        # Structural discriminator (Bohr msg-2139 §5 / PR-gate 9b023fe):
        # match the envelope's machine-owned ``error_type`` tag, NOT a
        # substring in the free-form ``error`` text. A permission fault
        # whose text happens to contain "not found" (e.g. "role X not
        # found in closeable_roles") must surface as GateBootstrapCloseError,
        # not be silently swallowed as absent. See
        # :data:`_ENVELOPE_NOT_FOUND_MARKER` for the full rationale.
        if _is_thread_not_found_envelope(exc):
            return False
        raise
    return True


async def close_alert(
    mcp: McpToolCaller,
    *,
    project: str,
    merge_commit_sha: str | None = None,
    owner: str = DEFAULT_SWEEPER_OWNER,
) -> CloseResult:
    """Close ``T-gate-bootstrap-<project>`` — take the sweeper's own alert down.

    Called when the predicate has become false: ``.mindwire-gate`` is now
    declared, so the system alert no longer describes reality. This is a
    **fact-sync**, not a judgement (msg-1967 N-5-B): the sweeper opened the
    thread and the sweeper takes it down; no ``closeable_role`` is claimed
    because the sweeper does not have one and has no business inventing one.

    ``merge_commit_sha``, if known, is written into ``summary_content`` as
    the concrete evidence the predicate flipped ("gate is now declared, sha
    ``<...>``"). Not required — the mechanism works without it — but including
    it makes the close self-explanatory in the ledger.

    Structure of this call (T-new-project-gate-bootstrap S-3b, verified
    against the live magickit schema in Heisenberg msg-2083):

    1. **Precheck** via :func:`_alert_thread_exists`. If the thread was never
       opened for this project, return ``CloseResult(was_open=False)``
       immediately without any write-shaped call — msg-2027 §設計側の指摘 (ii)
       (「閉じる対象が無いなら呼ばない」). This is the fix for the msg-2024
       measurement (153 close_refused/day against threads that never existed).
    2. **Close call**. Payload uses magickit's current
       ``chatroom_close_thread`` schema: ``author`` (not ``owner``),
       ``summary_content`` (not ``decide_content``), and ``embodiment``
       (required by ADR-2026-05-29-12 mandatory-on-state-transition, since
       the close emits a ``decide`` msg). ``tags`` carries both
       ``system-alert`` and ``gate-bootstrap`` — this is the tag msg-1968's
       carve-out predicate names for the eventual magickit-side role-check
       exemption ("`system-alert` タグを持ち、かつ owner 自身によるクローズ
       である場合はロールチェックを免除する"), and emitting it now means
       the predicate will actually fire when the carve-out lands rather than
       silently missing it.

    Swallowed envelopes at the close call (idempotent no-op):
      * "already resolved" / "already closed" — the thread was taken down
        between the precheck and the close (a human closed it, or a race).
      * "not found" — same, if the thread disappeared in the race window.

    Swallowed envelope at the precheck call (idempotent no-op):
      * "not found" — no thread was ever opened for this project; the
        desired end-state is already the case.

    Any other envelope — at either the precheck OR the close boundary —
    raises :class:`GateBootstrapCloseError` (the payload is preserved via
    chained exception) so a permission fault surfaces loudly rather than
    being read as success. Both boundaries must satisfy this invariant, or
    a read-side permission fault at the precheck would silently propagate
    as ``MagickitMcpError`` and every alert would fail closed without
    reaching the msg-1968 obligation's failure surface.

    The not-found swallow is discriminated **structurally** —
    :func:`_is_thread_not_found_envelope` matches the envelope's machine-owned
    ``error_type='ChatroomNotFoundError'`` tag as embedded by
    :func:`~spirrow_mindwire.magickit.client._elevation_message`, NOT the
    natural-language "not found" substring in the envelope's free-form
    ``error`` text. This is the fix for the PR-gate objection on head
    9b023fe (Bohr msg-2139 §5): a genuine ``ChatroomPermissionError`` whose
    text says "role 'orchestrator' not found in closeable_roles", or a
    transport error mentioning a project name containing "not-found", would
    have matched the old ``"not found" in str(exc).lower()`` predicate and
    been silently swallowed as absent. The structural marker cannot be
    forged by any envelope whose ``error_type`` is anything other than
    ``ChatroomNotFoundError``, so the surfacing invariant holds even under
    adversarial free-form ``error`` text.

    Both boundaries route through the same
    :func:`_is_thread_not_found_envelope` call — one predicate, two
    swallows, no divergence hazard (Bohr §5 併せて 1 点).
    """
    thread_id = thread_id_for(project)
    try:
        thread_exists = await _alert_thread_exists(mcp, project=project, thread_id=thread_id)
    except MagickitMcpError as exc:
        # Any non-"not found" envelope at the read boundary is a genuine
        # fault (permission / transport / auth). Surface it loudly through
        # the same exception class the close boundary uses, so the operator
        # sees ONE failure surface across the whole close_alert contract.
        raise GateBootstrapCloseError(
            f"chatroom_get_thread precheck refused for {thread_id!r} in project {project!r}: {exc}"
        ) from exc
    if not thread_exists:
        return CloseResult(thread_id=thread_id, was_open=False)
    summary = _format_close_decide(project=project, merge_commit_sha=merge_commit_sha)
    payload: dict[str, Any] = {
        "project": project,
        "thread_id": thread_id,
        "author": owner,
        "summary_content": summary,
        "embodiment": _SWEEPER_EMBODIMENT,
        "tags": list(ALERT_TAGS),
    }
    try:
        await mcp.call_tool("chatroom_close_thread", payload)
    except MagickitMcpError as exc:
        # not-found: shares the SAME structural discriminator as the precheck
        # (Bohr msg-2139 §5 "併せて 1 点"). Both boundaries route through
        # :func:`_is_thread_not_found_envelope`; any future change to the
        # matcher affects both by construction.
        if _is_thread_not_found_envelope(exc):
            return CloseResult(thread_id=thread_id, was_open=False)
        # already-* is a distinct free-form substring — its misclassification
        # hazard is different (see :data:`_ENVELOPE_NOT_FOUND_MARKER` docstring):
        # unlike "not found", an "already resolved" text cannot be plausibly
        # emitted by an unrelated permission or transport fault.
        message = str(exc).lower()
        if "already resolved" in message or "already closed" in message:
            return CloseResult(thread_id=thread_id, was_open=False)
        raise GateBootstrapCloseError(
            f"chatroom_close_thread refused for {thread_id!r} in project {project!r}: {exc}"
        ) from exc
    return CloseResult(thread_id=thread_id, was_open=True)


def _format_close_decide(*, project: str, merge_commit_sha: str | None) -> str:
    """The decide body written when the sweeper takes its own alert down.

    Kept short and machine-friendly (no LLM call, no interpretation): says
    what happened, cites the sha if we have it, and points the reader at
    the design thread's justification for closing without claiming a role
    (msg-1967 N-5-B).
    """
    lines = [
        f"System alert resolved: `.mindwire-gate` is now declared for project {project!r}.",
        "",
        f"This close is a fact-sync (the predicate `gate_missing({project})` has become "
        "false), not a judgement.",
        "The sweeper opened this thread and the sweeper takes it down; no "
        "`closeable_role` is claimed. Design ref: T-new-project-gate-bootstrap "
        "msg-1967 N-5-B (system-alert / owner-close semantics). ADR refs: "
        "2026-05-29-10 (role registry, `close_reason` enum), "
        "2026-06-03-16 (naysayer CI-gate — the PR-gate carries the review "
        "of the gate's contents; no separate design loop is opened here).",
    ]
    if merge_commit_sha:
        lines.append("")
        lines.append(f"Evidence: merge commit `{merge_commit_sha}`.")
    return "\n".join(lines)


__all__ = [
    "ALERT_TAGS",
    "DEFAULT_SWEEPER_OWNER",
    "THREAD_ID_PREFIX",
    "CloseResult",
    "GateBootstrapCloseError",
    "GateStatus",
    "OpenResult",
    "RepoInspection",
    "close_alert",
    "inspect_gate",
    "open_alert",
    "should_alert",
    "thread_id_for",
]
