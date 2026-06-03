"""Fire an independent **design-time** naysayer review of a chatroom thread.

The design-time twin of ``naysayer_review.py`` (ADR-2026-06-03-17 N-1④). Instead
of a PR diff, the input is a **deterministic context bundle** built from a design
thread; the naysayer's contrarian critique is relayed **verbatim** back into the
thread under the independent ``Einstein`` identity.

Pattern (ADR-05 §5 independence): **gather deterministically → independent model
(Gemini) judges → relay verbatim**. This launching session is the *gatherer/relay*,
never the judge — it must not curate the bundle (the gather is mechanical, N-5)
nor edit the verdict it relays.

Pieces, all reused / single-sourced:
* preamble  = ``naysayer.build_preamble()`` — the 5 principles injected verbatim
  from ``spec/NAYSAYER_PRINCIPLES.md`` (D-1), tagged with ``principles_version``;
* bundle    = ``naysayer.build_context_bundle()`` — thread full + extracted
  ADR/doc refs + CLAUDE.md §M ADR index, with an audit manifest (N-5);
* judge     = Lexora ``naysayer`` tier → Gemini (one-shot, no tools — the
  data-governance gate), model tier pinned in ``naysayer.NAYSAYER_MODEL_TIER`` (N-4);
* relay     = post the critique to the thread as ``Einstein`` (advisory, not a
  veto — D-5; final call is Takahito / Tier C).

Transport (D-8): the chatroom MCP is reached over ``StreamableHttpChatroomMcp``
(``MINDWIRE_MAGICKIT_MCP_URL`` — the loop host serves it at ``localhost:8117``).
``:8117`` is **not** hardcoded here; off-host runs override the env var. (A
Claude *session* can instead relay via its in-process connector — that is the
other D-8 concrete path and is not what this standalone subprocess uses.)

Preconditions (fail-loud if missing):
- ``MINDWIRE_MAGICKIT_MCP_URL``   reachable magickit chatroom MCP (gather + post)
- ``MINDWIRE_LEXORA_URL``         Lexora gateway (``naysayer`` tier → Gemini)

Run::

    uv run python scripts/design_review.py --thread T-some-design-thread
    uv run python scripts/design_review.py --thread T-... --decision msg-403
    uv run python scripts/design_review.py --thread T-... --dry-run   # build+print bundle only

⚠️ COST / SIDE EFFECTS (without ``--dry-run``): one real Gemini call (billed) +
a chatroom post under the Einstein identity. Fire deliberately.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from spirrow_mindwire.lexora.client import ChatMessage, LexoraClient
from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp
from spirrow_mindwire.naysayer import (
    NAYSAYER_MODEL_TIER,
    NAYSAYER_UPSTREAM_MODEL,
    build_context_bundle,
    build_preamble,
)

# Gemini (the naysayer tier) is a reasoning model: reasoning tokens count against
# the output budget, so a small cap truncates the visible critique. Match the PR
# adapter / Lexora gateway (16000) — see naysayer_pr_review._DEFAULT_MAX_TOKENS.
_MAX_TOKENS = 16000

# Windows consoles default to legacy codepages (cp932) that can't encode the
# naysayer's reply / em-dashes; emit UTF-8 so print() doesn't raise.
_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8", errors="backslashreplace")


def _relay_body(
    critique: str, *, manifest_header: str, finish_reason: str | None, usage: object
) -> str:
    """Wrap the verbatim critique with a provenance header (judge + manifest)."""
    return (
        f"# [Einstein (独立 naysayer) → design thread] design-time review (relay)\n\n"
        f"> **relay provenance** — judge = `{NAYSAYER_UPSTREAM_MODEL}` "
        f"(Lexora `{NAYSAYER_MODEL_TIER}` tier) / finish_reason=`{finish_reason}` "
        f"/ usage={usage}.\n"
        f"> gatherer/relay = launching session (did **not** judge; critique below is the "
        f"model's verbatim output — no summary/reorder/edit).\n"
        f"> {manifest_header}\n\n"
        f"---\n\n"
        f"{critique}\n"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fire an independent design-time naysayer review of a chatroom thread."
    )
    parser.add_argument(
        "--thread", required=True, help="design thread id (e.g. T-some-design-thread)"
    )
    parser.add_argument(
        "--decision", default=None, help="decision msg id to review (default: whole thread)"
    )
    parser.add_argument("--project", default="spirrow-mindwire", help="chatroom project id")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build + print the bundle only (no Gemini call, no chatroom post)",
    )
    args = parser.parse_args()

    mcp = StreamableHttpChatroomMcp()  # MINDWIRE_MAGICKIT_MCP_URL (D-8: not hardcoded)

    print(f"[design-review] gathering deterministic bundle for {args.thread} ...")
    bundle = await build_context_bundle(
        mcp, project=args.project, thread_id=args.thread, decision_msg_id=args.decision
    )
    print(f"[design-review] {bundle.manifest.as_header()}")

    if args.dry_run:
        print("\n=== BUNDLE (dry-run; not sent) ===\n")
        print(bundle.text)
        return

    preamble = build_preamble()
    print("[design-review] invoking independent naysayer (Gemini — billed) ...")
    async with LexoraClient() as lexora:  # MINDWIRE_LEXORA_URL
        completion = await lexora.chat_completion(
            model=NAYSAYER_MODEL_TIER,
            messages=[
                ChatMessage(role="system", content=preamble),
                ChatMessage(role="user", content=bundle.text),
            ],
            max_tokens=_MAX_TOKENS,
        )
    critique = (completion.content or "").strip()
    if not critique:
        raise SystemExit(
            f"[design-review] naysayer returned an empty critique "
            f"(finish_reason={completion.finish_reason!r}); refusing to post empty."
        )

    body = _relay_body(
        critique,
        manifest_header=bundle.manifest.as_header(),
        finish_reason=completion.finish_reason,
        usage=completion.usage,
    )
    posted = await mcp.call_tool(
        "chatroom_post_message",
        {
            "project": args.project,
            "thread_id": args.thread,
            "msg_type": "report",
            "author": "Einstein",
            "content": body,
            "embodiment": "unknown",
            **({"reply_to": args.decision} if args.decision else {}),
        },
    )
    msg_id = posted.get("msg", {}).get("msg_id") if isinstance(posted, dict) else None
    print(f"[design-review] relayed naysayer critique to {args.thread} as Einstein (msg={msg_id}).")


if __name__ == "__main__":
    asyncio.run(main())
