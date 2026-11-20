"""Security holds, read back from the Tier-C decisions log.

T-tier-c-six-articles-security-boundary.

A security hold is opened on a ``(thread, author)`` pair when a security request is refused for
lack of a basis, and while it is open the admission gate lets only an admittable label through
(:func:`~spirrow_mindwire.tier_c_admission_gate.decide_admission`, ``security_hold=True``).
Design: Bohr msg-9653 §1.1' → msg-9655 §1.1'' → msg-9659 / msg-9661 §1.1⁵ (one hold per
request, never overwritten) → msg-9663 §1.1⁶ → msg-9665 §1.1⁷ (a structural refusal always
records its own hold; an identical body re-sent appends to it instead).

There is no separate store: the holds are derived from the same append-only JSONL the gate and
the RETRY lookup already use (msg-3648 v1 §4, one physical source of truth). The rows that
matter:

* a ``BOUNCED`` row with ``security_hold: true`` opens a hold whose id is the row's
  ``retry_uuid`` — unless it is ``structural`` and its ``body_sha256`` equals an open structural
  hold of the same pair, in which case it is appended to that hold's history (msg-9665 §1.1⁷);
* a row carrying ``hold_history: [{hold_id, relation, outcome}, ...]`` appends to those holds
  (msg-9663 §1.1⁶ (a));
* nothing in the log releases a hold. Only a human's ``HOLD-RELEASE: <hold_id>`` line does
  (msg-9655 §1.1''; PR #595 gate Obj-2, fix_cut — the release of supplemented holds after
  delivery, msg-9661 §1.1⁵ 3., comes with part 1b). It lives in the thread, not in the log, so
  the caller passes the released ids in. A released hold never absorbs a later refusal: an
  identical body refused after the release opens a new hold (PR #595 gate Obj-3).

Release ordering (PR #595 gate round 2): the thread line carries no position in the log, so the
gate stamps it there. The first rows it writes after it sees a release carry
``holds_released: [<hold_id>, ...]``, and replay applies that release at that row, before the row
itself. Refusals logged before the stamp stay in the released hold's history; only refusals at or
after it open holds of their own. A release the caller passes in that has no stamp yet is applied
after every row, which is the right place for it: every row in the log predates it.

A resolved thread releases all its holds simply by never running again — holds are keyed by
thread.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tier_c_admission_gate import LogKind
from .tier_c_decisions_log import iter_rows

#: The line a human writes to release one hold by its id (the bounce's RETRY uuid).
HOLD_RELEASE_KEYWORD = "HOLD-RELEASE"

_HOLD_RELEASE_RE: re.Pattern[str] = re.compile(
    rf"^[ \t]*{HOLD_RELEASE_KEYWORD}:[ \t]*(?P<id>\S+)[ \t]*\r?$", re.MULTILINE
)


@dataclass
class SecurityHold:
    """One open hold. ``history`` lists later refusals or re-checks that touched it."""

    hold_id: str
    thread: str
    author: str
    held_msg_id: str | None
    held_basis: str | None
    hold_kind: str
    body_sha256: str | None
    history: list[dict[str, Any]] = field(default_factory=list)


def hold_release_ids(body: str) -> list[str]:
    """The hold ids a message's ``HOLD-RELEASE:`` lines name, in order."""
    return [m.group("id") for m in _HOLD_RELEASE_RE.finditer(body)]


def open_holds(
    log_path: Path, *, thread: str, author: str, released: Iterable[str] = ()
) -> list[SecurityHold]:
    """The open holds of ``(thread, author)``, oldest first.

    ``released`` is the set of hold ids a human released with ``HOLD-RELEASE:`` in this thread.
    A release counts from its ``holds_released`` stamp in the log, or after every row if it has
    none yet (module docstring, release ordering).
    """
    released_ids = set(released)
    released_so_far: set[str] = set()
    holds: dict[str, SecurityHold] = {}
    for row in _rows_of(log_path, thread=thread, author=author):
        released_so_far.update(_stamped_releases(row))
        kind = row.get("kind")
        if kind == LogKind.BOUNCED.value and row.get("security_hold") is True:
            _open_or_append(holds, row, thread=thread, author=author, released=released_so_far)
        for item in row.get("hold_history") or ():
            if isinstance(item, dict):
                hold = holds.get(str(item.get("hold_id")))
                if hold is not None:
                    hold.history.append({**item, "msg_id": row.get("msg_id")})
    gone = released_ids | released_so_far
    return [h for h in holds.values() if h.hold_id not in gone]


def stamped_releases(log_path: Path, *, thread: str, author: str) -> set[str]:
    """The hold ids whose release the log already carries as a ``holds_released`` stamp."""
    stamped: set[str] = set()
    for row in _rows_of(log_path, thread=thread, author=author):
        stamped.update(_stamped_releases(row))
    return stamped


def _rows_of(log_path: Path, *, thread: str, author: str) -> Iterator[dict[str, Any]]:
    for row in iter_rows(log_path):
        if row.get("thread") == thread and row.get("author") == author:
            yield row


def _stamped_releases(row: dict[str, Any]) -> list[str]:
    value = row.get("holds_released")
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


def _open_or_append(
    holds: dict[str, SecurityHold],
    row: dict[str, Any],
    *,
    thread: str,
    author: str,
    released: set[str],
) -> None:
    hold_kind = str(row.get("hold_kind") or "structural")
    sha = row.get("body_sha256")
    if hold_kind == "structural" and isinstance(sha, str):
        for hold in holds.values():
            if hold.hold_id in released:
                continue  # released before this row: a later refusal is a hold of its own (Obj-3)
            if hold.hold_kind == "structural" and hold.body_sha256 == sha:
                hold.history.append(
                    {
                        "msg_id": row.get("msg_id"),
                        "relation": "identical",
                        "outcome": row.get("reason"),
                    }
                )
                return
    hold_id = str(row.get("retry_uuid"))
    holds[hold_id] = SecurityHold(
        hold_id=hold_id,
        thread=thread,
        author=author,
        held_msg_id=row.get("msg_id"),
        held_basis=row.get("held_basis"),
        hold_kind=hold_kind,
        body_sha256=sha if isinstance(sha, str) else None,
    )


__all__ = [
    "HOLD_RELEASE_KEYWORD",
    "SecurityHold",
    "hold_release_ids",
    "open_holds",
    "stamped_releases",
]
