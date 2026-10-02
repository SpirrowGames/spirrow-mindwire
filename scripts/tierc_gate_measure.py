"""Measure what the enforced Tier-C admission gate would have done to past ``NEXT: human`` turns.

Spec: T-decider-conductor-hook DECIDED 2e-1a (Bohr msg-5141; Takahito decide via operator proxy).
No Lexora call, no cost: the gate is :func:`~spirrow_mindwire.tier_c_admission_gate.
decide_admission`, a pure function, run here exactly as the live gate runs it.

**Selection.** Every chatroom message in ``[--since, --until)`` that the conductor's own resolver
(``resolve_handoff``, against the same routing roster ``build_tierc_eval_fixture.py`` uses) reads
as an author-written ``NEXT: human``. Each falls into exactly one bucket:

* ``i_bounce`` — a proposer / implementer / naysayer turn the gate bounces (no label, ``other:``,
  ``release-cross-repo``, unknown). The upper bound of what stops reaching Takahito under
  ``[tierc_gate] mode = "enforce"``. Split further by bounce reason (counts only).
* ``ii_migrated`` — admitted by rewriting a legacy label (``scope`` → ``goal``, ``billing`` →
  ``cost``).
* ``iii_admitted`` — admitted on a valid label (``unsure:goal?`` included; counted separately as
  ``iii_unsure``).
* ``iv_pr_gate_relay`` — authored by ``pr-gate-relay``. Outside the hook and the gate (msg-4361),
  counted on its own; it is not part of the role total.
* ``other_author`` — anyone else (operator, the human, off-roster authors). Never gated.

``retry_lookup`` is ``never_retry``: no past turn carries a ``RETRY:`` token issued by a live
gate, so "no unresolved bounce exists" is the true answer for history.

**Output.** Counts and ``msg_id`` lists only (``--out`` JSON, LF). No message body, no label text
and no Jev score is written — the pre-registration in T-decider-tierc-replay-eval forbids looking
at the shadow scores before evaluation, and this script has none to look at. Nothing is posted to
any chatroom thread.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spirrow_mindwire.conductor.gate_records import RELAY_AUTHOR
from spirrow_mindwire.conductor.handoff import HandoffKind, resolve_handoff
from spirrow_mindwire.decider.hook import TIERC_ENTRY_ROLES, never_retry
from spirrow_mindwire.tier_c_admission_gate import AdmissionVerdict, LogKind, decide_admission
from spirrow_mindwire.value_objects import Role


def _load_builder() -> Any:
    """The fixture builder, loaded by path: its harvest / roster code is reused, not copied."""
    name = "build_tierc_eval_fixture_for_gate_measure"
    path = Path(__file__).resolve().parent / "build_tierc_eval_fixture.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


builder = _load_builder()

BUCKETS: tuple[str, ...] = (
    "i_bounce",
    "ii_migrated",
    "iii_admitted",
    "iv_pr_gate_relay",
    "other_author",
)


def _role_of(roster: Mapping[str, Role], author: str) -> Role | None:
    folded = author.casefold()
    for name, role in roster.items():
        if name.casefold() == folded:
            return role
    return None


def classify(
    threads: Sequence[tuple[str, str, Sequence[Any]]],
    current_roster: Mapping[str, Role],
    *,
    since: datetime,
    until: datetime,
) -> dict[str, Any]:
    """Bucket every author-written ``NEXT: human`` in ``[since, until)``. Pure; counts + ids."""
    ids: dict[str, list[str]] = {b: [] for b in BUCKETS}
    unsure: list[str] = []
    bounce_reasons: Counter[str] = Counter()
    by_author: Counter[str] = Counter()
    for project, thread_id, messages in threads:
        for i, m in enumerate(messages):
            if not (since <= m.timestamp < until):
                continue
            roster = builder.routing_roster(messages[: i + 1], current_roster)
            handoff = resolve_handoff(m.content, roster, next_participant=m.next_participant)
            if handoff.kind is not HandoffKind.HUMAN or not handoff.author_requested_human:
                continue
            ref = f"{project}/{thread_id}#{m.msg_id}"
            if m.author == RELAY_AUTHOR:
                ids["iv_pr_gate_relay"].append(ref)
                continue
            if _role_of(roster, m.author) not in TIERC_ENTRY_ROLES:
                ids["other_author"].append(ref)
                continue
            by_author[m.author] += 1
            decision = decide_admission(
                body=m.content,
                author=m.author,
                retry_lookup=never_retry,
                now=m.timestamp,
                bounce_uuid="measure",
            )
            kinds = {e.kind for e in decision.log_entries}
            if decision.verdict is AdmissionVerdict.BOUNCE:
                ids["i_bounce"].append(ref)
                assert decision.bounce_reason is not None
                bounce_reasons[decision.bounce_reason.value] += 1
            elif LogKind.LABEL_MIGRATION in kinds:
                ids["ii_migrated"].append(ref)
            else:
                ids["iii_admitted"].append(ref)
                if LogKind.ADMIT_UNSURE in kinds:
                    unsure.append(ref)
    role_total = len(ids["i_bounce"]) + len(ids["ii_migrated"]) + len(ids["iii_admitted"])
    return {
        "schema": 1,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "counts": {
            **{b: len(v) for b, v in ids.items()},
            "iii_unsure": len(unsure),
            "role_total": role_total,
        },
        "i_bounce_by_reason": dict(sorted(bounce_reasons.items())),
        "role_total_by_author": dict(sorted(by_author.items())),
        "msg_ids": {**ids, "iii_unsure": unsure},
    }


def _date(raw: str) -> datetime:
    return builder.parse_timestamp(raw)  # type: ignore[no-any-return]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", type=_date, required=True, help="ISO date/time, inclusive")
    parser.add_argument("--until", type=_date, required=True, help="ISO date/time, exclusive")
    parser.add_argument("--project", action="append", default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    from spirrow_mindwire.config import load_settings

    current_roster = dict(load_settings().conductor.roster)
    threads = asyncio.run(builder.harvest(tuple(args.project or builder.PROJECTS)))
    result = classify(threads, current_roster, since=args.since, until=args.until)
    result["measured_at"] = datetime.now(UTC).isoformat()
    result["projects"] = list(args.project or builder.PROJECTS)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(result["counts"], ensure_ascii=False), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
