"""The pure half of the park-wake tick: which parked threads wake this tick, and what is written.

T-next-line-carries-who-not-why Slice 3 — D-7, the sweeper's trigger evaluator (Bohr msg-2142
D-7, narrowed to the four agent-writable arms by msg-4716 §2; scheduled into Slice 3 by msg-5175
§3). A thread "parks" by ending in::

    STOP: blocked-on <arm>:<ref> wake:<persona>
    NEXT: none

The head-skip sweep never launches a ``NEXT: none`` head (Stage 1, :mod:`~..conductor.head_skip`),
so an unfired park already costs nothing. This module decides the other half — when the trigger
has fired, the park becomes the handoff it was deferring: one message ending in ``NEXT: <wake>``
is posted into the thread, and from there the conductor routes it like any other handoff,
**including guard (i)**. A park therefore cannot launder a handoff the conductor would gate if it
were written directly (the same reasoning as B-5, msg-4716 §2).

Arms (msg-2140 D-3 / msg-2142 D-7). Each declares whether it is monotonic, as D-7 requires of
every arm ("新 arm 追加時に必ずどちらかを宣言する"):

=============  ==========================================  =========  ===========================
arm            fires when                                  monotonic  operand
=============  ==========================================  =========  ===========================
``thread``     that thread's status is ``resolved``        yes        ``[<project>/]<thread_id>``
``pr``         that PR is merged or closed                 yes        ``<owner>/<repo>#<n>``
``deploy``     that deploy request is terminal             yes        ``<request_id>``
``queue-empty``  no open thread of the project hands to a    no         ``<project>[:<role>]``
               participant (holding ``<role>``)
=============  ==========================================  =========  ===========================

Evaluation order (D-7): every fact is read before anything is written, so the tick evaluates
against one committed state and a wake posted this tick is seen only on the next one. Monotonic
arms wake every fired park at once. The non-monotonic arm wakes at most ONE park per trigger key
per tick, the earliest parked first (FIFO by the parking message's sequence number), because
waking one falsifies the predicate the others wait on.

Act once without a counter: after a wake lands, the thread's tail is the wake message, which
ends in ``NEXT: <wake>`` and is therefore not a park. The next tick has nothing to re-fire.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from ..conductor.handoff import STOP_TRIGGER_ARMS, StopLine, StopStatus
from ..github.client import parse_pr_ref
from ..value_objects import Role

#: The author wake messages are posted under. A machine restating a trigger fact holds no role
#: (I-6), so it is registered ``kind: machine`` / ``legitimate: []`` in
#: ``spec/identity/legitimate_roles.yaml``, like ``pr-event-relay``.
PARK_WAKE_RELAY_AUTHOR = "park-wake-relay"


class TriggerArm(StrEnum):
    THREAD = "thread"
    PR = "pr"
    DEPLOY = "deploy"
    QUEUE_EMPTY = "queue-empty"


#: D-7's per-arm declaration. Checked against the parser's arm set at import, so an arm the
#: parser accepts but nobody declared fails here instead of being parked forever.
MONOTONIC: Mapping[TriggerArm, bool] = {
    TriggerArm.THREAD: True,
    TriggerArm.PR: True,
    TriggerArm.DEPLOY: True,
    TriggerArm.QUEUE_EMPTY: False,
}
if tuple(arm.value for arm in MONOTONIC) != STOP_TRIGGER_ARMS:  # pragma: no cover - import guard
    raise RuntimeError(f"park arms {tuple(MONOTONIC)!r} != parser arms {STOP_TRIGGER_ARMS!r}")

#: Deploy request statuses that end a request (magickit ``deploy_status``).
DEPLOY_TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "interrupted"})

_PROJECT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_THREAD_ID_RE = re.compile(r"\AT-\S+\Z")


@dataclass(frozen=True)
class ThreadTarget:
    project: str
    thread_id: str


@dataclass(frozen=True)
class QueueTarget:
    project: str
    role: Role | None


@dataclass(frozen=True)
class Trigger:
    """A parsed ``<arm>:<ref>``. ``target`` is the arm's typed operand."""

    arm: TriggerArm
    ref: str
    target: ThreadTarget | QueueTarget | str

    @property
    def key(self) -> str:
        """The identity of the predicate — two parks on the same key wait on the same fact."""
        return f"{self.arm.value}:{self.ref}"


def parse_trigger(arm: str, ref: str, *, project: str) -> Trigger | None:
    """Type the operand of ``arm``, or ``None`` when it cannot name anything to observe.

    ``project`` is the parked thread's own project, the default for ``thread:`` and the
    operand-less form of nothing else. A ``None`` is reported by the caller as an unreadable
    trigger; it is never guessed into a different one.
    """
    try:
        typed_arm = TriggerArm(arm)
    except ValueError:
        return None
    match typed_arm:
        case TriggerArm.THREAD:
            proj, _, thread_id = ref.rpartition("/")
            proj = proj or project
            if not (_PROJECT_RE.match(proj) and _THREAD_ID_RE.match(thread_id)):
                return None
            return Trigger(typed_arm, ref, ThreadTarget(proj, thread_id))
        case TriggerArm.PR:
            pr = parse_pr_ref(ref)
            if pr is None or f"{pr.owner}/{pr.repo}#{pr.number}" != ref:
                return None
            return Trigger(typed_arm, ref, ref)
        case TriggerArm.DEPLOY:
            return Trigger(typed_arm, ref, ref)
        case TriggerArm.QUEUE_EMPTY:
            proj, sep, role_name = ref.partition(":")
            if not _PROJECT_RE.match(proj):
                return None
            role: Role | None = None
            if sep:
                try:
                    role = Role(role_name)
                except ValueError:
                    return None
            return Trigger(typed_arm, ref, QueueTarget(proj, role))


@dataclass(frozen=True)
class Park:
    """One parked thread: its head said ``STOP: blocked-on …`` above ``NEXT: none``."""

    thread_id: str
    head_msg_id: str
    trigger: Trigger
    wake: str
    raw: str

    @property
    def seq(self) -> int:
        """The parking message's sequence number, the FIFO order (unparseable ids sort last)."""
        _, _, digits = self.head_msg_id.rpartition("-")
        return int(digits) if digits.isdigit() else 2**63


def park_of(stop: StopLine, *, thread_id: str, head_msg_id: str, project: str) -> Park | None:
    """The :class:`Park` a ``BLOCKED_ON`` line describes, or ``None``.

    ``None`` both when ``stop`` is not ``BLOCKED_ON`` and when it is but its operand names
    nothing observable (:func:`parse_trigger`); the caller tells the two apart by the status and
    reports the second as an unreadable trigger.
    """
    if stop.status is not StopStatus.BLOCKED_ON:
        return None
    assert stop.trigger_arm is not None
    assert stop.trigger_operand is not None
    assert stop.wake is not None
    trigger = parse_trigger(stop.trigger_arm, stop.trigger_operand, project=project)
    if trigger is None:
        return None
    return Park(
        thread_id=thread_id,
        head_msg_id=head_msg_id,
        trigger=trigger,
        wake=stop.wake,
        raw=stop.raw or "",
    )


class Fact(StrEnum):
    """What the tick read about one trigger key."""

    FIRED = "fired"
    NOT_FIRED = "not-fired"
    UNKNOWN = "unknown"  # could not be read — nothing is written on it (fail closed)


@dataclass(frozen=True)
class Selection:
    """The D-7 verdict for one tick: who wakes, and why each other park does not."""

    wake: list[Park]
    held: list[tuple[Park, str]]


def select_wakes(parks: Iterable[Park], facts: Mapping[str, Fact]) -> Selection:
    """Apply D-7 to ``parks`` against ``facts`` (keyed by :attr:`Trigger.key`).

    A park whose key has no fact, or an ``UNKNOWN`` one, is held with ``unknown`` — the tick
    could not tell, so it writes nothing. Monotonic arms wake every fired park; the
    non-monotonic arm wakes the lowest-``seq`` park of each fired key and holds the rest.
    """
    wake: list[Park] = []
    held: list[tuple[Park, str]] = []
    woken_keys: set[str] = set()
    for park in sorted(parks, key=lambda p: (p.seq, p.thread_id)):
        fact = facts.get(park.trigger.key, Fact.UNKNOWN)
        if fact is Fact.UNKNOWN:
            held.append((park, "unknown"))
            continue
        if fact is Fact.NOT_FIRED:
            held.append((park, "not-fired"))
            continue
        if not MONOTONIC[park.trigger.arm]:
            if park.trigger.key in woken_keys:
                held.append((park, "non-monotonic-one-per-tick"))
                continue
            woken_keys.add(park.trigger.key)
        wake.append(park)
    return Selection(wake=wake, held=held)


def render_wake(park: Park) -> str:
    """The wake message: what fired, which line asked for it, and the deferred handoff."""
    return (
        f"Park wake (D-7, {PARK_WAKE_RELAY_AUTHOR}) — trigger `{park.trigger.key}` fired\n\n"
        f"{park.head_msg_id} がこのスレッドを `{park.raw}` で止めていた。"
        f"その条件 `{park.trigger.key}` が満たされたので、指定された {park.wake} を起こす。\n\n"
        f"NEXT: {park.wake}"
    )


__all__ = [
    "DEPLOY_TERMINAL_STATUSES",
    "MONOTONIC",
    "PARK_WAKE_RELAY_AUTHOR",
    "Fact",
    "Park",
    "QueueTarget",
    "Selection",
    "ThreadTarget",
    "Trigger",
    "TriggerArm",
    "park_of",
    "parse_trigger",
    "render_wake",
    "select_wakes",
]
