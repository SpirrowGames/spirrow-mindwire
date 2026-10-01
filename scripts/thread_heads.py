"""Print each chatroom thread's head message id — the conductor sweep's work detector.

Why this exists: the sweep used to launch the conductor once per candidate on every tick just to
discover that nothing had changed. That is cheap for a settled thread (an MCP read, no inference)
but NOT cheap for one whose ``NEXT:`` names a role — the conductor dispatches that role, the role
posts nothing, and the tick has burned an inference for no progress. Repeated every tick, that is
(24h / tick interval) wasted dispatches a day.

The fix is to decide "has this thread moved?" from data instead of from a timer: the conductor
already reports ``last_msg=msg-NNNN`` per run, and ``chatroom_list_threads`` returns ``last_msg_id``
for every thread in ONE call without fetching a single message body. Equal ids => the conductor
would resolve the same handoff and reach the same stop, so the launch can be skipped outright.

Why the listing and not the inbox (T-unread-correlated-count-scale, 2026-09-30). This probe used
to call ``chatroom_my_unread`` as a never-reads identity (``conductor-probe``) so that every thread
stayed unread and hence listed. Two things were wrong with that:

* **cost** — the inbox ranks and filters by a per-thread unread count that conclair evaluates for
  EVERY thread of the project, and a cursorless identity is its worst case (every message of every
  thread is counted). The probe then threw the count away: it only ever read the head id. Measured
  in conclair's perf harness at 100x today's data (300k msgs / 5k threads, CI run 35128633398):
  ``/unread`` 657 ms vs ``/threads`` 15 ms on the same rows. The probe runs every tick for every
  project, so it was the heaviest caller of that path.
* **coverage** — an inbox lists only threads with unread messages, so its completeness hung on a
  read cursor never advancing, and its exclusion rule was never fully characterised (once it
  returned 11 threads where ``chatroom_list_threads`` reported 33 active).

The listing carries the same value — magickit documents ``last_msg_id`` as "the same value the inbox
reports as ``latest_msg_id``" — and was checked live before the switch: for all six projects the
two calls returned the identical thread set and identical head ids (216/216). The status filter
below is every status except ``resolved``, i.e. exactly the inbox's ``include_resolved=false``.

Paging: one call at ``limit=1000``, as before (the largest project has ~90 open threads). A project
beyond that is not silently wrong — the threads past the page are simply absent, and absent means
UNKNOWN (below), so they cost a conductor run each rather than being parked.

Output: one JSON object ``{"heads": {thread_id: latest_msg_id}, "count": n}`` on stdout. Callers
MUST treat a thread that is absent from ``heads`` as UNKNOWN and run the conductor anyway —
failing open costs one cheap run whereas failing closed would park a live thread forever.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from spirrow_mindwire.magickit.client import MagickitMcpError, StreamableHttpChatroomMcp

# Windows stdout defaults to a legacy codepage (e.g. cp932). The machine-read
# JSON on stdout uses ``ensure_ascii=True`` below, so stdout is guaranteed
# ASCII-only — no reconfiguration needed, and any reconfiguration to UTF-8
# would in fact corrupt the operator's console by writing raw UTF-8 bytes
# into a cp932 read path (PR #210 gate round-2).
#
# stderr is a separate concern: thread_ids are ASCII but the fail-open ``print``
# in ``main()`` wraps a caught exception, and that message may carry native text.
# Setting ``errors="backslashreplace"`` — WITHOUT overriding the encoding —
# makes stderr incapable of raising while preserving native console
# readability. The ``getattr`` guard exists because ``reconfigure`` is a
# ``TextIOWrapper`` method; see ``scripts/dogfood_smoke.py`` for the same
# probe convention.
_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(errors="backslashreplace")

# Every thread status except ``resolved`` — the inbox's ``include_resolved=false`` set, which
# is what this probe reported before it moved to the listing. magickit's status vocabulary:
# active / awaiting_reply / resolved / superseded / parked.
OPEN_STATUSES = ("active", "awaiting_reply", "parked", "superseded")


async def fetch_heads(project: str, url: str | None, limit: int) -> dict[str, str]:
    mcp = StreamableHttpChatroomMcp(url)
    payload: Any = await mcp.call_tool(
        "chatroom_list_threads",
        {"project": project, "status_filter": list(OPEN_STATUSES), "limit": limit},
    )
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise MagickitMcpError(f"chatroom_list_threads returned no item list: {payload!r}")

    heads: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        thread_id = item.get("thread_id")
        latest = item.get("last_msg_id")
        # A thread with no head id (e.g. no messages) tells us nothing; omitting it makes the
        # caller fail open.
        if isinstance(thread_id, str) and isinstance(latest, str) and latest:
            heads[thread_id] = latest
    return heads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--url", default=None, help="magickit MCP URL (default: in-code/env default)"
    )
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()

    try:
        heads = asyncio.run(fetch_heads(args.project, args.url, args.limit))
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
