"""Tier-C fulltext eval runner — T-decider-tierc-fulltext-eval (Bohr msg-4318 / 4327 / 4329).

Sends two fixtures over the **same rows** to ``/v1/decide``: ``full`` (``--head-m 8000``) and
``base`` (the 500-char fixture, re-run as the same-input noise baseline, msg-4327 decision 1).

**One independent request per (row, side)** (msg-4329): every call is
``decide_once(state_i, ...)`` with a request body built only from that row's own fixture line —
no conversation history, no preamble other than the fixed question set, no earlier answer.
:func:`request_body` is the only builder and :func:`plan` / :func:`check_plan` pin, before any
call, that (1) every ``base`` body is byte-identical to the body the original 500-char fixture
row produces, and (2) every ``full`` body is a function of its own row only. A failed check stops
the run before the first call.

**Interleaving** (msg-4329): rows go in fixture order; for each row the two sides are sent back
to back, and which side goes first is drawn per row from ``random.Random(seed)``. The seed and
the resulting order are written to ``--plan-out`` before any call.

**Stops** (same as the original eval, common to both sides): a contract mismatch
(``no_verdict_null`` / ``no_verdict_malformed``, or an answer served by a provider other than
``jev``) stops at once; any other non-``evaluated`` outcome counts as an error and the run stops
once errors exceed 10% of calls (after at least 10 calls); the first ``evaluated`` record missing
any ``tierc-v1`` answer stops it too. Records are
written as each call returns, so ``--resume`` continues from disk; ``--max-calls`` caps this
invocation's calls.

**Reader of the output.** Files only; nothing is posted to any chatroom thread.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import decider_replay as dr

from spirrow_mindwire.adapters.decider_lexora import (
    DECIDER_TIMEOUT_SECONDS,
    decide_once,
)
from spirrow_mindwire.decider.questions import TIERC_QUESTIONS_V1
from spirrow_mindwire.decider.result import decision_result_to_dict
from spirrow_mindwire.decider.state import DecisionState, state_builder
from spirrow_mindwire.decider.wire import (
    POLICY_REPLAY_TIERC,
    build_decide_request,
)
from spirrow_mindwire.lexora.client import LexoraClient

SIDES = ("full", "base")
ERROR_RATE_MAX = 0.10
ERROR_RATE_MIN_CALLS = 10
QUESTION_KEYS = frozenset(q.key for q in TIERC_QUESTIONS_V1)
EXPECTED_PROVIDER = "jev"
CONTRACT_MISMATCH = frozenset({"no_verdict_null", "no_verdict_malformed"})
"""Outcomes that mean the server did not answer the ``tierc-v1`` contract: stop at once."""

Key = tuple[str, int]


def row_state(row: Mapping[str, Any]) -> DecisionState:
    return state_builder(dr.parse_turn(dict(row)))


def request_body(row: Mapping[str, Any]) -> str:
    """The exact ``/v1/decide`` body for one fixture row, as canonical JSON bytes (text)."""
    body = build_decide_request(row_state(row), policy=POLICY_REPLAY_TIERC)
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _keyed(rows: Sequence[Mapping[str, Any]]) -> dict[Key, Mapping[str, Any]]:
    out = {(str(r["thread_id"]), int(r["round_index"])): r for r in rows}
    if len(out) != len(rows):
        raise ValueError("duplicate (thread_id, round_index) in fixture")
    return out


@dataclass(frozen=True)
class Step:
    key: Key
    side: str


def plan(keys: Sequence[Key], seed: int) -> list[Step]:
    """Row order kept; per row, which side goes first is ``random.Random(seed)``'s coin."""
    rng = random.Random(seed)
    steps: list[Step] = []
    for k in keys:
        first = SIDES[rng.randrange(2)]
        second = SIDES[1 - SIDES.index(first)]
        steps += [Step(k, first), Step(k, second)]
    return steps


def check_plan(
    *,
    full: Sequence[Mapping[str, Any]],
    base: Sequence[Mapping[str, Any]],
    original: Sequence[Mapping[str, Any]],
    steps: Sequence[Step],
) -> dict[str, Any]:
    """msg-4329 pre-run leak checks (no call). Raises on any failure; returns the evidence.

    (1) base body == original body, byte for byte, for every row.
    (2) every body built while walking ``steps`` in order equals the body built from that row
        alone — nothing from an earlier step reaches a later one — and names only its own row.
    """
    f, b, o = _keyed(full), _keyed(base), _keyed(original)
    if not (set(f) == set(b) and set(b) <= set(o)):
        raise ValueError("full / base key sets differ, or base has a row the original lacks")
    for k in b:
        if request_body(b[k]) != request_body(o[k]):
            raise AssertionError(f"base request differs from the original at {k}")
    alone = {("full", k): request_body(f[k]) for k in f} | {
        ("base", k): request_body(b[k]) for k in b
    }
    for s in steps:
        rows = f if s.side == "full" else b
        body = request_body(rows[s.key])
        if body != alone[(s.side, s.key)]:
            raise AssertionError(f"request for {s} depends on an earlier step")
        state = json.loads(json.loads(body)["state"])  # state_to_wire: a JSON string
        if (str(state["thread_id"]), int(state["round_index"])) != s.key:
            raise AssertionError(f"request for {s} names another row")
    return {
        "rows": len(f),
        "steps": len(steps),
        "base_equals_original": len(b),
        "full_sha256": hashlib.sha256(
            "\n".join(alone[("full", k)] for k in sorted(f)).encode("utf-8")
        ).hexdigest(),
    }


def missing_answers(decision: Mapping[str, Any]) -> list[str]:
    raw = decision.get("raw_answers")
    if not isinstance(raw, Mapping):
        return sorted(QUESTION_KEYS)
    return sorted(QUESTION_KEYS - set(raw))


@dataclass
class Tally:
    called: int = 0
    errors: int = 0
    stopped: str | None = None


def record_outcome(t: Tally, decision: Mapping[str, Any]) -> None:
    """Count one call and set ``t.stopped`` when a stop condition fires."""
    t.called += 1
    outcome = decision.get("outcome")
    if outcome in CONTRACT_MISMATCH:
        t.stopped = f"contract mismatch: outcome={outcome}"
        return
    if outcome == "evaluated" and decision.get("provider") != EXPECTED_PROVIDER:
        t.stopped = f"contract mismatch: provider={decision.get('provider')!r}"
        return
    if outcome != "evaluated":
        t.errors += 1
    elif missing := missing_answers(decision):
        t.stopped = f"raw_answers missing {missing}"
        return
    if t.called >= ERROR_RATE_MIN_CALLS and t.errors / t.called > ERROR_RATE_MAX:
        t.stopped = f"error rate {t.errors}/{t.called} > {ERROR_RATE_MAX:.0%}"


async def run(
    *,
    endpoint: str,
    steps: Sequence[Step],
    rows: Mapping[str, Mapping[Key, Mapping[str, Any]]],
    sinks: Mapping[str, IO[str]],
    done: Mapping[str, set[Key]],
    max_calls: int | None,
) -> Tally:
    t = Tally()
    async with LexoraClient(endpoint, timeout_seconds=DECIDER_TIMEOUT_SECONDS) as client:
        for s in steps:
            if s.key in done[s.side]:
                continue
            if max_calls is not None and t.called >= max_calls:
                break
            state = row_state(rows[s.side][s.key])
            result = await decide_once(state, client=client, policy=POLICY_REPLAY_TIERC)
            rec = dr.build_tierc_record(state)
            rec["side"] = s.side
            rec["decision"] = decision_result_to_dict(result)
            sinks[s.side].write(json.dumps(rec, ensure_ascii=False) + "\n")
            sinks[s.side].flush()
            record_outcome(t, rec["decision"])
            if t.stopped:
                break
    return t


def _load(path: Path) -> list[dict[str, Any]]:
    return list(dr.iter_fixture(path))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--full", type=Path, required=True, help="head-m 8000 fixture")
    p.add_argument("--base", type=Path, required=True, help="head-m 500 fixture (same rows)")
    p.add_argument("--original", type=Path, required=True, help="the original fixture.jsonl")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--plan-out", type=Path, required=True)
    p.add_argument("--out-full", type=Path, required=True)
    p.add_argument("--out-base", type=Path, required=True)
    p.add_argument("--endpoint", default=None, help="omit = checks only, no call")
    p.add_argument("--sides", default="full,base", help="restrict to one side (e.g. contract)")
    p.add_argument("--key", action="append", default=None, help="thread_id#round_index")
    p.add_argument("--max-calls", type=int, default=None)
    p.add_argument("--resume", action="store_true")
    a = p.parse_args(argv)

    full, base, original = _load(a.full), _load(a.base), _load(a.original)
    keys = [(str(r["thread_id"]), int(r["round_index"])) for r in full]
    if a.key:
        wanted = [(k.rsplit("#", 1)[0], int(k.rsplit("#", 1)[1])) for k in a.key]
        unknown = set(wanted) - set(keys)
        if unknown:
            p.error(f"--key not in fixture: {sorted(unknown)}")
        keys = wanted
    sides = tuple(s for s in a.sides.split(",") if s)
    if not set(sides) <= set(SIDES) or not sides:
        p.error(f"--sides must name {SIDES}")
    steps = [s for s in plan(keys, a.seed) if s.side in sides]
    evidence = check_plan(full=full, base=base, original=original, steps=steps)
    a.plan_out.parent.mkdir(parents=True, exist_ok=True)
    a.plan_out.write_text(
        json.dumps(
            {
                "seed": a.seed,
                "sides": list(sides),
                "checks": evidence,
                "steps": [[s.key[0], s.key[1], s.side] for s in steps],
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence), file=sys.stderr)
    if a.endpoint is None:
        return 0

    outs = {"full": a.out_full, "base": a.out_base}
    done = {s: (dr.load_done_keys(outs[s]) if a.resume else set()) for s in SIDES}
    sinks = {s: outs[s].open("a" if a.resume else "w", encoding="utf-8") for s in sides}
    try:
        t = asyncio.run(
            run(
                endpoint=a.endpoint,
                steps=steps,
                rows={"full": _keyed(full), "base": _keyed(base)},
                sinks=sinks,
                done=done,
                max_calls=a.max_calls,
            )
        )
    finally:
        for fh in sinks.values():
            fh.close()
    print(
        json.dumps({"called": t.called, "errors": t.errors, "stopped": t.stopped}),
        file=sys.stderr,
    )
    return 3 if t.stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
