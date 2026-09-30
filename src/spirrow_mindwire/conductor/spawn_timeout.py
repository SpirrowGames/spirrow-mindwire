"""Spawn timeout: retry once, then say so in the thread (T43 follow-up).

Thread: T-agmsg-transport-lessons-readiness-session-claim-board (Fermi msg-3099 §1 T43, design
Bohr msg-5053 D-1..D-4, approved by Einstein msg-5054, authorised by the human). ``spawn.ready``
(PR #376) records a spawn that became usable. This is the other half: a spawn whose ``connect()``
never finished.

What the conductor does, in :meth:`~spirrow_mindwire.conductor.core.Conductor._spawn`:

1. Every spawn goes through that one method (D-1), so there is one spawn path to reason about.
2. Only :class:`~spirrow_mindwire.exceptions.AdapterSpawnTimeoutError` is retried, once, straight
   away and in the same process (D-2). Each timeout writes one ``spawn.timeout`` event. Any other
   spawn failure propagates untouched: exit 1, wrapper quarantine, Discord, as before.
3. If the second attempt also times out, a notice ending ``NEXT: human`` is posted under
   ``conductor-relay`` and the run stops on ``StopReason.HUMAN`` with exit 0 (D-3). The rule is
   the T42 / T44 one (msg-4569): exit 0 if the stop was reported in the chatroom, non-zero if
   not. If the notice does not land, the original timeout is re-raised, which is the pre-existing
   exit 1. No new exit code and no new ``StopReason``; the cause is in the ``spawn.timeout``
   lines.

Event vocabulary: kind ``spawn.timeout``. Fields: ``adapter_id``, ``instance_id``, ``role``,
``attempt`` (1-based), ``timeout_s``. It goes to the log as one warning line, the same surface as
``conductor.stalled`` and ``conductor.stand_down``, so a quarantine record's log tail carries it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from ..ulid_util import new_ulid
from ..value_objects import Event, Role
from .handoff import HUMAN_TOKEN
from .stop_marker import render_stop_marker

logger = logging.getLogger(__name__)

EVENT_KIND_SPAWN_TIMEOUT = "spawn.timeout"

# A policy constant, not a knob: the first attempt plus exactly one retry (msg-5053 D-2). With
# the adapters' 60 s connect budget, a spawn that never connects costs two budgets, plus the
# SDK's teardown after each (up to roughly 10 s, see ``adapters/_connect_budget``), before the
# conductor speaks. ``timeout_s`` in the event is the budget, not that elapsed time.
SPAWN_ATTEMPTS = 2


@dataclass(frozen=True)
class SpawnGaveUp:
    """``Conductor._spawn`` returned this instead of a handle: every attempt timed out.

    ``notice_msg_id`` is the posted notice, never empty. A notice that did not land is not a
    give-up; the timeout is re-raised instead.
    """

    notice_msg_id: str


def spawn_timeout_event(
    *, adapter_id: str, instance_id: str, role: Role, attempt: int, timeout_s: float
) -> Event:
    """Build the ``spawn.timeout`` event (no message body, per the I6 Event convention)."""
    return Event(
        event_id=new_ulid(),
        occurred_at=datetime.now(UTC),
        kind=EVENT_KIND_SPAWN_TIMEOUT,
        fields={
            "adapter_id": adapter_id,
            "instance_id": instance_id,
            "role": role.value,
            "attempt": attempt,
            "timeout_s": timeout_s,
        },
    )


def emit_spawn_timeout(event: Event) -> None:
    """Write the event as one warning line (the line a quarantine record's log tail carries)."""
    f = event.fields
    logger.warning(
        "%s adapter_id=%s instance_id=%s role=%s attempt=%s timeout_s=%s",
        event.kind,
        f.get("adapter_id"),
        f.get("instance_id"),
        f.get("role"),
        f.get("attempt"),
        f.get("timeout_s"),
    )


def render_spawn_timeout_notice(event: Event) -> str:
    """The notice posted into the thread after the last attempt timed out.

    ``event`` is the last attempt's ``spawn.timeout``. Ends on ``NEXT: human`` like the conductor's
    other stop notices: a person has to act, and a stop-token head is what makes the next sweep
    tick SKIP instead of launching into the same timeout again.
    """
    f = event.fields
    return (
        f"Conductor spawn timeout — {f.get('instance_id')} のセッションを起動できませんでした\n\n"
        f"- 対象: `{f.get('instance_id')}` (role `{f.get('role')}`, "
        f"adapter `{f.get('adapter_id')}`)\n"
        f"- 試行回数: {f.get('attempt')} (上限 {SPAWN_ATTEMPTS})。"
        f"どの回も {f.get('timeout_s')} 秒以内に SDK の connect が終わりませんでした\n\n"
        f"timeout のあと同じプロセスで 1 回だけ再試行し、それも timeout したので止めました "
        f"(`{EVENT_KIND_SPAWN_TIMEOUT}`)。{f.get('instance_id')} のターンは実行されていません。"
        f"connect が終わらなかった原因は特定していません。\n\n"
        f"次にやること: ループ host で Claude Code CLI が起動して応答するかを確認してください。"
        f"直したら、進め先の `NEXT:` を書いてください。head が動くまでループは再開しません。\n\n"
        f"{render_stop_marker(event)}\n\n"
        f"NEXT: {HUMAN_TOKEN}"
    )


__all__ = [
    "EVENT_KIND_SPAWN_TIMEOUT",
    "SPAWN_ATTEMPTS",
    "SpawnGaveUp",
    "emit_spawn_timeout",
    "render_spawn_timeout_notice",
    "spawn_timeout_event",
]
