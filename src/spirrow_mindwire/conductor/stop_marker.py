"""Machine-readable marker on the conductor's stop notices (T47 S-3).

Thread: T-agmsg-transport-lessons-readiness-session-claim-board (Bohr msg-5196 S-3, revised in
msg-5199, endorsed by Einstein msg-5200; adopted by the human as Tier-C point 3). Design:
``docs/operator-board-design.md`` §18.

The conductor posts three stop notices under ``conductor-relay``, all ending ``NEXT: human``:
``conductor.stalled`` (:mod:`.stall`), ``conductor.stand_down`` (:mod:`.stand_down`) and the
spawn give-up after the last ``spawn.timeout`` (:mod:`.spawn_timeout`). To the board's R-HUMAN
rule they look like any Tier-C request, and R-HUMAN would have the composer build options for
them, turning a watchdog stop into a question nobody needed to be asked (D7). The board's
R-RELAY-STOP rule (§18) takes them first, into ``stalled``. It recognises them by this marker,
not by matching the Japanese prose:

``<!-- mindwire:stop v1 {"kind":"conductor.stalled","project":"...",...} -->``

The payload is the notice's own :class:`~spirrow_mindwire.value_objects.Event`: ``kind`` plus
the event's fields, flat, exactly as the log line carries them (§18 ``facts.stop_event``).

Placement: the marker goes on its own line **above** the final ``NEXT: human`` line, with a blank
line between them. The ``NEXT:`` line stays the last line of the body, so nothing that reads the
last line changes, and the line directly above ``NEXT:`` stays empty, so the ``TIER-C:`` /
``STOP:`` readers in :mod:`.handoff` see nothing there. (The ci-route marker sits after its
``NEXT:``; this one does not, for those two reasons.)

The writer is exact and the reader tolerant, as with the ci-route marker in
:mod:`.gate_records`. The reader is here so that one test drives both directions. The board
(P1) is its consumer; nothing in the conductor reads it back.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from ..value_objects import Event

_STOP_OPEN = "<!-- mindwire:stop v1 "
_STOP_CLOSE = " -->"

#: Tolerant on read: whitespace may vary and the JSON may be re-wrapped. The payload is captured
#: non-greedily up to the closing comment; the writer's ``>`` escape keeps ``-->`` out of it.
_STOP_RE = re.compile(r"<!--\s*mindwire:stop\s+v1\s+(\{.*?\})\s*-->", re.DOTALL)


def render_stop_marker(event: Event) -> str:
    """The marker for ``event``: ``kind`` first, then the event's fields.

    ``>`` is escaped because several fields are free text (``detail`` on a stand-down, a thread
    or identity name). Without the escape, a value containing ``-->`` would end the comment early
    and the reader's non-greedy capture would stop short. ``>`` is never JSON syntax, so the
    escape can only land inside a string, and ``json.loads`` restores it.
    """
    if "kind" in event.fields:
        raise ValueError(f"event field 'kind' would shadow the event kind ({event.kind!r})")
    payload = json.dumps(
        {"kind": event.kind, **event.fields},
        separators=(",", ":"),
        ensure_ascii=True,
    ).replace(">", "\\u003e")
    return f"{_STOP_OPEN}{payload}{_STOP_CLOSE}"


def parse_stop_marker(body: str) -> dict[str, Any] | None:
    """The payload of the **last** stop marker in ``body``, or ``None``.

    A malformed marker (not a JSON object, or no string ``kind``) is skipped rather than raised:
    the caller is a scheduled tick reading a thread it does not control. The cost of skipping
    one is that the notice falls through to R-HUMAN, which is how it was handled before.
    """
    found: dict[str, Any] | None = None
    for raw in _STOP_RE.findall(body):
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if isinstance(payload, Mapping) and isinstance(payload.get("kind"), str):
            found = dict(payload)
    return found


__all__ = ["parse_stop_marker", "render_stop_marker"]
