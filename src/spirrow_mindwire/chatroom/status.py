"""The closed set of magickit thread statuses that mean "this thread is finished".

One set, two readers (T-sweep-admission-ignores-thread-status):

* ``scripts/head_skip_decide.py`` — the sweep's per-tick admission. A finished thread must be
  SKIPPED before any role session starts, because magickit refuses the post only after the role
  has already spent a full inference (measured: 22,970 input tokens discarded on
  ``ChatroomThreadResolvedError``).
* :mod:`spirrow_mindwire.pr_review_sweep.phase0` — S-pre intake exclusion.

The function takes a **status string**, never an API payload. The two callers read that string
from differently-shaped responses, and each caller owns its own extraction:

* ``chatroom_list_threads`` items carry ``status`` at the item's top level;
* ``chatroom_get_thread(mode="full")`` carries it NESTED, at ``result["thread"]["status"]``
  (operator measurement against production magickit, msg-4749). Reading ``result["status"]``
  there always yields nothing — and because head-skip fails open on an empty status, that
  mistake would ship the feature silently disabled.

This set is deliberately NOT :data:`spirrow_mindwire.lifecycle.transitions.TERMINAL_STATES`:
that one describes mindwire's own local thread schema (``terminated`` / ``archived``), not the
statuses magickit reports.

Closed on purpose: adding a status widens what the sweep silently skips, so a change here needs
a test change. An unknown status is NOT terminal — head-skip fails open on it.
"""

from __future__ import annotations

FINISHED_STATUSES: frozenset[str] = frozenset({"resolved", "superseded"})


def is_terminal_status(status: str) -> bool:
    """True iff ``status`` is one of :data:`FINISHED_STATUSES` (exact, case-sensitive match)."""
    return status in FINISHED_STATUSES
