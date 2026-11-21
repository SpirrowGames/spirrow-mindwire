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
from .clone_slot_lease import Lease, LeaseState, LeaseStore
from .clone_slot_prep import PUSH_REMOTE, rename_to_quarantine
from .clone_slot_save import SaveResult, SlotSaver, _subprocess_git, record_turn_result
from .conductor.continuation_ledger import SlotTurnOutcome, record_slot_turn

__all__ = [
    "FinishOutcome",
    "FinishResult",
    "SlotFinish",
]

RETREATS_FILE = "clone-retreats.jsonl"
QUARANTINE_REASON = "rescue_failed"


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

    @property
    def notice(self) -> dict[str, Any] | None:
        """The operator notice for a turn whose work is not confirmed saved (msg-9400 §2,
        msg-9402 Obj-36-4): the path (the quarantine, or the slot itself when the rename failed),
        the thread is added by the wrapper, and the reason. ``None`` when nothing needs a person."""
        notice: dict[str, Any]
        if self.outcome is FinishOutcome.QUARANTINED:
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
    ) -> None:
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

    def _held(self, slot: str, turn_id: str) -> Lease | None:
        for lease in self.store.leases(self.repo):
            if lease.slot == slot and lease.turn_id == turn_id:
                return lease
        return None

    def finish(self, *, slot: str, thread_id: str, turn_id: str) -> FinishResult:
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
        if ref.path.is_dir():
            saver = SlotSaver(ref.path, git=self._git, push_remote=PUSH_REMOTE)
            save = saver.check_and_rescue(thread_id, turn_id, remote=PUSH_REMOTE)
        else:
            save = SaveResult(SlotTurnOutcome.RESCUE_FAILED, detail=f"{ref.path} is missing")
        save = record_turn_result(
            self.ledger,
            save,
            project=self.project,
            thread=thread_id,
            turn_id=turn_id,
            slot=ref.name,
            recovered_by=self.recovered_by,
        )
        if save.saved:
            self.store.release(self.repo, ref.name, turn_id)
            return FinishResult(FinishOutcome.RELEASED, ref, save, recovered_by=self.recovered_by)
        return self._quarantine(ref, lease, save)

    def _quarantine(self, ref: SlotRef, lease: Lease, save: SaveResult) -> FinishResult:
        detail = save.detail or ", ".join(save.reasons) or "not saved"
        target: Path | None = None
        errors: list[str] = []
        if ref.path.exists():
            target, errors = rename_to_quarantine(ref, lease.thread_id, self._clock(), self._rename)
        else:
            errors = [f"{ref.path} is missing"]
        rescue = {"outcome": save.outcome.value, "refs": save.refs, "detail": save.detail}
        if target is not None:
            row = build_slot_quarantine_record(
                thread_id=lease.thread_id,
                repo=self.repo,
                slot_path=ref.path,
                action=SLOT_ACTION_REBUILT,
                quarantine_reason=QUARANTINE_REASON,
                quarantine_path=target,
                detail=f"turn end {lease.turn_id}: {detail}",
                recoverable=False,
                recoverable_reason=QUARANTINE_REASON,
                rescue=rescue,
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
            quarantine_reason=QUARANTINE_REASON,
            park_code=PARK_UNRECOVERABLE,
            detail=f"turn end {lease.turn_id}: {detail}; {why}",
            recoverable=False,
            recoverable_reason=QUARANTINE_REASON,
            rescue=rescue,
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
