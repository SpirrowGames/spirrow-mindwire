"""Fire an independent naysayer PR review (Stage 3 Tier B gate) for one PR.

Reuses the production machinery end-to-end — no new review logic here: it builds the
:class:`~spirrow_mindwire.naysayer.pr_review.NaysayerPrReviewDriver` and drives it through
:class:`~spirrow_mindwire.orchestrator.PrReviewOrchestrator`, which opens a
``T-pr-review-<repo>-<n>`` chatroom thread and then:

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

    uv run python scripts/naysayer_review.py --pr SpirrowGames/spirrow-mindwire#82 \
        --design-thread T-the-thread-this-gate-was-fired-from

``--design-thread`` is required and is NOT the ``T-pr-review-<repo>-<n>`` ledger id this script
prints — the ledger holds the critique, the design thread is where the verdict must arrive for
the loop to move. Exit code 2 = the review ran but the relay did not land.

Preflight (T-gate-firing-ignores-loop-state-and-prior-verdicts §A-1, policy tier —
this script does not enforce it; the operator does):

    手発火の前に、``--design-thread`` に指定するスレッドの最新 ``NEXT:`` を目視で確認せよ。
    role persona (Heisenberg / Bohr / Einstein 等) を指している場合、その head は fix loop
    の中 = 未確定であり、gate に読ませても空撃ちになる (R-A-1, msg-456)。``NEXT: pr-review`` /
    ``NEXT: human`` / ``NEXT: none`` のいずれかに揃ってから撃つ。

⚠️ COST / SIDE EFFECTS: one real Gemini call (billed) + a real GitHub PR review submission + a
chatroom post. Fire deliberately, once per PR.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from spirrow_mindwire.conductor.roster import RoleResolutionError, derive_identity_by_role
from spirrow_mindwire.config import load_settings
from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp
from spirrow_mindwire.naysayer.pr_review import NaysayerPrReviewDriver
from spirrow_mindwire.orchestrator import PrReviewOrchestrator
from spirrow_mindwire.value_objects import Role

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
        "--design-thread",
        required=True,
        help="design thread the verdict is relayed into (NOT the T-pr-review-<repo>-<n> ledger)",
    )
    args = parser.parse_args()

    # Resolve the implementer persona from the SAME roster the conductor uses at runtime
    # (T-hand-fired-gate-cannot-name-the-implementer msg-3885 D-1). Before this landed, the
    # manual driver silently omitted ``implementer=`` and every REQUEST_CHANGES relay routed
    # to ``NEXT: human`` — the design thread parked with no dispatch. Fail-loud (exit 3): the
    # roster is the daemon's [conductor].roster SOT, an operator is at the terminal, so a
    # misconfigured roster prints the exact miswiring and stops before the gate is billed.
    # ``Role.IMPLEMENTER`` is the same role the daemon's own ``Conductor(...)`` construction
    # uses in production (``loop_runner.py`` never overrides the ctor default — msg-3887); the
    # ``implementer_role`` ctor arg is a testing seam without a config surface, and passing the
    # literal here is what keeps the two lanes byte-identical rather than routing through a
    # settings field that does not exist.
    settings = load_settings()
    try:
        implementer = derive_identity_by_role(settings.conductor.roster, Role.IMPLEMENTER)
    except RoleResolutionError as exc:
        print(f"[naysayer-review] {exc}", file=sys.stderr)
        raise SystemExit(3) from exc

    mcp = StreamableHttpChatroomMcp()  # MINDWIRE_MAGICKIT_MCP_URL or package default
    # The driver resolves Lexora + the spirrowgames-ops GitHub token from env.
    driver = NaysayerPrReviewDriver()
    orchestrator = PrReviewOrchestrator(mcp, driver=driver)

    print(f"[naysayer-review] opening review thread for {args.pr} (Gemini review, billed) ...")
    try:
        thread_ref, outcome, relay = await orchestrator.fire_pr_review(
            project=args.project,
            pr_ref=args.pr,
            design_thread=args.design_thread,
            implementer=implementer,
        )
        # Both destinations are named: printing only the ledger id is what supplied the one
        # wrong answer an operator could reach for when asked for a design thread (msg-2765 §2).
        print(
            f"[naysayer-review] thread={thread_ref.thread_id}  "
            f"design_thread={args.design_thread}  relay={relay['msg_id'] or 'DROPPED'}  "
            f"verdict={outcome.verdict.value}  "
            f"ci={outcome.ci_state.value}  head={outcome.head_sha}"
        )
        print(f"\n[naysayer-review] critique:\n{outcome.body}")
        if not relay["msg_id"]:
            # D-5: the conductor already fails safe to the human on an empty relay id; this
            # path had none, so a dropped relay ended in a green shell.
            print(
                f"[naysayer-review] relay DROPPED — the verdict is in {thread_ref.thread_id} "
                f"but never reached {args.design_thread}; record it there by hand",
                file=sys.stderr,
            )
            raise SystemExit(2)
    finally:
        await driver.aclose()


if __name__ == "__main__":
    asyncio.run(main())
