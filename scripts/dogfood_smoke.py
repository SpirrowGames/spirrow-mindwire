"""Dogfood driver — run the §8 round-trip against the live magickit chatroom.

Wires the real Phase 1 runtime (StreamableHttpChatroomMcp → Dispatcher →
ClaudeCodeSdkAdapter → MagickitChatroomGateway) and polls one watched thread
once: any unseen non-proposer message is answered by the proposer via a real
Claude Agent SDK ``query()``.

⚠️ COST: every unseen non-proposer message triggers one real Claude query
(billed to the dev PC's Claude auth). Run against a **fresh thread containing
only the question(s) you want answered** so cost is controlled (1 question →
1 query). The watcher has no "baseline on start" yet (it would otherwise reply
to the whole thread history) — that gap is itself a dogfood finding.

Prereqs:
- dev PC reachable to the magickit MCP (``MINDWIRE_MAGICKIT_MCP_URL`` env, else
  the package default); verify first with the read-only connectivity smoke:
  ``uv run --extra dev pytest tests/test_magickit_smoke.py -m manual``.
- Claude Agent SDK authenticated on the dev PC (``claude`` runs in a terminal).

Run::

    MINDWIRE_SMOKE_THREAD_ID=T-dogfood-xxx uv run python scripts/dogfood_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from spirrow_mindwire.adapters.claude_code_sdk import ClaudeCodeSdkAdapter
from spirrow_mindwire.dispatcher.core import Dispatcher
from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp
from spirrow_mindwire.magickit.gateway import MagickitChatroomGateway
from spirrow_mindwire.magickit.watcher import ChatroomWatcher, WatchSpec
from spirrow_mindwire.value_objects import Role, ThreadRef

# Windows consoles default to legacy codepages (e.g. cp932) that can't encode
# the proposer's reply or em-dashes; emit UTF-8 so print() doesn't raise
# UnicodeEncodeError. getattr keeps mypy happy (reconfigure is TextIOWrapper-only).
_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8", errors="backslashreplace")


async def main() -> None:
    project = os.environ.get("MINDWIRE_SMOKE_PROJECT", "spirrow-mindwire")
    thread_id = os.environ.get("MINDWIRE_SMOKE_THREAD_ID")
    if not thread_id:
        raise SystemExit("set MINDWIRE_SMOKE_THREAD_ID to the thread to watch")

    mcp = StreamableHttpChatroomMcp()  # MINDWIRE_MAGICKIT_MCP_URL or package default
    thread_ref = ThreadRef(
        project_id=project,
        thread_id=thread_id,
        chatroom_uri=f"magickit://{project}/{thread_id}",
    )
    registry = InMemoryAdapterRegistry()
    registry.register(ClaudeCodeSdkAdapter(cwd=Path.cwd()))  # real Claude Agent SDK
    dispatcher = Dispatcher(registry=registry, gateway=MagickitChatroomGateway(mcp))
    watcher = ChatroomWatcher(mcp, dispatcher, [WatchSpec(thread_ref, Role.PROPOSER)])

    print(f"[dogfood] watching {project}/{thread_id} as proposer (real Claude, billed)...")
    # baseline=False: the dogfood intentionally answers the question already in
    # the thread (production watchers use the default baseline=True).
    await watcher.start(baseline=False)
    try:
        dispatched = await watcher.poll_once()
        print(f"[dogfood] dispatched {dispatched} message(s).")

        thread = await mcp.call_tool(
            "chatroom_get_thread",
            {"project": project, "thread_id": thread_id, "mode": "full"},
        )
        messages = thread.get("messages", []) if isinstance(thread, dict) else []
        replies = [m for m in messages if m.get("author") == Role.PROPOSER.value]
        if replies:
            last = replies[-1]
            print(f"\n[dogfood] proposer reply ({last.get('msg_id')}):\n{last.get('content')}")
        else:
            print("[dogfood] no author=proposer message found in the thread yet.")
    finally:
        await watcher.stop()  # graceful halt -> SDK disconnect (no unclosed transport)


if __name__ == "__main__":
    asyncio.run(main())
