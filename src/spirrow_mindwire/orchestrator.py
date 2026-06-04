"""PR-review orchestrator — WIRING_ALLOWLIST_SPEC §A.2 (T20 → ADR-19 driver-化).

Event-driven trigger for the naysayer's Tier B gate (ADR-07 §2.2): when a develop→main PR is
opened, :meth:`PrReviewOrchestrator.fire_pr_review` opens a ``T-pr-review-<n>`` chatroom thread
carrying the PR ref and **drives the review directly** via
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

from typing import Any

from .github.client import CiState, CiStatus, GitHubReviewClient, PrRef, parse_pr_ref
from .magickit.client import McpToolCaller
from .naysayer.pr_review import NaysayerPrReviewDriver, PrReviewOutcome
from .value_objects import ThreadRef

_DEFAULT_THREAD_PREFIX = "T-pr-review-"
_DEFAULT_OWNER = "orchestrator"
_DEFAULT_NAYSAYER_AUTHOR = "naysayer-pr-review"


class PrReviewOrchestrator:
    """Opens a ``T-pr-review-<n>`` thread + drives the naysayer PR-review driver (§A.2)."""

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
        number: int | None = None,
        title: str | None = None,
    ) -> tuple[ThreadRef, PrReviewOutcome]:
        """Open the review thread for a develop→main PR and drive the naysayer review.

        ``number`` is the ``<n>`` suffix; if omitted it is derived as one past the highest existing
        ``T-pr-review-<n>`` thread in the project. Returns the new thread's :class:`ThreadRef` and
        the :class:`~spirrow_mindwire.naysayer.pr_review.PrReviewOutcome`. Raises ``ValueError`` if
        ``pr_ref`` is unparseable; the driver fail-closes (raises) on an unreachable Lexora/GitHub
        or an empty review, so a failed review is never silently treated as a pass.
        """
        pr = parse_pr_ref(pr_ref)
        if pr is None:
            raise ValueError(
                f"unparseable PR ref: {pr_ref!r} (expected 'owner/repo#n' or a PR URL)"
            )

        n = number if number is not None else await self._next_number(project)
        thread_id = f"{self._thread_prefix}{n}"
        title = title or f"PR review (develop→main) — {pr_ref}"
        propose = (
            f"naysayer review request — develop→main PR {pr_ref}\n\n"
            "Independent naysayer: the CI-gate runs first; if CI is green the diff is "
            "reviewed and a GitHub PR review (Approve / Request-changes) is submitted. "
            "An objection returns the change to the proposer/implementer fix loop; an "
            "approve is the necessary condition for Takahito's merge GO (Tier C)."
        )
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
        thread_ref = ThreadRef(
            project_id=project,
            thread_id=thread_id,
            chatroom_uri=f"magickit://chatroom/thread/{thread_id}",
        )

        async def post_critique(body: str) -> None:
            # The critique is posted to the just-opened review thread (the human-visible record),
            # authored as the naysayer; the driver calls this before the GitHub submission.
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

    async def _next_number(self, project: str) -> int:
        result = await self._mcp.call_tool("chatroom_list_threads", {"project": project})
        items: list[Any] = result.get("items", []) if isinstance(result, dict) else []
        nums: list[int] = []
        for item in items:
            tid = item.get("thread_id") if isinstance(item, dict) else None
            if isinstance(tid, str) and tid.startswith(self._thread_prefix):
                suffix = tid[len(self._thread_prefix) :]
                if suffix.isdigit():
                    nums.append(int(suffix))
        return (max(nums) + 1) if nums else 1


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
