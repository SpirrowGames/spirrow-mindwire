"""Generic stall watchdog (T42).

Thread: T-silent-stops-need-a-generic-watchdog-and-a-loud-stand-down (Fermi msg-3098 §3 T42,
redesigned by Bohr msg-4532 §1 and msg-4569, approved by Einstein msg-4583; build instructions in
Bohr msg-4907). The individual fixes for the 2026-09-14 stall (self-handoff detection, the
terminal no-progress park) catch the causes someone has already seen. This is the last line
behind them. It does not guess the cause. It watches for the absence of progress.

What is observed: **the same thread head launched :data:`STALL_THRESHOLD` times in a row.**

- Progress means the thread head moved, i.e. somebody posted. A skip, a self-filter or an
  adapter that returns without posting produces no message, so none of them count as progress.
  No special case is needed to make that true.
- The count lives in the sweep's ``head_skip`` record (``launches_same_head``). Only a committed
  LAUNCH changes it; a DEFER (backoff) tick never does, so backoff cannot move the threshold.
- The sweep passes the count and the head it launched on to the conductor
  (``--launches-same-head K --launch-head-msg-id M``). The sweep does not import the conductor and
  gets no chatroom write path. It counts, and the conductor acts.

What the conductor does when ``K >= STALL_THRESHOLD`` on the first round, the head is still ``M``,
and the head names a participant the conductor would spawn (an AI-addressed handoff, resolved
through the roster by ``Conductor._route``; msg-4532 §1):

1. It does not spawn.
2. It writes a ``conductor.stalled`` event to the log.
3. It posts a STALLED notice into the thread, ending ``NEXT: human``, and stops with
   ``StopReason.STALLED`` (``stalled_to_human``). The exit is 0, because the stop was reported in
   the chatroom (msg-4569: "chatroom で言えたら exit 0、言えなかったら非 0"). The wrapper's normal
   park path sends the Discord notification, and the next tick's head_skip Stage 1 SKIPs the
   ``NEXT: human`` head.
4. If the notice does not land (thread resolved underneath the run, or no ``msg_id``), it raises
   :class:`StalledError` and the process exits :data:`STALLED_EXIT_CODE`. The wrapper quarantines
   any unknown non-zero code and sends a Discord alert, with the ``conductor.stalled`` line in
   the quarantine record's log tail.

Event vocabulary (for T47): kind ``conductor.stalled``. Fields: ``project``, ``thread``,
``head_msg_id``, ``launches_same_head``, ``target``. ``head_msg_id`` is both the head that was
launched K times and the last point of progress: nothing has been posted since it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ..ulid_util import new_ulid
from ..value_objects import Event
from .handoff import HUMAN_TOKEN

logger = logging.getLogger(__name__)

EVENT_KIND_STALLED = "conductor.stalled"

# A policy constant, not a knob (the agmsg reference fixes its threshold in code on purpose, and
# msg-3098 / msg-4532 keep N=3 fixed). The Nth launch on one head is the one that stands down, so
# N-1 real sessions have already run on this head and posted nothing. With the head_skip backoff
# (BASE 15 min, then 30, capped at 60) the 3rd launch on a stuck head comes about 45 minutes after
# the first at the earliest. Einstein msg-4583 judged the slow side correct for a fallback detector.
STALL_THRESHOLD = 3

# Distinct from 1 (crash / adapter error), 2 (environment terminal, not quarantined) and 3 (T44
# stand-down with no chatroom to speak in). Like 3, it gets no special case in the wrapper:
# "unknown non-zero goes to quarantine" is what a stall that could not be reported should get.
STALLED_EXIT_CODE = 4


class StalledError(SystemExit):
    """Exit non-zero because the STALLED notice could not be posted in the thread.

    A ``SystemExit`` subclass for the same reason as
    :class:`~spirrow_mindwire.conductor.stand_down.StandDownError`: ``loop_runner.main`` re-raises
    it without a traceback, and the process exit code is :data:`STALLED_EXIT_CODE`.
    """

    def __init__(self, event: Event) -> None:
        super().__init__(STALLED_EXIT_CODE)
        self.event = event


def is_stalled(
    *, launches_same_head: int, launch_head_msg_id: str | None, head_msg_id: str
) -> bool:
    """Is this launch the one that must stand down instead of spawning?

    Both halves are required. The count comes from the sweep, and the head id pins it to the head
    the sweep counted on. If the head moved between the sweep's commit and the conductor's read,
    somebody posted, which is progress, and the count no longer describes this head. A missing
    head id (a caller that passed only a count) never stalls: an unpinned count is not evidence.
    """
    if launches_same_head < STALL_THRESHOLD:
        return False
    if not launch_head_msg_id or not head_msg_id:
        return False
    return launch_head_msg_id == head_msg_id


def stalled_event(
    *, project: str, thread: str, head_msg_id: str, launches_same_head: int, target: str
) -> Event:
    """Build the ``conductor.stalled`` event (no message body, per the I6 Event convention)."""
    return Event(
        event_id=new_ulid(),
        occurred_at=datetime.now(UTC),
        kind=EVENT_KIND_STALLED,
        fields={
            "project": project,
            "thread": thread,
            "head_msg_id": head_msg_id,
            "launches_same_head": launches_same_head,
            "target": target,
        },
    )


def emit_stalled(event: Event) -> None:
    """Write the event as one warning line (the line a quarantine record's log tail carries)."""
    f = event.fields
    logger.warning(
        "%s project=%s thread=%s head_msg_id=%s launches_same_head=%s target=%s",
        event.kind,
        f.get("project"),
        f.get("thread"),
        f.get("head_msg_id"),
        f.get("launches_same_head"),
        f.get("target"),
    )


def render_stalled_notice(event: Event) -> str:
    """The STALLED body posted into the thread.

    Ends on ``NEXT: human`` like the conductor's other stop notices: a person has to act, and a
    stop-token head is what makes the next sweep tick SKIP instead of launching a 4th time.
    """
    f = event.fields
    return (
        f"Conductor STALLED — 同じ head で起動を繰り返しても進捗がありません\n\n"
        f"- head: `{f.get('head_msg_id')}` (`NEXT: {f.get('target')}`)\n"
        f"- この head での連続起動回数: {f.get('launches_same_head')} "
        f"(しきい値 {STALL_THRESHOLD})\n\n"
        f"この head のまま {f.get('target')} を起動しても、スレッドには何も投稿されていません。"
        f"原因は特定していません (汎用 watchdog は進捗が無いことだけを見ています)。"
        f"同じことを繰り返さないよう、今回は spawn せずに止めました "
        f"(`{EVENT_KIND_STALLED}`)。\n\n"
        f"次にやること: sweep / conductor のログで、この head に対する直前の起動が"
        f"何も投稿せずに終わった理由を確認してください。直したら、進め先の `NEXT:` を"
        f"書いてください。head が動くまでループは再開しません。\n\n"
        f"NEXT: {HUMAN_TOKEN}"
    )


__all__ = [
    "EVENT_KIND_STALLED",
    "STALLED_EXIT_CODE",
    "STALL_THRESHOLD",
    "StalledError",
    "emit_stalled",
    "is_stalled",
    "render_stalled_notice",
    "stalled_event",
]
