"""PR-review orchestrator — WIRING_ALLOWLIST_SPEC §A.2 (T20).

Event-driven trigger for the naysayer's Tier B gate (ADR-07 §2.2): when a
develop→main PR is opened, :meth:`PrReviewOrchestrator.fire_pr_review` opens a
``T-pr-review-<n>`` chatroom thread carrying the PR ref and registers a naysayer
watch, so the **existing** Stage-1 :class:`~spirrow_mindwire.magickit.watcher.ChatroomWatcher`
dispatches the PR-review naysayer adapter on its next poll.

No webhook (inconsistent with the Tailscale-only egress) and no extra poller
(the reused watcher loop is the only daemon). The trigger is invoked by whoever
opens the develop→main PR (the main chain / implementer's git mechanism); a
**manual** call to :meth:`fire_pr_review` is the documented fallback (§A.2).
"""

from __future__ import annotations

from typing import Any

from .magickit.client import McpToolCaller
from .magickit.watcher import ChatroomWatcher, WatchSpec
from .value_objects import Role, ThreadRef

_DEFAULT_THREAD_PREFIX = "T-pr-review-"
_DEFAULT_OWNER = "orchestrator"


class PrReviewOrchestrator:
    """Opens a ``T-pr-review-<n>`` thread + wires the naysayer watch (§A.2)."""

    def __init__(
        self,
        mcp: McpToolCaller,
        *,
        watcher: ChatroomWatcher | None = None,
        owner: str = _DEFAULT_OWNER,
        thread_prefix: str = _DEFAULT_THREAD_PREFIX,
    ) -> None:
        self._mcp = mcp
        self._watcher = watcher
        self._owner = owner
        self._thread_prefix = thread_prefix

    async def fire_pr_review(
        self,
        *,
        project: str,
        pr_ref: str,
        number: int | None = None,
        title: str | None = None,
    ) -> ThreadRef:
        """Open the review thread for a develop→main PR and wire the naysayer.

        ``number`` is the ``<n>`` suffix; if omitted it is derived as one past
        the highest existing ``T-pr-review-<n>`` thread in the project. Returns
        the new thread's :class:`ThreadRef`.
        """
        n = number if number is not None else await self._next_number(project)
        thread_id = f"{self._thread_prefix}{n}"
        title = title or f"PR review (develop→main) — {pr_ref}"
        propose = (
            f"naysayer review request — develop→main PR {pr_ref}\n\n"
            f"Independent naysayer: fetch the PR diff, review it, post your critique "
            f"in this thread, and submit a GitHub PR review (Approve / Request-changes). "
            f"End with `VERDICT: APPROVE` or `VERDICT: REQUEST_CHANGES`. "
            f"An objection sends the change back to the proposer↔implementer fix loop; "
            f"an approve is the necessary condition for Takahito's merge GO (Tier C)."
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
        if self._watcher is not None:
            # baseline=False: dispatch the just-posted review-request to the
            # naysayer immediately (don't treat it as ignored backlog).
            await self._watcher.add_watch(
                WatchSpec(thread_ref=thread_ref, role=Role.NAYSAYER), baseline=False
            )
        return thread_ref

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


__all__ = ["PrReviewOrchestrator"]
