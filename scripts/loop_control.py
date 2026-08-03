"""Print a project's loop control state — the conductor sweep's stop switch.

The conductor reads this state itself, every round, and stops on ``hold``. So why read it here too?
Because the sweep's job is to decide whether a launch is worth paying for. Without this probe a held
project still costs a process start, a venv resolve and an MCP round trip per tick, ~288 times a
day, to be told something one cheap read already knew.

**This probe is an optimisation, not the enforcement.** That distinction is what lets it fail open
like the head probe: if it cannot read the state, the sweep launches anyway and the conductor's own
read — which fails *closed* — decides. A probe outage therefore costs one wasted launch, never an
unwanted autonomous run. Do not "fix" this by failing closed here; that would park every project the
moment magickit hiccups, which is exactly the silent-stop failure the sweep is built to avoid.

Output: one JSON object on stdout, e.g.

    {"project": "spirrow-voxelworld", "desired_state": "hold", "observed_state": "run",
     "configured": true}

``desired_state`` is what the operator set (or ``run`` for a project nobody has configured — see
``conductor/control.py`` on why an absent row is not a failure). ``observed_state`` is what the loop
last reported acting on; the sweep does not route on it, but it is printed so the log shows whether
a HOLD had already landed before this tick.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from spirrow_mindwire.magickit.client import MagickitMcpError, StreamableHttpChatroomMcp


async def fetch_control(project: str, url: str | None) -> dict[str, Any]:
    mcp = StreamableHttpChatroomMcp(url)
    payload: Any = await mcp.call_tool("loop_control_get", {"project": project})
    if not isinstance(payload, dict) or not isinstance(payload.get("desired_state"), str):
        raise MagickitMcpError(f"loop_control_get returned no desired_state: {payload!r}")
    return {
        "project": project,
        "desired_state": payload["desired_state"],
        "observed_state": payload.get("observed_state"),
        "configured": bool(payload.get("configured")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--url", default=None, help="magickit MCP URL (default: in-code/env default)"
    )
    args = parser.parse_args()

    try:
        state = asyncio.run(fetch_control(args.project, args.url))
    except Exception as exc:  # the caller only needs "probe unusable", not which way it broke
        # stderr + non-zero: the sweep launches the project anyway and lets the conductor decide.
        print(f"loop_control: probe failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(state, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
