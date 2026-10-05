"""Fail-closed launch resolution and the loud stand-down (T44).

Thread: T-silent-stops-need-a-generic-watchdog-and-a-loud-stand-down (Fermi msg-3098 §3 T44,
reworked by Bohr msg-4532 §2 and msg-4569, approved by Einstein). Before the conductor spawns
anything it has to know four things: **project**, **thread**, **repo_dir** and the **identity**
it would dispatch. If one of them is unknown it must not spawn, and it must not go quiet either.
The measured failure (2026-08-28) was a ``task_thread_id`` / ``[loop].project`` mismatch: the
thread was not found, the daemon exited, and the reason was nowhere in the chatroom.

Where each point is resolved:

- project / thread / repo_dir: :func:`resolve_launch`, called by
  :func:`spirrow_mindwire.loop_runner.run_conductor` before ``_preflight`` and before any adapter
  is built.
- identity: in :meth:`Conductor._route`, the same place the handoff is resolved against the roster
  (msg-4569). The identity can only be known once the thread head has been read, so it is not
  checked here, and no second, parallel check is added anywhere else.

The exit code follows one rule (msg-4569): **exit 0 if the stop was reported in the chatroom,
non-zero if it was not.**

- The thread resolved, so the stop can be reported there. A repo_dir that did not resolve, or an
  identity that did not resolve, is posted into the target thread ending in ``NEXT: human``, and
  the run exits 0 with a ``conductor stopped: reason=human`` line. The notice changes the head, so
  the next tick's head_skip Stage 1 finds a stop token and SKIPs. No new parked state is created.
- The thread did not resolve (project unset or mismatched, thread id unset or wrong). There is no
  chatroom to report in, so the run exits :data:`STAND_DOWN_EXIT_CODE`. The wrapper sends every
  code other than 0 and 2 to quarantine, and it treats unknown codes the same way (Bohr msg-1987
  §Q2-A condition 3). The quarantine record keeps the session-log tail, which includes the
  ``conductor.stand_down`` line below, and quarantine sends a Discord notification. No wrapper
  change is needed, and none is made.
- The stop notice for a resolved thread fails to post, including a thread that has been resolved
  out from under the run. Same as above: non-zero, so quarantine and Discord report it.

Event vocabulary. This is provisional in msg-4532 §3 and is fixed here for T47 (the Operator board
state model):

- event kind ``conductor.stand_down``, with fields ``unresolved`` (a :class:`UnresolvedItem`),
  ``reason`` (a :class:`StandDownReason`), ``project``, ``thread``, ``detail``.
- The event goes to the log through :func:`emit_stand_down`, on the same logger surface as
  ``loop_runner._log_event_sink``. The wrapper's quarantine record captures that log tail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..magickit.client import MagickitMcpError, McpToolCaller, ThreadResolvedError
from ..ulid_util import new_ulid
from ..value_objects import Event
from .gate_records import RELAY_AUTHOR
from .handoff import HUMAN_TOKEN
from .stop_marker import render_stop_marker

logger = logging.getLogger(__name__)

EVENT_KIND_STAND_DOWN = "conductor.stand_down"

# Distinct from 1 (a crash / adapter error) and 2 (an environment-scoped terminal the wrapper
# does NOT quarantine). The wrapper gets no special case for this code: "unknown non-zero goes to
# quarantine" is exactly what a stand-down with no chatroom to speak in should get.
STAND_DOWN_EXIT_CODE = 3


class UnresolvedItem(StrEnum):
    """Which of the four launch points did not resolve (msg-3098 §3 T44)."""

    PROJECT = "project"
    THREAD = "thread"
    REPO_DIR = "repo_dir"
    IDENTITY = "identity"


class StandDownReason(StrEnum):
    """Why the point did not resolve. The ``unresolved`` item says which point; this says how."""

    PROJECT_UNSET = "project_unset"
    THREAD_UNSET = "thread_unset"
    # chatroom_get_thread refused the (project, thread) pair. This covers the 2026-08-28 case
    # (``task_thread_id`` and ``[loop].project`` disagree), and it cannot be told apart from a
    # thread id that is simply wrong, because both come back as the same refusal.
    THREAD_UNREADABLE = "thread_unreadable"
    REPO_DIR_UNSET = "repo_dir_unset"
    REPO_DIR_MISSING = "repo_dir_missing"  # set, but not an existing directory
    # The head's ``NEXT:`` names something that is neither a roster participant nor a sentinel,
    # for example the typo ``NEXT: Bohrr``.
    IDENTITY_UNRESOLVED = "identity_unresolved"
    # The head's ``NEXT:`` is a role name (``implementer``), but the roster has zero or several
    # identities holding that role, so it cannot be mapped to one (T-next-role-name-stands-down-
    # to-human D3). Kept apart from IDENTITY_UNRESOLVED so a role-name miss is not read as a typo.
    IDENTITY_ROLE_AMBIGUOUS = "identity_role_ambiguous"
    # ``NEXT: operator`` refused (D6'''', msg-5428). The values equal those of
    # :class:`.handoff.OperatorFault`, and a test pins that, so the parser's verdict and the
    # event's reason are one vocabulary.
    IDENTITY_OPERATOR_NO_TASK = "identity_operator_no_task"
    OPERATOR_NO_TIER_C_CHECK = "operator_no_tier_c_check"
    # The one stand-down that stays in the board's decision lane: the author declared a Tier-C and
    # still tried ``NEXT: operator`` (msg-5428, D7 supplement).
    OPERATOR_TIER_C_CONFLICT = "operator_tier_c_conflict"
    # The head names an identity whose embodiment has no adapter (ADR-2026-09-14-21 D-3, e.g.
    # Fermi = web_ai_chat).
    IDENTITY_NOT_SPAWNABLE = "identity_not_spawnable"
    # The structured ``next_participant`` field and the body's ``NEXT:`` line disagree, so the
    # conductor stopped on the field while head_skip (which reads only the body) keeps launching
    # the head (T-role-body-field-divergence-relaunch D3 N1, Bohr msg-5855/5859). The values equal
    # those of :class:`.handoff.MismatchReason`, and a test pins that, so the resolver's verdict and
    # the event's reason are one vocabulary.
    FIELD_BODY_DIVERGENCE = "target_divergence"
    FIELD_UNRESOLVABLE = "field_unresolvable"


class StandDownError(SystemExit):
    """Exit non-zero because a stand-down could not be reported in any chatroom thread.

    A ``SystemExit`` subclass, so ``loop_runner.main``'s catch-all re-raises it without printing a
    traceback, and the process exit code is :data:`STAND_DOWN_EXIT_CODE`.
    """

    def __init__(self, event: Event) -> None:
        super().__init__(STAND_DOWN_EXIT_CODE)
        self.event = event


def stand_down_event(
    *,
    unresolved: UnresolvedItem,
    reason: StandDownReason,
    project: str,
    thread: str,
    detail: str,
) -> Event:
    """Build the ``conductor.stand_down`` event (no message body, per the I6 Event convention)."""
    return Event(
        event_id=new_ulid(),
        occurred_at=datetime.now(UTC),
        kind=EVENT_KIND_STAND_DOWN,
        fields={
            "unresolved": unresolved.value,
            "reason": reason.value,
            "project": project,
            "thread": thread,
            "detail": detail,
        },
    )


def emit_stand_down(event: Event) -> None:
    """Write the event as one warning line.

    This is the line that ends up in the quarantine record's ``session_log_tail`` when the run
    exits non-zero.
    """
    f = event.fields
    logger.warning(
        "%s unresolved=%s reason=%s project=%s thread=%s detail=%s",
        event.kind,
        f.get("unresolved"),
        f.get("reason"),
        f.get("project"),
        f.get("thread"),
        f.get("detail"),
    )


@dataclass(frozen=True)
class LaunchResolution:
    """Outcome of :func:`resolve_launch`.

    ``stand_down`` is ``None`` when all three points resolved. It is set only when the thread
    resolved and something after it did not. In that case the caller must post
    :func:`render_stand_down_notice` into the thread and must not spawn anything.
    """

    stand_down: Event | None


async def resolve_launch(
    *,
    mcp: McpToolCaller,
    project: str,
    thread_id: str,
    repo_dir: Path | None,
) -> LaunchResolution:
    """Resolve project → thread → repo_dir, in that order. Never spawns anything.

    The order is deliberate. Project and thread come first because they decide whether any
    chatroom can be spoken in at all. If either fails, the stand-down is emitted and
    :class:`StandDownError` is raised (non-zero exit, quarantine). If both resolve, the repo_dir
    failure is returned instead, so that the caller reports it in the thread and exits 0.

    Only :class:`MagickitMcpError` counts as "thread did not resolve". A transport failure
    (magickit unreachable) is not a statement about this thread, so it propagates unchanged and
    takes the existing exit-1 path.
    """

    def _halt(item: UnresolvedItem, reason: StandDownReason, detail: str) -> StandDownError:
        event = stand_down_event(
            unresolved=item, reason=reason, project=project, thread=thread_id, detail=detail
        )
        emit_stand_down(event)
        return StandDownError(event)

    if not project.strip():
        raise _halt(
            UnresolvedItem.PROJECT, StandDownReason.PROJECT_UNSET, "[loop].project is empty"
        )
    if not thread_id.strip():
        raise _halt(
            UnresolvedItem.THREAD,
            StandDownReason.THREAD_UNSET,
            "[conductor].task_thread_id is empty",
        )
    try:
        await mcp.call_tool(
            "chatroom_get_thread",
            {"project": project, "thread_id": thread_id, "mode": "full"},
        )
    except MagickitMcpError as exc:
        raise _halt(
            UnresolvedItem.THREAD,
            StandDownReason.THREAD_UNREADABLE,
            f"chatroom_get_thread refused (project={project!r}, thread={thread_id!r}): {exc}",
        ) from exc

    if repo_dir is None:
        event = stand_down_event(
            unresolved=UnresolvedItem.REPO_DIR,
            reason=StandDownReason.REPO_DIR_UNSET,
            project=project,
            thread=thread_id,
            detail="[loop].repo_dir is not configured",
        )
    elif not Path(repo_dir).is_dir():
        event = stand_down_event(
            unresolved=UnresolvedItem.REPO_DIR,
            reason=StandDownReason.REPO_DIR_MISSING,
            project=project,
            thread=thread_id,
            detail=f"[loop].repo_dir {str(repo_dir)!r} is not an existing directory",
        )
    else:
        return LaunchResolution(stand_down=None)
    emit_stand_down(event)
    return LaunchResolution(stand_down=event)


def render_stand_down_notice(event: Event) -> str:
    """Build the thread body for a stand-down in a thread that did resolve.

    It ends on ``NEXT: human`` for the same reason the other conductor stop notices do: it hands
    the thread to the person who has to act, and it parks the sweep through head_skip Stage 1.
    """
    f = event.fields
    return (
        f"Conductor stand-down — 起動条件が解決できないため spawn しませんでした\n\n"
        f"- 未解決の項目: `{f.get('unresolved')}`\n"
        f"- 理由: `{f.get('reason')}`\n"
        f"- 詳細: {f.get('detail')}\n\n"
        f"identity / project / thread / repo_dir のいずれかが確定しない状態では、"
        f"Conductor は誰も起動しません。その理由をここに残します。\n\n"
        f"次にやること: 上の項目を直してから、このスレッドの head に進め先の `NEXT:` を"
        f"書いてください。head が動くまでループは再開しません。\n\n"
        f"{render_stop_marker(event)}\n\n"
        f"NEXT: {HUMAN_TOKEN}"
    )


async def post_stand_down_notice(
    mcp: McpToolCaller, *, project: str, thread_id: str, event: Event
) -> str:
    """Post the stand-down notice into the target thread. Returns the posted ``msg_id``.

    Producer declaration (OBL-CHATROOM-PRODUCER-READER-SURFACE):

    - Intended reader: the human who owns the target thread. That person opens the thread
      because its head asked for work, and this notice tells them why nothing was started.
    - Fallback surface when the thread is gone: disposition **(3) fail loudly**. A
      :class:`ThreadResolvedError`, or any post that returns no ``msg_id``, raises
      :class:`StandDownError`. The run then exits non-zero, and the wrapper quarantines the thread
      and sends a Discord notification. The quarantine record carries the ``conductor.stand_down``
      line already emitted. The refusal itself is never posted into another chatroom thread.
    """
    try:
        result: Any = await mcp.call_tool(
            "chatroom_post_message",
            {
                "project": project,
                "thread_id": thread_id,
                "msg_type": "report",
                "author": RELAY_AUTHOR,
                "content": render_stand_down_notice(event),
            },
        )
    except ThreadResolvedError as exc:
        logger.warning(
            "conductor.stand_down notice refused (thread %r resolved): %s — exiting non-zero",
            thread_id,
            exc,
        )
        raise StandDownError(event) from exc
    msg = result.get("msg") if isinstance(result, dict) else None
    msg_id = str(msg.get("msg_id") or "") if isinstance(msg, dict) else ""
    if not msg_id:
        logger.warning(
            "conductor.stand_down notice did not land in thread %r (no msg_id) — exiting non-zero",
            thread_id,
        )
        raise StandDownError(event)
    return msg_id


__all__ = [
    "EVENT_KIND_STAND_DOWN",
    "STAND_DOWN_EXIT_CODE",
    "LaunchResolution",
    "StandDownError",
    "StandDownReason",
    "UnresolvedItem",
    "emit_stand_down",
    "post_stand_down_notice",
    "render_stand_down_notice",
    "resolve_launch",
    "stand_down_event",
]
