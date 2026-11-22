"""Handing a pooled clone slot back at turn end (T-ephemeral-impl-clone-slot-per-thread, A3).

:meth:`SlotFinish.finish` is what the wrapper's ``finally`` runs for a slot-mode launch, on every
way the turn ends, a timeout kill included (msg-8047 D-4, Bohr msg-9400 §1-§2 as revised by
msg-9402 and msg-9404):

1. The lease must be this turn's. A lease held by another turn, or not held at all, is refused and
   nothing in the slot is touched.
2. :meth:`~.clone_slot_save.SlotSaver.check_and_rescue` against ``origin`` (the A2 function: the
   explicit-refspec fetch, status, ``rev-list --branches HEAD --not --remotes=origin``, stash, and
   the numbered ``rescue/<thread>/<turn>/<NN>`` refs confirmed by ``ls-remote``).
3. :func:`~.clone_slot_save.record_turn_result` writes the ``slot_turn_result`` row of the
   continuation ledger. A failed write turns a saved result into ``rescue_failed`` (msg-9405).
4. Saved (``pushed`` / ``rescued``): the lease is released. Only then (msg-9754 §1).
5. ``rescue_failed``: the slot is renamed into ``_quarantine/<repo>/`` (``-retryN``) with a
   ``slot_quarantine`` row whose ``recoverable`` is ``false``, then the lease is released; the next
   borrow provisions the slot anew (msg-9400 §2).
6. Every rename failed (msg-9402 Obj-36-4): the lease is NOT released — it becomes
   ``quarantine_failed`` and the slot is never lent again; a ``quarantine_failed`` ledger row is
   written next to the ``rescue_failed`` one, and the thread alone parks with
   ``clone_slot_unrecoverable``. The notice names the slot's own (unrenamed) path.

7. A5 D-5 (Bohr msg-9899 §1): when the conductor exited 8 because the clone guard refused the
   borrowed slot (``guard_reason`` given), the slot is never handed back as it is. Steps 2-3 run
   as above, then the slot is quarantined whatever the save outcome (the D-7 reason comes from
   :func:`refusal_reason`), and a failed rename parks this thread only, as in step 6. The repo is
   never parked. A ``pushed`` outcome writes no ``slot_turn_result`` row: the guard may have
   refused before any role ran, and a ``pushed`` row for an unused borrow would let a refused
   CONTINUE through (the PR #598 gate Obj-1-1 hazard). Leaving the earlier turn the last recorded
   one fails closed. Only an unsaved outcome carries an operator notice.

Nothing here deletes a directory or runs ``reset --hard``. Nothing here posts to a thread either:
:class:`FinishResult` carries the notice fields and the wrapper sends them.

Notice reader and fallback surface (OBL-CHATROOM-PRODUCER-READER-SURFACE): this module produces
no chatroom post. The wrapper turns :attr:`FinishResult.notice` into an operator notification
(Discord webhook, the same surface as the dirty-clone alerts); its reader is the operator who
must look at the quarantined or stuck slot. The durable fallback is the ``slot_quarantine`` row
in ``clone-retreats.jsonl`` and the ``slot_turn_result`` rows in the continuation ledger, both
written before the notice is attempted.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .clone_guard import GitRunner
from .clone_retreat_log import (
    SLOT_ACTION_PARKED,
    SLOT_ACTION_REBUILT,
    append_slot_quarantine,
    build_slot_quarantine_record,
)
from .clone_slot import PARK_UNRECOVERABLE, SlotRef
from .clone_slot_gc import FINISH_ALLOWANCE_S, export_deadline, export_slot, write_manifest
from .clone_slot_lease import Lease, LeaseState, LeaseStore
from .clone_slot_prep import PUSH_REMOTE, judge_recoverable, rename_to_quarantine
from .clone_slot_save import SaveResult, SlotSaver, _subprocess_git, record_turn_result
from .conductor.continuation_ledger import SlotTurnOutcome, record_slot_turn

__all__ = [
    "FinishOutcome",
    "FinishResult",
    "SlotFinish",
    "refusal_reason",
]

RETREATS_FILE = "clone-retreats.jsonl"
QUARANTINE_REASON = "rescue_failed"

# A5 D-5: the clone guard's refusal reason (clone_guard.DirtyCloneReason) -> the D-7 reason.
# ``dirty_tree`` is split by the slot's own status (untracked_only / modified).
_GUARD_TO_D7 = {
    "op_in_progress": "merge_in_progress",
    "clone_busy": "index_lock",
    "wrong_head": "wrong_head",
    "pin_not_ignored": "modified",
    "git_unavailable": "provision_failed",
}


def refusal_reason(slot: Path, guard_reason: str, git: GitRunner = _subprocess_git) -> str:
    """The D-7 quarantine reason of a slot the clone guard refused with ``guard_reason``.

    An unknown guard reason, or a ``dirty_tree`` whose status cannot be read, is ``modified``:
    the slot is quarantined either way, the reason only feeds the D-7 counts.
    """
    if guard_reason != "dirty_tree":
        return _GUARD_TO_D7.get(guard_reason, "modified")
    try:
        res = git(slot, ("status", "--porcelain", "--untracked-files=all"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "modified"
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if res.returncode == 0 and lines and all(ln.startswith("??") for ln in lines):
        return "untracked_only"
    return "modified"


class FinishOutcome(StrEnum):
    RELEASED = "released"  # saved (pushed / rescued) and handed back
    QUARANTINED = "quarantined"  # rescue_failed: renamed away and handed back; no continuation
    PARKED = "parked"  # rescue_failed and the rename failed: lease kept, thread parked
    REFUSED = "refused"  # the lease is not this turn's: nothing touched


@dataclass(frozen=True)
class FinishResult:
    outcome: FinishOutcome
    slot: SlotRef | None = None
    save: SaveResult | None = None
    quarantine_path: Path | None = None
    park_code: str | None = None
    detail: str = ""
    record_error: str | None = None
    recovered_by: str | None = None
    quarantine_reason: str = QUARANTINE_REASON

    @property
    def notice(self) -> dict[str, Any] | None:
        """The operator notice for a turn whose work is not confirmed saved (msg-9400 §2,
        msg-9402 Obj-36-4): the path (the quarantine, or the slot itself when the rename failed),
        the thread is added by the wrapper, and the reason. ``None`` when nothing needs a person."""
        notice: dict[str, Any]
        if self.outcome is FinishOutcome.QUARANTINED:
            if self.save is not None and self.save.saved:
                # A5 D-5: a guard-refused slot quarantined with its work saved needs nobody.
                return None
            notice = {
                "reason": QUARANTINE_REASON,
                "path": str(self.quarantine_path),
                "detail": self.detail,
            }
        elif self.outcome is FinishOutcome.PARKED:
            notice = {
                "reason": self.park_code,
                "path": None if self.slot is None else str(self.slot.path),
                "detail": self.detail,
            }
        else:
            return None
        if self.recovered_by is not None:
            # A4 (msg-9877 (a)): the same notice, marked as an operator's recovery.
            notice["recovered_by"] = self.recovered_by
        return notice

    def to_json(self) -> dict[str, Any]:
        save = self.save
        return {
            "outcome": self.outcome.value,
            "slot": None if self.slot is None else str(self.slot.path),
            "slot_name": None if self.slot is None else self.slot.name,
            "turn_outcome": None if save is None else save.outcome.value,
            "rescue_refs": [] if save is None else save.refs,
            "save_reasons": [] if save is None else list(save.reasons),
            "quarantine_path": None if self.quarantine_path is None else str(self.quarantine_path),
            "park_code": self.park_code,
            "detail": self.detail,
            "record_error": self.record_error,
            "recovered_by": self.recovered_by,
            "quarantine_reason": self.quarantine_reason,
            "notice": self.notice,
        }


class SlotFinish:
    """Turn-end procedure of one repo's slot pool."""

    def __init__(
        self,
        *,
        slots_root: Path,
        repo: str,
        project: str,
        store: LeaseStore,
        ledger: Path,
        retreats: Path,
        git: GitRunner = _subprocess_git,
        rename: Callable[[Path, Path], None] = os.rename,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        recovered_by: str | None = None,
        exports_root: Path | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        # #600 gate Obj-4: when this finish started, so the export can leave time for the rename
        # and the lease bookkeeping inside the wrapper's $SlotFinishTimeoutSeconds.
        self._mono = monotonic
        self._started = monotonic()
        self.slots_root = slots_root
        self.repo = repo
        self.project = project
        self.store = store
        self.ledger = ledger
        self.retreats = retreats
        self._git = git
        self._rename = rename
        self._clock = clock
        # A4 (msg-9877 (a)): "operator" when ``mindwire slot-recover`` runs this turn end for a
        # lease whose own turn end never ran; written on every row this finish writes.
        self.recovered_by = recovered_by
        # A5 D-6: where the quarantine export goes (<state>/slot-exports); None = no export.
        self.exports_root = exports_root

    def _held(self, slot: str, turn_id: str) -> Lease | None:
        for lease in self.store.leases(self.repo):
            if lease.slot == slot and lease.turn_id == turn_id:
                return lease
        return None

    def finish(
        self, *, slot: str, thread_id: str, turn_id: str, guard_reason: str | None = None
    ) -> FinishResult:
        lease = self._held(slot, turn_id)
        if lease is None:
            return FinishResult(
                FinishOutcome.REFUSED, detail=f"{self.repo}/{slot} is not leased to {turn_id}"
            )
        if lease.thread_id != thread_id:
            return FinishResult(
                FinishOutcome.REFUSED,
                detail=f"{self.repo}/{slot} is leased to {lease.thread_id}, not {thread_id}",
            )
        if lease.state is LeaseState.QUARANTINE_FAILED:
            return FinishResult(
                FinishOutcome.REFUSED, detail=f"{self.repo}/{slot} is already quarantine_failed"
            )
        ref = SlotRef(self.slots_root, self.repo, lease.index)
        # Classified before the rescue, which may stage the slot's files.
        reason = None if guard_reason is None else refusal_reason(ref.path, guard_reason, self._git)
        if ref.path.is_dir():
            saver = SlotSaver(ref.path, git=self._git, push_remote=PUSH_REMOTE)
            save = saver.check_and_rescue(thread_id, turn_id, remote=PUSH_REMOTE)
        else:
            save = SaveResult(SlotTurnOutcome.RESCUE_FAILED, detail=f"{ref.path} is missing")
        if guard_reason is None or save.outcome is not SlotTurnOutcome.PUSHED:
            save = record_turn_result(
                self.ledger,
                save,
                project=self.project,
                thread=thread_id,
                turn_id=turn_id,
                slot=ref.name,
                recovered_by=self.recovered_by,
            )
        if guard_reason is not None and reason is not None:
            recoverable, rec_reason = (
                judge_recoverable(ref.path, self._git)
                if save.saved and ref.path.is_dir()
                else (False, QUARANTINE_REASON)
            )
            return self._quarantine(
                ref,
                lease,
                save,
                reason=reason,
                recoverable=recoverable,
                recoverable_reason=rec_reason,
                why=f"clone guard refused ({guard_reason})",
            )
        if save.saved:
            self.store.release(self.repo, ref.name, turn_id)
            return FinishResult(FinishOutcome.RELEASED, ref, save, recovered_by=self.recovered_by)
        return self._quarantine(ref, lease, save)

    def _quarantine(
        self,
        ref: SlotRef,
        lease: Lease,
        save: SaveResult,
        *,
        reason: str = QUARANTINE_REASON,
        recoverable: bool = False,
        recoverable_reason: str | None = QUARANTINE_REASON,
        why: str | None = None,
    ) -> FinishResult:
        detail = save.detail or ", ".join(save.reasons) or ("saved" if save.saved else "not saved")
        if why is not None:
            detail = f"{why}; {detail}"
        target: Path | None = None
        errors: list[str] = []
        export = None
        now = self._clock()
        if ref.path.exists():
            if self.exports_root is not None:
                export = export_slot(
                    ref.path,
                    self.exports_root,
                    self.repo,
                    f"{ref.name}-{lease.thread_id}-{now:%Y%m%d-%H%M%S}",
                    deadline=export_deadline(self._started, FINISH_ALLOWANCE_S, self._mono()),
                    monotonic=self._mono,
                )
            target, errors = rename_to_quarantine(ref, lease.thread_id, now, self._rename)
            if target is not None and export is not None:
                write_manifest(export, target, now)
        else:
            errors = [f"{ref.path} is missing"]
        saved_files: dict[str, Any] = (
            {}
            if export is None
            else {
                "patch": export.patch,
                "untracked": export.untracked_tar,
                "bundle": export.bundle,
            }
        )
        rescue = {"outcome": save.outcome.value, "refs": save.refs, "detail": save.detail}
        if target is not None:
            row = build_slot_quarantine_record(
                thread_id=lease.thread_id,
                repo=self.repo,
                slot_path=ref.path,
                action=SLOT_ACTION_REBUILT,
                quarantine_reason=reason,
                quarantine_path=target,
                detail=f"turn end {lease.turn_id}: {detail}",
                recoverable=recoverable,
                recoverable_reason=recoverable_reason,
                rescue=rescue,
                **saved_files,
            )
            record_error = self._append(row)
            # The slot is gone from its path, so it can go back to the pool: the next borrow
            # provisions it anew (msg-9400 §2). The record failing does not change that.
            self.store.release(self.repo, ref.name, lease.turn_id)
            return FinishResult(
                FinishOutcome.QUARANTINED,
                ref,
                save,
                quarantine_path=target,
                detail=detail,
                record_error=record_error,
                recovered_by=self.recovered_by,
                quarantine_reason=reason,
            )
        # Every rename failed (msg-9402 Obj-36-4): keep the lease so the slot is never lent again.
        self.store.mark_quarantine_failed(self.repo, ref.name, lease.turn_id)
        why = "rename failed: " + "; ".join(errors)
        record_errors: list[str] = []
        try:
            record_slot_turn(
                self.ledger,
                project=self.project,
                thread=lease.thread_id,
                turn_id=lease.turn_id,
                outcome=SlotTurnOutcome.QUARANTINE_FAILED,
                slot=ref.name,
                refs=save.refs,
                detail=why,
                recovered_by=self.recovered_by,
            )
        except (OSError, ValueError) as exc:
            record_errors.append(f"ledger: {type(exc).__name__}: {exc}")
        row = build_slot_quarantine_record(
            thread_id=lease.thread_id,
            repo=self.repo,
            slot_path=ref.path,
            action=SLOT_ACTION_PARKED,
            quarantine_reason=reason,
            park_code=PARK_UNRECOVERABLE,
            detail=f"turn end {lease.turn_id}: {detail}; {why}",
            recoverable=False,
            recoverable_reason=recoverable_reason or QUARANTINE_REASON,
            rescue=rescue,
            **saved_files,
        )
        err = self._append(row)
        if err is not None:
            record_errors.append(err)
        return FinishResult(
            FinishOutcome.PARKED,
            ref,
            save,
            park_code=PARK_UNRECOVERABLE,
            detail=f"{detail}; {why}",
            record_error="; ".join(record_errors) or None,
            recovered_by=self.recovered_by,
            quarantine_reason=reason,
        )

    def recover(self, *, slot: str, confirm_no_process: bool) -> FinishResult:
        """``mindwire slot-recover`` (A4, msg-9877 (a)): run the turn end of a lease whose own turn
        end never ran (exit 7, a throw after spawn, a wrapper that died without its finally).

        A thin entry to :meth:`finish`, never a second way to return a slot: the turn and the
        thread come from the lease file, not from the operator, and the lease must still be
        ``launching`` (spawning marker or not) or ``running``. ``quarantine_failed`` is refused here
        and in :meth:`finish` alike; clearing it is not A4. Every refusal touches nothing.
        """
        if not confirm_no_process:
            return FinishResult(
                FinishOutcome.REFUSED,
                detail="--confirm-no-process is required: check no session is left in the slot",
            )
        held = [lease for lease in self.store.leases(self.repo) if lease.slot == slot]
        if not held:
            return FinishResult(
                FinishOutcome.REFUSED, detail=f"{self.repo}/{slot} is not leased (already returned)"
            )
        lease = held[0]
        if lease.state not in (LeaseState.LAUNCHING, LeaseState.RUNNING):
            return FinishResult(
                FinishOutcome.REFUSED,
                detail=f"{self.repo}/{slot} is {lease.state.value}: slot-recover does not clear it",
            )
        return self.finish(slot=slot, thread_id=lease.thread_id, turn_id=lease.turn_id)

    def _append(self, row: dict[str, Any]) -> str | None:
        try:
            append_slot_quarantine(self.retreats, row)
        except (OSError, ValueError) as exc:
            return f"{self.retreats.name}: {type(exc).__name__}: {exc}"
        return None
