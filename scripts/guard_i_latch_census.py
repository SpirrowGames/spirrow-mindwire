"""Read-only census: which open threads are latched (or already stopped) by guard (i)'s G1.

Thread ``T-guard-i-latches-on-discharged-tierc-line`` (operator msg-7182 §2 (c), Bohr msg-7202 §2
(c) / msg-7204 "Step 0"). spirrow-lexora ``T-streaming-ledger-row`` msg-880 sat ~18 h on the human
because G1 was latched by ``TIER-C:`` lines whose decision had already been given outside the
chatroom. This lists every open thread in the named projects that is in the same state, so each
can be unlatched (a human post, or a recorder's ``TIER-C-RESOLVED:`` line) instead of being found
one stop at a time.

It changes nothing. It reads ``chatroom_list_threads`` and ``chatroom_get_thread`` only, and it
applies the conductor's OWN rules by import — :func:`~spirrow_mindwire.conductor.tier_c_segment.
scan_tier_c_segment` (G1), :func:`~spirrow_mindwire.routing.diagnose_carve_out_iii` (the closed
conditions, in the guard's order), :func:`~spirrow_mindwire.conductor.handoff.declares_no_tier_c`
(G2) and :func:`~spirrow_mindwire.conductor.handoff.resolve_handoff` — so it cannot drift from what
the conductor would decide.

Usage::

    uv run python scripts/guard_i_latch_census.py \\
        --project spirrow-lexora --project spirrow-mindwire --naysayer Einstein \\
        [--human-identity human] [--recorder operator --recorder Fermi]

    # point-in-time check of one head (the msg-880 confirmation, msg-7204 Step 0):
    uv run python scripts/guard_i_latch_census.py --project spirrow-lexora \\
        --thread T-streaming-ledger-row --until msg-880 --naysayer Einstein

``--recorder`` should mirror the deployed ``[conductor].tier_c_recorders`` (default: none, which is
the pre-change G1). ``--until`` truncates the thread after that message, so the row describes the
state the conductor saw when that message was the head.

Output (stdout, one ASCII JSON object)::

    {"rows": [...], "errors": [...], "counts": {"polled": n, "latched": n, "stopped": n}}

One row per thread that is ``latched`` (G1 closed now, so the next naysayer proceed will be
redirected) or ``stopped`` (the head is a guard-(i) relay, or the head parks on ``NEXT: human``
with a guard-(i) relay since the human last spoke); ``--all`` emits every polled thread. A row
carries the head, the last human message, every open declaration, ``unmatched`` / ``malformed`` /
``ignored_non_recorder`` resolution lines, and ``head_closed`` — the offline conditions that close
carve-out ③ for the head read as a naysayer proceed (``first_closed`` is the one the guard's
short-circuit stops at). ``control_state`` is NOT read: ``not_run`` never appears, and a reader
must check loop control separately. G3 (the Decider) is never called.

Fail direction: a thread that cannot be read is an ``errors`` row, never a silent "not latched".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Collection, Sequence
from typing import Any

from spirrow_mindwire.conductor.core import CONDUCTOR_RELAY_AUTHOR, GUARD_I_REDIRECT_TITLE
from spirrow_mindwire.conductor.handoff import HandoffKind, declares_no_tier_c, resolve_handoff
from spirrow_mindwire.conductor.human_identity import is_human_identity
from spirrow_mindwire.conductor.tier_c_segment import scan_tier_c_segment
from spirrow_mindwire.magickit.client import MagickitMcpError, StreamableHttpChatroomMcp
from spirrow_mindwire.routing import diagnose_carve_out_iii
from spirrow_mindwire.source_marker import parse_attestation_marker

_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(errors="backslashreplace")

# Same open-status set as ``scripts/thread_heads.py`` (everything but ``resolved``).
OPEN_STATUSES = ("active", "awaiting_reply", "parked", "superseded")


def _is_guard_i_relay(msg: dict[str, Any]) -> bool:
    return str(msg.get("author") or "") == CONDUCTOR_RELAY_AUTHOR and str(
        msg.get("content") or ""
    ).lstrip().startswith(GUARD_I_REDIRECT_TITLE)


def _attested(body: str) -> bool:
    """The conductor's ``_attested`` rule: a well-formed stamp whose backend equals expected."""
    record = parse_attestation_marker(body)
    return record is not None and record.backend == record.expected


def census_thread(
    thread_id: str,
    messages: Sequence[dict[str, Any]],
    *,
    is_human: Callable[[str], bool],
    recorders: Collection[str],
    naysayer: str,
    until: str | None = None,
) -> dict[str, Any] | None:
    """The census row for one thread, or ``None`` when it has no messages (or no ``until``)."""
    msgs = [m for m in messages if isinstance(m, dict)]
    if until is not None:
        ids = [str(m.get("msg_id") or "") for m in msgs]
        if until not in ids:
            return None
        msgs = msgs[: ids.index(until) + 1]
    if not msgs:
        return None
    head = msgs[-1]
    head_id = str(head.get("msg_id") or "")
    head_author = str(head.get("author") or "")
    head_body = str(head.get("content") or "")
    scan = scan_tier_c_segment(msgs, is_human=is_human, recorders=recorders)
    # Guard-(i) relays since the human last spoke (the boundary the scan stopped at).
    relays: list[str] = []
    for msg in reversed(msgs):
        if is_human(str(msg.get("author") or "")):
            break
        if _is_guard_i_relay(msg):
            relays.append(str(msg.get("msg_id") or ""))
    relays.reverse()
    handoff = resolve_handoff(head_body, {})
    stopped = _is_guard_i_relay(head) or (handoff.kind is HandoffKind.HUMAN and bool(relays))
    head_is_naysayer = bool(naysayer) and head_author.casefold() == naysayer.casefold()
    closed = diagnose_carve_out_iii(
        head_msg_id=head_id,
        # Read the head as a naysayer proceed: the question is "would the NEXT proceed pass?".
        author_is_naysayer=True,
        # Loop control is not read here (see the module docstring).
        control_state_is_run=True,
        message_is_attested=lambda: _attested(head_body),
        open_tier_c_declarations=lambda: scan.open_declarations,
        naysayer_declared_no_tier_c=lambda: declares_no_tier_c(head_body),
    )
    return {
        "thread_id": thread_id,
        "head_msg_id": head_id,
        "head_author": head_author,
        "head_is_naysayer": head_is_naysayer,
        "head_next": handoff.token or "",
        "latched": scan.declares_tier_c,
        "stopped": stopped,
        "last_human_msg_id": scan.boundary_msg_id,
        "open_declarations": list(scan.open_declarations),
        "resolved_declarations": list(scan.resolved_declarations),
        "unmatched": list(scan.unmatched),
        "malformed": list(scan.malformed),
        "ignored_non_recorder": list(scan.ignored_non_recorder),
        "guard_i_relays_since_human": relays,
        "head_closed": [
            {"condition": c.condition.value, "evidence": list(c.evidence_ids)} for c in closed
        ],
        "first_closed": closed[0].condition.value if closed else None,
    }


async def _list_threads(mcp: Any, project: str, limit: int) -> list[str]:
    payload: Any = await mcp.call_tool(
        "chatroom_list_threads",
        {"project": project, "status_filter": list(OPEN_STATUSES), "limit": limit},
    )
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise MagickitMcpError(f"chatroom_list_threads returned no item list: {payload!r}")
    return [
        str(item["thread_id"])
        for item in items
        if isinstance(item, dict) and isinstance(item.get("thread_id"), str)
    ]


async def run_census(
    mcp: Any,
    *,
    projects: Sequence[str],
    threads: Sequence[str],
    is_human: Callable[[str], bool],
    recorders: Collection[str],
    naysayer: str,
    until: str | None,
    emit_all: bool,
    limit: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    polled = 0
    for project in projects:
        try:
            thread_ids = list(threads) or await _list_threads(mcp, project, limit)
        except Exception as exc:
            errors.append({"project": project, "thread_id": "", "reason": f"list failed: {exc}"})
            continue
        for thread_id in thread_ids:
            polled += 1
            try:
                result: Any = await mcp.call_tool(
                    "chatroom_get_thread",
                    {"project": project, "thread_id": thread_id, "mode": "full"},
                )
            except Exception as exc:
                errors.append(
                    {"project": project, "thread_id": thread_id, "reason": f"read failed: {exc}"}
                )
                continue
            messages = result.get("messages") if isinstance(result, dict) else None
            if not isinstance(messages, list):
                errors.append(
                    {"project": project, "thread_id": thread_id, "reason": "malformed response"}
                )
                continue
            row = census_thread(
                thread_id,
                messages,
                is_human=is_human,
                recorders=recorders,
                naysayer=naysayer,
                until=until,
            )
            if row is None:
                reason = f"{until} not in thread" if until else "no messages"
                errors.append({"project": project, "thread_id": thread_id, "reason": reason})
                continue
            if emit_all or row["latched"] or row["stopped"]:
                rows.append({"project": project, **row})
    return {
        "rows": rows,
        "errors": errors,
        "counts": {
            "polled": polled,
            "latched": sum(1 for r in rows if r["latched"]),
            "stopped": sum(1 for r in rows if r["stopped"]),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", action="append", required=True)
    parser.add_argument("--thread", action="append", default=[], help="limit to these threads")
    parser.add_argument("--until", default=None, help="truncate each thread after this msg id")
    parser.add_argument("--naysayer", required=True, help="the roster naysayer persona")
    parser.add_argument("--human-identity", default="human")
    parser.add_argument("--recorder", action="append", default=[], help="tier_c_recorders entry")
    parser.add_argument("--all", action="store_true", help="emit every polled thread")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--url", default=None, help="magickit MCP URL (default: in-code/env)")
    args = parser.parse_args(argv)

    human = args.human_identity

    def is_human(author: str) -> bool:
        return is_human_identity(author, human)

    try:
        result = asyncio.run(
            run_census(
                StreamableHttpChatroomMcp(args.url),
                projects=args.project,
                threads=args.thread,
                is_human=is_human,
                recorders=tuple(args.recorder),
                naysayer=args.naysayer,
                until=args.until,
                emit_all=args.all,
                limit=args.limit,
            )
        )
    except Exception as exc:
        print(f"guard_i_latch_census: failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
