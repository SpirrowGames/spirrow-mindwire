"""Head-skip nomination-predicate CLI — the sweep's per-tick launch/defer/skip decision.

Called by ``deploy/run-conductor-scheduled.ps1`` in a **two-phase protocol** (Tier B naysayer
round 5, PR #140):

**Phase 1 (``decide`` — default mode):** batch-evaluate every candidate for the tick, apply
:func:`spirrow_mindwire.conductor.head_skip.decide`, emit one JSON verdict per candidate. This
phase refreshes ONLY the observation-side of the state file (``last_observed_*`` and
``head_observed_at``) for candidates whose head body was successfully re-fetched — the launch
baseline (``last_launch_at``, ``launch_attempts``, ``*_at_launch``) is NEVER touched here.

Rationale: the sweep processes at most one candidate per tick ("thread did work — sweep done").
If ``decide`` batch-committed launch state for every LAUNCH verdict, all-but-the-first-launched
candidate would be "phantom-launched" — their record would say "we launched" while no session
was ever started, and next tick would apply backoff to threads that never actually ran. That
was measured by the naysayer as an exponential-starvation loop for concurrently eligible
threads. Splitting decide from commit removes the coupling.

**Phase 2 (``commit-launch`` mode):** the sweep, after receiving the ``decide`` verdicts and
deciding which ONE thread to launch, calls ``commit-launch --payload <json>`` (feeding back the
``commit_launch_payload`` from the chosen verdict) BEFORE starting the conductor session. This
records the launch baseline for exactly that thread. Any LAUNCH verdicts the sweep did not act
on are left with their launch baseline untouched — they stay eligible on the next tick.

Input to ``decide`` (JSON, from ``--candidates`` or stdin):

    [
      {"thread_id": "T-a", "head_msg_id": "msg-2242", "control_state": "run"},
      {"thread_id": "T-b", "head_msg_id": "msg-1919", "control_state": "run"},
      ...
    ]

Output of ``decide`` (JSON, to stdout):

    {
      "mode": "decide",
      "verdicts": [
        {
          "thread_id": "T-a", "decision": "launch", "reason": "no-prior-record",
          "token": "bohr", "token_raw": "Bohr", "progressed": false,
          "delay_seconds": 0.0, "eligible_at": "2026-08-11T12:00:00+00:00",
          "attempts_before": 0, "attempts_after": 1,
          "head_msg_id": "msg-2242", "head_fetched": true,
          "commit_launch_payload": {
            "thread_id": "T-a", "head_msg_id": "msg-2242",
            "token": "bohr", "token_raw": "Bohr", "attempts_after": 1,
            "control_state": "run", "head_fetched": true
          }
        },
        ...
      ]
    }

Input to ``commit-launch`` (``--payload <json>`` argument): exactly the
``commit_launch_payload`` object from the corresponding decide verdict.

State file: one JSON object keyed by ``thread_id`` (values are Record dicts per
:func:`~spirrow_mindwire.conductor.head_skip.record_to_json`). Written in both live modes;
``--mode report`` prints the verdicts but touches nothing on disk.

Fail-open contract: if a body fetch fails, the candidate falls through with an UNRESOLVED
token; :func:`decide` then LAUNCHes it. The ``commit_launch_payload`` carries
``head_fetched=False`` so ``commit-launch`` knows to preserve the prior observation rather than
poisoning the cache with an empty parse (round-2 anti-poison rule).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
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

# Windows consoles default to legacy codepages (e.g. cp932) that cannot encode
# non-ASCII characters this script's stderr messages carry (state-file parse
# errors quote raw path bytes; head_skip reasons may contain em dashes).
# Reconfigure the streams so print() cannot raise. The machine-read JSON on
# stdout is separately hardened by ``ensure_ascii=True`` below (see per-call
# comments): reconfigure alone is not sufficient because
# ``errors="backslashreplace"`` emits astral characters as ``\Uxxxxxxxx`` -
# not a JSON escape and would defeat the sweep wrapper's ``ConvertFrom-Json``.
# See scripts/dogfood_smoke.py:42-45 for the same pattern.
_reconfigure = getattr(sys.stdout, "reconfigure", None)
if _reconfigure is not None:
    _reconfigure(encoding="utf-8", errors="backslashreplace")
_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(encoding="utf-8", errors="backslashreplace")


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


def _build_commit_launch_payload(
    *,
    thread_id: str,
    head_msg_id: str,
    verdict: Verdict,
    control_state: str,
    head_fetched: bool,
) -> dict[str, Any]:
    """The self-describing blob the sweep feeds back to ``commit-launch`` for one thread.

    Includes everything ``commit_launch`` needs — token / token_raw / attempts_after /
    control_state / head_fetched — plus the thread_id and head_msg_id used as the state-file
    key. The blob is intentionally self-describing so ``commit-launch`` can be invoked without
    re-fetching or re-deciding: the sweep already knows what to do, this just records it.
    """
    return {
        "thread_id": thread_id,
        "head_msg_id": head_msg_id,
        "token": verdict.token,
        "token_raw": verdict.token_raw,
        "attempts_after": int(verdict.attempts_after),
        "control_state": control_state,
        "head_fetched": bool(head_fetched),
    }


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

    This is the ``decide`` phase of the two-phase protocol. Launch-baseline state is **NEVER**
    touched here — the sweep applies it separately via :func:`_apply_commit_launch` for the ONE
    thread it actually chooses to start (see the module docstring for the phantom-launch
    scenario this splits closes).

    Observation-side state IS refreshed when the head body was successfully fetched, regardless
    of the verdict — that fetch is a real observation and caching its result lets the next tick
    skip the fetch. A LAUNCH candidate that the sweep never actually starts still benefits from
    the observation refresh (subsequent ticks re-evaluate and cache-hit).

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

        # Observation-only refresh. NEVER touches the launch baseline (that is the
        # commit-launch phase's job, applied to only ONE thread per tick). Two guards:
        #
        #   1. head_fetched required: a synthesised empty body (fail-open) is not a real
        #      observation and must not overwrite the cache. commit_observation writing
        #      empty here would trip both the round-2 cache-poisoning failure and would
        #      re-open the round-4 disjunct-3 loop against an empty observation.
        #   2. rec is None allowed: commit_observation(record=None) mints an initial
        #      record with only observation fields set — a never-launched parked thread
        #      whose first evaluation is a SKIP now cache-hits on the next tick
        #      (naysayer round 2).
        if is_live and head_fetched:
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
        # LAUNCH decisions carry a self-describing commit-launch payload for the sweep to
        # feed back to ``commit-launch`` if it decides to actually start this thread.
        if v.decision is Decision.LAUNCH:
            payload["commit_launch_payload"] = _build_commit_launch_payload(
                thread_id=thread_id,
                head_msg_id=actual_msg_id,
                verdict=v,
                control_state=control_state,
                head_fetched=head_fetched,
            )
        verdicts.append(payload)

    return new_state, verdicts


def _apply_commit_launch(
    *,
    state_path: Path,
    payload: dict[str, Any],
    now: datetime,
) -> Record:
    """Apply a commit-launch payload to the state file for one thread.

    Called by the sweep AFTER it has received ``decide`` verdicts and chosen the single thread
    it will start. Writes the launch baseline (``last_launch_at``, ``launch_attempts``, etc.)
    for exactly that thread. Any other LAUNCH verdicts from the same ``decide`` batch that the
    sweep did NOT commit will re-evaluate freshly on the next tick — no phantom-launch. Called
    strictly BEFORE the conductor spawn so a mid-flight kill leaves the attempt counted (the
    session-start-before write contract, test #10).

    Payload is a plain dict (JSON-safe) exactly as ``decide`` emitted it — the sweep is
    expected to feed it back verbatim from the corresponding verdict's ``commit_launch_payload``
    field.
    """
    thread_id = str(payload.get("thread_id") or "")
    if not thread_id:
        raise ValueError("commit-launch payload missing thread_id")
    state = _load_state(state_path)
    prior = state.get(thread_id)
    verdict = Verdict(
        decision=Decision.LAUNCH,
        reason="",  # not used by commit_launch
        token=str(payload.get("token") or ""),
        token_raw=payload.get("token_raw"),
        progressed=False,  # not used by commit_launch
        attempts_before=0,  # not used by commit_launch
        attempts_after=int(payload.get("attempts_after") or 0),
        delay=_ZERO_TIMEDELTA,  # not used by commit_launch
        eligible_at=None,  # not used by commit_launch
    )
    new_record = commit_launch(
        now=now,
        head_msg_id=str(payload.get("head_msg_id") or ""),
        verdict=verdict,
        control_state=str(payload.get("control_state") or ""),
        head_fetched=bool(payload.get("head_fetched", True)),
        prior_record=prior,
    )
    state[thread_id] = new_record
    _save_state(state_path, state)
    return new_record


_ZERO_TIMEDELTA = timedelta(0)


def _main_decide(args: argparse.Namespace) -> int:
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
            # Machine-read output: emit ASCII-only so the sweep wrapper's
            # ``ConvertFrom-Json`` can decode it under any stdout encoding.
            # msg-2292 D-3: ``errors="backslashreplace"`` on the stream is
            # not sufficient (produces ``\Uxxxxxxxx`` for astral chars,
            # which is not a JSON string escape).
            ensure_ascii=True,
        )
    )
    return 0


def _main_commit_launch(args: argparse.Namespace) -> int:
    # Payload from --payload (inline JSON) or --payload-file. Sweep passes back exactly the
    # commit_launch_payload sub-object it received in the corresponding decide verdict.
    try:
        if args.payload_file:
            payload_raw = Path(args.payload_file).read_text(encoding="utf-8")
        else:
            payload_raw = args.payload or sys.stdin.read()
        payload = json.loads(payload_raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"head_skip: commit-launch payload unreadable: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("head_skip: commit-launch payload must be a JSON object", file=sys.stderr)
        return 1

    state_path = Path(args.state_file)
    now = _resolve_now(args.now_iso)
    try:
        new_record = _apply_commit_launch(state_path=state_path, payload=payload, now=now)
    except ValueError as exc:
        print(f"head_skip: commit-launch failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "mode": "commit-launch",
                "thread_id": payload.get("thread_id", ""),
                "record": record_to_json(new_record),
            },
            # Machine-read output — see comment on the decide-mode print
            # above (msg-2292 D-3).
            ensure_ascii=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="")
    parser.add_argument(
        "--candidates",
        default=None,
        help="path to a JSON file of candidates; omit to read from stdin (decide mode only)",
    )
    parser.add_argument(
        "--state-file",
        required=True,
        help="path to head_skip.json (JSON object keyed by thread_id)",
    )
    parser.add_argument(
        "--mode",
        default="decide",
        choices=("decide", "commit-launch", REPORT_MODE_VALUE),
        help=(
            "decide: batch-evaluate, emit verdicts, refresh observation only (never touch "
            "launch baseline); commit-launch: apply launch baseline for one thread; "
            "report: dry-run decide (never touch state)"
        ),
    )
    parser.add_argument(
        "--url", default=None, help="magickit MCP URL (default: in-code/env default)"
    )
    parser.add_argument(
        "--payload",
        default=None,
        help="inline JSON payload for commit-launch (alternative: --payload-file or stdin)",
    )
    parser.add_argument(
        "--payload-file",
        default=None,
        help="path to a JSON file with the commit-launch payload",
    )
    parser.add_argument(
        "--now-iso",
        default=None,
        help="override the current UTC time (ISO 8601); tests only",
    )
    args = parser.parse_args()

    if args.mode == "commit-launch":
        return _main_commit_launch(args)
    # decide (default) or report
    if not args.project:
        print("head_skip: --project is required in decide/report mode", file=sys.stderr)
        return 1
    return _main_decide(args)


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
