"""D-2 log-only enumerator CLI — count threads live in the chatroom but missing from ``sweep.json``.

Owns the CLI side of :ref:`T-sweep-intake-and-quarantine-stalls`'s D-2
(Bohr msg-2529 §8 refined by msg-2531 §1). The predicate itself lives in
:mod:`spirrow_mindwire.unregistered_threads`; this script is the network
+ subprocess boundary around it, patterned after
:mod:`scripts.pr_review_sweep_phase0` (write-zero MCP wrapper) and
:mod:`scripts.parked_humans` (per-project, fail-open on per-item errors).

**Log-only until P-1 ∧ P-2 hold** (msg-2531 §1). The wrapper that would
render this count in the daily digest is intentionally NOT wired in this
PR: doing so today would ship an alarm with a known 31 % false-positive
rate (msg-2530 point 2) because the eight threads Bohr enumerated in
msg-2529 §3 as "intentionally parked" cannot yet be moved to
``status=parked`` (magickit exposes no transition, msg-2531 §1 P-1). The
script exists so those counts can be *observed* against real chatroom
data before the digest starts publishing them.

Read-only: every chatroom tool this script may call is wrapped in
:class:`ReadOnlyMcp` (a two-entry read allowlist). A future edit that
reaches for a write tool fails loudly here, not silently in a downstream
side effect — the same discipline
``scripts/pr_review_sweep_phase0.py`` documents at ``READ_ONLY_TOOLS``.

Input
-----

The one required argument is the ``sweep.json`` path — the projects list
and the "already-registered" set both come from there. Distinct projects
appearing in ``sweep.json``'s ``candidates`` array are enumerated in the
order they first appear in the file, so the output is stable across
runs.

::

    uv run python scripts/unregistered_threads.py \\
        --sweep-config ~/spirrow-mindwire-data/config/sweep.json

Output (stdout, JSON, ASCII-only per D-33)
------------------------------------------

::

    {
      "projects": [
        {
          "project": "spirrow-mindwire",
          "unregistered_count": 3,
          "unregistered": ["T-a", "T-b", "T-c"],
          "error": null
        },
        {
          "project": "spirrow-voxelworld",
          "unregistered_count": null,
          "unregistered": [],
          "error": "chatroom_list_threads failed: TimeoutException: ..."
        }
      ],
      "unregistered_count_total": 3,
      "unmeasured_projects": ["spirrow-voxelworld"],
      "any_unmeasured": true
    }

``unregistered_count == null`` means "did not measure" — the MCP call
failed, and ``error`` carries the reason. This is intentionally distinct
from ``unregistered_count == 0`` (measured, and nothing is unregistered)
so the wrapper can render ``?`` for the former and a real number for
the latter (msg-2531 §2 invariant 2: "0 件" と "測れなかった" を同じ
表示にしない).

Fail direction
--------------

* Setup failures (missing / unparseable ``sweep.json``, or an outright
  broken ``asyncio.run``) exit non-zero. The wrapper reads a non-zero
  exit as "the probe itself broke" and renders ``?`` — the same
  contract :mod:`scripts.parked_humans` uses.
* Per-project MCP failures (transport, envelope, unexpected shape) do
  NOT exit non-zero. They land in ``projects[].error`` with
  ``unregistered_count = null``, so a one-project outage never masks
  the answer for the others.
* Bounded execution (msg-2531 §2 invariant 3) is the wrapper's job, not
  this script's: the wrapper subprocess-launches this script with a
  timeout and treats timeout / non-zero exit / JSON-parse failure
  uniformly as ``?``. Adding a script-side timeout on top would double-
  count that boundary and produce two different failure modes for
  "took too long".

Wiring
------

There is intentionally no ``scripts/`` wiring in this PR. The wrapper
call site (``deploy/run-conductor-scheduled.ps1`` digest section, around
L1067 / L1243) will be added in a follow-up once P-1 and P-2 both hold.
Until then this script is manually runnable for observation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Protocol

from spirrow_mindwire.config import DEFAULT_DATA_DIR
from spirrow_mindwire.magickit.client import MagickitMcpError, StreamableHttpChatroomMcp
from spirrow_mindwire.unregistered_threads import (
    LIVE_STATUSES,
    EnumerateReport,
    ProjectReport,
    RegisteredIndex,
    UnregisteredThreadsError,
    enumerate_project,
    load_registered,
    project_error_report,
)

#: Every chatroom tool this script may call. Membership is enforced by
#: :class:`ReadOnlyMcp`; adding a name here is a design decision, not a
#: convenience knob.
READ_ONLY_TOOLS = frozenset({"chatroom_list_threads"})

#: One listing page — the same size Phase 0 uses. Large enough that a
#: typical project fits in one round trip, small enough that a runaway
#: response stops after a bounded read.
_PAGE = 200


class Phase0WriteAttemptedError(RuntimeError):
    """A tool outside :data:`READ_ONLY_TOOLS` was requested. D-2 does not write."""


class ToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class ReadOnlyMcp:
    """Allowlist wrapper: the machine check behind this script's write-zero claim."""

    def __init__(self, inner: ToolCaller) -> None:
        self._inner = inner

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in READ_ONLY_TOOLS:
            raise Phase0WriteAttemptedError(
                f"D-2 is read-only; refusing to call {name!r}. Allowed: {sorted(READ_ONLY_TOOLS)}"
            )
        return await self._inner.call_tool(name, arguments)


def _as_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


async def _list_live_threads(mcp: ToolCaller, project: str) -> list[dict[str, Any]]:
    """Every live thread in a project, paged, filtered server-side to :data:`LIVE_STATUSES`.

    Passing ``status_filter`` at the tool boundary keeps the request
    scope narrow: a project with hundreds of resolved threads does not
    pay for their metadata just so this predicate can throw them away.
    The same trim also documents intent — the client asks for exactly
    the set it plans to consume.
    """
    statuses = sorted(LIVE_STATUSES)
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _as_dict(
            await mcp.call_tool(
                "chatroom_list_threads",
                {
                    "project": project,
                    "status_filter": statuses,
                    "limit": _PAGE,
                    "offset": offset,
                },
            )
        )
        if page is None:
            break
        items = page.get("items")
        if not isinstance(items, list) or not items:
            break
        out.extend(item for item in items if isinstance(item, dict))
        offset += len(items)
        total = page.get("total")
        if isinstance(total, int) and offset >= total:
            break
    return out


async def _enumerate_project_with_recovery(
    mcp: ToolCaller, project: str, registered: RegisteredIndex
) -> ProjectReport:
    """Enumerate one project, converting any MCP failure into a :class:`ProjectReport` error.

    Two catch tiers, matching the failure envelope msg-2531 §2 asks for:

    * :class:`~spirrow_mindwire.magickit.client.MagickitMcpError` — the
      wrapped transport / envelope failure. Reason string is preserved.
    * Every other :class:`Exception` — a programming error inside the
      listing helper. Recorded as ``unexpected <ClassName>: <str(exc)>``
      so a one-project outage still surfaces without crashing the whole
      enumeration. ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``)
      is deliberately NOT caught: those must still terminate the CLI.
    """
    try:
        threads = await _list_live_threads(mcp, project)
    except MagickitMcpError as exc:
        return project_error_report(project, f"chatroom_list_threads failed: {exc}")
    except Exception as exc:  # pragma: no cover - defence in depth
        return project_error_report(project, f"unexpected {type(exc).__name__}: {exc}")
    return enumerate_project(project, threads, registered)


async def _run(registered: RegisteredIndex, url: str | None) -> EnumerateReport:
    mcp = ReadOnlyMcp(StreamableHttpChatroomMcp(url))
    reports: list[ProjectReport] = []
    for project in registered.projects:
        reports.append(await _enumerate_project_with_recovery(mcp, project, registered))
    return EnumerateReport(projects=tuple(reports))


def default_sweep_config_path() -> Path:
    return DEFAULT_DATA_DIR / "config" / "sweep.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sweep-config",
        default=None,
        help=f"sweep.json path (default: {default_sweep_config_path()})",
    )
    parser.add_argument("--url", default=None, help="magickit MCP URL (default: env/in-code)")
    args = parser.parse_args()

    path = Path(args.sweep_config) if args.sweep_config else default_sweep_config_path()
    try:
        registered = load_registered(path)
    except UnregisteredThreadsError as exc:
        print(f"unregistered_threads: {exc}", file=sys.stderr)
        return 2

    if not registered.projects:
        # A sweep.json with no candidates is a valid file but produces no
        # projects to enumerate. Report an empty run rather than exit
        # non-zero: the operator has already been warned by the sweep
        # wrapper (which throws on an empty candidate list), and D-2 is
        # not the right place to re-litigate that decision.
        print(json.dumps(EnumerateReport().as_json(), ensure_ascii=True, sort_keys=True))
        return 0

    try:
        report = asyncio.run(_run(registered, args.url))
    except Phase0WriteAttemptedError as exc:
        print(f"unregistered_threads: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defence in depth
        print(f"unregistered_threads: run failed: {exc}", file=sys.stderr)
        return 1

    # D-33: stdout JSON is ASCII-only. ``error`` fields embed ``str(exc)``
    # which may contain non-ASCII; ``ensure_ascii=True`` emits ``\uXXXX``
    # escapes so the byte stream is invariant across code pages, matching
    # the wrapper's UTF-8 read side.
    print(json.dumps(report.as_json(), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
