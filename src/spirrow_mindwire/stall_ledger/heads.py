"""D-16d-1: the thread head -- what it says about who acts next, and the cache that keeps it.

Spec: T-stalled-pr-has-no-detector msg-8359 §2-2 (the four groups), msg-8361 Obj-1-2 (the
classification is total: whatever is not positively A, B or D is C), msg-8363 / msg-8365 (D,
its wait kinds and the trigger-prefix table), msg-8368 ① (read a head only when it changed, the
per-group N, ``pending_heads``), msg-8370 Obj-4-3 (the cache key is ``(last_msg_id,
last_activity_at)``) and msg-8480 (the eviction rule).

**The groups.**

* **A** -- ``nominated``: the head names a participant (``NEXT: <persona>`` or the
  ``pr-review`` gate), and the ``next_participant`` field, when set, names the same one.
* **B** -- ``done``: ``STOP: done``, either as the final control line or above ``NEXT: none``.
* **C** -- ``unexplained``: no stop reason a machine can read. Every head that is not
  positively A, B or D lands here, including a head the ledger has not read yet. Examples:
  ``NEXT: none`` without a ``STOP:`` line, no ``NEXT:`` line at all, a refused terminal form,
  a ``next_participant`` field that disagrees with the body's ``NEXT:`` (the M-1 shape).
* **D** -- ``waiting``: ``BLOCKED-ON:`` (any trigger -- never evaluated, msg-8363), ``NEXT:
  human`` (MERGE-WAIT included) and a well-formed ``NEXT: operator`` (with ``OPERATOR-TASK``).

The parse is :mod:`spirrow_mindwire.conductor.handoff`'s, read without a roster (the ledger has
none, like head_skip). One consequence is stated rather than hidden: a LEGACY ``STOP: blocked-on
... wake:<x>`` line above ``NEXT: none`` needs a roster to resolve its wake, so it reads here as
C, not D -- the loud side. The terminal ``BLOCKED-ON:`` form needs no roster and reads as D.

**What a group changes.** :func:`apply_head` turns a group into the observation's ``N`` and
``needs_actor`` (msg-8368 ① table): A and C open after 6h; B does not open; D uses its wait
threshold; a head not read yet keeps today's ``other_thread`` 72h. ``T_WAIT_AUTO`` and
``T_WAIT_MANUAL`` are fixed in D-16d-2 from the distribution D-16d-1 measures (msg-8363 /
msg-8365); until then both are the 72h every thread used before D-16d-1, so a D thread opens no
later and no earlier than it did.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from spirrow_mindwire.conductor.handoff import (
    HUMAN_TOKEN,
    NONE_TOKEN,
    OPERATOR_TOKEN,
    PR_REVIEW_TOKEN,
    MalformedHandoff,
    StopLine,
    StopStatus,
    TerminalView,
    _stop_line_above_last_next,
    parse_next_token,
    resolve_handoff,
    terminal_form_roster_free,
    terminal_form_view,
)
from spirrow_mindwire.stall_ledger.predicates import N_THRESHOLDS

if TYPE_CHECKING:  # adapters imports this module; the type is only needed for hints
    from spirrow_mindwire.stall_ledger.adapters import UnitObservation


class HeadGroup(StrEnum):
    """The four groups of msg-8359 §2-2. Stored in the cache as these strings."""

    NOMINATED = "A"
    DONE = "B"
    UNEXPLAINED = "C"
    WAITING = "D"


class WaitKind(StrEnum):
    """Which threshold a D head waits under (msg-8365 Obj-3-2)."""

    AUTO = "auto"  # a machine should finish it: deploy, and any unknown trigger
    MANUAL = "manual"  # a person acts: pr / thread / queue-empty, NEXT: operator
    HUMAN = "human"  # NEXT: human -- counted only; the human-waiting list already shows it


#: Trigger arm -> wait kind (msg-8365 Obj-3-2). Fixed table; the trigger is never evaluated.
#: An arm missing from the table -- unknown, or unreadable -- is AUTO: surface it sooner.
WAIT_KIND_BY_ARM: dict[str, WaitKind] = {
    "deploy": WaitKind.AUTO,
    "pr": WaitKind.MANUAL,
    "thread": WaitKind.MANUAL,
    "queue-empty": WaitKind.MANUAL,
}

#: D thresholds. PROVISIONAL: D-16d-2 sets them from D-16d-1's distribution (msg-8363 /
#: msg-8365). Until then they equal the ``other_thread`` value every thread used before D-16d-1.
T_WAIT_AUTO: timedelta = N_THRESHOLDS["other_thread"]
T_WAIT_MANUAL: timedelta = N_THRESHOLDS["other_thread"]
T_WAIT_HUMAN: timedelta = N_THRESHOLDS["other_thread"]

#: N for a head the ledger has not read yet: today's 72h (msg-8368 ①).
N_UNREAD: timedelta = N_THRESHOLDS["other_thread"]


def wait_kind_for_arm(arm: str | None) -> WaitKind:
    return WAIT_KIND_BY_ARM.get(arm or "", WaitKind.AUTO)


@dataclass(frozen=True)
class HeadClass:
    """One head's group, and why. ``detail`` is a short machine-readable reason."""

    group: HeadGroup
    detail: str
    next_token: str | None = None
    wait: WaitKind | None = None
    trigger: str | None = None


def _norm(token: str | None) -> str:
    return (token or "").strip().casefold()


def classify_head(body: str, *, next_participant: str | None) -> HeadClass:
    """The group of a thread head (module docstring). Total: never raises, always a group."""
    field = next_participant.strip() if isinstance(next_participant, str) else ""
    token = parse_next_token(body)

    view = terminal_form_view(body)
    if view is TerminalView.REFUSED:
        return HeadClass(HeadGroup.UNEXPLAINED, "refused_terminal_form", token)
    if view is TerminalView.SETTLES:
        form = terminal_form_roster_free(body)
        if isinstance(form, StopLine) and form.status is StopStatus.DONE:
            return HeadClass(HeadGroup.DONE, "stop_done", token)
        if isinstance(form, StopLine) and form.status is StopStatus.BLOCKED_ON:
            trigger = f"{form.trigger_arm} {form.trigger_operand}"
            return HeadClass(
                HeadGroup.WAITING,
                "blocked_on",
                token,
                wait_kind_for_arm(form.trigger_arm),
                trigger,
            )
        # SETTLES always parses to DONE / BLOCKED_ON; anything else is the loud side.
        detail = form.value if isinstance(form, MalformedHandoff) else "unexpected_terminal_form"
        return HeadClass(HeadGroup.UNEXPLAINED, detail, token)

    if token is not None and field and _norm(token) != _norm(field):
        return HeadClass(HeadGroup.UNEXPLAINED, "field_body_mismatch", token)
    effective = field or token
    if not effective:
        return HeadClass(HeadGroup.UNEXPLAINED, "no_next", token)
    name = _norm(effective)
    if name == NONE_TOKEN:
        # The legacy STOP line above NEXT: none. Read roster-free: DONE needs no roster; a
        # blocked-on line's wake does, so it comes back MALFORMED and lands in C (docstring).
        stop = _stop_line_above_last_next(body, {})
        if stop.status is StopStatus.DONE:
            return HeadClass(HeadGroup.DONE, "stop_done_legacy", token)
        return HeadClass(HeadGroup.UNEXPLAINED, f"none_stop_{stop.status.value}", token)
    if name == HUMAN_TOKEN:
        return HeadClass(HeadGroup.WAITING, "next_human", token, WaitKind.HUMAN)
    if name == OPERATOR_TOKEN:
        handoff = resolve_handoff(body, {}, next_participant=field or None)
        if handoff.operator_task is not None and handoff.operator_fault is None:
            return HeadClass(HeadGroup.WAITING, "next_operator", token, WaitKind.MANUAL)
        return HeadClass(HeadGroup.UNEXPLAINED, "operator_malformed", token)
    if name == PR_REVIEW_TOKEN:
        return HeadClass(HeadGroup.NOMINATED, "pr_review", token)
    return HeadClass(HeadGroup.NOMINATED, "next_participant", token)


# ─── The cache entry ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HeadKey:
    """The cache key (msg-8370 Obj-4-3): a change in either part means "read the head again"."""

    last_msg_id: str
    last_activity_at: str  # the listing's value, kept as the string it was


@dataclass(frozen=True)
class CachedHead:
    key: HeadKey
    head: HeadClass
    next_participant: str | None
    read_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "last_msg_id": self.key.last_msg_id,
            "last_activity_at": self.key.last_activity_at,
            "group": self.head.group.value,
            "detail": self.head.detail,
            "next_token": self.head.next_token,
            "next_participant": self.next_participant,
            "wait": self.head.wait.value if self.head.wait is not None else None,
            "trigger": self.head.trigger,
            "read_at": self.read_at,
        }

    @staticmethod
    def from_json(raw: object) -> CachedHead | None:
        """``None`` for an entry that does not parse: it is simply read again (cache, not truth)."""
        if not isinstance(raw, dict):
            return None
        try:
            key = HeadKey(_str(raw["last_msg_id"]), _str(raw["last_activity_at"]))
            wait_raw = raw.get("wait")
            head = HeadClass(
                group=HeadGroup(raw["group"]),
                detail=_str(raw["detail"]),
                next_token=_opt_str(raw.get("next_token")),
                wait=WaitKind(wait_raw) if wait_raw is not None else None,
                trigger=_opt_str(raw.get("trigger")),
            )
            return CachedHead(
                key, head, _opt_str(raw.get("next_participant")), _str(raw["read_at"])
            )
        except (KeyError, TypeError, ValueError):
            return None


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("not a string")
    return value


def _opt_str(value: object) -> str | None:
    return None if value is None else _str(value)


# ─── Group -> N / needs_actor ──────────────────────────────────────────────────────────


def threshold_for(head: HeadClass | None) -> timedelta | None:
    """``N`` for a head (msg-8368 ① table). ``None`` = B, which never opens."""
    if head is None:
        return N_UNREAD
    if head.group is HeadGroup.DONE:
        return None
    if head.group in (HeadGroup.NOMINATED, HeadGroup.UNEXPLAINED):
        return N_THRESHOLDS["thread_with_nomination"]
    if head.wait is WaitKind.MANUAL:
        return T_WAIT_MANUAL
    if head.wait is WaitKind.HUMAN:
        return T_WAIT_HUMAN
    return T_WAIT_AUTO


def apply_head(obs: UnitObservation, head: HeadClass | None) -> UnitObservation:
    """The observation with its ``N`` (and, for B, ``needs_actor``) set from the head.

    ``head=None`` is a head not read yet: the observation stays as the listing built it (C,
    72h). A B head reads ``needs_actor = false``, so it opens nothing and closes an open record
    by rule (a).
    """
    if head is None or not obs.needs_actor:
        return obs
    n = threshold_for(head)
    if n is None:
        return dataclasses.replace(obs, needs_actor=False)
    return dataclasses.replace(obs, n=n)


__all__ = [
    "N_UNREAD",
    "T_WAIT_AUTO",
    "T_WAIT_HUMAN",
    "T_WAIT_MANUAL",
    "WAIT_KIND_BY_ARM",
    "CachedHead",
    "HeadClass",
    "HeadGroup",
    "HeadKey",
    "WaitKind",
    "apply_head",
    "classify_head",
    "threshold_for",
    "wait_kind_for_arm",
]
