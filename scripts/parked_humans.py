"""Poll a sweep candidate set for threads currently parked on a human decision.

Owns the parking judgment on the ``mindwire`` side of the T-decision-request-composer wiring
(msg-1370 §0 defect 2 / §4 for the digest role; msg-1391 §13.2 / §13.3 for the D-32 grammar-
ownership rule this file exists to satisfy). A previous revision of S4 (commit ``31a0373``, since
reverted in ``d4cbf02``) tried to derive parking from ``notified.json`` — a *derived* record of
"did we notify?", not a judgment about the thread — and Einstein's msg-1389 correctly rejected it
as a D-24' violation. Takahito's msg-1391 §13.2 then reframed the rule: the parking judgment IS
mindwire's, because the ``NEXT:`` grammar it depends on is owned here
(:mod:`spirrow_mindwire.conductor.handoff`), pinned against real chatroom traffic in
``tests/data/next_line_corpus.tsv``, and its own module docstring warns:

    "A second spelling also silently withholds whatever the owner learns later."

∴ the parking answer must be produced by re-using the grammar owner, not by re-spelling it on
the PowerShell side. This CLI is the one permitted re-use point: the sweep wrapper
(``deploy/run-conductor-scheduled.ps1``) calls this, receives a list of parked threads, and
feeds them into ``New-DailyDigest``'s "判断待ち: N 件" section. The PS side never parses
``NEXT:`` lines itself.

Contract with the wrapper:

    Input (stdin or ``--input <path>``, JSON, one project per call — mirroring
    ``thread_heads.py``'s per-project shape):

        {
          "candidates": [
            {"thread_id": "T-a", "head_msg_id": "msg-2242"},
            {"thread_id": "T-b", "head_msg_id": "msg-1919"}
          ]
        }

    ``head_msg_id`` may be an empty string when the head probe did not report the thread
    (see ``thread_heads.py``'s fail-open note). In that case the cross-check below is
    skipped and the fetched thread's own last-message id is trusted.

    Output (stdout, JSON):

        {
          "project": "spirrow-voxelworld",
          "polled": 2,
          "parked": [
            {"thread_id": "T-a", "head_msg_id": "msg-2242", "token": "human"}
          ],
          "errors": [
            {"thread_id": "T-c", "reason": "chatroom_get_thread failed: ..."}
          ]
        }

    ``token`` is the raw participant token the parser recognised (case preserved from the
    author's ``NEXT:`` line for observability); membership in ``parked`` is decided from
    :attr:`HandoffKind.HUMAN`, which is the case-insensitive reserved sentinel. Non-human
    handoffs (a role, ``none``, absent, ``pr-review``) simply do not appear in ``parked``.

Fail direction — the opposite of head_skip's cache, on purpose. head_skip fails OPEN into a
launch (one cheap MCP call beats a silent park). This poll fails CLOSED on the parked side:

  * a fetch failure  → recorded in ``errors`` (visible in the digest via the wrapper's log line)
                       AND excluded from ``parked`` — we cannot claim parking on unreadable data
  * a head that moved between the probe and this call → silently excluded
                       (a naysayer may have posted between ticks; the next tick re-establishes)
  * a body with no ``NEXT:`` line, or a ``NEXT:`` that resolves to something other than the
                       reserved ``human`` sentinel → simply not parked

A false negative here costs one digest row; a false positive would put the wrong count in front
of the operator. This is not a hedge — different question from head_skip, different safe
answer (msg-1391 §13.3 explicitly).

Roster: an EMPTY roster is passed to :func:`resolve_handoff` on purpose. The ``human`` sentinel
resolves BEFORE the roster lookup in that function (see its resolution-order docstring), so an
empty roster does not affect the answer we care about — and it removes the temptation to keep
a roster copy on this side of the wire. When a future change makes the roster load-bearing here
(e.g. wanting to distinguish "handoff to a live role" from "handoff to a stale persona"), fix it
at that time; do not smuggle a roster in preemptively.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from spirrow_mindwire.conductor.handoff import HandoffKind, resolve_handoff
from spirrow_mindwire.magickit.client import MagickitMcpError, StreamableHttpChatroomMcp


async def _fetch_last_message(
    mcp: StreamableHttpChatroomMcp, project: str, thread_id: str
) -> tuple[str, str] | None:
    """Return ``(last_msg_id, last_body)`` for a thread, or ``None`` when the shape is unusable.

    ``chatroom_get_thread`` with ``mode="full"`` is what ``head_skip_decide.py`` also uses; keeping
    the two on the same tool avoids a per-tick divergence in what "the thread" means. Errors from
    the tool itself are RAISED (:class:`MagickitMcpError`) so the caller can record the reason
    verbatim in the ``errors`` list — swallowing them here would collapse two very different
    failure modes ("tool broke" vs. "tool returned an empty thread") into an indistinguishable
    ``None``.
    """
    result: Any = await mcp.call_tool(
        "chatroom_get_thread",
        {"project": project, "thread_id": thread_id, "mode": "full"},
    )
    if not isinstance(result, dict):
        return None
    messages = result.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    last = messages[-1]
    if not isinstance(last, dict):
        return None
    msg_id = str(last.get("msg_id") or "")
    body = str(last.get("content") or "")
    return (msg_id, body) if msg_id else None


async def _poll(project: str, candidates: list[dict[str, Any]], url: str | None) -> dict[str, Any]:
    """Iterate the candidates and classify each as parked-on-human / not / error.

    Order-preserving: ``parked`` and ``errors`` come out in the input candidate order, so a
    downstream digest row is stable across ticks when the candidate list has not been reordered.
    """
    mcp = StreamableHttpChatroomMcp(url)
    parked: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for cand in candidates:
        thread_id = str(cand.get("thread_id") or "")
        if not thread_id:
            # Malformed candidate — skip silently, same rule as head_skip_decide.
            continue
        expected_head = str(cand.get("head_msg_id") or "")
        try:
            fetched = await _fetch_last_message(mcp, project, thread_id)
        except MagickitMcpError as exc:
            errors.append({"thread_id": thread_id, "reason": f"chatroom_get_thread failed: {exc}"})
            continue
        except Exception as exc:
            # A backend that raised something outside MagickitMcpError is a bug in the client;
            # record it in errors so the wrapper's log still shows the outage without crashing
            # the entire poll (a single misbehaving thread must not blank the whole digest).
            errors.append(
                {
                    "thread_id": thread_id,
                    "reason": f"unexpected {type(exc).__name__}: {exc}",
                }
            )
            continue
        if fetched is None:
            errors.append({"thread_id": thread_id, "reason": "no messages / malformed response"})
            continue
        actual_msg_id, body = fetched
        # Head cross-check. When the probe reported a head, it must still match the fetched
        # thread's actual last-message id; a drift means either the head moved between the
        # probe and this call (a live thread we cannot claim parking on) or the probe was
        # stale (same conclusion). No error, no parked entry — silently excluded, next tick
        # tries again.
        if expected_head and actual_msg_id != expected_head:
            continue
        handoff = resolve_handoff(body, {})
        if handoff.kind is HandoffKind.HUMAN:
            parked.append(
                {
                    "thread_id": thread_id,
                    "head_msg_id": actual_msg_id,
                    "token": handoff.token or "",
                }
            )
    return {
        "project": project,
        "polled": len(candidates),
        "parked": parked,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--input",
        default="-",
        help="path to the candidates JSON payload, or '-' for stdin (default)",
    )
    parser.add_argument(
        "--url", default=None, help="magickit MCP URL (default: in-code/env default)"
    )
    args = parser.parse_args()

    try:
        if args.input == "-":
            raw = sys.stdin.read()
        else:
            with open(args.input, encoding="utf-8") as f:
                raw = f.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"parked_humans: input unreadable: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("parked_humans: input must be a JSON object", file=sys.stderr)
        return 1
    candidates_raw = payload.get("candidates", [])
    if not isinstance(candidates_raw, list):
        print("parked_humans: candidates must be a JSON array", file=sys.stderr)
        return 1
    candidates = [c for c in candidates_raw if isinstance(c, dict)]

    try:
        result = asyncio.run(_poll(args.project, candidates, args.url))
    except Exception as exc:
        # A whole-poll failure (asyncio setup, transport dead, etc.) is different from a per-
        # candidate failure and short-circuits with a non-zero exit so the wrapper's caller side
        # can record "the probe itself broke" once, rather than silently returning an empty
        # parked list (which would look like "nobody parked" — the exact silent-degradation
        # this file exists not to do).
        print(f"parked_humans: poll failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
