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
* a row carrying ``holds_released: [<hold_id>, ...]`` releases those holds at that row. There
  are two writers and one replay path (Bohr msg-9886 §3): the gate stamps a human's
  ``HOLD-RELEASE: <hold_id>`` line (msg-9655 §1.1'') on the rows it next writes, and the
  ``ESCALATED`` row of a delivered ``RH-recheck`` head carries the holds Jev confirmed it
  supplements (part 1b; written only after delivery, so a failed write releases nothing,
  msg-9779). A human's line lives in the thread, so the caller also passes those ids in. A
  released hold never absorbs a later refusal: an identical body refused after the release
  opens a new hold (PR #595 gate Obj-3).

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
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .decider.questions import (
    HOLD_SUPPLEMENTED,
    HOLD_UNRELATED,
    HOLD_UNSUPPLEMENTED,
    UNHELD_NO,
    UNHELD_UNBASED_KEY,
    UNHELD_YES,
    hold_relation_key,
)
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
) -> str | None:
    """Open a hold on ``row`` or append it to an identical open structural one. Returns the id
    of the hold it opened, or ``None`` when it was appended (msg-9665 §1.1⁷)."""
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
                return None
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
    return hold_id


@dataclass(frozen=True)
class HoldReplayStep:
    """What one log row did to the holds of its ``(thread, author)`` pair, in log order.

    ``opened`` is the id of a hold this row opened (``None`` when it opened none, including a
    refusal appended to an identical open structural hold). ``released`` are the ids of known
    holds of the pair this row's ``holds_released`` stamp releases for the first time;
    ``released_unknown`` are stamped ids that name no hold of the pair or were already released.
    """

    row: dict[str, Any]
    opened: str | None
    released: tuple[str, ...]
    released_unknown: tuple[str, ...]


def replay_hold_rows(rows: Iterable[dict[str, Any]]) -> Iterator[HoldReplayStep]:
    """Replay log rows of every ``(thread, author)`` pair with the rules :func:`open_holds` uses.

    For counting (``scripts/tierc_gate_measure.py``, msg-9914 Obj-3): the same
    ``_open_or_append`` decides whether a refusal opens a hold, and a stamp is applied at its row
    before the row itself, as in :func:`open_holds`. A release only a thread line carries (no
    stamp yet) is invisible here: replay has the log, not the thread.
    """
    holds: dict[tuple[Any, Any], dict[str, SecurityHold]] = {}
    released: dict[tuple[Any, Any], set[str]] = {}
    for row in rows:
        pair = (row.get("thread"), row.get("author"))
        pair_holds = holds.setdefault(pair, {})
        pair_released = released.setdefault(pair, set())
        fresh: list[str] = []
        unknown: list[str] = []
        for hold_id in dict.fromkeys(_stamped_releases(row)):
            if hold_id in pair_holds and hold_id not in pair_released:
                fresh.append(hold_id)
                pair_released.add(hold_id)
            else:
                unknown.append(hold_id)
        opened = None
        if row.get("kind") == LogKind.BOUNCED.value and row.get("security_hold") is True:
            opened = _open_or_append(
                pair_holds,
                row,
                thread=str(pair[0]),
                author=str(pair[1]),
                released=pair_released,
            )
        yield HoldReplayStep(
            row=row, opened=opened, released=tuple(fresh), released_unknown=tuple(unknown)
        )


@dataclass(frozen=True)
class HoldAnswers:
    """Jev's answers to the ``tierc-v5-hold`` questions, read per hold (msg-9661 §1.1⁵).

    ``relations`` maps each asked hold id to ``supplemented`` / ``unsupplemented`` /
    ``unrelated``; a hold whose answer is missing or not one of those is absent, and then
    ``complete`` is ``False`` (no verdict — nothing may be delivered or released).
    """

    relations: dict[str, str]
    unheld_unbased: str | None
    complete: bool

    @property
    def all_supplemented(self) -> bool:
        """Every asked hold answered ``supplemented`` — the only answer that admits (Bohr
        msg-9886 §2). ``False`` for no holds: there is nothing to have supplemented."""
        return bool(self.relations) and all(r == HOLD_SUPPLEMENTED for r in self.relations.values())

    @property
    def supplemented_ids(self) -> list[str]:
        return sorted(h for h, r in self.relations.items() if r == HOLD_SUPPLEMENTED)

    def history(self, outcome: str) -> list[dict[str, Any]]:
        """§1.1⁶ (a): an entry for every hold the handoff touched, delivered or not."""
        return [
            {"hold_id": hold_id, "relation": relation, "outcome": outcome}
            for hold_id, relation in sorted(self.relations.items())
            if relation in (HOLD_SUPPLEMENTED, HOLD_UNSUPPLEMENTED)
        ]


_RELATIONS = frozenset({HOLD_SUPPLEMENTED, HOLD_UNSUPPLEMENTED, HOLD_UNRELATED})


def _choice(answers: Mapping[str, Any] | None, key: str, allowed: frozenset[str]) -> str | None:
    if answers is None:
        return None
    answer = answers.get(key)
    choice = answer.get("choice") if isinstance(answer, Mapping) else None
    return choice if isinstance(choice, str) and choice in allowed else None


def read_hold_answers(
    holds: Iterable[SecurityHold], answers: Mapping[str, Any] | None
) -> HoldAnswers:
    """Each hold's relation from Jev's raw answers, independently (one question per hold)."""
    relations: dict[str, str] = {}
    complete = True
    for hold in holds:
        relation = _choice(answers, hold_relation_key(hold.hold_id), _RELATIONS)
        if relation is None:
            complete = False
        else:
            relations[hold.hold_id] = relation
    unheld = _choice(answers, UNHELD_UNBASED_KEY, frozenset({UNHELD_YES, UNHELD_NO}))
    return HoldAnswers(
        relations=relations, unheld_unbased=unheld, complete=complete and unheld is not None
    )


__all__ = [
    "HOLD_RELEASE_KEYWORD",
    "HoldAnswers",
    "HoldReplayStep",
    "SecurityHold",
    "hold_release_ids",
    "open_holds",
    "read_hold_answers",
    "replay_hold_rows",
    "stamped_releases",
]
