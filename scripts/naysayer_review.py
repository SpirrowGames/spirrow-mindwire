"""Fire an independent naysayer PR review (Stage 3 Tier B gate) for one PR.

Reuses the production machinery end-to-end — no new review logic here:
``PrReviewOrchestrator`` opens a ``T-pr-review-<n>`` chatroom thread + wires the
naysayer watch, then one poll drives ``NaysayerPrReviewAdapter``, which:

  1. fetches the PR diff from GitHub **raw** (no curation),
  2. runs an independent review through Lexora's ``naysayer`` tier (Gemini —
     plain-text, no tools: the data-governance gate forbids tools on that
     surface), and
  3. posts the critique to the chatroom **and** submits a GitHub PR review
     (APPROVE / REQUEST_CHANGES) as the naysayer identity (spirrowgames-ops).

This is the "**gather raw → independent model judges → relay verbatim**" pattern
(ADR-05 §5 independence): the diff is passed raw; the verdict is the Gemini
naysayer's, not this script's (and not the caller's). The caller must not
curate what the naysayer sees or edit the verdict it returns.

Scope (v1): reviews the PR **diff** only. A richer context bundle (design
threads / ADRs / changed-file context) is the follow-up "bundle builder"
(T-stage3-loop-wiring msg-385 §4 / context-bundle decide msg-370).

Preconditions (env — resolved by the adapter at spawn; fail-loud if missing):
- ``MINDWIRE_MAGICKIT_MCP_URL``      reachable magickit chatroom MCP
  (the host serves it at ``localhost:8117``; over Tailscale only if 0.0.0.0-bound)
- ``MINDWIRE_LEXORA_URL``            Lexora gateway (``naysayer`` tier → Gemini)
- ``MINDWIRE_NAYSAYER_GITHUB_TOKEN`` spirrowgames-ops PAT (PR diff read + review submit)

Run::

    uv run python scripts/naysayer_review.py --pr SpirrowGames/spirrow-mindwire#82

⚠️ COST / SIDE EFFECTS: one real Gemini call (billed) + a real GitHub PR review
submission + a chatroom post. Fire deliberately, once per PR.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from spirrow_mindwire.adapters.naysayer_pr_review import NaysayerPrReviewAdapter
from spirrow_mindwire.dispatcher.core import Dispatcher
from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp
from spirrow_mindwire.magickit.gateway import MagickitChatroomGateway
from spirrow_mindwire.magickit.watcher import ChatroomWatcher
from spirrow_mindwire.orchestrator import PrReviewOrchestrator

# Windows consoles default to legacy codepages (cp932) that can't encode the
# naysayer's reply / em-dashes; emit UTF-8 so print() doesn't raise.
_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8", errors="backslashreplace")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Fire an independent naysayer PR review.")
    parser.add_argument("--pr", required=True, help="PR ref: 'owner/repo#n' or a GitHub PR URL")
    parser.add_argument("--project", default="spirrow-mindwire", help="chatroom project id")
    parser.add_argument(
        "--number",
        type=int,
        default=None,
        help="T-pr-review-<n> suffix (default: one past the highest existing)",
    )
    args = parser.parse_args()

    mcp = StreamableHttpChatroomMcp()  # MINDWIRE_MAGICKIT_MCP_URL or package default
    registry = InMemoryAdapterRegistry()
    # The adapter resolves Lexora + the spirrowgames-ops GitHub token from env.
    registry.register(NaysayerPrReviewAdapter())
    dispatcher = Dispatcher(registry=registry, gateway=MagickitChatroomGateway(mcp))
    watcher = ChatroomWatcher(mcp, dispatcher, [])
    orchestrator = PrReviewOrchestrator(mcp, watcher=watcher)

    print(f"[naysayer-review] opening review thread for {args.pr} ...")
    thread_ref = await orchestrator.fire_pr_review(
        project=args.project, pr_ref=args.pr, number=args.number
    )
    print(
        f"[naysayer-review] thread={thread_ref.thread_id}; "
        f"polling (independent Gemini review — billed) ..."
    )
    try:
        dispatched = await watcher.poll_once()
        print(f"[naysayer-review] dispatched {dispatched} message(s).")

        thread = await mcp.call_tool(
            "chatroom_get_thread",
            {"project": args.project, "thread_id": thread_ref.thread_id, "mode": "full"},
        )
        messages = thread.get("messages", []) if isinstance(thread, dict) else []
        replies = [m for m in messages if str(m.get("author", "")).startswith("naysayer")]
        if replies:
            last = replies[-1]
            print(
                f"\n[naysayer-review] verdict by {last.get('author')} "
                f"({last.get('msg_id')}):\n{last.get('content')}"
            )
        else:
            print(
                "[naysayer-review] no naysayer reply found — check Lexora / GitHub "
                "reachability and the env preconditions."
            )
    finally:
        await watcher.stop()


if __name__ == "__main__":
    asyncio.run(main())
