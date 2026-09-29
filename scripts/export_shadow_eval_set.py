"""Export the Tier-C production-shadow rows as an evaluation set — T-decider-conductor-hook step 2d.

Spec (Bohr, each approved by Einstein): msg-4634 DECIDED 2d-2 (export → lock → label → measure;
Jev's output in a separate file the labellers never read), msg-4636 DECIDED 2d-3 (read the
point-in-time input the hook logged instead of rebuilding it; ``--replay`` / ``--fixture``
shapes), msg-4639 DECIDED 2d-4 / 2d-6 (count ``tierc-v2`` rows only; ``following`` = the 3
messages after, as in the replay), msg-4641 DECIDED 2d-7 (a row with fewer than 3 is counted when
its thread ended with ``NEXT: none`` or has been quiet for 72 h at ``--as-of``, otherwise held)
and msg-4643 DECIDED 2d-8 (everything after ``--as-of`` is dropped first).

**Step 0 — the ``--as-of`` cut (2d-8).** Straight after reading, before any other logic, every
thread message and every ``decider_decision`` row with a timestamp **after** ``--as-of`` is
dropped (``== as_of`` is kept). Everything below — the last message, ``NEXT: none``, the 72-hour
test, ``following`` / ``following_n``, the ``round_index`` position, the duplicate check — reads
only what is left, so the same inputs and the same ``--as-of`` give the same bytes however much
the threads and the log have grown since. ``--as-of`` must be timezone-aware ISO 8601 and not in
the future (a future value would age live threads into "quiet"). A row without ``logged_at``
cannot be placed in time; it is one of the rows without point-in-time columns (below).

**Nothing is rebuilt (2d-3).** This script never calls ``state_builder`` / ``turn_from_messages``
and imports nothing from ``spirrow_mindwire.decider`` itself (pinned by a test): the Jev-side
``state`` is ``json.loads`` of the ``state_wire`` string the hook logged — the bytes Lexora was
sent — and the roster is the one the hook logged. The material constants are imported from
``build_tierc_eval_fixture`` so the replay and the shadow share one definition (2d-6).

**Inputs.** ``--log PATH`` (repeatable; every line containing ``decider_decision `` is a hook
row), ``--rules-sha256 HEX`` (the value registered in ``eval/tierc/shadow-prereg.md``),
``--project NAME`` (repeatable; where to fetch the counted rows' threads), ``--as-of``.

**Scope.** :func:`classify` puts each surviving row in one bucket: no point-in-time columns
(``state_wire`` / ``latest_msg_id`` / ``logged_at``), ``routed:<value>`` for a row ``_route`` did
not stop (the separate forced / spawn-blocked table, 2d-1), a questions version other than
``tierc-v2`` (2d-4), a different ``rules_sha256``, else a candidate. A candidate is ``counted``
when its thread has 3 messages after it, or fewer and the thread is terminated / quiet (2d-7);
otherwise ``held`` — counted by number only, exported on a later ``--as-of``.

**Outputs.**

* ``<out-dir>/materials.jsonl`` — what the labellers read (``label_eval_set.py --dir``): the
  message, the 5 before it and ``following`` (up to 3 after it), ``following_n``. No Jev column;
* ``<out-dir>/fixture.jsonl`` — the ``tierc_eval_report.py --fixture`` join rows (thread /
  author / ``msg_id`` = ``latest_msg_id`` / ``roster_source=logged`` / ``following_n``);
* ``--replay-out`` — the Jev side in ``decider_replay.py --endpoint`` record shape (``state`` +
  ``decision``), read by ``tierc_eval_report.py --replay``. It must not be inside ``--out-dir``;
* ``<out-dir>/export.json`` — the export lock (msg-4646 DECIDED 2d-9): ``as_of``, the counts by
  bucket, the sha256 of ``materials.jsonl`` / ``fixture.jsonl`` / the replay file (checked by
  ``tierc_eval_report.py --export-manifest``, msg-4648 / msg-4650 DECIDED 2d-11), and the name +
  sha256 of the rubric and label prompt copied beside them;
* ``<out-dir>/RUBRIC-v2.md`` and ``<out-dir>/label_prompt-v2.md`` — byte copies of
  ``eval/tierc/RUBRIC-v2.md`` / ``eval/tierc/label_prompt-v2.md`` under the same names (msg-4656
  DECIDED 2d-13). The sources are constants, not arguments. An existing file of that name with
  other bytes stops the export before anything is written; identical bytes are left as they are.
  Label with ``label_eval_set.py --dir <out-dir> --prompt-file label_prompt-v2.md
  --rubric-file RUBRIC-v2.md``.

**Join key.** ``tierc_eval_report.py`` joins on ``(thread_id, round_index)``. The conductor's
``round_index`` is its per-run loop counter and restarts at 0 on every dispatch, so the exported
``round_index`` is the ``latest_msg_id`` message's 0-based position in its thread, as in the
replay fixture (adopted in msg-4639); the logged counter is kept as ``conductor_round_index``.
Two candidate rows for the same ``latest_msg_id`` stop the export (:class:`ExportError`).

**Reader of the output.** Files only; nothing is posted to any chatroom thread.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_tierc_eval_fixture import (  # the sibling script, as tierc_fulltext_run does
    MATERIAL_BODY_MAX,
    MATERIAL_FOLLOWING_N,
    MATERIAL_HEAD_M,
    MATERIAL_PRIOR_N,
)

from spirrow_mindwire.conductor.handoff import parse_next_token

LOG_PREFIX = "decider_decision "
ROUTED_STOP = "stop"
QUESTIONS_V2 = "tierc-v2"
EVAL_SET_SHADOW = "shadow"
ROSTER_SOURCE_LOGGED = "logged"
QUIET_AFTER = timedelta(hours=72)
"""msg-4641 DECIDED 2d-7 condition 2: the thread's last message is at least this old at
``--as-of`` (exactly 72 h counts)."""
TERMINAL_NEXT = "none"

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval" / "tierc"
LABEL_INPUTS: dict[str, Path] = {
    "rubric": EVAL_DIR / "RUBRIC-v2.md",
    "label_prompt": EVAL_DIR / "label_prompt-v2.md",
}
"""msg-4656 DECIDED 2d-13: the v2 rubric / prompt the exporter places in ``--out-dir``. Fixed in
code so the runner cannot substitute another file for the pre-registered one."""
MATERIALS_NAME = "materials.jsonl"
FIXTURE_NAME = "fixture.jsonl"
EXPORT_NAME = "export.json"

COUNTED = "counted"
HELD = "held_following"
NO_POINT_IN_TIME = "no_point_in_time_columns"
NOT_V2 = "questions_version_not_tierc_v2"
RULES_SHA_MISMATCH = "rules_sha256_mismatch"
CANDIDATE = "candidate"
""":func:`classify` returns ``candidate`` / ``routed:<value>`` / one of the reasons above;
``counted`` vs ``held`` is decided later, against the thread (:func:`following_ready`)."""

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
    """The input cannot be exported as one evaluation set. Always fatal."""


@dataclass(frozen=True)
class Message:
    msg_id: str
    author: str
    content: str
    timestamp: str


# ---------------------------------------------------------------------------
# time (2d-8)
# ---------------------------------------------------------------------------


def parse_time(raw: str, what: str) -> datetime:
    """Timezone-aware ISO 8601 → ``datetime``. A naive or unparseable value is an error."""
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExportError(f"{what}: not ISO 8601: {raw!r}") from exc
    if ts.tzinfo is None:
        raise ExportError(f"{what}: no timezone: {raw!r}")
    return ts


def parse_as_of(raw: str, now: datetime) -> datetime:
    """msg-4643 DECIDED 2d-8: timezone-aware, and not after ``now``."""
    as_of = parse_time(raw, "--as-of")
    if as_of > now:
        raise ExportError(f"--as-of {raw} is in the future (now {now.isoformat()})")
    return as_of


def cut_rows(rows: Sequence[Mapping[str, Any]], as_of: datetime) -> list[Mapping[str, Any]]:
    """Drop every row logged after ``as_of``. A row without ``logged_at`` is kept here and falls
    into ``no_point_in_time_columns`` (it predates the column, and so this change)."""
    out: list[Mapping[str, Any]] = []
    for r in rows:
        at = r.get("logged_at")
        if at and parse_time(str(at), "logged_at") > as_of:
            continue
        out.append(r)
    return out


def cut_thread(thread: Sequence[Message], as_of: datetime) -> list[Message]:
    """Drop every message posted after ``as_of`` (``== as_of`` stays)."""
    return [m for m in thread if parse_time(m.timestamp, f"{m.msg_id} timestamp") <= as_of]


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
    """The row-only buckets (2d-3 / 2d-4). ``candidate`` still needs the thread check."""
    if not row.get("state_wire") or not row.get("latest_msg_id") or not row.get("logged_at"):
        return NO_POINT_IN_TIME
    if row.get("routed") != ROUTED_STOP:
        return f"routed:{row.get('routed')}"
    if row.get("questions_version") != QUESTIONS_V2:
        return NOT_V2
    if row.get("rules_sha256") != rules_sha256:
        return RULES_SHA_MISMATCH
    return CANDIDATE


def candidates(
    rows: Sequence[Mapping[str, Any]], rules_sha256: str
) -> tuple[list[Mapping[str, Any]], Counter[str]]:
    """The candidate rows (log order) and every other row counted by bucket."""
    counts: Counter[str] = Counter()
    out: list[Mapping[str, Any]] = []
    for r in rows:
        bucket = classify(r, rules_sha256)
        if bucket == CANDIDATE:
            out.append(r)
        else:
            counts[bucket] += 1
    seen: Counter[str] = Counter(str(r["latest_msg_id"]) for r in out)
    if dup := sorted(m for m, n in seen.items() if n > 1):
        raise ExportError(f"more than one candidate row for latest_msg_id {dup}")
    return out, counts


# ---------------------------------------------------------------------------
# thread side
# ---------------------------------------------------------------------------


def position(thread: Sequence[Message], latest_msg_id: str) -> int:
    for i, m in enumerate(thread):
        if m.msg_id == latest_msg_id:
            return i
    raise ExportError(f"{latest_msg_id} is not in the fetched thread up to --as-of")


def following_ready(thread: Sequence[Message], i: int, as_of: datetime) -> bool:
    """msg-4641 DECIDED 2d-7: 3 messages after ``i``, or the thread ended with ``NEXT: none``,
    or its last message is at least 72 h before ``as_of``. ``thread`` is already cut at
    ``as_of``."""
    if len(thread) - (i + 1) >= MATERIAL_FOLLOWING_N:
        return True
    last = thread[-1]
    token = parse_next_token(last.content)
    if token is not None and token.casefold() == TERMINAL_NEXT:
        return True
    return as_of - parse_time(last.timestamp, f"{last.msg_id} timestamp") >= QUIET_AFTER


def _head(m: Message) -> dict[str, str]:
    return {"msg_id": m.msg_id, "author": m.author, "head": m.content[:MATERIAL_HEAD_M]}


def material_row(
    row: Mapping[str, Any], project: str, thread: Sequence[Message], i: int
) -> dict[str, Any]:
    """What a labeller reads — the replay's ``material_row`` shape plus ``following_n``."""
    head = thread[i]
    body = head.content
    following = [_head(m) for m in thread[i + 1 : i + 1 + MATERIAL_FOLLOWING_N]]
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
        "prior": [_head(m) for m in thread[max(0, i - MATERIAL_PRIOR_N) : i]],
        "following": following,
        "following_n": len(following),
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
        "following_n": material["following_n"],
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
        "logged_at": row.get("logged_at"),
        "questions_version": row.get("questions_version"),
        "state": json.loads(str(row["state_wire"])),
        "decision": {k: row.get(k) for k in _DECISION_KEYS},
    }


def build_outputs(
    rows: Sequence[Mapping[str, Any]],
    rules_sha256: str,
    threads: Mapping[str, tuple[str, Sequence[Message]]],
    as_of: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Pure: log rows + fetched threads → (materials, fixture, replay, export summary).

    ``threads`` maps ``thread_id`` → ``(project, messages oldest → newest)``. The ``as_of`` cut
    is the first thing done to both (2d-8)."""
    kept = cut_rows(rows, as_of)
    cands, counts = candidates(kept, rules_sha256)
    cut: dict[str, tuple[str, list[Message]]] = {
        tid: (project, cut_thread(msgs, as_of)) for tid, (project, msgs) in threads.items()
    }
    materials: list[dict[str, Any]] = []
    fixture: list[dict[str, Any]] = []
    replay: list[dict[str, Any]] = []
    for r in cands:
        tid = str(r["thread_id"])
        if tid not in cut:
            raise ExportError(f"thread {tid} was not fetched")
        project, msgs = cut[tid]
        i = position(msgs, str(r["latest_msg_id"]))
        if not following_ready(msgs, i, as_of):
            counts[HELD] += 1
            continue
        counts[COUNTED] += 1
        m = material_row(r, project, msgs, i)
        materials.append(m)
        fixture.append(fixture_row(m, r))
        replay.append(replay_record(r, i))
    summary = {
        "as_of": as_of.isoformat(),
        "rules_sha256": rules_sha256,
        # Only what survives the cut: a count of rows after ``as_of`` would change as the log
        # grows, and 22d asks for the same bytes on the same ``as_of``.
        "rows_up_to_as_of": len(kept),
        "by_bucket": dict(sorted(counts.items())),
        "counted": counts[COUNTED],
        "held": counts[HELD],
        "following_n_lt_3": sum(1 for m in materials if m["following_n"] < MATERIAL_FOLLOWING_N),
    }
    return materials, fixture, replay, summary


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_label_inputs(out_dir: Path, sources: Mapping[str, Path]) -> None:
    """msg-4656 DECIDED 2d-13: refuse when ``out_dir`` already holds one of the names with other
    bytes. Checked before anything is written, so a refused export leaves no partial output."""
    for src in sources.values():
        dst = out_dir / src.name
        if dst.exists() and dst.read_bytes() != src.read_bytes():
            raise ExportError(f"{dst} exists with other content than {src}; not overwriting")


def write_outputs(
    out_dir: Path,
    replay_out: Path,
    materials: Sequence[Mapping[str, Any]],
    fixture: Sequence[Mapping[str, Any]],
    replay: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    sources: Mapping[str, Path] = LABEL_INPUTS,
) -> dict[str, Any]:
    """Write the three sets, copy the rubric / prompt, then ``export.json`` (2d-9 / 2d-13)."""
    check_label_inputs(out_dir, sources)
    out_dir.mkdir(parents=True, exist_ok=True)
    replay_out.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out_dir / MATERIALS_NAME, materials)
    _write_jsonl(out_dir / FIXTURE_NAME, fixture)
    _write_jsonl(replay_out, replay)
    placed: dict[str, dict[str, str]] = {}
    for key, src in sources.items():
        dst = out_dir / src.name
        if not dst.exists():
            dst.write_bytes(src.read_bytes())
        placed[key] = {"name": src.name, "sha256": _sha256(dst)}
    export: dict[str, Any] = {
        **summary,
        "files": {
            "materials": {"name": MATERIALS_NAME, "sha256": _sha256(out_dir / MATERIALS_NAME)},
            "fixture": {"name": FIXTURE_NAME, "sha256": _sha256(out_dir / FIXTURE_NAME)},
            "replay": {"path": str(replay_out), "sha256": _sha256(replay_out)},
        },
        **placed,
    }
    (out_dir / EXPORT_NAME).write_text(
        json.dumps(export, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return export


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
                if isinstance(m, dict) and m.get("timestamp")
            ]
            if msgs:
                out[tid] = (project, msgs)
                errors.pop(tid, None)
                break
            errors.setdefault(tid, []).append(f"{project}: no messages")
    return out, errors


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--log", type=Path, action="append", required=True)
    parser.add_argument("--rules-sha256", required=True)
    parser.add_argument("--project", action="append", required=True)
    parser.add_argument("--as-of", required=True, help="timezone-aware ISO 8601 (msg-4643)")
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
    try:
        as_of = parse_as_of(args.as_of, now if now is not None else datetime.now(UTC))
    except ExportError as exc:
        print(f"export_shadow_eval_set: {exc}", file=sys.stderr)
        return 2
    rows: list[dict[str, Any]] = []
    for path in args.log:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            rows.extend(parse_log_lines(fh))
    try:
        cands, _ = candidates(cut_rows(rows, as_of), args.rules_sha256)
        threads, errors = asyncio.run(
            fetch_threads((str(r["thread_id"]) for r in cands), args.project)
        )
        for tid, errs in sorted(errors.items()):
            print(f"export_shadow_eval_set: {tid} not fetched: {errs}", file=sys.stderr)
        materials, fixture, replay, summary = build_outputs(rows, args.rules_sha256, threads, as_of)
    except ExportError as exc:
        print(f"export_shadow_eval_set: {exc}", file=sys.stderr)
        return 1
    summary["logs"] = [str(p) for p in args.log]
    summary["projects"] = list(args.project)
    try:
        export = write_outputs(out_dir, replay_out, materials, fixture, replay, summary)
    except ExportError as exc:
        print(f"export_shadow_eval_set: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(export, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
