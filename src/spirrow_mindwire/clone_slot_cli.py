"""``mindwire slot-prep`` and ``mindwire slot-lease`` (T-ephemeral-impl-clone-slot-per-thread, A2).

``slot-prep`` borrows a slot for one turn (:meth:`~.clone_slot_prep.SlotPrep.borrow`), appends a
``slot_quarantine`` row to ``<state-dir>/clone-retreats.jsonl`` when the borrow-time check
quarantined the slot, and prints one line ``MINDWIRE_SLOT_PREP <json>``. Exit codes:

* 0 — ready: the slot is leased, checked, and on the thread's branch;
* 3 — waiting: no free slot; the thread is neither launched nor parked this tick;
* 4 — parked: this thread only, with ``park_code`` in the json;
* 1 — the procedure failed (the wrapper parks with ``clone_slot_prep_crashed``).

``slot-lease`` moves one lease: ``spawning`` / ``running --child-pid N`` / ``release`` /
``quarantine-failed``. Exit 0 done, 1 refused (wrong turn, not leased, unreadable record).

Nothing here posts to a thread: the json is read by the wrapper, which sends notices (A3).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .clone_retreat_log import (
    SLOT_ACTION_PARKED,
    SLOT_ACTION_REBUILT,
    append_slot_quarantine,
    build_slot_quarantine_record,
)
from .clone_slot_lease import LeaseError, LeaseStore
from .clone_slot_prep import PrepOutcome, PrepResult, SlotPrep
from .clone_slot_trust import TrustReadError, is_trusted

PREP_PREFIX = "MINDWIRE_SLOT_PREP "
RETREATS_FILE = "clone-retreats.jsonl"
EXIT_READY = 0
EXIT_FAILED = 1
EXIT_WAITING = 3
EXIT_PARKED = 4


def add_parsers(sub: Any) -> None:
    prep = sub.add_parser("slot-prep", help="borrow a clone slot for one turn (slot pool mode)")
    prep.add_argument("--slots-root", required=True, type=Path)
    prep.add_argument("--repo", required=True)
    prep.add_argument("--origin-url", required=True, help="the GitHub URL the mirror follows")
    prep.add_argument("--state-dir", required=True, type=Path)
    prep.add_argument("--slots-per-repo", required=True, type=int)
    prep.add_argument("--thread-id", required=True)
    prep.add_argument("--turn-id", required=True)
    prep.add_argument("--branch", required=True, help="the thread's branch")
    prep.add_argument("--base", required=True, help="where a new branch starts (mirror/<base>)")
    prep.add_argument("--wrapper-pid", type=int, default=None)
    prep.add_argument("--session-name", default=None)
    prep.add_argument("--job-name", default=None)
    lease = sub.add_parser("slot-lease", help="move one slot lease (slot pool mode)")
    lease.add_argument("action", choices=("spawning", "running", "release", "quarantine-failed"))
    lease.add_argument("--state-dir", required=True, type=Path)
    lease.add_argument("--repo", required=True)
    lease.add_argument("--slot", required=True, help="slot-NN")
    lease.add_argument("--turn-id", required=True)
    lease.add_argument("--child-pid", type=int, default=None, help="(running) the child's pid")


def _trusted(slot: Path) -> bool:
    try:
        return is_trusted(slot)
    except TrustReadError:
        return False


def _record_quarantine(state_dir: Path, thread_id: str, repo: str, res: PrepResult) -> None:
    q = res.quarantine
    if q is None or res.slot is None:
        return
    parked = res.outcome is PrepOutcome.PARKED
    row = build_slot_quarantine_record(
        thread_id=q.previous_thread,
        repo=repo,
        slot_path=res.slot.path,
        action=SLOT_ACTION_PARKED if parked else SLOT_ACTION_REBUILT,
        quarantine_reason=q.reason,
        quarantine_path=q.quarantine_path,
        park_code=res.park_code if parked else None,
        clone_seconds=None if parked else res.clone_seconds,
        detail=(q.detail or res.detail or f"borrowed by {thread_id}").strip(),
        recoverable=q.recoverable,
        recoverable_reason=q.recoverable_reason,
        rescue=None
        if q.rescue is None
        else {
            "outcome": q.rescue.outcome.value,
            "refs": q.rescue.refs,
            "detail": q.rescue.detail,
        },
    )
    append_slot_quarantine(state_dir / RETREATS_FILE, row)


def run_prep(args: argparse.Namespace) -> int:
    try:
        store = LeaseStore(args.state_dir)
        prep = SlotPrep(
            slots_root=args.slots_root,
            repo=args.repo,
            origin_url=args.origin_url,
            store=store,
            slots_per_repo=args.slots_per_repo,
            trusted=_trusted,
        )
        res = prep.borrow(
            thread_id=args.thread_id,
            turn_id=args.turn_id,
            branch=args.branch,
            base=args.base,
            wrapper_pid=args.wrapper_pid,
            session_name=args.session_name,
            job_name=args.job_name,
        )
        _record_quarantine(args.state_dir, args.thread_id, args.repo, res)
    except Exception as exc:  # the wrapper parks on exit 1; say why on stderr
        print(f"slot-prep failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(PREP_PREFIX + json.dumps(res.to_json(), ensure_ascii=False, sort_keys=True))
    return {
        PrepOutcome.READY: EXIT_READY,
        PrepOutcome.WAITING: EXIT_WAITING,
        PrepOutcome.PARKED: EXIT_PARKED,
    }[res.outcome]


def run_lease(args: argparse.Namespace) -> int:
    store = LeaseStore(args.state_dir)
    try:
        if args.action == "spawning":
            store.mark_spawning(args.repo, args.slot, args.turn_id)
        elif args.action == "running":
            if args.child_pid is None:
                print("slot-lease running: --child-pid is required", file=sys.stderr)
                return EXIT_FAILED
            store.mark_running(args.repo, args.slot, args.turn_id, child_pid=args.child_pid)
        elif args.action == "release":
            store.release(args.repo, args.slot, args.turn_id)
        else:
            store.mark_quarantine_failed(args.repo, args.slot, args.turn_id)
    except LeaseError as exc:
        print(f"slot-lease {args.action}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    return 0
