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

    ``resolved_by_msg`` names the ``decide`` msg that resolved the thread when
    the caller observed a *resolved* state (either at the precheck or at the
    post-refusal read-back — T-gate-bootstrap-close-retried-on-resolved-thread
    S-2-prime). It is ``None`` in every other case: on a first-call close
    (``was_open=True``), on an absent thread (``was_open=False``, thread never
    opened), and when the observed thread payload had no ``resolved_by_msg``
    field. The caller writes it into the tick's JSON output so the operator
    can see WHO resolved the thread — the observation Bohr msg-2456 §2 named as
    the ground truth in the incident report ("`msg-429` に close 済み").
    """

    thread_id: str
    was_open: bool
    resolved_by_msg: str | None = None


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


# The two chatroom statuses that make :func:`open_alert`'s target state
# ("the alert is up and visible to a reader") satisfied. This is
# T-gate-bootstrap-close-retried-on-resolved-thread S-4-prime (Bohr msg-2460 §2,
# Einstein msg-2461 endorsement): the read-back after an open refusal accepts
# ONLY these two statuses as "target reached". A ``resolved`` thread with the
# same fixed id is NOT the target of ``open_alert`` — the alert is not up, it
# was up and got taken down. Returning ``already_exists=True`` there would
# silently pretend the alert exists (Einstein msg-2459 correctness blocking).
# ``superseded`` and ``parked`` are deliberately absent from this set: their
# semantics have not been measured (Bohr msg-2460 §4), and the safe direction
# is to raise — a noisy 1-line refusal is better than a silent misclassification.
_OPEN_ALERT_TARGET_STATUSES: frozenset[str] = frozenset({"active", "awaiting_reply"})


async def open_alert(
    mcp: McpToolCaller,
    *,
    project: str,
    owner: str = DEFAULT_SWEEPER_OWNER,
    title_template: str = _TITLE_TEMPLATE,
    propose_template: str = _PROPOSE_TEMPLATE,
) -> OpenResult:
    """Open ``T-gate-bootstrap-<project>`` idempotently.

    "Already up" is reported as ``already_exists=True``: the same thread id is
    the whole idempotency mechanism, and the second call within one project's
    lifetime is the *expected* second call, not a bug.

    **Refusal handling (T-gate-bootstrap-close-retried-on-resolved-thread S-4-prime,
    Bohr msg-2460 §2, Einstein msg-2461 endorsement).** On any
    :class:`MagickitMcpError` from ``chatroom_open_thread``, this function
    **does not inspect the exception**. It reads the world (via
    :func:`_read_thread_reading_or_none`) and asks a single question about
    :func:`open_alert`'s own target state — "is the alert now up and visible?"
    — i.e., is the observed status in :data:`_OPEN_ALERT_TARGET_STATUSES`
    (``active`` / ``awaiting_reply``). Only in that case is
    ``already_exists=True`` returned. Every other outcome — status of
    ``resolved`` / ``superseded`` / ``parked``, an unknown status, an absent
    thread, or a read that itself failed — re-raises the original
    :class:`MagickitMcpError` unchanged.

    Why the refusal path DOES NOT accept ``resolved`` here (Einstein msg-2459
    blocking): the fixed thread id can hold a *resolved* thread from an earlier
    life-cycle of the same alert. That is a thread that exists but is NOT open
    — the target of :func:`open_alert` (a visible alert) is unmet. A prior
    implementation accepted "existence" as sufficient, and Einstein named
    that as exactly the same class of error that S-3's precheck rewrite fixed
    in :func:`close_alert` (status-blindness). The recorded-but-not-fixed
    consequence: once a project's alert life-cycles end in ``resolved`` and the
    predicate fires again, this function raises on every subsequent open
    attempt because a fixed-id thread cannot be reopened. That defect is
    scope-boundaried out of this thread (Bohr msg-2460 §3) — see the raise
    site for the message the operator sees.

    Why the refusal path DOES NOT inspect the exception (Einstein msg-2457
    invariant blocking): a name-based filter at the entry of the refusal
    branch — "swallow only if ``error_type == '<Name>'``" — puts an
    exception-classification predicate in the *control flow*, which
    reintroduces the very class of bug this whole thread exists to close.
    A future name drift on the server would then bypass the read-back and
    the target-state check with it. The safe direction is: no filter at the
    entry, and let the read-back's own answer decide.
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
        # No exception inspection. Read the world; ask whether open's target
        # state — an alert that is up and visible — has been reached.
        reading = await _read_thread_reading_or_none(mcp, project=project, thread_id=thread_id)
        if reading is not None and reading.status in _OPEN_ALERT_TARGET_STATUSES:
            return OpenResult(thread_id=thread_id, already_exists=True)
        # Enrich the message with the observed post-refusal state so the
        # operator sees at a glance WHY the swallow did not fire. The most
        # common non-target reading — ``status='resolved'`` — is the
        # recorded-but-not-fixed defect: a fixed thread id cannot host two
        # life-cycles of the alert (Bohr msg-2460 §3). The name of that
        # defect is spelled out below so the next operator does not have to
        # re-derive it. Frequency is expected to be zero today (playproof /
        # lexora are in the DECLARED terminal state and do not call
        # open_alert), and if it becomes non-zero the operator sees it here.
        observed = _describe_reading(reading)
        raise MagickitMcpError(
            f"chatroom_open_thread refused for {thread_id!r} in project {project!r}; "
            f"observed thread state after refusal: {observed}. "
            f"If observed status is 'resolved', the alert cannot be raised again "
            f"under the fixed thread_id: the alert life-cycle for this project has "
            f"ended once and re-opening is not supported by this module (recorded "
            f"as a scope-boundaried defect in T-gate-bootstrap-close-retried-on-"
            f"resolved-thread §3; independent finding). Original refusal: {exc}",
            error_type=exc.error_type,
        ) from exc
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


# The conclair ``error_type`` value that classifies a benign "this thread does
# not exist" answer. Recorded value, measured live against the magickit MCP
# endpoint on 2026-09-03 by reading a thread id that was never opened:
#
#   {"error_type": "ChatroomNotFoundError",
#    "error": "Thread 'T-...' not found in project 'spirrow-mindwire'",
#    "details": {"project": "...", "thread_id": "..."}}
_NOT_FOUND_ERROR_TYPE = "ChatroomNotFoundError"


def _is_thread_not_found_envelope(exc: MagickitMcpError) -> bool:
    """``True`` iff conclair classified ``exc``'s envelope as not-found.

    **Envelope-classified, by field equality.** The comparison reads
    :attr:`~spirrow_mindwire.magickit.client.MagickitMcpError.error_type` —
    the envelope's own top-level classification key, captured by
    :func:`~spirrow_mindwire.magickit.client.raise_if_envelope` from the parsed
    payload *before* it is formatted into the exception message. It is not a
    match against ``str(exc)``, and not a Python type dispatch either (magickit
    has one exception class, not a subclass per envelope kind — client.py:40).

    **Where this is used** (as of
    T-gate-bootstrap-close-retried-on-resolved-thread S-3, Bohr msg-2460 §6):
    exactly one call site — :func:`_alert_thread_state`, the precheck read
    that opens :func:`close_alert`. That is the ONE remaining spot in this
    module that uses envelope-name classification, and it does so because
    conclair returns thread-absence as a *refusal*, not as a payload — there
    is no other observable form of "the thread does not exist". At every
    other spot (the refusal recheck at both :func:`open_alert` and
    :func:`close_alert`), the design rule is "look at the observed world
    state, never at the exception" (Bohr msg-2458 §2 as adopted after
    Einstein msg-2457's invariant blocking; msg-2460 §1). A refusal-time
    filter on ``error_type`` would place a name-based predicate in the
    entrance of the control flow, which is exactly the L-A re-appearance the
    thread was opened to close.

    History, because rounds of the earlier PR got it wrong and the record
    should not have to be reconstructed from the chatroom:

    - Round 2 matched ``"not found" in str(exc).lower()`` — natural-language
      text the server writes freely. A ``ChatroomPermissionError`` reading
      "role 'orchestrator' not found in closeable_roles" matched it.
    - Round 3 matched the literal ``error_type='ChatroomNotFoundError'`` in
      ``str(exc)`` and called itself structural. It was not: ``str(exc)`` is a
      flatten of the machine-owned field *and* the free-form ``error`` prose,
      so an envelope classified ``ChatroomPermissionError`` whose ``error``
      text merely quotes ``error_type='ChatroomNotFoundError'`` matched too.
      The PR-gate on head ``523d400`` named exactly that forgery.

    The general rule the two rounds share, and the reason a third substring
    would have failed as well (Bohr msg-2383 §2, "INV-D"): *a discriminator
    must read a field the distrusted party cannot write.* Flattening puts
    trusted and untrusted bytes in one string, so after the flatten no
    substring of it qualifies — the fix is to move the read before the flatten,
    not to pick a longer needle.

    **The exact claim, no stronger:** nothing in the envelope other than its own
    top-level ``error_type`` key can make this return ``True`` — not the
    ``error`` prose, not ``details``, not any other key, and not a transport
    failure (those carry no envelope, so ``error_type`` is ``None`` and they
    surface). What is **not** claimed is that conclair classified correctly: if
    the server labels a permission fault ``ChatroomNotFoundError``, this
    swallows it. That trust is deliberate and is the narrowest available — we
    trust the far end's classifier and nothing else it says.
    """
    return exc.error_type == _NOT_FOUND_ERROR_TYPE


# Chatroom status the precheck cares about (and, transitively, the refusal
# recheck's "target reached" test for close_alert). The design comment on
# :func:`close_alert` and the discharge block on :func:`_read_thread_reading_or_none`
# state why ``resolved`` is the ONLY status that means "close_alert's target
# reached", not ``superseded`` / ``parked`` (Bohr msg-2460 §4).
_RESOLVED_STATUS = "resolved"


class _AlertThreadState(StrEnum):
    """Three-value verdict of the precheck (T-gate-bootstrap-close-retried-on-
    resolved-thread S-3 / Bohr msg-2460 §6 item 1).

    The old two-value form (``bool``: exists / does not) was named
    ``_alert_thread_exists`` and was the direct cause of L-B in the incident
    report (Bohr msg-2456 §5): "存在するか" しか見ていない ∴ ``resolved`` を
    「開いている」と誤って通し、毎 tick write-shaped な往復を出す. The rename
    is deliberate — leaving the old name in place while widening the body would
    let the same misreading re-occur (Bohr msg-2460 §6 note on the rename).
    """

    ABSENT = "absent"
    """The thread does not exist at all (never opened for this project)."""

    OPEN = "open"
    """
    The thread exists AND its status is anything other than ``resolved``.
    :func:`close_alert` proceeds to issue the write-shaped close.
    """

    RESOLVED = "resolved"
    """
    The thread exists AND its status is ``resolved``. :func:`close_alert`
    exits without issuing any write; the caller reports ``already_closed``
    with the ``resolved_by_msg`` observation carried on the result.
    """


@dataclass(frozen=True)
class _AlertThreadReading:
    """The precheck's structured verdict: the tri-state plus the raw evidence.

    ``status`` is the ``thread.status`` string the read observed (``None`` for
    :attr:`_AlertThreadState.ABSENT` and for a malformed payload). It is
    exposed so callers whose target state is not "resolved" — :func:`open_alert`
    — can apply their own predicate on the same reading without re-deriving it
    from ``state``.

    ``resolved_by_msg`` is the ``thread.resolved_by_msg`` string the read
    observed. Populated only when :attr:`state` is :attr:`_AlertThreadState.RESOLVED`
    and the payload carries a string value at that key. This is what surfaces
    into :class:`CloseResult` and, transitively, into the tick's JSON output —
    it is the observable fact Bohr msg-2456 §2 named as the ground truth in
    the incident report.
    """

    state: _AlertThreadState
    status: str | None
    resolved_by_msg: str | None


def _classify_reading(payload: Any) -> _AlertThreadReading:
    """Parse a ``chatroom_get_thread(mode='summary')`` payload into a reading.

    Total over ``Any``: a payload whose shape is not the documented ``{"thread":
    {"status": ..., ...}, ...}`` degrades to :attr:`_AlertThreadState.OPEN`
    with ``status=None`` (safe-side: caller attempts / retries the close;
    silently classifying a malformed payload as ``resolved`` would be the
    opposite failure mode). Same tolerance philosophy the client boundary
    takes (client.py's :func:`is_envelope` loose detection): favour a
    diagnosable action over a silent misclassification.
    """
    thread = payload.get("thread") if isinstance(payload, dict) else None
    thread_dict = thread if isinstance(thread, dict) else {}
    status_val = thread_dict.get("status")
    status = status_val if isinstance(status_val, str) else None
    if status == _RESOLVED_STATUS:
        rbm_val = thread_dict.get("resolved_by_msg")
        rbm = rbm_val if isinstance(rbm_val, str) else None
        return _AlertThreadReading(
            state=_AlertThreadState.RESOLVED, status=status, resolved_by_msg=rbm
        )
    return _AlertThreadReading(state=_AlertThreadState.OPEN, status=status, resolved_by_msg=None)


def _describe_reading(reading: _AlertThreadReading | None) -> str:
    """One-line human-readable description of a reading, for error messages.

    Fits inside a ``GateBootstrapCloseError`` message (or the wrapped
    ``MagickitMcpError`` from :func:`open_alert`) so the operator sees the
    world-state observation the swallow decision was made against without
    having to reproduce it locally.
    """
    if reading is None:
        return "state=unavailable (read-back failed or refused)"
    if reading.state == _AlertThreadState.ABSENT:
        return "state=absent (thread does not exist)"
    if reading.state == _AlertThreadState.RESOLVED:
        rbm = reading.resolved_by_msg or "unknown"
        return f"state=resolved (resolved_by_msg={rbm!r})"
    # OPEN — surface the raw status so an unexpected value ("parked",
    # "superseded", or a future addition) is visible in the log rather than
    # buried under a generic "open".
    return f"state=open (status={reading.status!r})"


async def _alert_thread_state(
    mcp: McpToolCaller,
    *,
    project: str,
    thread_id: str,
) -> _AlertThreadReading:
    """Precheck read: classify the alert thread into
    :attr:`~_AlertThreadState.ABSENT` / :attr:`~_AlertThreadState.OPEN` /
    :attr:`~_AlertThreadState.RESOLVED`.

    Reads ``chatroom_get_thread`` (``mode='summary'``) — one MCP round-trip
    against a read-shaped tool. Returns the parsed reading on success. On a
    :class:`MagickitMcpError` envelope classified as not-found by
    :func:`_is_thread_not_found_envelope`, returns ABSENT (this is the ONE
    remaining envelope-classification site in this module; see the docstring
    on :func:`_is_thread_not_found_envelope` for why it is permitted only
    here). Any other envelope re-raises unchanged so the caller sees genuine
    transport / permission faults instead of swallowing them here.

    **T-gate-bootstrap-close-retried-on-resolved-thread S-3** (Bohr msg-2460
    §6 item 1). The old two-value form (``_alert_thread_exists``, returning
    ``bool``) caused L-B in the incident report: a ``resolved`` thread was
    misread as "exists → close it" and every tick issued a write-shaped call
    that got refused (338/day across playproof / lexora). The rename to
    ``_alert_thread_state`` and the widening to a tri-state is the fix: the
    ``resolved`` case is now first-class, and the caller can skip the write
    entirely (``action=already_closed`` with ``resolved_by_msg`` on the
    log line).

    Cost is unchanged from the two-value predecessor: one read per tick, no
    write-shaped call unless the state is :attr:`_AlertThreadState.OPEN`.
    """
    try:
        payload = await mcp.call_tool(
            "chatroom_get_thread",
            {"project": project, "thread_id": thread_id, "mode": "summary"},
        )
    except MagickitMcpError as exc:
        # Field equality on the envelope's own classification key, never a
        # search of the flattened message (Bohr msg-2383 §2 INV-D; PR-gate on
        # heads 9b023fe and 523d400). A permission fault must surface as
        # GateBootstrapCloseError however its free-form text happens to read.
        # Rationale in :func:`_is_thread_not_found_envelope`.
        if _is_thread_not_found_envelope(exc):
            return _AlertThreadReading(
                state=_AlertThreadState.ABSENT, status=None, resolved_by_msg=None
            )
        raise
    return _classify_reading(payload)


async def _read_thread_reading_or_none(
    mcp: McpToolCaller,
    *,
    project: str,
    thread_id: str,
) -> _AlertThreadReading | None:
    """Post-refusal read-back: return a reading, or ``None`` on ANY read failure.

    **The design's core predicate** (T-gate-bootstrap-close-retried-on-
    resolved-thread S-2-prime / Bohr msg-2458 §2 as adopted after Einstein
    msg-2457's invariant blocking). Called by :func:`open_alert` and
    :func:`close_alert` on the *refusal* branch of their write-shaped call,
    replacing the prose-substring swallows that used to live there
    (``"already resolved"`` / ``"already closed"`` / ``"already exists"``)
    and the earlier plan to use ``error_type == "ChatroomStateError"`` as a
    swallow filter.

    **Never inspects the exception.** The caller catches the refusal, calls
    this helper, and lets its structured answer decide. Any read failure —
    envelope refusal (including ChatroomNotFoundError), permission, transport,
    unusable payload — returns ``None``, and the caller then raises. That
    fail-closed direction is deliberate:

    - ABSENT after a write refusal cannot be positively observed:
      ``chatroom_get_thread`` returns not-found as a refusal, so recognising
      absence there would require the same envelope-name filter this
      function was written to avoid (Bohr msg-2460 §3). The precheck at the
      start of :func:`close_alert` already handled ABSENT for the common
      case; anything reaching here is a rare TOCTOU race where the safe
      direction is a loud 1-line refusal and let the next tick's precheck
      re-classify.
    - Any other read failure means the target state could not be confirmed
      at this call ∴ the caller must raise. Silent swallowing would
      re-introduce the whole "wrote a swallow against something we cannot
      actually see" family of bugs.

    On success, callers apply their OWN target-state predicate to
    ``reading.status``:
      * :func:`open_alert` accepts ``{"active", "awaiting_reply"}``
        (:data:`_OPEN_ALERT_TARGET_STATUSES`).
      * :func:`close_alert` accepts ``_RESOLVED_STATUS`` only
        (via :attr:`_AlertThreadState.RESOLVED`).
    """
    try:
        payload = await mcp.call_tool(
            "chatroom_get_thread",
            {"project": project, "thread_id": thread_id, "mode": "summary"},
        )
    except MagickitMcpError:
        return None
    return _classify_reading(payload)


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

    **Structure** (T-gate-bootstrap-close-retried-on-resolved-thread S-2-prime +
    S-3, Bohr msg-2460 §6):

    1. **Precheck** via :func:`_alert_thread_state`. Three-value:
       - :attr:`_AlertThreadState.ABSENT` — the thread was never opened for
         this project. Return ``CloseResult(was_open=False)`` immediately;
         no write-shaped call is issued (msg-2027 §設計側の指摘 (ii): 「閉
         じる対象が無いなら呼ばない」).
       - :attr:`_AlertThreadState.RESOLVED` — the thread exists and its
         status is already ``resolved``. Return ``CloseResult(was_open=False,
         resolved_by_msg=...)`` immediately; NO write-shaped call is issued.
         This is the direct fix for L-B in the incident report (Bohr
         msg-2456 §5): the old two-value precheck missed this state and
         issued a doomed close on every tick (338/day across playproof /
         lexora — measured 2026-09-03).
       - :attr:`_AlertThreadState.OPEN` — the thread exists and is not
         resolved. Proceed to the write.
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

    **Refusal handling — the design's core rule** (Bohr msg-2458 §2, adopted
    after Einstein msg-2457's invariant blocking). If the close call raises
    :class:`MagickitMcpError`, this function **does not inspect the
    exception**. It calls :func:`_read_thread_reading_or_none` and asks a
    single question: is the observed status ``resolved``? Only that one
    outcome — the target state has actually been reached in the world —
    swallows the refusal (``CloseResult(was_open=False,
    resolved_by_msg=...)``). Every other outcome raises
    :class:`GateBootstrapCloseError`:

      * ``status == "active"`` / ``"awaiting_reply"`` — target state not
        reached; the refusal is real.
      * ``status == "superseded"`` / ``"parked"`` — semantics are unmeasured
        (Bohr msg-2460 §4). The safe direction is raise, not a guess about
        whether the state counts as "closed enough".
      * Any other status (unknown / malformed / a future addition) — same
        conservative direction.
      * :attr:`_AlertThreadState.ABSENT` — cannot be positively observed at
        the read-back (would require the same envelope-name filter this
        design avoids), so it too raises. The frequency floor here is small:
        the precheck already handled ABSENT for the common case; only a
        TOCTOU race (precheck saw OPEN, close saw absent) reaches here, and
        the next tick's precheck re-classifies to ABSENT (1 loud line, then
        silence).
      * Read-back itself refused / transport failure — the target state
        could not be confirmed ∴ raise (fail-closed).

    **Discharge of the earlier follow-up marker** (former substring branch —
    `"already resolved" in str(exc).lower()`). The origin/main comment there
    declared a "discharge condition, one call: close an already-resolved
    thread once, record the envelope's ``error_type``". That measurement was
    made (Bohr msg-2456 §2, incident report, 2026-09-03): the observed
    envelope carries ``error_type='ChatroomStateError'`` and the ``error``
    text ``"Cannot close thread '<id>' in status='resolved'"``. The measured
    value is **recorded** here for the next reader, and **not used as a
    predicate**:

        The comment's proposal was to convert both substring branches to
        ``exc.error_type == "<Name>"`` equality. Einstein msg-2457 blocked
        that shape on the invariant grounds that placing a name-based
        filter at the ENTRY of the refusal branch is an execution-path
        dependence on the name — a future server rename bypasses the
        swallow and re-introduces L-A silently. The measurement therefore
        does not become a predicate: the design instead reads the world
        after the refusal and asks about the *goal state*. The
        ``error_type`` measurement is preserved above so a future reader
        does not repeat the measurement, and so no reader mistakes its
        absence for "we do not know the value".

    Both boundaries — the precheck read AND the post-refusal read-back —
    obey the ONE failure surface: a read fault at the precheck surfaces as
    :class:`GateBootstrapCloseError` (below); a refusal-plus-unconfirmed-
    state at the write also surfaces as :class:`GateBootstrapCloseError`.
    The operator sees one exception class for "the close contract could not
    be discharged".
    """
    thread_id = thread_id_for(project)
    try:
        reading = await _alert_thread_state(mcp, project=project, thread_id=thread_id)
    except MagickitMcpError as exc:
        # Any non-"not found" envelope at the read boundary is a genuine
        # fault (permission / transport / auth). Surface it loudly through
        # the same exception class the close boundary uses, so the operator
        # sees ONE failure surface across the whole close_alert contract.
        raise GateBootstrapCloseError(
            f"chatroom_get_thread precheck refused for {thread_id!r} in project {project!r}: {exc}"
        ) from exc

    if reading.state == _AlertThreadState.ABSENT:
        return CloseResult(thread_id=thread_id, was_open=False)
    if reading.state == _AlertThreadState.RESOLVED:
        # Target state already reached — return without issuing any write.
        # ``resolved_by_msg`` surfaces into the tick's JSON output so the
        # operator sees WHO resolved the thread (incident report §2 named
        # this observation as the ground truth).
        return CloseResult(
            thread_id=thread_id,
            was_open=False,
            resolved_by_msg=reading.resolved_by_msg,
        )

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
        # Do not inspect the exception. Read the world; ask a single
        # question about close_alert's target state ("thread is resolved").
        # Bohr msg-2458 §2 as adopted after Einstein msg-2457 invariant
        # blocking. The former substring branches
        # (``"already resolved"`` / ``"already closed"``) and the fallback
        # plan of ``error_type == "ChatroomStateError"`` are BOTH replaced
        # by this one predicate, and the measured value of that error_type
        # (2026-09-03: ``"ChatroomStateError"``) is recorded in the docstring
        # above but deliberately NOT used as a filter (rationale there).
        recheck = await _read_thread_reading_or_none(mcp, project=project, thread_id=thread_id)
        if recheck is not None and recheck.state == _AlertThreadState.RESOLVED:
            return CloseResult(
                thread_id=thread_id,
                was_open=False,
                resolved_by_msg=recheck.resolved_by_msg,
            )
        observed = _describe_reading(recheck)
        raise GateBootstrapCloseError(
            f"chatroom_close_thread refused for {thread_id!r} in project {project!r} "
            f"and post-refusal read-back did not confirm target state 'resolved' "
            f"({observed}). Original refusal: {exc}"
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
