"""U4b acceptance scan: the decisions log against the decision lines in thread message bodies.

Spec: T-tier-c-admission-gate msg-5655 (U4b), msg-6244 / 6246 / 6248 / 6250 / 6252 / 6254 / 6256
(the comparison), endorsed by Einstein msg-6257. The comparison itself lives in
:mod:`spirrow_mindwire.decision_log_audit`; this script only fetches threads read-only, reads the
log, and writes counts.

**Output.** Per thread and in total: the counts of :class:`ThreadAudit` plus the ``msg_id`` of
every non-zero row; each ``missing`` row carries its thread's status. Also, from the log alone,
the ``DECIDED`` / ``DEFERRED`` / ``BOUNCED`` totals for the scanned threads. There is no pass/fail
verdict (msg-6252). A source that is absent is printed as ``unmeasured: <reason>``, never as 0
(msg-5655 "no silent zeros").

**Reader and surface.** The reader is Bohr, who posts the result in T-tier-c-admission-gate
himself. This script reads chatroom threads (``chatroom_list_threads`` / ``chatroom_get_thread``)
and never posts to any thread, so no chatroom fallback surface applies; its only output is the
``--out`` JSON (LF) and stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from spirrow_mindwire.decision_log_audit import ThreadAudit, audit_thread
from spirrow_mindwire.tier_c_admission_gate import LogKind
from spirrow_mindwire.tier_c_decisions_log import logged_decision_keys_by_thread
from spirrow_mindwire.value_objects import Role

_PAGE = 200


def log_kind_totals(log_path: Path, threads: set[str]) -> dict[str, int | str]:
    """``DECIDED`` / ``DEFERRED`` / ``BOUNCED`` row counts for ``threads``; unmeasured if no log."""
    kinds = (LogKind.DECIDED.value, LogKind.DEFERRED.value, LogKind.BOUNCED.value)
    if not log_path.exists():
        return dict.fromkeys(kinds, f"unmeasured: decisions log not found at {log_path}")
    counts: Counter[str] = Counter()
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("thread") in threads and row.get("kind") in kinds:
            counts[str(row["kind"])] += 1
    return {k: counts[k] for k in kinds}


def summarise(audits: Sequence[ThreadAudit]) -> dict[str, Any]:
    """Totals over ``audits`` plus every thread that is not all-zero."""
    total: Counter[str] = Counter()
    for a in audits:
        total.update(a.counts())
    keys = (
        "expected",
        "logged",
        "missing",
        "roster_changed",
        "unmatched_log_row",
        "malformed",
        "unattributed_author",
    )
    return {
        "threads_scanned": len(audits),
        "totals": {k: total[k] for k in keys},
        "threads": [a.to_json() for a in audits if not a.clean],
    }


async def list_threads(
    call_tool: Callable[[str, dict[str, Any]], Awaitable[Any]], project: str
) -> list[dict[str, Any]]:
    """Every thread of ``project``, paging ``chatroom_list_threads`` until the list is exhausted.

    Stops on an empty page; else, when the response carries an integer ``total``, once ``offset``
    reaches it; else (no ``total``) on a short page. Defaulting a missing ``total`` to the count
    read so far would stop after the first page every time (PR #449 gate, finding 1).
    """
    threads: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await call_tool(
            "chatroom_list_threads", {"project": project, "limit": _PAGE, "offset": offset}
        )
        items = (page.get("items") or []) if isinstance(page, dict) else []
        threads.extend(t for t in items if isinstance(t, dict))
        offset += len(items)
        total = page.get("total") if isinstance(page, dict) else None
        if not items:
            break
        if isinstance(total, int) and not isinstance(total, bool):
            if offset >= total:
                break
        elif len(items) < _PAGE:
            break
    return threads


async def _fetch(
    projects: Sequence[str], only: set[str] | None
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    from spirrow_mindwire.magickit.client import StreamableHttpChatroomMcp

    mcp = StreamableHttpChatroomMcp()
    out: list[tuple[str, str, list[dict[str, Any]]]] = []
    for project in projects:
        for t in await list_threads(mcp.call_tool, project):
            thread_id = str(t.get("thread_id", ""))
            if only is not None and thread_id not in only:
                continue
            body = await mcp.call_tool(
                "chatroom_get_thread", {"project": project, "thread_id": thread_id, "mode": "full"}
            )
            msgs = body.get("messages") or [] if isinstance(body, dict) else []
            status = str((body.get("thread") or {}).get("status", t.get("status", "unknown")))
            out.append((thread_id, status, [m for m in msgs if isinstance(m, dict)]))
    return out


def scan(
    fetched: Sequence[tuple[str, str, list[dict[str, Any]]]],
    *,
    log_path: Path,
    roster: Mapping[str, Role],
) -> dict[str, Any]:
    """The whole report for already-fetched threads. Pure apart from reading ``log_path``."""
    if not log_path.exists():
        return {"comparison": f"unmeasured: decisions log not found at {log_path}"}
    logged = logged_decision_keys_by_thread(log_path, threads={t for t, _, _ in fetched})
    audits = [
        audit_thread(
            thread=thread,
            status=status,
            messages=msgs,
            logged=logged[thread],
            roster=roster,
        )
        for thread, status, msgs in fetched
    ]
    report = summarise(audits)
    report["log_kinds"] = log_kind_totals(log_path, {t for t, _, _ in fetched})
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", action="append", required=True)
    parser.add_argument("--thread", action="append", default=None, help="limit to these ids")
    parser.add_argument("--log", type=Path, default=None, help="default: the configured log")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    from spirrow_mindwire.config import load_settings, resolve_tier_c_decisions_log_path

    settings = load_settings()
    log_path = args.log or resolve_tier_c_decisions_log_path(settings)
    fetched = asyncio.run(_fetch(args.project, set(args.thread) if args.thread else None))
    report = scan(fetched, log_path=log_path, roster=dict(settings.conductor.roster))
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(text.encode("utf-8"))
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
