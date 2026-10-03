"""The ``STOP:`` line as a disposition: what is sent to magickit, and how a stop is classified.

T-next-line-carries-who-not-why, Slice 3 (Bohr msg-5175 §3; the input table is msg-4716 §2; the
mindwire side of the send is msg-5179 §3; the classification moved here from magickit in magickit
msg-1015 v11 §1). Slice 1 (#363) taught :mod:`.handoff` to PARSE the line into a
:class:`~.handoff.StopLine`; this module is the one place that turns that parse into the two
things Slice 3 consumes, so the gateway, the conductor's stop line and the park-wake tick cannot
hold three readings of the same line:

* :func:`disposition_payload` — the ``disposition`` argument of ``chatroom_post_message``
  (magickit #98's shape: ``{"kind": "done"}`` or ``{"kind": "blocked_on", "trigger": {"arm",
  "ref"}, "wake": <identity>}``). Only the two accepted forms produce one. ``ABSENT`` and
  ``MALFORMED`` send nothing: msg-5179 §3, "disposition を付けずに送る" — magickit then records
  the post with a null disposition and does not classify it (v11 §1).
* :func:`classify_stop` — the four-way split of a ``NEXT: none`` (v11 §1): a valid ``STOP:`` line
  is ``done`` / ``blocked_on``; no valid line is ``unclassified`` when an agent wrote it (the
  accident candidate, msg-2014 §1 (4)) and ``human_close`` when the human did, so a person
  closing a thread is never counted as an accident (msg-5179 §2).

The ``human`` arm and a ``human`` / ``none`` wake never reach :func:`disposition_payload`: the
parser already reads them as ``MALFORMED`` (B-5, msg-4716 §2), which sends nothing. magickit #98
refuses them from an agent anyway (``DispositionHumanNotAllowedError``); this side not producing
them is what keeps an agent's reply from being refused for them.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from ..value_objects import Role
from .handoff import HandoffKind, StopLine, StopStatus, resolve_handoff


class StopClass(StrEnum):
    """Why a ``NEXT: none`` thread stopped (v11 §1). Exactly one per ``NEXT: none`` head."""

    DONE = "done"
    BLOCKED_ON = "blocked_on"
    UNCLASSIFIED = "unclassified"
    HUMAN_CLOSE = "human_close"


def classify_stop(stop: StopLine, *, author_is_human: bool) -> StopClass:
    """The class of a ``NEXT: none`` whose ``STOP:`` line parsed to ``stop``.

    The author only matters when there is no valid line: a human who writes ``STOP: done`` is
    ``done`` like anyone else, while a human's bare ``NEXT: none`` is ``human_close``. Who counts
    as the human is the caller's rule (the conductor's ``human_identity``; the park-wake tick's
    registry lookup), and this function never guesses it.
    """
    if stop.status is StopStatus.DONE:
        return StopClass.DONE
    if stop.status is StopStatus.BLOCKED_ON:
        return StopClass.BLOCKED_ON
    return StopClass.HUMAN_CLOSE if author_is_human else StopClass.UNCLASSIFIED


def disposition_payload(stop: StopLine) -> dict[str, Any] | None:
    """The ``disposition`` value for ``stop``, or ``None`` when nothing is to be sent."""
    if stop.status is StopStatus.DONE:
        return {"kind": "done"}
    if stop.status is StopStatus.BLOCKED_ON:
        # The parser sets all three on BLOCKED_ON (handoff._stop_line_above_last_next).
        assert stop.trigger_arm is not None
        assert stop.trigger_operand is not None
        assert stop.wake is not None
        return {
            "kind": "blocked_on",
            "trigger": {"arm": stop.trigger_arm, "ref": stop.trigger_operand},
            "wake": stop.wake,
        }
    return None


def stop_line_of(
    body: str, roster: Mapping[str, Role], *, next_participant: str | None = None
) -> StopLine | None:
    """The ``STOP:`` parse of a message that resolves to ``NEXT: none``, else ``None``.

    Resolved exactly as the conductor resolves the head (:func:`~.handoff.resolve_handoff`, with
    the ``next_participant`` field when the reader has one), so a thread the conductor settled on
    is the thread classified here. The gateway passes no field: it posts none.
    """
    handoff = resolve_handoff(body, roster, next_participant=next_participant)
    if handoff.kind is not HandoffKind.NONE:
        return None
    return handoff.stop_line


def body_disposition(body: str, roster: Mapping[str, Role]) -> dict[str, Any] | None:
    """The ``disposition`` to send with ``body`` (only a valid ``STOP:`` line has one)."""
    stop = stop_line_of(body, roster)
    return disposition_payload(stop) if stop is not None else None


__all__ = [
    "StopClass",
    "body_disposition",
    "classify_stop",
    "disposition_payload",
    "stop_line_of",
]
