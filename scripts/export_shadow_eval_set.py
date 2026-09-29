"""Export the Tier-C production-shadow rows as an evaluation set — T-decider-conductor-hook step 2d.

Spec: Bohr msg-4634 DECIDED 2d-2 (export → lock → label → measure; Jev's output in a separate
file the labellers never read; negative test on the labeller file) and msg-4636 DECIDED 2d-3
(read the point-in-time input the hook logged instead of rebuilding it; the counted scope is the
rows that carry ``state_wire`` + ``latest_msg_id`` with the registered ``rules_sha256``; the
labeller material is cut at ``latest_msg_id``; the Jev file is ``--replay``-shaped, the fixture
``--fixture``-shaped). Einstein approved msg-4636.

**Nothing is rebuilt.** This script never calls ``state_builder`` / ``turn_from_messages`` and
imports nothing from ``spirrow_mindwire.decider`` (pinned by a test): the Jev-side ``state`` is
``json.loads`` of the ``state_wire`` string the hook logged — the bytes Lexora was sent — and the
roster is the one the hook logged. Rebuilding weeks later would read a thread that has grown,
today's roster and today's builder (msg-4636 §確かめた事実 1-3).

**Inputs.**

* ``--log PATH`` (repeatable) — conductor log files; every line containing ``decider_decision ``
  is a hook row (``hook.log_decision``: the JSON after that prefix);
* ``--rules-sha256 HEX`` — the ``rules_sha256`` registered in ``eval/tierc/shadow-prereg.md``;
* ``--project NAME`` (repeatable) — chatroom projects to fetch the counted rows' threads from
  (for the labeller material only).

**Scope (msg-4636 DECIDED 2d-3).** A row is *counted* when it has ``state_wire`` and
``latest_msg_id``, ``routed == "stop"`` and ``rules_sha256`` equals ``--rules-sha256``. Every
other row is only counted by reason (:func:`classify`): no point-in-time columns (written before
this change was deployed), ``forced_naysayer`` / ``spawn_blocked`` (the separate table msg-4634
2d-1 asks for), a different ``rules_sha256``.

**Outputs.**

* ``<out-dir>/materials.jsonl`` — what the labellers read (``label_eval_set.py --dir``): the
  ``latest_msg_id`` message and the 5 before it, **nothing after it** (``following`` is always
  empty). No Jev column appears here (pinned by a negative test);
* ``<out-dir>/fixture.jsonl`` — the ``tierc_eval_report.py --fixture`` join rows (thread / author /
  ``msg_id`` = ``latest_msg_id`` / ``roster_source=logged``). No Jev column either;
* ``--replay-out`` — the Jev side in ``decider_replay.py --endpoint`` record shape (``state`` +
  ``decision``), read by ``tierc_eval_report.py --replay``. It must not be inside ``--out-dir``;
* ``<out-dir>/export.json`` — the counts by reason and the inputs used.

**Join key.** ``tierc_eval_report.py`` joins on ``(thread_id, round_index)``. The conductor's
``round_index`` is its per-run loop counter and restarts at 0 on every dispatch, so two
escalations in one thread can share it. As in the replay fixture (``build_tierc_eval_fixture.py``)
the exported ``round_index`` is therefore the ``latest_msg_id`` message's 0-based position in its
thread — stable and unique per thread; the logged counter is kept as ``conductor_round_index``.
Two counted rows for the same ``latest_msg_id`` stop the export (:class:`ExportError`).

**Reader of the output.** Files only; nothing is posted to any chatroom thread.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG_PREFIX = "decider_decision "
ROUTED_STOP = "stop"
EVAL_SET_SHADOW = "shadow"
ROSTER_SOURCE_LOGGED = "logged"

COUNTED = "counted"
NO_POINT_IN_TIME = "no_point_in_time_columns"
RULES_SHA_MISMATCH = "rules_sha256_mismatch"
REASONS: tuple[str, ...] = (COUNTED, NO_POINT_IN_TIME, RULES_SHA_MISMATCH)
""":func:`classify` also returns ``routed:<value>`` for a row ``_route`` did not stop."""

MATERIAL_BODY_MAX = 8000
MATERIAL_PRIOR_N = 5
MATERIAL_HEAD_M = 500
"""Same material shape as the replay's ``build_tierc_eval_fixture.material_row`` (msg-4229 §2),
except ``following``, which is always empty here (msg-4636 DECIDED 2d-3)."""

JEV_COLUMNS: frozenset[str] = frozenset(
    {
        "outcome",
        "decision_id",
        "provider",
        "raw_answers",
        "verdict",
        "policy",
        "questions_version",
        "latency_ms",
        "error",
        "matched_rule",
        "matched_rule_source",
        "matched_rule_error",
        "rules_sha256",
        "decision",
        "state",
        "state_wire",
    }
)
"""Every column that carries Jev's input or output (``decision_result_to_dict`` keys + the
state). None may appear in a labeller-facing file (msg-4634 DECIDED 2d-2)."""


class ExportError(ValueError):
    """The log cannot be exported as one evaluation set. Always fatal."""


@dataclass(frozen=True)
class Message:
    msg_id: str
    author: str
    content: str
    timestamp: str


# ---------------------------------------------------------------------------
# log rows
# ---------------------------------------------------------------------------


def parse_log_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Every ``decider_decision {json}`` record in ``lines``, in order."""
    out: list[dict[str, Any]] = []
    for line in lines:
        at = line.find(LOG_PREFIX)
        if at < 0:
            continue
        payload = line[at + len(LOG_PREFIX) :].strip()
        try:
            rec = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ExportError(
                f"unparseable decider_decision record: {exc}: {payload[:120]}"
            ) from exc
        if isinstance(rec, dict):
            out.append(rec)
    return out


def classify(row: Mapping[str, Any], rules_sha256: str) -> str:
    """msg-4636 DECIDED 2d-3: ``counted``, or why not."""
    if not row.get("state_wire") or not row.get("latest_msg_id"):
        return NO_POINT_IN_TIME
    if row.get("routed") != ROUTED_STOP:
        return f"routed:{row.get('routed')}"
    if row.get("rules_sha256") != rules_sha256:
        return RULES_SHA_MISMATCH
    return COUNTED


def select(
    rows: Sequence[Mapping[str, Any]], rules_sha256: str
) -> tuple[list[Mapping[str, Any]], Counter[str]]:
    """The counted rows (log order) and the count of every row by :func:`classify` reason."""
    counts: Counter[str] = Counter()
    counted: list[Mapping[str, Any]] = []
    for r in rows:
        reason = classify(r, rules_sha256)
        counts[reason] += 1
        if reason == COUNTED:
            counted.append(r)
    seen: Counter[str] = Counter(str(r["latest_msg_id"]) for r in counted)
    if dup := sorted(m for m, n in seen.items() if n > 1):
        raise ExportError(f"more than one counted row for latest_msg_id {dup}")
    return counted, counts


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------


def _cut(thread: Sequence[Message], latest_msg_id: str) -> tuple[list[Message], int]:
    """``thread`` up to and including ``latest_msg_id`` (its index). Nothing after it."""
    for i, m in enumerate(thread):
        if m.msg_id == latest_msg_id:
            return list(thread[: i + 1]), i
    raise ExportError(f"{latest_msg_id} is not in the fetched thread")


def _head(m: Message) -> dict[str, str]:
    return {"msg_id": m.msg_id, "author": m.author, "head": m.content[:MATERIAL_HEAD_M]}


def material_row(row: Mapping[str, Any], project: str, thread: Sequence[Message]) -> dict[str, Any]:
    """What a labeller reads — the thread cut at ``latest_msg_id`` (msg-4636 DECIDED 2d-3)."""
    upto, i = _cut(thread, str(row["latest_msg_id"]))
    head = upto[i]
    body = head.content
    return {
        "thread_id": str(row["thread_id"]),
        "round_index": i,
        "project": project,
        "msg_id": head.msg_id,
        "author": head.author,
        "posted_at": head.timestamp,
        "eval_set": EVAL_SET_SHADOW,
        "body": body[:MATERIAL_BODY_MAX],
        "body_truncated": len(body) > MATERIAL_BODY_MAX,
        "body_chars": len(body),
        "prior": [_head(m) for m in upto[max(0, i - MATERIAL_PRIOR_N) : i]],
        "following": [],
    }


def fixture_row(material: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """The ``tierc_eval_report.py --fixture`` join row. Label-side metadata only — no Jev column."""
    return {
        "thread_id": material["thread_id"],
        "round_index": material["round_index"],
        "conductor_round_index": row.get("round_index"),
        "project": material["project"],
        "msg_id": material["msg_id"],
        "author": material["author"],
        "posted_at": material["posted_at"],
        "eval_set": EVAL_SET_SHADOW,
        "live_entry": True,
        "roster": dict(row.get("roster") or {}),
        "roster_source": ROSTER_SOURCE_LOGGED,
    }


_DECISION_KEYS: tuple[str, ...] = (
    "outcome",
    "decision_id",
    "provider",
    "raw_answers",
    "verdict",
    "policy",
    "questions_version",
    "latency_ms",
    "error",
    "matched_rule",
    "matched_rule_source",
    "matched_rule_error",
    "rules_sha256",
)


def replay_record(row: Mapping[str, Any], round_index: int) -> dict[str, Any]:
    """The Jev side, ``decider_replay.py --endpoint`` record shape (``tierc_eval_report --replay``).

    ``state`` is ``json.loads(state_wire)`` — the logged string, not a rebuilt state."""
    return {
        "thread_id": str(row["thread_id"]),
        "round_index": round_index,
        "conductor_round_index": row.get("round_index"),
        "msg_id": str(row["latest_msg_id"]),
        "questions_version": row.get("questions_version"),
        "state": json.loads(str(row["state_wire"])),
        "decision": {k: row.get(k) for k in _DECISION_KEYS},
    }


def build_outputs(
    rows: Sequence[Mapping[str, Any]],
    rules_sha256: str,
    threads: Mapping[str, tuple[str, Sequence[Message]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Pure: log rows + fetched threads → (materials, fixture, replay, export summary).

    ``threads`` maps ``thread_id`` → ``(project, messages oldest → newest)``."""
    counted, counts = select(rows, rules_sha256)
    materials: list[dict[str, Any]] = []
    fixture: list[dict[str, Any]] = []
    replay: list[dict[str, Any]] = []
    for r in counted:
        tid = str(r["thread_id"])
        if tid not in threads:
            raise ExportError(f"thread {tid} was not fetched")
        project, msgs = threads[tid]
        m = material_row(r, project, msgs)
        materials.append(m)
        fixture.append(fixture_row(m, r))
        replay.append(replay_record(r, int(m["round_index"])))
    summary = {
        "rules_sha256": rules_sha256,
        "rows_read": len(rows),
        "by_reason": dict(sorted(counts.items())),
        "counted": len(counted),
    }
    return materials, fixture, replay, summary


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


async def fetch_threads(
    thread_ids: Iterable[str], projects: Sequence[str]
) -> tuple[dict[str, tuple[str, list[Message]]], dict[str, list[str]]]:
    """Fetch each thread from the first project that has it (read-only chatroom calls).

    Returns the threads found and, per thread not found, the error from each project tried —
    so a missing thread is reported with its cause instead of silently skipped."""
    from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp

    mcp = StreamableHttpChatroomMcp()
    out: dict[str, tuple[str, list[Message]]] = {}
    errors: dict[str, list[str]] = {}
    for tid in sorted(set(thread_ids)):
        for project in projects:
            try:
                body = await mcp.call_tool(
                    "chatroom_get_thread",
                    {"project": project, "thread_id": tid, "mode": "full"},
                )
            except Exception as exc:
                errors.setdefault(tid, []).append(f"{project}: {type(exc).__name__}: {exc}")
                continue
            msgs = [
                Message(
                    msg_id=str(m.get("msg_id", "")),
                    author=str(m.get("author", "")),
                    content=str(m.get("content", "")),
                    timestamp=str(m.get("timestamp", "")),
                )
                for m in (body.get("messages") or [])
                if isinstance(m, dict)
            ]
            if msgs:
                out[tid] = (project, msgs)
                errors.pop(tid, None)
                break
            errors.setdefault(tid, []).append(f"{project}: no messages")
    return out, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--log", type=Path, action="append", required=True)
    parser.add_argument("--rules-sha256", required=True)
    parser.add_argument("--project", action="append", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--replay-out", type=Path, required=True)
    args = parser.parse_args(argv)

    out_dir: Path = args.out_dir.resolve()
    replay_out: Path = args.replay_out.resolve()
    if replay_out.parent == out_dir or out_dir in replay_out.parents:
        print(
            "export_shadow_eval_set: --replay-out must be outside --out-dir "
            "(the labellers' directory never holds Jev's output)",
            file=sys.stderr,
        )
        return 2
    rows: list[dict[str, Any]] = []
    for path in args.log:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            rows.extend(parse_log_lines(fh))
    try:
        counted, _ = select(rows, args.rules_sha256)
        threads, errors = asyncio.run(
            fetch_threads((str(r["thread_id"]) for r in counted), args.project)
        )
        for tid, errs in sorted(errors.items()):
            print(f"export_shadow_eval_set: {tid} not fetched: {errs}", file=sys.stderr)
        materials, fixture, replay, summary = build_outputs(rows, args.rules_sha256, threads)
    except ExportError as exc:
        print(f"export_shadow_eval_set: {exc}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    replay_out.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out_dir / "materials.jsonl", materials)
    _write_jsonl(out_dir / "fixture.jsonl", fixture)
    _write_jsonl(replay_out, replay)
    summary["logs"] = [str(p) for p in args.log]
    summary["projects"] = list(args.project)
    (out_dir / "export.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
