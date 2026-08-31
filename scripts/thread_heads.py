"""Print each chatroom thread's head message id — the conductor sweep's work detector.

Why this exists: the sweep used to launch the conductor once per candidate on every tick just to
discover that nothing had changed. That is cheap for a settled thread (an MCP read, no inference)
but NOT cheap for one whose ``NEXT:`` names a role — the conductor dispatches that role, the role
posts nothing, and the tick has burned an inference for no progress. At a 5-minute cadence that is
288 wasted dispatches a day.

The fix is to decide "has this thread moved?" from data instead of from a timer: the conductor
already reports ``last_msg=msg-NNNN`` per run, and ``chatroom_my_unread`` returns ``latest_msg_id``
for every thread in ONE call without fetching a single message body. Equal ids => the conductor
would resolve the same handoff and reach the same stop, so the launch can be skipped outright.

The identity matters. ``chatroom_my_unread`` is an inbox: it lists threads with unread messages, so
an identity whose read cursor has advanced (Heisenberg's had — measured 2026-08-02: it returned 5
threads and omitted T-track-b-seam-octree-retirement / T-lod0-sliver-shards) silently under-reports.
A dedicated identity that never posts and never marks read keeps every thread unread, hence listed.
Nothing here calls ``chatroom_mark_read``; keep it that way or this probe goes blind.

Output: one JSON object ``{"heads": {thread_id: latest_msg_id}, "count": n}`` on stdout. Callers
MUST treat a thread that is absent from ``heads`` as UNKNOWN and run the conductor anyway — the
inbox's exact filter is not fully characterised (it returned 11 threads where chatroom_list_threads
reported 33 active, omitting the T-pr-review-* family), and failing open costs one cheap run whereas
failing closed would park a live thread forever.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from spirrow_mindwire.magickit.client import MagickitMcpError, StreamableHttpChatroomMcp

# Windows consoles default to legacy codepages (e.g. cp932) that cannot encode
# non-ASCII characters that may appear in the exception message on the stderr
# fail-open path (thread_ids are ASCII, but a wrapped exception message might
# not be). Reconfigure so print() cannot raise. The machine-read JSON on
# stdout is separately hardened by ``ensure_ascii=True`` below (msg-2292 D-3
# — reconfigure alone emits ``\Uxxxxxxxx`` for astral chars, which is not a
# JSON escape). See scripts/dogfood_smoke.py:42-45 for the same pattern.
_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8", errors="backslashreplace")
_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(encoding="utf-8", errors="backslashreplace")

# The probe identity. Never post or mark_read as this name (see module docstring).
DEFAULT_IDENTITY = "conductor-probe"


async def fetch_heads(project: str, identity: str, url: str | None, limit: int) -> dict[str, str]:
    mcp = StreamableHttpChatroomMcp(url)
    payload: Any = await mcp.call_tool(
        "chatroom_my_unread",
        {"project": project, "identity_name": identity, "limit": limit},
    )
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise MagickitMcpError(f"chatroom_my_unread returned no item list: {payload!r}")

    heads: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        thread_id = item.get("thread_id")
        latest = item.get("latest_msg_id")
        # A thread with no head id tells us nothing; omitting it makes the caller fail open.
        if isinstance(thread_id, str) and isinstance(latest, str) and latest:
            heads[thread_id] = latest
    return heads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--identity", default=DEFAULT_IDENTITY)
    parser.add_argument(
        "--url", default=None, help="magickit MCP URL (default: in-code/env default)"
    )
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()

    try:
        heads = asyncio.run(fetch_heads(args.project, args.identity, args.url, args.limit))
    except Exception as exc:  # the caller only needs "probe unusable", not which way it broke
        # stderr + non-zero: the sweep falls back to launching every candidate.
        print(f"thread_heads: probe failed: {exc}", file=sys.stderr)
        return 1

    # Machine-read JSON: emit ASCII-only so the sweep wrapper's
    # ``ConvertFrom-Json`` can decode it under any stdout encoding
    # (msg-2292 D-3).
    print(json.dumps({"heads": heads, "count": len(heads)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
