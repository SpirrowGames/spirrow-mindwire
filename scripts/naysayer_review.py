"""Fire an independent naysayer PR review (Stage 3 Tier B gate) for one PR.

Reuses the production machinery end-to-end — no new review logic here: it builds the
:class:`~spirrow_mindwire.naysayer.pr_review.NaysayerPrReviewDriver` and drives it through
:class:`~spirrow_mindwire.orchestrator.PrReviewOrchestrator`, which opens a ``T-pr-review-<n>``
chatroom thread and then:

  1. runs the deterministic CI-gate (ADR-16) — fail-closed when CI is not green,
  2. (if CI green) fetches the PR diff from GitHub **raw** (no curation) and has Lexora's
     ``naysayer`` tier (Gemini) judge it in one shot, with the 5-principles SOT injected,
  3. posts the critique to the chatroom **and** submits a GitHub PR review (APPROVE /
     REQUEST_CHANGES) as the naysayer identity (spirrowgames-ops).

This is the "**gather raw → independent model judges → relay verbatim**" pattern (ADR-05 §5
independence): the diff is passed raw; the verdict is the Gemini naysayer's, not this script's
(and not the caller's). The caller must not curate what the naysayer sees or edit the verdict.

ADR-2026-06-04-19 (driver-化 unify): the PR-gate is a *driver* invoked directly by the
orchestrator — no watcher/dispatch round-trip (the registry's sole ``NAYSAYER_QUALIFIED`` adapter
is the design-time ``NaysayerSdkAdapter``).

Scope (v1): reviews the PR **diff** only.

Preconditions (env — resolved at construction; fail-loud if missing):
- ``MINDWIRE_MAGICKIT_MCP_URL``      reachable magickit chatroom MCP
- ``MINDWIRE_LEXORA_URL``            Lexora gateway (``naysayer`` tier → Gemini)
- ``MINDWIRE_NAYSAYER_GITHUB_TOKEN`` spirrowgames-ops PAT (PR diff read + review submit)

Run::

    uv run python scripts/naysayer_review.py --pr SpirrowGames/spirrow-mindwire#82

⚠️ COST / SIDE EFFECTS: one real Gemini call (billed) + a real GitHub PR review submission + a
chatroom post. Fire deliberately, once per PR.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp
from spirrow_mindwire.naysayer.pr_review import NaysayerPrReviewDriver
from spirrow_mindwire.orchestrator import PrReviewOrchestrator

# Windows consoles default to legacy codepages (cp932) that can't encode the naysayer's reply /
# em-dashes; emit UTF-8 so print() doesn't raise.
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
    # The driver resolves Lexora + the spirrowgames-ops GitHub token from env.
    driver = NaysayerPrReviewDriver()
    orchestrator = PrReviewOrchestrator(mcp, driver=driver)

    print(f"[naysayer-review] opening review thread for {args.pr} (Gemini review, billed) ...")
    try:
        thread_ref, outcome = await orchestrator.fire_pr_review(
            project=args.project, pr_ref=args.pr, number=args.number
        )
        print(
            f"[naysayer-review] thread={thread_ref.thread_id}  verdict={outcome.verdict.value}  "
            f"ci={outcome.ci_state.value}  head={outcome.head_sha}"
        )
        print(f"\n[naysayer-review] critique:\n{outcome.body}")
    finally:
        await driver.aclose()


if __name__ == "__main__":
    asyncio.run(main())
