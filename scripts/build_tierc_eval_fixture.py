"""Build the Tier-C replay-evaluation fixture from past chatroom escalations.

Spec: T-decider-tierc-replay-eval — Bohr msg-4219 §2 (design), msg-4224 (double-blind labels,
no ``decision_request`` import), msg-4226 (three future-leak paths sealed), approved by Einstein
msg-4225 / msg-4227.

**What it does.** Reads every thread of the chatroom projects, picks every message the
conductor would resolve to ``NEXT: human`` (``resolve_handoff`` — the conductor's own resolver,
not a second parser), and turns each into one replay fixture row that ``decider_replay.py``
reads unchanged (``parse_turn`` shape). The ground truth never enters the fixture (msg-4219 §2:
labels live in a separate file keyed by ``(thread_id, round_index)``, joined only by
``tierc_eval_report.py``).

**Outputs (msg-4229 §1 / §2):** ``population.jsonl`` — every harvested row with its gate result
only (no Jev call; the (B) evidence); ``fixture.jsonl`` — the ``body`` rows (eval window, live
entry check passes) plus ``supplement`` rows (retrospective candidates, gate-ADMIT rows), the
replay input; ``materials.jsonl`` — what the labellers read for the same rows, including the 3
messages *after* the escalation, which appear nowhere else; ``harvest.json`` — counts.

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
import hashlib
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


def routing_roster(
    upto: Sequence[RawMessage], current_roster: Mapping[str, Role]
) -> dict[str, Role]:
    """The roster ``resolve_handoff`` routes against when deciding *whether* a turn stops.

    The live conductor resolves every handoff against its configured roster (``current_roster``
    here), not an empty one. ``resolve_handoff`` is not roster-free: a ``next_participant`` field
    naming a persona resolves to ``FIELD_UNRESOLVABLE`` → ``HUMAN`` against ``{}`` but to that
    persona against the real roster, and a sentinel field with a ``NEXT: <persona>`` body flips
    the other way. So selection must use the conductor's roster; the authors seen so far
    (seal (b)'s historical roles) are overlaid so a persona since dropped from the config still
    resolves. This roster is for routing only — the state's ``roster`` stays seal (b)'s.
    """
    roster = dict(current_roster)
    roster.update(historical_roster(upto, current_roster)[0])
    return roster


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

    handoff = resolve_handoff(
        head.content,
        routing_roster(upto, current_roster),
        next_participant=head.next_participant,
    )
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
) -> list[tuple[dict[str, Any], int]]:
    out: list[tuple[dict[str, Any], int]] = []
    for i, m in enumerate(messages):
        handoff = resolve_handoff(
            m.content,
            routing_roster(messages[: i + 1], current_roster),
            next_participant=m.next_participant,
        )
        if handoff.kind is not HandoffKind.HUMAN:
            continue
        label = handoff.tier_c_label
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
        out.append((row, i))
    return out


EVAL_BODY = "body"
EVAL_SUPPLEMENT = "supplement"
EVAL_GATE_ONLY = "gate_only"

MATERIAL_BODY_MAX = 8000
"""msg-4229 §2: the escalation's full text, capped at 8,000 chars (truncation is stated)."""
MATERIAL_PRIOR_N = 5
MATERIAL_FOLLOWING_N = 3
MATERIAL_HEAD_M = 500


def eval_set_of(row: Mapping[str, Any]) -> str:
    """msg-4229 §1: body = the eval-window rows the live entry check admits; supplement = the
    retrospective candidates and every gate-ADMIT row not already in the body (the genuine side
    is thin); everything else is gate-only (no Jev call — the (B)=0 evidence)."""
    if row["set"] == SET_EVAL and row["live_entry"]:
        return EVAL_BODY
    gate = row.get("gate_result")
    if row["set"] == SET_RETRO or (gate is not None and gate.get("verdict") == "admit"):
        return EVAL_SUPPLEMENT
    return EVAL_GATE_ONLY


def _head(m: RawMessage) -> dict[str, str]:
    return {"msg_id": m.msg_id, "author": m.author, "head": m.content[:MATERIAL_HEAD_M]}


def material_row(row: Mapping[str, Any], messages: Sequence[RawMessage], i: int) -> dict[str, Any]:
    """What a labeller reads for one row (msg-4229 §2 / msg-4231). Identical for both labellers.

    ``following`` is the 3 messages **after** the escalation — labelling-only evidence of what
    the human actually did. It is written here and nowhere else: never into the fixture, never
    to Jev (seal (a) is about the fixture; this file is not an input to the replay).
    """
    head = messages[i]
    body = head.content
    truncated = len(body) > MATERIAL_BODY_MAX
    return {
        "thread_id": row["thread_id"],
        "round_index": row["round_index"],
        "project": row["project"],
        "msg_id": row["msg_id"],
        "author": row["author"],
        "posted_at": row["posted_at"],
        "eval_set": row["eval_set"],
        "body": body[:MATERIAL_BODY_MAX],
        "body_truncated": truncated,
        "body_chars": len(body),
        "prior": [_head(m) for m in messages[max(0, i - MATERIAL_PRIOR_N) : i]],
        "following": [_head(m) for m in messages[i + 1 : i + 1 + MATERIAL_FOLLOWING_N]],
    }


def population_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """The compact gate-only record kept for every harvested row (the (B) evidence)."""
    return {
        k: row[k]
        for k in (
            "thread_id",
            "round_index",
            "project",
            "msg_id",
            "author",
            "posted_at",
            "set",
            "eval_set",
            "live_entry",
            "author_wrote_next_human",
            "tier_c_label",
            "gate_result",
        )
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Pure: harvested threads → (population, Jev fixture, labelling materials, manifest).

    * population — every harvested row, gate result only (no Jev call);
    * fixture — body + supplement rows, the ``decider_replay`` input;
    * materials — the same rows, what the labellers read (``following`` lives only here).
    """
    everything: list[tuple[dict[str, Any], Sequence[RawMessage], int]] = []
    for project, thread_id, msgs in threads:
        for row, i in rows_for_thread(
            project=project, thread_id=thread_id, messages=msgs, current_roster=current_roster
        ):
            row["eval_set"] = eval_set_of(row)
            everything.append((row, msgs, i))
    keys = Counter((r["thread_id"], r["round_index"]) for r, _, _ in everything)
    dup = sorted(k for k, n in keys.items() if n > 1)
    if dup:
        # The join key (msg-4219 §2) has no project. Same-named threads in two projects are
        # fine unless both produce a row at the same position — then the key would merge them.
        raise ValueError(f"duplicate (thread_id, round_index) across projects: {dup}")
    everything.sort(key=lambda t: (t[0]["posted_at"], t[0]["thread_id"], t[0]["round_index"]))
    population = [population_row(r) for r, _, _ in everything]
    chosen = [(r, m, i) for r, m, i in everything if r["eval_set"] != EVAL_GATE_ONLY]
    fixture = [r for r, _, _ in chosen]
    materials = [material_row(r, m, i) for r, m, i in chosen]
    gate_kinds: Counter[str] = Counter()
    for r in population:
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
        "rows": len(population),
        "by_set": dict(Counter(r["set"] for r in population)),
        "by_eval_set": dict(Counter(r["eval_set"] for r in population)),
        "by_author": dict(Counter(r["author"] for r in population).most_common()),
        "roster_source": dict(Counter(r["roster_source"] for r in fixture)),
        "live_entry": dict(Counter(str(r["live_entry"]) for r in population)),
        "grey_zone": sum(
            1 for r in population if r["gate_result"] and r["gate_result"]["is_grey_zone"]
        ),
        "gate": dict(gate_kinds.most_common()),
        "materials_body_truncated": sum(1 for m in materials if m["body_truncated"]),
    }
    return population, fixture, materials, manifest


def _fixture_line(row: Mapping[str, Any]) -> str:
    """One fixture row exactly as :func:`_write_jsonl` writes it (byte comparison, msg-4298 §3)."""
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def as_of(
    threads: Sequence[tuple[str, str, Sequence[RawMessage]]], cutoff: datetime
) -> list[tuple[str, str, list[RawMessage]]]:
    """The harvest's view: drop every message posted after ``cutoff`` (and threads left empty)."""
    out = [(p, t, [m for m in msgs if m.timestamp <= cutoff]) for p, t, msgs in threads]
    return [(p, t, msgs) for p, t, msgs in out if msgs]


def _entry(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in ("project", "thread_id", "round_index", "msg_id")}


def corrections_for(
    *,
    original_fixture_text: str,
    threads: Sequence[tuple[str, str, Sequence[RawMessage]]],
    current_roster: Mapping[str, Role],
    selection_code_commit: str,
) -> dict[str, Any]:
    """msg-4298 §2-§3: which rows of an already-replayed fixture the fixed selection still yields.

    ``threads`` must be the harvest's view (:func:`as_of`) and ``current_roster`` the roster the
    harvest used. A row is **kept** only if the rebuilt fixture line is byte-identical to the
    original — then what Jev was sent is what the fixed code would send. Every other row is
    **excluded** with a reason: ``not_an_escalation`` (with the old and new resolution),
    ``moved_to_gate_only``, or ``rebuilt_row_differs`` (with the differing fields). This function
    never re-labels a changed row; it excludes it. ``added`` lists rebuilt fixture rows the
    original lacks — they were never replayed, so a report cannot use them.
    """
    orig = [json.loads(line) for line in original_fixture_text.splitlines() if line.strip()]
    orig_line = {(r["thread_id"], r["round_index"]): _fixture_line(r) for r in orig}
    population, fixture, _, _ = build_outputs(threads, current_roster)
    new_pop = {(r["thread_id"], r["round_index"]) for r in population}
    new_fix = {(r["thread_id"], r["round_index"]): r for r in fixture}
    msgs_of = {t: msgs for _, t, msgs in threads}
    keep: list[dict[str, Any]] = []
    exclude: list[dict[str, Any]] = []
    for r in orig:
        k = (r["thread_id"], r["round_index"])
        if k in new_fix and _fixture_line(new_fix[k]) == orig_line[k]:
            keep.append(_entry(r))
            continue
        entry = _entry(r)
        if k not in new_pop:
            msgs = msgs_of[r["thread_id"]]
            m = msgs[r["round_index"]]
            old = resolve_handoff(m.content, {}, next_participant=m.next_participant)
            now = resolve_handoff(
                m.content,
                routing_roster(msgs[: r["round_index"] + 1], current_roster),
                next_participant=m.next_participant,
            )
            entry["reason"] = "not_an_escalation"
            entry["detail"] = {
                "next_participant": m.next_participant,
                "empty_roster": f"{old.kind.value}/{old.mismatch_reason or '-'}",
                "conductor_roster": f"{now.kind.value}/{now.identity or now.token or '-'}",
            }
        elif k not in new_fix:
            entry["reason"] = "moved_to_gate_only"
        else:
            n = new_fix[k]
            entry["reason"] = "rebuilt_row_differs"
            entry["detail"] = {"fields": sorted(f for f in set(r) | set(n) if r.get(f) != n.get(f))}
        exclude.append(entry)
    added = [_entry(new_fix[k]) for k in sorted(set(new_fix) - set(orig_line))]
    return {
        "schema": 1,
        "fixture_sha256": hashlib.sha256(original_fixture_text.encode("utf-8")).hexdigest(),
        "selection_code_commit": selection_code_commit,
        "roster": {k: v.value for k, v in current_roster.items()},
        "counts": {
            "fixture": len(orig),
            "keep": len(keep),
            "exclude": dict(Counter(e["reason"] for e in exclude)),
            "added": len(added),
        },
        "keep": keep,
        "exclude": exclude,
        "added": added,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--project", action="append", default=None)
    corr = parser.add_argument_group(
        "corrections mode (msg-4298): check an already-replayed fixture against today's selection"
    )
    corr.add_argument("--corrections-for", type=Path, default=None, help="the original fixture")
    corr.add_argument(
        "--reproduce", type=Path, default=None, help="its harvest.json (harvested_at + roster)"
    )
    corr.add_argument("--corrections-out", type=Path, default=None)
    corr.add_argument("--selection-code-commit", default=None)
    args = parser.parse_args(argv)

    if args.corrections_for is not None:
        if None in (args.reproduce, args.corrections_out, args.selection_code_commit):
            parser.error(
                "--corrections-for needs --reproduce, --corrections-out, --selection-code-commit"
            )
        hv = json.loads(args.reproduce.read_text(encoding="utf-8"))
        roster = {k: Role(v) for k, v in hv["current_roster"].items()}
        harvested = asyncio.run(harvest(tuple(args.project or hv.get("projects") or PROJECTS)))
        out = corrections_for(
            original_fixture_text=args.corrections_for.read_bytes().decode("utf-8"),
            threads=as_of(harvested, parse_timestamp(hv["harvested_at"])),
            current_roster=roster,
            selection_code_commit=args.selection_code_commit,
        )
        args.corrections_out.parent.mkdir(parents=True, exist_ok=True)
        args.corrections_out.write_text(
            json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(out["counts"], ensure_ascii=False), file=sys.stderr)
        return 0
    if args.out_dir is None:
        parser.error("--out-dir is required (or use --corrections-for)")

    from spirrow_mindwire.config import load_settings

    current_roster = dict(load_settings().conductor.roster)
    threads = asyncio.run(harvest(tuple(args.project or PROJECTS)))
    population, fixture, materials, manifest = build_outputs(threads, current_roster)
    manifest["harvested_at"] = datetime.now(UTC).isoformat()
    manifest["current_roster"] = {k: v.value for k, v in current_roster.items()}

    args.out_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(args.out_dir / "population.jsonl", population)
    _write_jsonl(args.out_dir / "fixture.jsonl", fixture)
    _write_jsonl(args.out_dir / "materials.jsonl", materials)
    (args.out_dir / "harvest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
