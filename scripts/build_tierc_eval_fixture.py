"""Build the Tier-C replay-evaluation fixture from past chatroom escalations.

Spec: T-decider-tierc-replay-eval — Bohr msg-4219 §2 (design), msg-4224 (double-blind labels,
no ``decision_request`` import), msg-4226 (three future-leak paths sealed), approved by Einstein
msg-4225 / msg-4227.

**What it does.** Reads every thread of the chatroom projects, picks every message the
conductor would resolve to ``NEXT: human`` (``resolve_handoff`` — the conductor's own resolver,
not a second parser), and turns each into one replay fixture row that ``decider_replay.py``
reads unchanged (``parse_turn`` shape). Two blank label templates are written beside it, one
per labeller; the ground truth never enters the fixture (msg-4219 §2: labels live in a separate
file keyed by ``(thread_id, round_index)``, joined only by ``tierc_eval_report.py``).

**Same functions as live (msg-4219 §2).** The row is built by :func:`~spirrow_mindwire.decider.
hook.compute_gate_result` (``retry_lookup=never_retry``, as step 2b-1) and
:func:`~spirrow_mindwire.decider.hook.turn_from_messages` → ``state_builder`` →
``decider.wire.state_to_dict`` — the functions the live hook calls. Nothing here re-derives
``head_summary`` / ``recent_events`` / ``prev_next``.

**The three future-leak seals (msg-4226 §5, each pinned by a test):**

(a) the message list handed to ``turn_from_messages`` stops at the escalation message
    (inclusive) — nothing posted after it can reach ``recent_events`` / ``head_summary``;
(b) the roster is the authors who had posted in the thread **up to** the escalation; each role
    comes from the message's own ``role`` field when it names a :class:`Role`
    (``roster_source=historical``), else from today's ``[conductor].roster``
    (``current_fallback``); an author with neither is left out (as live leaves out an off-roster
    author such as ``pr-gate-relay``);
(c) ``now`` is the escalation's own timestamp, always passed explicitly — ``now=None`` raises.

**Deliberately current, not historical (msg-4226):** the admission gate rules (allowed labels,
label migration) are today's ``compute_gate_result`` — the evaluation asks what *today's* D18
gating + Jev would do on past inputs.

**``round_index``** is the escalation's 0-based position in its thread's message list. The live
conductor's ``round_index`` is its loop counter, which does not exist for a past message; the
position is stable and unique within a thread, which is what the join key needs.

**Not imported:** ``spirrow_mindwire.decision_request`` (the ADR-2026-09-18-22 dashboard-URL
fail-fast lives there; msg-4224 — pinned by a test).

**Reader of the output.** Files only (``--out-dir``); nothing is posted to any chatroom thread.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spirrow_mindwire.conductor.handoff import (
    HUMAN_TOKEN,
    HandoffKind,
    parse_next_token,
    resolve_handoff,
)
from spirrow_mindwire.decider.hook import (
    ThreadMessage,
    compute_gate_result,
    is_tierc_entry,
    never_retry,
    turn_from_messages,
)
from spirrow_mindwire.decider.state import state_builder
from spirrow_mindwire.decider.wire import state_to_dict
from spirrow_mindwire.value_objects import Role

_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(errors="backslashreplace")

PROJECTS: tuple[str, ...] = (
    "spirrow-mindwire",
    "spirrow-magickit",
    "spirrow-lexora",
    "spirrow-voxelworld",
    "spirrow-playproof",
    "spirrow-verimend",
    "spirrow-conclair",
    "spirrow-unrealwise",
)
"""The 8 projects with threads (Fermi msg-3630 §1 scanned "8 project")."""

EVAL_SINCE = datetime(2026, 9, 1, tzinfo=UTC)
"""msg-3630 §1: human-bound escalations "09-01 以降"."""
EVAL_UNTIL = datetime(2026, 9, 19, 17, 58, 3, tzinfo=UTC)
"""The timestamp of msg-3630 itself — the scan could not have seen anything later."""

RETRO_LABELS: frozenset[str] = frozenset({"scope", "billing", "irreversible", "goal", "cost"})
"""msg-4219 §1 遡及正例: pre-``EVAL_SINCE`` escalations whose author tagged them with a judgement
label. They are *candidates* — both labellers still label them."""

SET_EVAL = "eval"
SET_RETRO = "retro_candidate"

ROSTER_HISTORICAL = "historical"
ROSTER_CURRENT_FALLBACK = "current_fallback"


@dataclass(frozen=True)
class RawMessage:
    """The slice of one ``chatroom_get_thread`` message the builder reads."""

    msg_id: str
    author: str
    content: str
    timestamp: datetime
    role: str | None
    next_participant: str | None


def parse_timestamp(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def raw_message_from_dict(msg: Mapping[str, Any]) -> RawMessage:
    np_raw = msg.get("next_participant")
    role_raw = msg.get("role")
    return RawMessage(
        msg_id=str(msg.get("msg_id", "")),
        author=str(msg.get("author", "")),
        content=str(msg.get("content", "")),
        timestamp=parse_timestamp(str(msg["timestamp"])),
        role=str(role_raw) if isinstance(role_raw, str) and role_raw else None,
        next_participant=str(np_raw) if isinstance(np_raw, str) else None,
    )


def _role_or_none(raw: str | None) -> Role | None:
    if raw is None:
        return None
    try:
        return Role(raw)
    except ValueError:
        return None


def _lookup_casefold(roster: Mapping[str, Role], author: str) -> Role | None:
    folded = author.casefold()
    for name, role in roster.items():
        if name.casefold() == folded:
            return role
    return None


def historical_roster(
    messages: Sequence[RawMessage], current_roster: Mapping[str, Role]
) -> tuple[dict[str, Role], str]:
    """Seal (b): the roster as of the last message in ``messages`` (msg-4226 §2).

    Only authors present in ``messages`` are included. A role from the author's latest message
    that carries a valid ``role`` wins (``historical``); otherwise today's roster is consulted
    and the row is marked ``current_fallback``. An author with neither is omitted.
    """
    roster: dict[str, Role] = {}
    used_current = False
    authors: list[str] = []
    for m in messages:
        if m.author not in authors:
            authors.append(m.author)
    for author in authors:
        hist: Role | None = None
        for m in messages:
            if m.author == author and (r := _role_or_none(m.role)) is not None:
                hist = r  # latest wins
        if hist is not None:
            roster[author] = hist
            continue
        cur = _lookup_casefold(current_roster, author)
        if cur is not None:
            roster[author] = cur
            used_current = True
    return roster, (ROSTER_CURRENT_FALLBACK if used_current else ROSTER_HISTORICAL)


def is_escalation(msg: RawMessage) -> bool:
    """Would the conductor resolve this message to the human stop? (``resolve_handoff``)."""
    return (
        resolve_handoff(msg.content, {}, next_participant=msg.next_participant).kind
        is HandoffKind.HUMAN
    )


def build_eval_row(
    *,
    project: str,
    thread_id: str,
    messages: Sequence[RawMessage],
    head_index: int,
    current_roster: Mapping[str, Role],
    now: datetime | None,
    set_name: str,
) -> dict[str, Any]:
    """One escalation → one ``decider_replay`` fixture row (plus metadata columns).

    ``now`` must be the escalation's own timestamp (seal (c)); ``None`` raises rather than
    falling back to the wall clock the way the live hook does.
    """
    if now is None:
        raise ValueError("build_eval_row: now is required (the escalation's own timestamp)")
    upto = list(messages[: head_index + 1])  # seal (a)
    head = upto[-1]
    roster, roster_source = historical_roster(upto, current_roster)  # seal (b)

    handoff = resolve_handoff(head.content, roster, next_participant=head.next_participant)
    author_wrote_next_human = handoff.mismatch_reason is None
    # The conductor states the resolved head's parsed_next as the reserved token
    # (core._decider_hook); every other message keeps its body token.
    thread_msgs = [
        ThreadMessage(
            msg_id=m.msg_id,
            author=m.author,
            content=m.content,
            parsed_next=parse_next_token(m.content),
        )
        for m in upto[:-1]
    ]
    thread_msgs.append(
        ThreadMessage(
            msg_id=head.msg_id, author=head.author, content=head.content, parsed_next=HUMAN_TOKEN
        )
    )
    gate = compute_gate_result(
        body=head.content, author=head.author, now=now, retry_lookup=never_retry
    )
    state = state_builder(
        turn_from_messages(
            thread_id=thread_id,
            round_index=head_index,
            roster=roster,
            messages=thread_msgs,
            gate_result=gate,
        )
    )
    row = state_to_dict(state)
    row.update(
        {
            "project": project,
            "msg_id": head.msg_id,
            "author": head.author,
            "posted_at": head.timestamp.isoformat(),
            "set": set_name,
            "roster_source": roster_source,
            "tier_c_label": handoff.tier_c_label,
            "author_wrote_next_human": author_wrote_next_human,
            # Would the live hook's entry check (msg-4203) have let this turn in? Measurement
            # column for the (B) estimate only — the replay calls every row regardless.
            "live_entry": is_tierc_entry(
                original_stop="human",
                author_wrote_next_human=author_wrote_next_human,
                author_role=_lookup_casefold(roster, head.author),
            ),
        }
    )
    return row


def classify_set(msg: RawMessage, tier_c_label: str | None) -> str | None:
    if EVAL_SINCE <= msg.timestamp <= EVAL_UNTIL:
        return SET_EVAL
    if msg.timestamp < EVAL_SINCE and tier_c_label in RETRO_LABELS:
        return SET_RETRO
    return None


def rows_for_thread(
    *,
    project: str,
    thread_id: str,
    messages: Sequence[RawMessage],
    current_roster: Mapping[str, Role],
) -> list[tuple[dict[str, Any], RawMessage]]:
    out: list[tuple[dict[str, Any], RawMessage]] = []
    for i, m in enumerate(messages):
        if not is_escalation(m):
            continue
        label = resolve_handoff(m.content, {}, next_participant=m.next_participant).tier_c_label
        set_name = classify_set(m, label)
        if set_name is None:
            continue
        row = build_eval_row(
            project=project,
            thread_id=thread_id,
            messages=messages,
            head_index=i,
            current_roster=current_roster,
            now=m.timestamp,
            set_name=set_name,
        )
        out.append((row, m))
    return out


def label_template_row(row: Mapping[str, Any], body: str) -> dict[str, Any]:
    """A blank label line. No Jev output and no other labeller's answer (msg-4224)."""
    return {
        "thread_id": row["thread_id"],
        "round_index": row["round_index"],
        "project": row["project"],
        "msg_id": row["msg_id"],
        "author": row["author"],
        "posted_at": row["posted_at"],
        "set": row["set"],
        "category": None,
        "label": None,
        "rationale": "",
        "body": body,
    }


async def harvest(projects: Sequence[str]) -> list[tuple[str, str, list[RawMessage]]]:
    from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp

    mcp = StreamableHttpChatroomMcp()
    out: list[tuple[str, str, list[RawMessage]]] = []
    for project in projects:
        threads: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await mcp.call_tool(
                "chatroom_list_threads", {"project": project, "limit": 200, "offset": offset}
            )
            items = page.get("items") or []
            threads.extend(items)
            offset += len(items)
            if not items or offset >= page.get("total", len(threads)):
                break
        for t in threads:
            body = await mcp.call_tool(
                "chatroom_get_thread",
                {"project": project, "thread_id": t["thread_id"], "mode": "full"},
            )
            msgs = [
                raw_message_from_dict(m)
                for m in (body.get("messages") or [])
                if isinstance(m, dict) and m.get("timestamp")
            ]
            out.append((project, str(t["thread_id"]), msgs))
    return out


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def build_outputs(
    threads: Sequence[tuple[str, str, Sequence[RawMessage]]],
    current_roster: Mapping[str, Role],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Pure: harvested threads → (fixture rows, blank label rows, manifest counts)."""
    fixture: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    for project, thread_id, msgs in threads:
        for row, msg in rows_for_thread(
            project=project, thread_id=thread_id, messages=msgs, current_roster=current_roster
        ):
            fixture.append(row)
            labels.append(label_template_row(row, msg.content))
    keys = Counter((r["thread_id"], r["round_index"]) for r in fixture)
    dup = sorted(k for k, n in keys.items() if n > 1)
    if dup:
        # The join key (msg-4219 §2) has no project. Same-named threads in two projects are
        # fine unless both produce a row at the same position — then the key would merge them.
        raise ValueError(f"duplicate (thread_id, round_index) across projects: {dup}")
    fixture.sort(key=lambda r: (r["posted_at"], r["thread_id"], r["round_index"]))
    labels.sort(key=lambda r: (r["posted_at"], r["thread_id"], r["round_index"]))
    gate_kinds: Counter[str] = Counter()
    for r in fixture:
        g = r["gate_result"]
        gate_kinds[
            "gate_none"
            if g is None
            else f"{g['verdict']}/{g['kind'] or '-'}/{g['bounce_reason'] or '-'}"
        ] += 1
    manifest: dict[str, Any] = {
        "eval_since": EVAL_SINCE.isoformat(),
        "eval_until": EVAL_UNTIL.isoformat(),
        "projects": list(dict.fromkeys(p for p, _, _ in threads)),
        "threads_scanned": len(threads),
        "rows": len(fixture),
        "by_set": dict(Counter(r["set"] for r in fixture)),
        "by_author": dict(Counter(r["author"] for r in fixture).most_common()),
        "roster_source": dict(Counter(r["roster_source"] for r in fixture)),
        "live_entry": dict(Counter(str(r["live_entry"]) for r in fixture)),
        "grey_zone": sum(
            1 for r in fixture if r["gate_result"] and r["gate_result"]["is_grey_zone"]
        ),
        "gate": dict(gate_kinds.most_common()),
    }
    return fixture, labels, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--project", action="append", default=None)
    parser.add_argument(
        "--labeller",
        action="append",
        default=None,
        help="Blank template per labeller (default: Bohr, Einstein — msg-4224).",
    )
    args = parser.parse_args(argv)

    from spirrow_mindwire.config import load_settings

    current_roster = dict(load_settings().conductor.roster)
    threads = asyncio.run(harvest(tuple(args.project or PROJECTS)))
    fixture, labels, manifest = build_outputs(threads, current_roster)
    manifest["harvested_at"] = datetime.now(UTC).isoformat()
    manifest["current_roster"] = {k: v.value for k, v in current_roster.items()}

    args.out_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(args.out_dir / "fixture.jsonl", fixture)
    for who in args.labeller or ["Bohr", "Einstein"]:
        _write_jsonl(args.out_dir / f"labels.{who.lower()}.jsonl", labels)
    (args.out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
