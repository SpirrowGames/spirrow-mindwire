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

``slot-finish`` (A3) is the wrapper's turn-end step (:class:`~.clone_slot_finish.SlotFinish`):
save check against ``origin``, the ``slot_turn_result`` row, then release / quarantine. It prints
one line ``MINDWIRE_SLOT_FINISH <json>``. Exit codes:

* 0 — released: the turn's work is ``pushed`` or ``rescued`` and the slot is back in the pool;
* 5 — quarantined: ``rescue_failed``; the slot was renamed away and returned, the json carries a
  ``notice`` for the operator;
* 4 — parked: ``rescue_failed`` and every rename failed; the lease stays ``quarantine_failed``
  and only this thread parks with ``clone_slot_unrecoverable`` (``notice`` in the json);
* 2 — refused: the lease is not this turn's, nothing was touched;
* 1 — the procedure failed (the lease is left as it was: never released as saved).

``slot-recover`` (A4, msg-9877 (a)) is the operator's way back for a lease left held because its
turn end never ran (exit 7, a throw after spawn, a wrapper gone without its finally): after
checking that no session is left, ``mindwire slot-recover --slots-root R --repo r --slot slot-NN
--project p --state-dir S --confirm-no-process``. It reads the turn and the thread from the lease
file and runs the same turn end as ``slot-finish`` (:meth:`~.clone_slot_finish.SlotFinish.recover`),
with ``recovered_by=operator`` on the rows. It prints ``MINDWIRE_SLOT_RECOVER <json>``; exit codes
as ``slot-finish``. Refused (exit 2, nothing touched): no ``--confirm-no-process``, the slot not
leased, or the lease ``quarantine_failed``.

Nothing here posts to a thread: the json is read by the wrapper, which sends notices (A3). The
``slot-recover`` json is read by the operator who ran it, on that terminal.
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
from .clone_slot_finish import FinishOutcome, SlotFinish
from .clone_slot_lease import LeaseError, LeaseStore
from .clone_slot_prep import PrepOutcome, PrepResult, SlotPrep
from .clone_slot_trust import TrustReadError, is_trusted
from .config import CONTINUATION_LEDGER_FILENAME
from .git_sync.branch import generation_name, normalize_stem

PREP_PREFIX = "MINDWIRE_SLOT_PREP "
FINISH_PREFIX = "MINDWIRE_SLOT_FINISH "
RECOVER_PREFIX = "MINDWIRE_SLOT_RECOVER "
RECOVERED_BY_OPERATOR = "operator"
RETREATS_FILE = "clone-retreats.jsonl"
EXIT_READY = 0
EXIT_FAILED = 1
EXIT_WAITING = 3
EXIT_PARKED = 4
EXIT_REFUSED = 2
EXIT_QUARANTINED = 5


def add_parsers(sub: Any) -> None:
    prep = sub.add_parser("slot-prep", help="borrow a clone slot for one turn (slot pool mode)")
    prep.add_argument("--slots-root", required=True, type=Path)
    prep.add_argument("--repo", required=True)
    prep.add_argument("--origin-url", required=True, help="the GitHub URL the mirror follows")
    prep.add_argument("--state-dir", required=True, type=Path)
    prep.add_argument("--slots-per-repo", required=True, type=int)
    prep.add_argument("--thread-id", required=True)
    prep.add_argument("--turn-id", required=True)
    prep.add_argument(
        "--branch",
        default=None,
        help="the thread's branch (default: loop/<stem>, the git-sync name, generation 1)",
    )
    prep.add_argument(
        "--base",
        default=None,
        help="where a new branch starts (mirror/<base>; default: the mirror's HEAD)",
    )
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
    fin = sub.add_parser("slot-finish", help="hand a clone slot back at turn end (slot pool mode)")
    fin.add_argument("--slots-root", required=True, type=Path)
    fin.add_argument("--repo", required=True)
    fin.add_argument("--project", required=True)
    fin.add_argument("--state-dir", required=True, type=Path)
    fin.add_argument("--slot", required=True, help="slot-NN")
    fin.add_argument("--thread-id", required=True)
    fin.add_argument("--turn-id", required=True)
    rec = sub.add_parser(
        "slot-recover",
        help="run the turn end of a lease left held (operator; after checking no process is left)",
    )
    rec.add_argument("--slots-root", required=True, type=Path)
    rec.add_argument("--repo", required=True)
    rec.add_argument("--project", required=True, help="the thread's project (for the ledger row)")
    rec.add_argument("--state-dir", required=True, type=Path)
    rec.add_argument("--slot", required=True, help="slot-NN")
    rec.add_argument(
        "--confirm-no-process",
        action="store_true",
        help="you checked that no session or child process is still using the slot",
    )


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
            branch=args.branch or generation_name(normalize_stem(args.thread_id), 1),
            base=args.base,
            wrapper_pid=args.wrapper_pid,
            session_name=args.session_name,
            job_name=args.job_name,
        )
    except Exception as exc:  # the wrapper parks on exit 1; say why on stderr
        print(f"slot-prep failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    try:
        _record_quarantine(args.state_dir, args.thread_id, args.repo, res)
    except Exception as exc:
        # The borrow already succeeded, but exit 1 means the wrapper will not launch the turn, and
        # an unreturned lease is never reclaimed automatically. So a READY lease goes back to the
        # pool here (PR #589 gate round 1, Obj-1-2). A quarantine_failed lease stays held on
        # purpose: that slot must not be lent again until a person clears it (msg-9402 Obj-36-4).
        print(
            f"slot-prep failed recording the quarantine: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        if res.outcome is PrepOutcome.READY and res.lease is not None:
            try:
                store.release(args.repo, res.lease.slot, res.lease.turn_id)
            except Exception as rel_exc:
                print(
                    f"slot-prep could not release {res.lease.slot}: "
                    f"{type(rel_exc).__name__}: {rel_exc}",
                    file=sys.stderr,
                )
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


def run_finish(args: argparse.Namespace) -> int:
    try:
        fin = SlotFinish(
            slots_root=args.slots_root,
            repo=args.repo,
            project=args.project,
            store=LeaseStore(args.state_dir),
            ledger=args.state_dir / CONTINUATION_LEDGER_FILENAME,
            retreats=args.state_dir / RETREATS_FILE,
        )
        res = fin.finish(slot=args.slot, thread_id=args.thread_id, turn_id=args.turn_id)
    except Exception as exc:  # the wrapper treats exit 1 as not saved; say why on stderr
        print(f"slot-finish failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(FINISH_PREFIX + json.dumps(res.to_json(), ensure_ascii=False, sort_keys=True))
    return _finish_code(res)


def _finish_code(res: Any) -> int:
    return {
        FinishOutcome.RELEASED: EXIT_READY,
        FinishOutcome.QUARANTINED: EXIT_QUARANTINED,
        FinishOutcome.PARKED: EXIT_PARKED,
        FinishOutcome.REFUSED: EXIT_REFUSED,
    }[res.outcome]


def run_recover(args: argparse.Namespace) -> int:
    try:
        fin = SlotFinish(
            slots_root=args.slots_root,
            repo=args.repo,
            project=args.project,
            store=LeaseStore(args.state_dir),
            ledger=args.state_dir / CONTINUATION_LEDGER_FILENAME,
            retreats=args.state_dir / RETREATS_FILE,
            recovered_by=RECOVERED_BY_OPERATOR,
        )
        res = fin.recover(slot=args.slot, confirm_no_process=args.confirm_no_process)
    except Exception as exc:  # the lease is left as it was: never released as saved
        print(f"slot-recover failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(RECOVER_PREFIX + json.dumps(res.to_json(), ensure_ascii=False, sort_keys=True))
    return _finish_code(res)
