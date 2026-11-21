"""Slot mode's continuation verdict, read from the ledger (T-ephemeral-impl-clone-slot A4).

Bohr msg-9400 §3-§4 (Fermi msg-9397), scope msg-9754 / msg-9875 item 1: once each turn borrows a
clone slot and hands it back at turn end, the next tick's clone is not the clone the CONTINUE turn
worked in, so judging that clone (``loop_runner._continuation_clone_check``, PR #581) would pass
on an empty slot and say nothing. In slot mode the conductor instead decides from what the turn
end recorded:

* the thread's last ``slot_turn_result`` turn in the continuation ledger (written by
  ``mindwire slot-finish`` / ``mindwire slot-recover``) — ``pushed`` / ``rescued`` continue;
  ``rescue_failed`` / ``quarantine_failed`` do not;
* the lease file — a lease this thread still holds on a slot other than the one this run
  borrowed means a turn whose end was never recorded (exit 7, a throw after spawn, a wrapper that
  died without its finally): no continuation;
* no record at all is never read as ``pushed`` (msg-9400 §3).

A refusal parks this thread only (msg-9400 §3), naming the quarantine path, or the slot still
leased, so the operator knows where the work is. :func:`judge_slot_continuation` is pure; the
conductor raises :class:`SlotContinuationRefused` from it, and ``loop_runner.main`` turns that
into exit :data:`SLOT_CONTINUE_REFUSED_EXIT_CODE` with one ``MINDWIRE_SLOT_CONTINUE_REFUSED``
payload line that the wrapper parks the thread on.

Not used for a CONTINUE posted in the same conductor run: that turn's slot is still held by this
run (it is handed back only after the run ends), so the next turn works in the very clone the
CONTINUE turn left, and the turn end that follows saves both. See ``Conductor._judge_continuation``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .clone_slot import quarantine_root
from .clone_slot_lease import Lease, LeaseState, LeaseStore
from .conductor.continuation_ledger import LedgerKind, SlotTurnOutcome, read_ledger

__all__ = [
    "SLOT_CONTINUE_REFUSED_EXIT_CODE",
    "SLOT_CONTINUE_REFUSED_PREFIX",
    "SlotContinuationRefused",
    "SlotContinuationVerdict",
    "build_slot_continuation_check",
    "emit_slot_continue_refused_payload",
    "judge_slot_continuation",
]

SLOT_CONTINUE_REFUSED_EXIT_CODE = 14
SLOT_CONTINUE_REFUSED_PREFIX = "MINDWIRE_SLOT_CONTINUE_REFUSED "
PARK_CODE = "clone_slot_continue_refused"

_SAVED = frozenset({SlotTurnOutcome.PUSHED.value, SlotTurnOutcome.RESCUED.value})

# Why the continuation was refused (the payload's ``reason``).
REASON_LEASE_HELD = "lease_held"
REASON_NO_RECORD = "no_record"
REASON_NOT_SAVED = "not_saved"


@dataclass(frozen=True)
class SlotContinuationVerdict:
    """``admit`` = continue. Otherwise ``reason`` / ``path`` / ``detail`` say why and where."""

    admit: bool
    reason: str | None = None
    turn_id: str | None = None
    outcomes: tuple[str, ...] = ()
    path: str | None = None
    detail: str = ""

    def to_json(self, *, project: str, thread: str) -> dict[str, Any]:
        return {
            "project": project,
            "thread": thread,
            "park_code": PARK_CODE,
            "reason": self.reason,
            "turn_id": self.turn_id,
            "outcomes": list(self.outcomes),
            "path": self.path,
            "detail": self.detail,
        }


class SlotContinuationRefused(Exception):  # noqa: N818 - a verdict, not a malfunction
    """The slot-mode continuation check refused; carries the verdict for the payload line."""

    def __init__(self, verdict: SlotContinuationVerdict, *, project: str, thread: str) -> None:
        super().__init__(verdict.detail)
        self.verdict = verdict
        self.project = project
        self.thread = thread


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _quarantine_path_for(
    retreat_rows: Iterable[dict[str, Any]], thread: str, turn_id: str
) -> str | None:
    """The turn end's quarantine target, from its ``slot_quarantine`` row (``detail`` starts with
    ``turn end <turn_id>:``, written by ``SlotFinish._quarantine``)."""
    found: str | None = None
    for row in retreat_rows:
        if row.get("thread_id") != thread:
            continue
        path = row.get("quarantine_path")
        detail = row.get("detail")
        if (
            isinstance(path, str)
            and path
            and isinstance(detail, str)
            and detail.startswith(f"turn end {turn_id}:")
        ):
            found = path
    return found


def judge_slot_continuation(
    *,
    project: str,
    thread: str,
    ledger_rows: Sequence[dict[str, Any]],
    leases: Sequence[Lease],
    current_slot: str | Path | None,
    slots_root: Path,
    repo: str,
    retreat_rows: Iterable[dict[str, Any]] = (),
) -> SlotContinuationVerdict:
    """May the thread's CONTINUE, posted in an earlier run, be continued? (msg-9400 §3)

    ``leases`` are the repo's held leases; ``current_slot`` is the slot this run borrowed (its
    ``[loop].repo_dir``), whose lease is this run's own and is ignored.
    """
    cur = _norm(current_slot) if current_slot is not None else None
    for lease in leases:
        if lease.thread_id != thread:
            continue
        slot_path = slots_root / repo / lease.slot
        if cur is not None and _norm(slot_path) == cur:
            continue
        state = "quarantine_failed" if lease.state is LeaseState.QUARANTINE_FAILED else "held"
        return SlotContinuationVerdict(
            False,
            REASON_LEASE_HELD,
            turn_id=lease.turn_id,
            path=str(slot_path),
            detail=(
                f"turn {lease.turn_id} still holds {slot_path} ({state}): its end was never "
                "recorded, so its work may be only in that slot"
            ),
        )
    mine = [
        r
        for r in ledger_rows
        if r.get("kind") == LedgerKind.SLOT_TURN.value
        and r.get("project") == project
        and r.get("thread") == thread
    ]
    if not mine:
        return SlotContinuationVerdict(
            False,
            REASON_NO_RECORD,
            detail="no slot_turn_result row for this thread: the turn end was not recorded",
        )
    last_turn = str(mine[-1]["turn_id"])
    rows = [r for r in mine if r.get("turn_id") == last_turn]
    outcomes = tuple(str(r.get("outcome")) for r in rows)
    if all(o in _SAVED for o in outcomes):
        return SlotContinuationVerdict(True, turn_id=last_turn, outcomes=outcomes)
    slot_name = str(rows[-1].get("slot") or "")
    if SlotTurnOutcome.QUARANTINE_FAILED.value in outcomes:
        path = str(slots_root / repo / slot_name) if slot_name else None
    else:
        path = _quarantine_path_for(retreat_rows, thread, last_turn) or str(
            quarantine_root(slots_root, repo)
        )
    details = "; ".join(str(r.get("detail")) for r in rows if r.get("detail"))
    return SlotContinuationVerdict(
        False,
        REASON_NOT_SAVED,
        turn_id=last_turn,
        outcomes=outcomes,
        path=path,
        detail=f"turn {last_turn} ended {'/'.join(outcomes)}" + (f": {details}" if details else ""),
    )


def emit_slot_continue_refused_payload(exc: SlotContinuationRefused) -> None:
    """One ``MINDWIRE_SLOT_CONTINUE_REFUSED <json>`` line on stdout for the wrapper."""
    payload = exc.verdict.to_json(project=exc.project, thread=exc.thread)
    print(SLOT_CONTINUE_REFUSED_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every JSON-object line of ``path``; a missing file or an unreadable line reads as nothing
    (only the quarantine *path* in the park reason comes from here, never the verdict)."""
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def build_slot_continuation_check(
    *,
    slots_root: Path,
    current_slot: Path,
    state_dir: Path,
    ledger_path: Path,
    retreats_path: Path,
    project: str,
    thread: str,
) -> Callable[[], None]:
    """The conductor's slot-mode continuation check for this run (``[loop].slots_root`` set).

    ``current_slot`` is this run's ``[loop].repo_dir`` — the slot the wrapper borrowed,
    ``<slots_root>/<repo>/slot-NN`` — so the repo is its parent's name. The ledger and the lease
    file are read at call time, after the earlier run's turn end wrote them. A read failure of
    either propagates (fail-closed: the run stops, nothing is continued on a guess).
    """
    repo = current_slot.parent.name

    def check() -> None:
        verdict = judge_slot_continuation(
            project=project,
            thread=thread,
            ledger_rows=read_ledger(ledger_path).rows,
            leases=LeaseStore(state_dir).leases(repo),
            current_slot=current_slot,
            slots_root=slots_root,
            repo=repo,
            retreat_rows=_read_jsonl(retreats_path),
        )
        if not verdict.admit:
            raise SlotContinuationRefused(verdict, project=project, thread=thread)

    return check
