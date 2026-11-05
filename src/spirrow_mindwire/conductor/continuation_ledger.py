"""The continuation ledger — D-3's record of implementer self-continuations.

T-implementer-self-continuation PR-2 (Bohr msg-7971 §2, revised msg-7973 Obj-1-2; naysayer go
from Einstein on the reply to msg-7973). PR #519 put the ``continue n/cap`` lines in the tick
log; this module adds the durable record the weekly view is read from.

One append-only JSONL file, ``<data_dir>/state/continuation_ledger.jsonl``, one row per line.
Two kinds of row, kept apart on purpose so "a continuation was admitted" and "a continuation was
tried" are never the same count (Einstein msg-7972 advisory 2):

* ``continue_admitted`` — a continuation passed every admission condition, the clone check
  included. Written on the report path once the relay is posted, and on the recovery path when a
  relay left at the head is re-admitted (Einstein, the go on msg-7973). Keyed on the **relay's**
  message id, so a relay picked up again after a restart is still one row.
  Fields: ``n``, ``cap``, ``thread``, ``project``, ``relay_msg_id``, ``report_msg_id``.
* ``continue_attempt`` — a continuation that was refused. ``outcome`` is one of
  :class:`AttemptOutcome`: ``dirty`` (the clone check refused a report's continuation), ``cap``
  (``n`` exceeded the cap; the cap notice went to the proposer), ``relay_recovered_dirty`` (a
  relay left at the head was refused by the clone check). Keyed on (``msg_id``, ``outcome``),
  where ``msg_id`` is the report's or the relay's id.

The weekly view (:func:`summarize`, ``mindwire continue-report``) reads this file and nothing
else: there is no second counter to drift from it (msg-7973 Obj-1-2).

Writing is observation, never gating: the conductor logs a failed read or write at WARNING and
routes exactly as it would without the ledger.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..config import CONTINUATION_LEDGER_FILENAME

__all__ = [
    "CONTINUATION_LEDGER_FILENAME",
    "AttemptOutcome",
    "LedgerKind",
    "LedgerRead",
    "ThreadSummary",
    "read_ledger",
    "record_admitted",
    "record_attempt",
    "render_summary",
    "report_text",
    "row_key",
    "summarize",
]


class LedgerKind(StrEnum):
    ADMITTED = "continue_admitted"
    ATTEMPT = "continue_attempt"


class AttemptOutcome(StrEnum):
    DIRTY = "dirty"
    CAP = "cap"
    RELAY_RECOVERED_DIRTY = "relay_recovered_dirty"


RowKey = tuple[str, ...]


def row_key(row: dict[str, Any]) -> RowKey | None:
    """The dedup key of a row; ``None`` for a row that is not a well-formed ledger row."""
    kind = row.get("kind")
    project = row.get("project")
    thread = row.get("thread")
    if not isinstance(project, str) or not isinstance(thread, str):
        return None
    if kind == LedgerKind.ADMITTED:
        relay = row.get("relay_msg_id")
        return (kind, project, thread, relay) if isinstance(relay, str) and relay else None
    if kind == LedgerKind.ATTEMPT:
        msg_id = row.get("msg_id")
        outcome = row.get("outcome")
        if not isinstance(msg_id, str) or not msg_id:
            return None
        if outcome not in {o.value for o in AttemptOutcome}:
            return None
        return (kind, project, thread, msg_id, outcome)
    return None


@dataclass
class LedgerRead:
    """The rows of the ledger, plus the count of lines that were not ledger rows."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    malformed: int = 0


def read_ledger(path: Path) -> LedgerRead:
    """Every well-formed row, in file order. A missing file is an empty ledger.

    A line that is not JSON, or is JSON but not a ledger row (:func:`row_key` is ``None``), is
    counted in ``malformed`` rather than dropped in silence, so the weekly view can say so.
    """
    read = LedgerRead()
    if not path.exists():
        return read
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                read.malformed += 1
                continue
            if not isinstance(row, dict) or row_key(row) is None:
                read.malformed += 1
                continue
            read.rows.append(row)
    return read


def _append_once(path: Path, row: dict[str, Any]) -> bool:
    """Append ``row`` unless a row with the same key is already there. ``True`` = written."""
    key = row_key(row)
    if key is None:  # a caller bug, not data: never write a row the reader would reject
        raise ValueError(f"not a ledger row: {row!r}")
    if any(row_key(r) == key for r in read_ledger(path).rows):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return True


def _now_iso(at: datetime | None) -> str:
    return (at or datetime.now(UTC)).astimezone(UTC).isoformat()


def record_admitted(
    path: Path,
    *,
    project: str,
    thread: str,
    relay_msg_id: str,
    report_msg_id: str,
    n: int,
    cap: int,
    at: datetime | None = None,
) -> bool:
    """Record an admitted continuation, once per relay. ``True`` = a new row was written."""
    return _append_once(
        path,
        {
            "kind": LedgerKind.ADMITTED.value,
            "at": _now_iso(at),
            "project": project,
            "thread": thread,
            "relay_msg_id": relay_msg_id,
            "report_msg_id": report_msg_id,
            "n": n,
            "cap": cap,
        },
    )


def record_attempt(
    path: Path,
    *,
    project: str,
    thread: str,
    msg_id: str,
    outcome: AttemptOutcome,
    n: int,
    cap: int,
    clone: str | None = None,
    at: datetime | None = None,
) -> bool:
    """Record a refused continuation, once per (message, outcome). ``True`` = a new row."""
    row: dict[str, Any] = {
        "kind": LedgerKind.ATTEMPT.value,
        "at": _now_iso(at),
        "project": project,
        "thread": thread,
        "msg_id": msg_id,
        "outcome": outcome.value,
        "n": n,
        "cap": cap,
    }
    if clone is not None:
        row["clone"] = clone
    return _append_once(path, row)


@dataclass
class ThreadSummary:
    """One thread's continuations in the window."""

    project: str
    thread: str
    admitted: int = 0
    max_n: int = 0
    cap_hits: int = 0
    dirty: int = 0
    relay_recovered_dirty: int = 0


def _parse_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def summarize(
    rows: Iterable[dict[str, Any]], *, since: datetime
) -> tuple[list[ThreadSummary], int]:
    """Per-thread counts of the rows at or after ``since``, sorted by (project, thread).

    Returns the summaries and the number of rows skipped because their ``at`` is unreadable.
    """
    by_thread: dict[tuple[str, str], ThreadSummary] = {}
    undated = 0
    for row in rows:
        at = _parse_at(row.get("at"))
        if at is None:
            undated += 1
            continue
        if at < since:
            continue
        key = (row["project"], row["thread"])
        summary = by_thread.setdefault(key, ThreadSummary(project=key[0], thread=key[1]))
        n = row.get("n")
        if row["kind"] == LedgerKind.ADMITTED:
            summary.admitted += 1
            if isinstance(n, int) and not isinstance(n, bool):
                summary.max_n = max(summary.max_n, n)
        elif row["outcome"] == AttemptOutcome.CAP:
            summary.cap_hits += 1
        elif row["outcome"] == AttemptOutcome.DIRTY:
            summary.dirty += 1
        else:
            summary.relay_recovered_dirty += 1
    return [by_thread[k] for k in sorted(by_thread)], undated


def render_summary(
    summaries: list[ThreadSummary], *, since: datetime, malformed: int, undated: int
) -> str:
    """The weekly view: one header line, one line per thread, one totals line."""
    lines = [
        f"continue-report since={since.astimezone(UTC).isoformat()} threads={len(summaries)} "
        f"malformed={malformed} undated={undated}"
    ]
    for s in summaries:
        lines.append(
            f"{s.project}/{s.thread} admitted={s.admitted} max_n={s.max_n} cap_hits={s.cap_hits} "
            f"dirty={s.dirty} relay_recovered_dirty={s.relay_recovered_dirty}"
        )
    lines.append(
        f"total admitted={sum(s.admitted for s in summaries)} "
        f"cap_hits={sum(s.cap_hits for s in summaries)} "
        f"dirty={sum(s.dirty for s in summaries)} "
        f"relay_recovered_dirty={sum(s.relay_recovered_dirty for s in summaries)}"
    )
    return "\n".join(lines)


def report_text(path: Path, *, days: int, now: datetime | None = None) -> str:
    """The report for the last ``days`` days (what ``mindwire continue-report`` prints)."""
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    read = read_ledger(path)
    summaries, undated = summarize(read.rows, since=since)
    return render_summary(summaries, since=since, malformed=read.malformed, undated=undated)
