"""PR-review orchestrator — WIRING_ALLOWLIST_SPEC §A.2 (T20 → ADR-19 driver-化).

Event-driven trigger for the naysayer's Tier B gate (ADR-07 §2.2): when a develop→main PR is
opened, :meth:`PrReviewOrchestrator.fire_pr_review` opens a ``T-pr-review-<repo>-<n>`` chatroom
thread carrying the PR ref and **drives the review directly** via
:class:`~spirrow_mindwire.naysayer.pr_review.NaysayerPrReviewDriver` — it runs the deterministic
CI-gate, has the independent Gemini judge the diff (Lexora one-shot), posts the critique to the
thread, and submits the GitHub PR review.

ADR-2026-06-04-19 (driver-化 unify): the PR-gate is **no longer a registry RoleAdapter**, so the
review no longer goes through the watcher → dispatcher round-trip (the registry's sole
``NAYSAYER_QUALIFIED`` adapter is the design-time ``NaysayerSdkAdapter``). The orchestrator owns
the chatroom transport (open thread + post critique); the driver owns the judging-behavior + the
deterministic guards (CI-gate / verdict / T22 submit). No webhook (inconsistent with the
Tailscale-only egress); the trigger is invoked by whoever opens the develop→main PR (the main
chain / a ``scripts/naysayer_review.py`` run / a future PR-event hook).
"""

from __future__ import annotations

from dataclasses import dataclass

from .github.client import CiState, CiStatus, GitHubReviewClient, PrRef, parse_pr_ref
from .magickit.client import MagickitMcpError, McpToolCaller
from .naysayer.pr_review import NaysayerPrReviewDriver, PrReviewOutcome
from .value_objects import ThreadRef

_DEFAULT_THREAD_PREFIX = "T-pr-review-"
_DEFAULT_OWNER = "orchestrator"
_DEFAULT_NAYSAYER_AUTHOR = "naysayer-pr-review"


class ThreadIdCollisionError(RuntimeError):
    """A PR-review thread id is already taken by a *different* PR.

    Raised instead of writing into the other PR's ledger. The gate fails loudly
    and produces nothing rather than appending a critique to a thread whose title,
    history and close-predicate belong to something else.
    """


def _qualified_thread_id(prefix: str, pr: PrRef) -> str:
    """``T-pr-review-<repo>-<n>`` — the repo is *in* the id, not assumed around it.

    The previous id was ``T-pr-review-<n>``, justified in a comment by "the chatroom
    is per-project, so a PR number is project-unique for a single-repo project — not
    the case today". That condition stopped holding without anyone noticing: one
    chatroom project now carries PR-gate threads for four repos, and PR numbers are
    only unique *within* a repo. Since ``_open_thread`` treats "already exists" as
    success, the resulting id clash would not have raised — it would have appended
    one repo's critique to another repo's thread.

    The repo is lower-cased so that the same repo written two ways
    (``Spirrow-VoxelWorld`` / ``spirrow-voxelworld``) cannot open two ledgers for
    one PR; GitHub repo names are case-insensitive for identity.

    The owner is deliberately *not* in the id: it would make every id half again as
    long for a distinction that only bites across organisations. That is a premise
    too — but unlike the old one it is not left to a comment: two different PRs
    landing on one id is checked at open time and raises
    :class:`ThreadIdCollisionError`. If that ever fires for a cross-org repo-name
    clash, the fix is to extend this function with ``pr.owner``.
    """
    return f"{prefix}{pr.repo.lower()}-{pr.number}"


@dataclass(frozen=True)
class _ThreadSubject:
    """What a lookup at a review-thread id found — **three** answers, not two.

    ``absent`` (nothing there, the id is free), ``about this PR`` (ours, reuse it)
    and ``exists but says nothing about which PR it is`` are genuinely different
    facts, and the third is not the first. Collapsing it into "absent" made
    :meth:`PrReviewOrchestrator._resolve_thread_id` report an occupied id as free,
    so the clash surfaced only at open time — i.e. after a Gemini review had been
    produced and paid for, which is exactly what resolving early exists to avoid.

    ``pr is None`` while ``exists`` is true is never treated as a match, so an
    unrecognised thread is still never written into.
    """

    exists: bool
    pr: PrRef | None

    @property
    def label(self) -> str:
        """How to name what is sitting on the id, in an error a human reads."""
        return self.pr.slug if self.pr is not None else "an unidentifiable PR"


_NO_THREAD = _ThreadSubject(exists=False, pr=None)


def _same_pr(found: PrRef | None, pr: PrRef) -> bool:
    """Whether a thread's PR ref denotes the same pull request as ``pr``.

    Case-insensitive on owner/repo, because the *id* is built from a lower-cased
    repo: ``Spirrow-VoxelWorld#12`` and ``spirrow-voxelworld#12`` land on one
    thread id by construction. Comparing case-sensitively there would report a
    thread as colliding with itself and turn an idempotent re-fire into a hard
    failure — and both spellings are in live use in the chatroom's titles.
    """
    if found is None:
        return False
    return (
        found.number == pr.number
        and found.repo.lower() == pr.repo.lower()
        and found.owner.lower() == pr.owner.lower()
    )


def _legacy_thread_id(prefix: str, pr: PrRef) -> str:
    """``T-pr-review-<n>`` — the pre-qualification id.

    Kept only so a PR whose gate already opened a thread under the old scheme keeps
    writing to it instead of sprouting a second, disjoint ledger mid-review. It is
    reused *only* when that thread is verifiably about this same PR. Deletable once
    no unqualified PR-gate thread is still live.
    """
    return f"{prefix}{pr.number}"


class PrReviewOrchestrator:
    """Opens a ``T-pr-review-<repo>-<n>`` thread + drives the naysayer PR-review driver (§A.2)."""

    def __init__(
        self,
        mcp: McpToolCaller,
        *,
        driver: NaysayerPrReviewDriver,
        owner: str = _DEFAULT_OWNER,
        thread_prefix: str = _DEFAULT_THREAD_PREFIX,
        naysayer_author: str = _DEFAULT_NAYSAYER_AUTHOR,
    ) -> None:
        self._mcp = mcp
        self._driver = driver
        self._owner = owner
        self._thread_prefix = thread_prefix
        self._naysayer_author = naysayer_author

    async def fire_pr_review(
        self,
        *,
        project: str,
        pr_ref: str,
        title: str | None = None,
    ) -> tuple[ThreadRef, PrReviewOutcome]:
        """Open the review thread for a develop→main PR and drive the naysayer review.

        The thread id is **PR-derived and deterministic** — ``T-pr-review-<repo>-<n>`` — so there
        is no max+1 numbering to race (Tier C decide msg-459, resolving the Tier B leak-vs-race
        flip-flop). Returns the thread's :class:`ThreadRef` and the
        :class:`~spirrow_mindwire.naysayer.pr_review.PrReviewOutcome`. Raises ``ValueError`` if
        ``pr_ref`` is unparseable; the driver fail-closes (raises) on an unreachable Lexora/GitHub
        or an empty review, so a failed review is never silently treated as a pass.
        """
        pr = parse_pr_ref(pr_ref)
        if pr is None:
            raise ValueError(
                f"unparseable PR ref: {pr_ref!r} (expected 'owner/repo#n' or a PR URL)"
            )

        # Deterministic, PR-derived thread id (Tier C decide msg-459): no max+1 numbering, so there
        # is no compute→defer-open TOCTOU window (Tier B round-2 msg-457) and re-firing the same PR
        # reuses its one thread. Resolved (and validated) *before* the review runs, not at post
        # time: an unusable id should cost nothing, whereas failing after `driver.review` would
        # throw away a paid-for Gemini judgement. Only reads happen here, so the thread itself is
        # still opened lazily (msg-453: no abandoned empty thread on a transient remote error).
        thread_id = await self._resolve_thread_id(project=project, pr=pr)
        title = title or f"PR review (develop→main) — {pr_ref}"
        propose = (
            f"naysayer review request — develop→main PR {pr_ref}\n\n"
            "Independent naysayer: the CI-gate runs first; if CI is green the diff is "
            "reviewed and a GitHub PR review (Approve / Request-changes) is submitted. "
            "An objection returns the change to the proposer/implementer fix loop; an "
            "approve is the necessary condition for Takahito's merge GO (Tier C)."
        )
        thread_ref = ThreadRef(
            project_id=project,
            thread_id=thread_id,
            chatroom_uri=f"magickit://chatroom/thread/{thread_id}",
        )

        opened = False

        async def post_critique(body: str) -> None:
            # Open the thread LAZILY — only once there is a critique to put in it. The driver
            # calls this exactly once, after a review is produced. Opening durable chatroom state
            # up-front (before the fallible Lexora/GitHub calls inside driver.review) would leave
            # an abandoned empty review thread on every transient remote error (Tier B msg-453).
            # Posted as the naysayer; the driver calls this before the GitHub submission.
            nonlocal opened
            if not opened:
                await self._open_thread(
                    project=project, thread_id=thread_id, title=title, propose=propose, pr=pr
                )
                opened = True
            await self._mcp.call_tool(
                "chatroom_post_message",
                {
                    "project": project,
                    "thread_id": thread_id,
                    "msg_type": "report",
                    "author": self._naysayer_author,
                    "content": body,
                },
            )

        outcome = await self._driver.review(pr, post_critique=post_critique)
        return thread_ref, outcome

    async def _resolve_thread_id(self, *, project: str, pr: PrRef) -> str:
        """Pick the thread id this PR's gate writes to, and prove it is free or ours.

        Order matters. The qualified id is the answer unless an *older* thread for
        this same PR already exists under the pre-qualification scheme, in which
        case the review continues where it started.

        The two ids are judged by different rules, and the difference is the point.
        At the **qualified** id, anything already there that is not provably this
        PR's blocks — including a thread that exists but names no PR, because an id
        under someone else's thread is unusable whether or not we can read who they
        are. At the **legacy** id, the same unreadable thread merely fails to be
        ours: the qualified id is still free, so the gate continues there rather
        than failing. "Is this id usable" and "is this thread mine" are separate
        questions and only the first one is a reason to stop.
        """
        qualified = _qualified_thread_id(self._thread_prefix, pr)
        found = await self._thread_subject(project=project, thread_id=qualified)
        if found.exists:
            if not _same_pr(found.pr, pr):
                raise ThreadIdCollisionError(
                    f"thread {qualified!r} in project {project!r} is about {found.label}, "
                    f"not {pr.slug} — refusing to post one PR's review into another's thread"
                )
            return qualified

        legacy = _legacy_thread_id(self._thread_prefix, pr)
        if legacy != qualified:
            older = await self._thread_subject(project=project, thread_id=legacy)
            if _same_pr(older.pr, pr):
                return legacy
        return qualified

    async def _thread_subject(self, *, project: str, thread_id: str) -> _ThreadSubject:
        """What is sitting on ``thread_id``: nothing, this PR, or something unnamed.

        Only an explicit "not found" from the far end counts as *absent*. A call
        that returned **something** describes a thread that exists, so a payload
        this cannot parse is reported as existing-but-unidentified rather than as
        free — guessing "free" there is how an occupied id gets handed to a review.

        ``mode="summary"`` keeps the payload small — a colliding id can point at a
        long resolved thread. A summary of a resolved thread carries only its decide
        msg, which is why the title is parsed first: it is present in every mode.
        """
        try:
            payload = await self._mcp.call_tool(
                "chatroom_get_thread",
                {"project": project, "thread_id": thread_id, "mode": "summary"},
            )
        except MagickitMcpError as exc:
            if "not found" in str(exc).lower():
                return _NO_THREAD
            raise

        candidates: list[str] = []
        if isinstance(payload, dict):
            thread = payload.get("thread")
            if isinstance(thread, dict):
                candidates.append(str(thread.get("title") or ""))
            messages = payload.get("messages")
            if isinstance(messages, list):
                candidates.extend(
                    str(m.get("content") or "") for m in messages if isinstance(m, dict)
                )
        for text in candidates:
            # parse_pr_ref accepts both `owner/repo#n` and PR URLs, so a thread opened
            # from a URL-shaped ref still compares equal to one opened from a slug.
            ref = parse_pr_ref(text)
            if ref is not None:
                return _ThreadSubject(exists=True, pr=ref)
        return _ThreadSubject(exists=True, pr=None)

    async def _open_thread(
        self, *, project: str, thread_id: str, title: str, propose: str, pr: PrRef
    ) -> None:
        """Open the review thread, treating an 'already exists' collision as success (idempotent).

        Re-reviewing the same PR reuses its existing thread: conclair rejects a duplicate
        ``thread_id`` with a ``ChatroomIntegrityError`` ("... already exists in project ...",
        surfaced here as a :class:`MagickitMcpError`). Only that condition is swallowed — any
        other open error re-raises (no masking), mirroring the driver's 422→COMMENT fallback.

        The swallow is **conditional on the existing thread being this PR's**. Unconditional,
        it turned an id clash into a silent write into someone else's ledger; conclair accepts
        a ``report`` into a ``resolved`` thread (only ``decide`` is status-gated), so the
        clash would not even have been noisy at the far end. This re-check is not redundant
        with :meth:`_resolve_thread_id`: the review runs between the two, which is ample time
        for another gate to have opened the same thread.
        """
        try:
            await self._mcp.call_tool(
                "chatroom_open_thread",
                {
                    "project": project,
                    "thread_id": thread_id,
                    "title": title,
                    "owner": self._owner,
                    "propose_content": propose,
                    "tags": ["pr-review", "naysayer", "stage3"],
                },
            )
        except MagickitMcpError as exc:
            if "already exists" in str(exc).lower():
                found = await self._thread_subject(project=project, thread_id=thread_id)
                if _same_pr(found.pr, pr):
                    return  # this PR's own thread → re-fire is idempotent
                raise ThreadIdCollisionError(
                    f"thread {thread_id!r} in project {project!r} already exists and is about "
                    f"{found.label}, not {pr.slug}"
                ) from exc
            raise

    async def aclose(self) -> None:
        """Close the PR-review driver's shared HTTP clients (loop teardown).

        The driver is orchestrator-held, not registry-registered (ADR-19 driver-化), so the loop's
        teardown closes it here rather than via the registry's adapter sweep (Tier B #93 round-4).
        """
        await self._driver.aclose()


class MergeBlockedError(RuntimeError):
    """Raised by :func:`require_ci_success` when CI is not green (ADR-16 L2 / D-3)."""

    def __init__(self, status: CiStatus) -> None:
        detail = ", ".join(status.failing) if status.failing else status.state.value
        super().__init__(
            f"merge blocked: CI is not green (state={status.state.value}, "
            f"head={status.head_sha or '?'}: {detail})"
        )
        self.status = status


async def require_ci_success(github: GitHubReviewClient, pr: PrRef) -> CiStatus:
    """Deterministic merge-GO precondition (ADR-2026-06-03-16 L2 / D-3): CI must be SUCCESS.

    The **deterministic** half of the two-condition merge gate: a green CI is a necessary
    condition for merge-GO, checked in code *independently* of the naysayer's (LLM) APPROVE — so a
    mis-firing L1 belt can never let a red PR through. L2 is the authoritative gate; L1 (the
    CI-aware naysayer driver) is the belt. Returns the
    :class:`~spirrow_mindwire.github.client.CiStatus` on success; raises :class:`MergeBlockedError`
    for failure / pending / UNKNOWN (fail-closed).
    """
    status = await github.fetch_ci_status(pr)
    if status.state is not CiState.SUCCESS:
        raise MergeBlockedError(status)
    return status


__all__ = ["MergeBlockedError", "PrReviewOrchestrator", "require_ci_success"]
