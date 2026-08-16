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
from typing import Any

from .github.client import CiState, CiStatus, GitHubReviewClient, PrRef, parse_pr_ref
from .magickit.client import MagickitMcpError, McpToolCaller
from .naysayer.pr_review import NaysayerPrReviewDriver, PrReviewOutcome
from .value_objects import Role, ThreadRef

_DEFAULT_THREAD_PREFIX = "T-pr-review-"
_DEFAULT_OWNER = "orchestrator"
_DEFAULT_NAYSAYER_AUTHOR = "naysayer-pr-review"


class ThreadIdCollisionError(RuntimeError):
    """A PR-review thread id is already taken by a *different* PR.

    Raised instead of writing into the other PR's ledger. The gate fails loudly
    and produces nothing rather than appending a critique to a thread whose title,
    history and close-predicate belong to something else.
    """


def _envelope_error(payload: Any) -> str | None:
    """The failure described by a chatroom payload, or ``None`` if it is not one.

    conclair does not raise when a chatroom call is refused. It answers with an
    ordinary **success** response whose body is an error envelope::

        {"error_type": "ChatroomNotFoundError",
         "error": "Thread 'T-pr-review-spirrow-magickit-22' not found in project '...'",
         "details": {"project": "...", "thread_id": "..."}}

    ``isError`` is not set, so :func:`~spirrow_mindwire.magickit.client.parse_tool_result`
    finds JSON, returns it, and every ``except MagickitMcpError`` below is simply
    never reached. ``chatroom_open_thread``'s own MCP docstring says as much --
    *"On success: {...}. On failure: conclair error envelope {...}"* -- so the
    contract those handlers were written against never existed.

    What that cost, measured on the live server (2026-08-16): a thread that is
    **not there** came back as a payload without a ``"thread"`` key, which
    :meth:`PrReviewOrchestrator._thread_subject` read as "exists, but names no PR"
    -- the one answer that blocks. So the gate refused every PR that did not
    already own a ledger thread, which is every *new* PR. It first fired on
    ``spirrow-magickit#22`` and had to be worked around by hand.

    Detection is by ``error_type`` rather than by the absence of an expected key:
    "this call failed" is a fact the far end states, and inferring it from a
    missing field is how "absent" became "unidentifiable" in the first place.
    """
    if not isinstance(payload, dict):
        return None
    error_type = payload.get("error_type")
    if not isinstance(error_type, str) or not error_type:
        return None
    message = payload.get("error")
    return f"{error_type}: {message}" if isinstance(message, str) and message else error_type


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


@dataclass(frozen=True)
class _ResolvedThread:
    """The id this PR's gate writes to, **and whether its thread is already there**.

    ``exists`` is not a convenience. Resolving the id already had to read the far
    end to prove the id is free or ours, and dropping that answer is what made the
    ordinary re-review path (an open PR that got another push) open a thread it had
    just been told was open: a guaranteed "already exists" failure, caught by the
    collision handler, which then re-read the very same thread to find out whose it
    was. Three round-trips to relearn what the first one returned, and the race
    handler standing in for normal control flow — so a genuine race and a re-review
    became indistinguishable in the logs. Carrying the bit forward leaves the race
    handler for races.
    """

    thread_id: str
    exists: bool


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

    async def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call one chatroom tool, raising :class:`MagickitMcpError` if it was refused.

        Every chatroom call this class makes goes through here, so "the far end
        raises when it says no" -- which the handlers in :meth:`_thread_subject` and
        :meth:`_open_thread` are written against, and which :func:`_envelope_error`
        shows is not true of the transport -- becomes true at this boundary.

        The scope is deliberately this class and not the client. Six other call
        sites read the same envelopes as successes (two in ``conductor/core``, two
        in ``conductor/control``, one each in ``magickit/watcher`` and
        ``magickit/gateway``) and raising there changes six behaviours at once; that
        is its own change, and it should be reviewed by a working gate rather than
        merged past a broken one. Nothing outside :class:`PrReviewOrchestrator`
        moves here.

        All three of this class's calls are wrapped, not just the read that was
        failing. Fixing only :meth:`_thread_subject` makes :meth:`_open_thread`
        reachable for the first time since the envelope was introduced -- and on the
        same measurement, ``chatroom_open_thread`` and ``chatroom_post_message``
        answer a refusal with an envelope too. A failed open would then be read as
        success, the critique posted into a thread that does not exist would be read
        as success as well, and the driver would go on to submit the GitHub review:
        a verdict on the PR with no ledger entry anywhere, reported as a clean run.
        That trades today's loud failure for a silent one on the branch the fix
        re-opens, so the read and the two writes are corrected together.
        """
        payload = await self._mcp.call_tool(name, arguments)
        failure = _envelope_error(payload)
        if failure is not None:
            raise MagickitMcpError(f"magickit tool {name!r} failed: {failure}")
        return payload

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
        resolved = await self._resolve_thread_id(project=project, pr=pr)
        thread_id = resolved.thread_id
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

        # Nothing to open when resolving already found this PR's thread — the re-review path.
        # Reading `exists` here is what keeps the "already exists" handler in `_open_thread` a
        # race handler: it is now reached only when the id was free at resolve time.
        opened = resolved.exists

        async def post_critique(body: str) -> None:
            # Open the thread LAZILY — only once there is a critique to put in it, and only if it
            # is not already there. The driver calls this exactly once, after a review is produced.
            # Opening durable chatroom state up-front (before the fallible Lexora/GitHub calls
            # inside driver.review) would leave an abandoned empty review thread on every transient
            # remote error (Tier B msg-453).
            # Posted as the naysayer; the driver calls this before the GitHub submission.
            nonlocal opened
            if not opened:
                await self._open_thread(
                    project=project, thread_id=thread_id, title=title, propose=propose, pr=pr
                )
                opened = True
            await self._call(
                "chatroom_post_message",
                {
                    "project": project,
                    "thread_id": thread_id,
                    "msg_type": "report",
                    "author": self._naysayer_author,
                    "content": body,
                    # D-1 (T-dispatched-turn-gets-one-message). This is the Tier B
                    # verdict — the single most gate-relevant message the harness
                    # writes — and it recorded ``role: null`` 346 times out of 346
                    # (live corpus, 2026-08-16). The claim is honest: this body IS
                    # the independent naysayer's critique, relayed verbatim.
                    #
                    # Whether it RECORDS depends on ``self._naysayer_author`` being
                    # a registered magickit identity with ``naysayer`` in its
                    # allowed_roles; an unregistered author has its role dropped and
                    # still posts. So this supplies the value and the registration is
                    # a magickit-side fact to confirm, not something this repo can
                    # assert. Read the posted message back to know which happened.
                    "role": Role.NAYSAYER.value,
                },
            )

        outcome = await self._driver.review(pr, post_critique=post_critique)
        return thread_ref, outcome

    async def _resolve_thread_id(self, *, project: str, pr: PrRef) -> _ResolvedThread:
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

        Both answers are returned (:class:`_ResolvedThread`), because deciding which
        id to use *is* deciding whether it already holds a thread — and the caller
        needs that second fact to know whether there is anything left to open.
        """
        qualified = _qualified_thread_id(self._thread_prefix, pr)
        found = await self._thread_subject(project=project, thread_id=qualified)
        if found.exists:
            if not _same_pr(found.pr, pr):
                raise ThreadIdCollisionError(
                    f"thread {qualified!r} in project {project!r} is about {found.label}, "
                    f"not {pr.slug} — refusing to post one PR's review into another's thread"
                )
            return _ResolvedThread(thread_id=qualified, exists=True)

        legacy = _legacy_thread_id(self._thread_prefix, pr)
        if legacy != qualified:
            older = await self._thread_subject(project=project, thread_id=legacy)
            if _same_pr(older.pr, pr):
                return _ResolvedThread(thread_id=legacy, exists=True)
        return _ResolvedThread(thread_id=qualified, exists=False)

    async def _thread_subject(self, *, project: str, thread_id: str) -> _ThreadSubject:
        """What is sitting on ``thread_id``: nothing, this PR, or something unnamed.

        Only an explicit "not found" from the far end counts as *absent*. A call
        that returned **something** describes a thread that exists, so a payload
        this cannot parse is reported as existing-but-unidentified rather than as
        free — guessing "free" there is how an occupied id gets handed to a review.

        That rule was right and was reading the wrong place. conclair states "not
        found" in an error envelope carried by a *successful* response, so the
        ``except`` below never ran and every absent thread arrived here as a payload
        with no ``"thread"`` key — i.e. as the unidentified answer, which blocks. The
        gate therefore refused every PR that did not already own a ledger thread.
        :meth:`_call` now turns that envelope into the exception this was always
        written for; see :func:`_envelope_error` for the measurement.

        Only the thread's **opening** message and its title are read as statements
        of which PR this is. Those are the two the gate itself writes when it opens
        the thread — the review request carries the ref, and so does the title.
        Everything after them is critiques, replies and closing notes, which
        routinely mention *other* PRs ("closing, superseded by owner/repo#99");
        letting any of those speak for the thread makes a passing mention decide
        what the thread is about.

        The two are **not** peers, and which one that makes authoritative is not a
        matter of taste. The opening message is fixed at open time and append-only
        thereafter; the title is caller-supplied (``fire_pr_review`` takes one) and
        is not part of what identifies the thread. So the opener decides, and the
        title is read **only when the opener names no PR at all** — never as a
        competing opinion. Letting the title win over a readable opener meant a
        caller who passed a title naming some *other* PR ("reviewing the fix for
        owner/repo#99") locked this PR out of its own ledger for good: every later
        gate re-read that title, disagreed with itself, and raised
        :class:`ThreadIdCollisionError` over a thread that was this PR's all along.

        The title is nevertheless still read, because dropping it strands threads
        that exist right now. Measured over every ``T-pr-review-<n>`` thread in both
        live gate projects (149 threads: 118 in ``spirrow-voxelworld``, 31 in
        ``spirrow-mindwire``), **four** name their PR in the title and nowhere else
        — ``T-pr-review-162/163/164/166``, all opened by hand rather than by this
        gate, whose opening message says "PR #163" without the ``owner/repo``
        qualification ``parse_pr_ref`` needs. On the same sweep, **zero** threads
        have a title and an opener naming *different* PRs, so the ordering above
        costs nothing today and the fallback is load-bearing today. Those four sit
        on *legacy* ids, where an unidentifiable thread is not an error but a
        fall-through to the qualified id: the gate would open a second ledger for a
        PR that already has one, which is the exact split :func:`_legacy_thread_id`
        exists to prevent.

        The opening message is taken **by the id the thread gives for it**
        (``created_by_msg``: required, and per the chatroom schema "the thread's
        first msg (the propose); it never changes"), not by position in
        ``messages``. Position is not identity: the returned list is filtered by
        ``mode`` — ``summary`` on a *resolved* thread returns only its ``decide``
        msgs — so "first element" there is the thread's closing note, i.e. exactly
        the kind of message excluded above. Asking for the opener by name means a
        payload that does not contain it yields *no* statement (unidentified, which
        blocks) rather than the wrong one (misattributed, which either locks a PR
        out of its own thread or writes its critique into someone else's).

        ``mode="full"`` is therefore requested explicitly: the opener has to be in
        the payload for the thread to be identifiable at all. The earlier
        ``"summary"`` was there to keep a long thread's payload small, but conclair
        filters only when ``status == "resolved"`` — every other state returns the
        full list regardless — so it shrank nothing on the live path (the gate's own
        threads are all still open, issue #147) and dropped the one message that
        matters exactly where it did shrink.
        """
        try:
            payload = await self._call(
                "chatroom_get_thread",
                {"project": project, "thread_id": thread_id, "mode": "full"},
            )
        except MagickitMcpError as exc:
            if "not found" in str(exc).lower():
                return _NO_THREAD
            raise

        title = ""
        opener = ""
        if isinstance(payload, dict):
            thread = payload.get("thread")
            opening_msg_id = ""
            if isinstance(thread, dict):
                title = str(thread.get("title") or "")
                opening_msg_id = str(thread.get("created_by_msg") or "")
            messages = payload.get("messages")
            # No id for the opener means no message speaks for the thread: an unnamed
            # opener is not grounds for promoting whichever message came back first.
            if opening_msg_id and isinstance(messages, list):
                opener = next(
                    (
                        str(m.get("content") or "")
                        for m in messages
                        if isinstance(m, dict) and m.get("msg_id") == opening_msg_id
                    ),
                    "",
                )

        # parse_pr_ref accepts both `owner/repo#n` and PR URLs, so a thread opened from a
        # URL-shaped ref still compares equal to one opened from a slug.
        subject = parse_pr_ref(opener)
        if subject is None:
            # Reached only when the opener names no PR at all -- the title recovers a
            # thread the gate did not open, it never overrules one it did.
            subject = parse_pr_ref(title)
        return _ThreadSubject(exists=True, pr=subject)

    async def _open_thread(
        self, *, project: str, thread_id: str, title: str, propose: str, pr: PrRef
    ) -> None:
        """Open the review thread, treating an 'already exists' collision as success (idempotent).

        Re-reviewing the same PR reuses its existing thread: conclair rejects a duplicate
        ``thread_id`` with a ``ChatroomIntegrityError`` ("... already exists in project ...",
        returned as an error envelope and surfaced here as a :class:`MagickitMcpError` by
        :meth:`_call`). Only that condition is swallowed — any other open error re-raises
        (no masking), mirroring the driver's 422→COMMENT fallback. Before :meth:`_call` the
        envelope was read as a successful open, so *every* refusal was swallowed, including
        the ones this is careful to re-raise.

        The swallow is **conditional on the existing thread being this PR's**. Unconditional,
        it turned an id clash into a silent write into someone else's ledger; conclair accepts
        a ``report`` into a ``resolved`` thread (only ``decide`` is status-gated), so the
        clash would not even have been noisy at the far end. This re-check is not redundant
        with :meth:`_resolve_thread_id`: the review runs between the two, which is ample time
        for another gate to have opened the same thread.

        It is also the *only* way to get here. ``fire_pr_review`` skips the open entirely when
        resolving found this PR's thread already in place, so "already exists" at this point
        means the id was free when the review started and is not free now — a real race, not the
        ordinary re-review of an open PR.
        """
        try:
            await self._call(
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
