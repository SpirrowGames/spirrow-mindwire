"""Head-skip nomination-predicate CLI — the sweep's per-tick launch/defer/skip decision.

Called by ``deploy/run-conductor-scheduled.ps1`` once per tick with the sweep's already-probed
per-candidate ``(head_msg_id, control_state)`` pairs; returns one verdict per candidate. The
predicate itself lives in :mod:`spirrow_mindwire.conductor.head_skip` — this script is the thin
wrapper that (a) fetches head bodies only for candidates whose cached nomination cannot be
reused, (b) applies :func:`spirrow_mindwire.conductor.head_skip.decide`, and (c) persists the
new state BEFORE any conductor session is started (session-start-before write, msg-878).

Input (JSON, from ``--candidates`` or stdin):

    [
      {"thread_id": "T-track-b-...", "head_msg_id": "msg-2242", "control_state": "run"},
      {"thread_id": "T-lod0-...",    "head_msg_id": "msg-1919", "control_state": "run"},
      ...
    ]

Output (JSON, to stdout):

    {
      "mode": "live",
      "verdicts": [
        {"thread_id": "T-track-b-...", "decision": "launch", "reason": "no-prior-record",
         "token": "bohr", "token_raw": "Bohr", "progressed": false, "delay_seconds": 0.0,
         "eligible_at": "2026-08-11T12:00:00+00:00", "attempts_before": 0, "attempts_after": 1,
         "head_msg_id": "msg-2242", "head_fetched": true},
        ...
      ]
    }

State file: one JSON object keyed by ``thread_id`` (values are Record dicts per
:func:`~spirrow_mindwire.conductor.head_skip.record_to_json`). Written only in ``--mode live``;
``--mode report`` prints the same verdicts but touches nothing on disk. The report mode is the
audit path for a "would-be-launch" dry run (Einstein Objection 2 / Bohr msg-878 §report).

Fail-open contract: if a body fetch fails, the candidate falls through with an UNRESOLVED token
(reason="unfetchable"); :func:`decide` then LAUNCHes it (no-prior-record or backoff-elapsed)
rather than SKIPping. This matches the wrapper's other probes (head, control) — every failure
mode of the optimisation path collapses to "launch anyway", never to "silently park a live
thread".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spirrow_mindwire.conductor.head_skip import (
    REPORT_MODE_VALUE,
    Decision,
    Record,
    Verdict,
    can_reuse_cached_parse,
    commit_launch,
    commit_observation,
    decide,
    parse_head_token,
    record_from_json,
    record_to_json,
    verdict_to_json,
)
from spirrow_mindwire.magickit.client import MagickitMcpError, StreamableHttpChatroomMcp


async def _fetch_head_body(
    mcp: StreamableHttpChatroomMcp, project: str, thread_id: str
) -> tuple[str, str] | None:
    """Return ``(head_msg_id, head_body)`` for the last message of a thread, or ``None`` on error.

    Errors are swallowed (fail-open at the caller): a probe gap costs one launched candidate,
    never a silent park. ``None`` means "could not fetch"; the caller synthesises an UNRESOLVED
    body and lets :func:`decide` fall open into LAUNCH.
    """
    try:
        result: Any = await mcp.call_tool(
            "chatroom_get_thread",
            {"project": project, "thread_id": thread_id, "mode": "full"},
        )
    except MagickitMcpError as exc:
        print(
            f"head_skip: chatroom_get_thread failed for {thread_id}: {exc}",
            file=sys.stderr,
        )
        return None
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


def _load_state(path: Path) -> dict[str, Record]:
    """Load ``head_skip.json`` into a ``{thread_id: Record}`` map. Missing / corrupt → empty."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"head_skip: state file unreadable ({path}: {exc}) — treating as empty",
            file=sys.stderr,
        )
        return {}
    out: dict[str, Record] = {}
    if isinstance(data, dict):
        for thread_id, rec_dict in data.items():
            if isinstance(rec_dict, dict):
                rec = record_from_json(rec_dict)
                if rec is not None:
                    out[str(thread_id)] = rec
    return out


def _save_state(path: Path, state: dict[str, Record]) -> None:
    """Atomically write the state file (temp + os.replace).

    Atomicity matters because the sweep and an operator can both touch the state directory
    concurrently — a partially-written state that a subsequent tick reads back as corrupt would
    trigger a rebuild-from-empty, which for this state means every thread launches once (a
    thundering herd rather than a silent stall — fail-safe direction, but still avoidable).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    serialisable = {tid: record_to_json(rec) for tid, rec in state.items()}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(serialisable, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


async def _decide_all(
    *,
    project: str,
    candidates: list[dict[str, Any]],
    state: dict[str, Record],
    now: datetime,
    mode: str,
    url: str | None,
) -> tuple[dict[str, Record], list[dict[str, Any]]]:
    """Apply the predicate to every candidate; return the new state + a JSON verdict list.

    The state map is UPDATED in place for LAUNCH decisions (the "session-start-before write"
    discipline is implemented here: the record is set before we hand the sweep back its verdict,
    so a wrapper that crashes after reading our stdout still has a persisted attempt count).
    ``mode == "report"`` gets the exact same verdicts but the returned state is unchanged from
    the input (the caller then does not persist it).
    """
    mcp = StreamableHttpChatroomMcp(url)
    is_live = mode != REPORT_MODE_VALUE
    verdicts: list[dict[str, Any]] = []
    new_state = dict(state)  # copy — report mode must leave the input untouched
    for cand in candidates:
        thread_id = str(cand.get("thread_id") or "")
        if not thread_id:
            continue
        head_msg_id = str(cand.get("head_msg_id") or "")
        control_state = str(cand.get("control_state") or "")
        rec = new_state.get(thread_id)

        # Decide whether we can reuse the cached parse. Two conditions (both required) —
        # ``can_reuse_cached_parse`` bundles them and is the SOLE cache-hit predicate:
        #   1. cache-hit: ``rec.last_observed_head_msg_id == head_msg_id`` (using the
        #      OBSERVATION field, not ``head_msg_id_at_launch``, so a parked or deferring
        #      thread's cache still hits after it was NOT launched — this is the Tier B
        #      naysayer round-1 fix on PR #140);
        #   2. TTL: the observation is not older than ``HEAD_CACHE_TTL`` (the edit-in-place
        #      recovery path).
        head_body: str
        head_fetched: bool
        if can_reuse_cached_parse(rec, head_msg_id, now):
            # Synthesise a body from the cached NORMALISED token. Round-tripping the same
            # normalised token through ``parse_head_token`` reproduces the identical token, so
            # ``decide()`` reaches the same verdict without a network fetch. Type-narrowing:
            # ``can_reuse_cached_parse`` returns False for a None record, so ``rec`` is present
            # in this branch.
            assert rec is not None
            head_body = f"NEXT: {rec.last_observed_nomination}"
            head_fetched = False
            actual_msg_id = head_msg_id
        else:
            fetched = await _fetch_head_body(mcp, project, thread_id)
            if fetched is None:
                # Fail-open: an unfetchable body -> UNRESOLVED -> decide() launches. The
                # candidate keeps its probe-reported head_msg_id for the verdict; the decision
                # still routes through the two-stage judgment on the empty token.
                head_body = ""
                head_fetched = False
                actual_msg_id = head_msg_id
            else:
                actual_msg_id, head_body = fetched
                head_fetched = True

        v: Verdict = decide(
            now=now,
            head_msg_id=actual_msg_id,
            head_body=head_body,
            control_state=control_state,
            record=rec,
        )

        # Commit-before-return: for LAUNCH in live mode, the record persists NOW (before the
        # sweep has even received our verdict, let alone acted on it). The sweep may then take
        # minutes to start the conductor and any wall-clock skew between now and that start is
        # bounded by CAP anyway (the delay for the next attempt); the invariant we care about is
        # that a launch is counted even if the session then dies (test #10).
        if is_live and v.decision is Decision.LAUNCH:
            new_state[thread_id] = commit_launch(
                now=now,
                head_msg_id=actual_msg_id,
                verdict=v,
                control_state=control_state,
            )
        elif is_live and rec is not None and head_fetched:
            # We re-fetched the head body (fetched=True) but did NOT launch — refresh the
            # OBSERVATION fields so the next tick can reuse this parse without another fetch.
            # ``commit_observation`` leaves the launch baseline (``_at_launch`` + attempts +
            # last_launch_at) untouched, preserving the backoff / progressed semantics; only
            # the observation-side fields (``last_observed_...`` + ``head_observed_at``) move.
            # This is the crucial half of the naysayer round-1 fix: without it, a parked
            # thread's second tick would find ``last_observed_head_msg_id`` still stale, cache
            # miss again, and re-fetch. With it, one fetch per real change plus TTL is the
            # steady state.
            new_state[thread_id] = commit_observation(
                now=now,
                head_msg_id=actual_msg_id,
                token=parse_head_token(head_body),
                record=rec,
            )

        payload = verdict_to_json(v)
        payload["thread_id"] = thread_id
        payload["head_msg_id"] = actual_msg_id
        payload["head_fetched"] = head_fetched
        verdicts.append(payload)

    return new_state, verdicts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--candidates",
        default=None,
        help="path to a JSON file of candidates; omit to read from stdin",
    )
    parser.add_argument(
        "--state-file",
        required=True,
        help="path to head_skip.json (JSON object keyed by thread_id)",
    )
    parser.add_argument(
        "--mode",
        default="live",
        choices=("live", REPORT_MODE_VALUE),
        help="live: write state on LAUNCH; report: dry-run (never touch state)",
    )
    parser.add_argument(
        "--url", default=None, help="magickit MCP URL (default: in-code/env default)"
    )
    parser.add_argument(
        "--now-iso",
        default=None,
        help="override the current UTC time (ISO 8601); tests only",
    )
    args = parser.parse_args()

    # Candidates: file or stdin. Both must be JSON arrays.
    try:
        if args.candidates:
            candidates_raw = Path(args.candidates).read_text(encoding="utf-8")
        else:
            candidates_raw = sys.stdin.read()
        candidates = json.loads(candidates_raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"head_skip: candidates input unreadable: {exc}", file=sys.stderr)
        return 1
    if not isinstance(candidates, list):
        print("head_skip: candidates must be a JSON array", file=sys.stderr)
        return 1

    state_path = Path(args.state_file)
    state = _load_state(state_path)
    now = _resolve_now(args.now_iso)

    new_state, verdicts = asyncio.run(
        _decide_all(
            project=args.project,
            candidates=candidates,
            state=state,
            now=now,
            mode=args.mode,
            url=args.url,
        )
    )

    if args.mode != REPORT_MODE_VALUE:
        _save_state(state_path, new_state)

    print(
        json.dumps(
            {"mode": args.mode, "verdicts": verdicts},
            ensure_ascii=False,
        )
    )
    return 0


def _resolve_now(override: str | None) -> datetime:
    if override:
        try:
            parsed = datetime.fromisoformat(override)
        except ValueError:
            print(f"head_skip: --now-iso not parseable: {override}", file=sys.stderr)
            raise SystemExit(1) from None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
