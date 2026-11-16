"""``mindwire slot-prep``: run the turn-pre procedure for one thread's clone slot, once.

Thread T-ephemeral-impl-clone-slot-per-thread, PR-A (Bohr msg-8061 steps 0-7, msg-8229 / msg-8231
wiring). The sweep wrapper calls this, bounded, right before it launches a candidate in slot mode;
the wrapper never judges a slot itself. Nothing here posts to a chatroom: the one reader of the
output is the wrapper, through the payload row and the exit code below.

Exit codes (the command's own space, like ``clone-check`` / ``clone-recover``):

* ``0`` LAUNCH — the slot is ready; the payload's ``slot_dir`` is the turn's working directory.
* ``3`` SKIP — do not launch this tick; nothing is parked and no row is written (a live session,
  a held slot lock, an ``index.lock`` still being counted).
* ``4`` PARK — park **this thread only**, with the payload's ``park_code``.
* ``1`` the procedure itself raised: a one-line summary on stderr, no payload, so "could not run"
  is never confused with a decision.

Every decided run prints exactly one ``MINDWIRE_SLOT_PREP_PAYLOAD <json>`` line on stdout.

``sessions_under`` asks ``claude agents --json --all`` for background sessions whose ``cwd`` is
the slot or below it. When that list cannot be read the answer is
:class:`~spirrow_mindwire.clone_slot_prep.SessionsUnknown`: step 5 then skips instead of renaming
a slot somebody may still be in, and after three such ticks the procedure parks the thread with
``clone_slot_session_unknown`` (Bohr msg-8234 判断 2).

When this command exits 1 (or with any code not listed above) the wrapper parks the thread on
that tick with ``clone_slot_prep_crashed`` and never falls back to the shared ``repo_dir``
(Bohr msg-8236, Obj-18-1); nothing here counts crashes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from spirrow_mindwire.adapters.claude_bg import AgentListError, parse_agent_list
from spirrow_mindwire.clone_slot import mirror_path
from spirrow_mindwire.clone_slot_prep import (
    PrepAction,
    PrepResult,
    SessionsUnknown,
    SlotPrep,
    refresh_mirror,
)

SLOT_PREP_PAYLOAD_PREFIX = "MINDWIRE_SLOT_PREP_PAYLOAD"
EXIT_LAUNCH = 0
EXIT_SKIP = 3
EXIT_PARK = 4
EXIT_FAILED = 1
AGENT_LIST_TIMEOUT_S = 20.0

_EXIT_FOR = {
    PrepAction.LAUNCH: EXIT_LAUNCH,
    PrepAction.SKIP: EXIT_SKIP,
    PrepAction.PARK: EXIT_PARK,
}


def _is_under(child: str, parent: Path) -> bool:
    try:
        c = os.path.normcase(os.path.abspath(child))
        p = os.path.normcase(os.path.abspath(parent))
    except (TypeError, ValueError):
        return False
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def _one_line(text: str, limit: int = 300) -> str:
    return (" ".join(text.split()) or "-")[:limit]


def count_sessions_under(slot_dir: Path, agent_list_json: str) -> int:
    """Background sessions in ``agent_list_json`` whose cwd is ``slot_dir`` or below it."""
    return sum(1 for a in parse_agent_list(agent_list_json) if a.cwd and _is_under(a.cwd, slot_dir))


def claude_sessions_under(claude: str | None = None) -> Callable[[Path], int | SessionsUnknown]:
    """``sessions_under`` backed by ``claude agents --json --all`` (unreadable = unknown)."""
    exe = claude or shutil.which("claude") or "claude"

    def _sessions(slot_dir: Path) -> int | SessionsUnknown:
        try:
            proc = subprocess.run(
                [exe, "agents", "--json", "--all"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=AGENT_LIST_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return SessionsUnknown(_one_line(f"{type(exc).__name__}: {exc}"))
        if proc.returncode != 0:
            return SessionsUnknown(_one_line(f"exit {proc.returncode}: {proc.stderr}"))
        try:
            return count_sessions_under(slot_dir, proc.stdout)
        except AgentListError as exc:
            return SessionsUnknown(_one_line(str(exc)))

    return _sessions


def payload(
    result: PrepResult,
    *,
    mirror_refreshed: bool | None = None,
    mirror_seconds: float | None = None,
    prep_seconds: float | None = None,
) -> dict[str, Any]:
    """The payload row's fields. The three timings feed the launch-budget measurement (item 4)."""
    return {
        "action": result.action.value,
        "slot_dir": str(result.slot_dir) if result.slot_dir is not None else None,
        "park_code": result.park_code,
        "detail": result.detail,
        "record": result.record,
        "steps": list(result.steps),
        "mirror_refreshed": mirror_refreshed,
        "mirror_seconds": mirror_seconds,
        "prep_seconds": prep_seconds,
    }


def emit(result: PrepResult, **timings: Any) -> int:
    """Print the payload row and return the exit code for ``result``."""
    line = json.dumps(
        payload(result, **timings), ensure_ascii=False, separators=(",", ":"), default=str
    )
    print(f"{SLOT_PREP_PAYLOAD_PREFIX} {line}", flush=True)
    return _EXIT_FOR[result.action]


def run(
    prep: SlotPrep,
    *,
    refresh: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Run ``prep`` once and map it to the exit codes in the module docstring.

    ``refresh`` fetches the repo's mirror first; the wrapper sets it on the first slot-mode
    candidate of each repo per tick (msg-8061: once per tick per repo). A mirror that does not
    exist yet is created whatever ``refresh`` says, since step 6 cannot clone from nothing. A failed
    fetch keeps the old mirror and parks nobody (msg-8061); it only shows as
    ``mirror_refreshed: false`` in the payload.
    """
    mirror_refreshed: bool | None = None
    mirror_seconds: float | None = None
    try:
        if refresh or not mirror_path(prep.slots_root, prep.repo).exists():
            t0 = monotonic()
            prep.slots_root.mkdir(parents=True, exist_ok=True)
            mirror_refreshed = refresh_mirror(
                prep.slots_root, prep.repo, prep.origin_url, git=prep.git
            )
            mirror_seconds = round(monotonic() - t0, 3)
        t1 = monotonic()
        result = prep.run()
        prep_seconds = round(monotonic() - t1, 3)
    except Exception as exc:  # the wrapper must see "could not run", never a decision
        summary = " ".join(str(exc).split()) or "-"
        print(
            f"slot-prep: cannot prepare {prep.repo}/{prep.thread_id}: "
            f"{type(exc).__name__}: {summary}",
            file=sys.stderr,
        )
        return EXIT_FAILED
    return emit(
        result,
        mirror_refreshed=mirror_refreshed,
        mirror_seconds=mirror_seconds,
        prep_seconds=prep_seconds,
    )
