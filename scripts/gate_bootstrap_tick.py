"""Run one gate-bootstrap tick for one project — the sweep wrapper's per-project entry point.

Called once per candidate project by ``deploy/run-conductor-scheduled.ps1``,
BEFORE the main sweep loop. Costs on the ``DECLARED`` branch (the steady state
once every project has a gate) are two stat/git calls and zero MCP traffic;
costs on the ``UNUSABLE`` branch are the same. Only when the predicate flips
does this script touch magickit.

**LLM-free**: this script never calls a model. The propose_content is a
fixed template (see :mod:`spirrow_mindwire.gate_bootstrap`) and the decide_content
on close is likewise a fixed string. The *question* is universal ("declare a
green"); only the *answer* is repo-specific, and the answer is what the
implementer writes.

The design source is chatroom thread ``T-new-project-gate-bootstrap``
(msg-1962 request; msg-1963/1965/1967 design; msg-1964/1966/1968 naysayer);
the module docstring on :mod:`spirrow_mindwire.gate_bootstrap` carries the
full write-up. The sweep wrapper interprets nothing — it reads the JSON on
stdout, logs the ``reason``, and moves on.

Output contract (JSON on stdout, one object):

  {
    "project":     "<project id>",
    "repo_dir":    "<path>",
    "status":      "declared" | "stale_worktree" | "missing" | "new_repo" | "unusable",
    "upstream_ref": "origin/HEAD" | "origin/main" | "origin/develop" | null,
    "reason":      "<human-readable summary>",
    "action":      "opened" | "already_open" | "closed" | "already_closed"
                    | "no_action" | "close_refused",
    "thread_id":   "T-gate-bootstrap-<project>" | null,
    "error":       "<message>" | null
  }

Exit codes:
  0  — the tick did what the predicate asked (including no_action).
  1  — a magickit failure or an unexpected error. Details on stderr. The sweep
       wrapper logs and continues; the next tick will retry (idempotent).

The script is fail-open on the SWEEP: a broken tick here must not stop the
main sweep from running. That is why the caller runs this out-of-band and
does not gate the rest of the tick on its exit code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from spirrow_mindwire.gate_bootstrap import (
    DEFAULT_SWEEPER_OWNER,
    GateBootstrapCloseError,
    GateStatus,
    close_alert,
    inspect_gate,
    open_alert,
    should_alert,
    thread_id_for,
)
from spirrow_mindwire.magickit.client import (
    MagickitMcpError,
    McpToolCaller,
    StreamableHttpChatroomMcp,
)

# stdout on Windows defaults to a legacy codepage (e.g. cp932) that cannot
# encode the U+2014 em dash carried in the ``MISSING`` / ``NEW_REPO`` / error
# branch ``reason`` strings. The load-bearing fix is ``ensure_ascii=True`` on
# every ``json.dumps`` call that writes to stdout (see the two ``print`` call
# sites in ``main``): that emits pure ASCII (astral chars as ``\uXXXX``
# surrogate pairs), which encodes safely under any terminal codepage AND
# parses under PowerShell's ``ConvertFrom-Json``. No stdout reconfiguration
# is needed — reconfiguring the stream to UTF-8 would in fact defeat the
# operator's console by writing raw UTF-8 bytes into a cp932 read path,
# rendering any encodable native text as mojibake (PR #210 gate round-2).
#
# stderr is a separate concern. The tick prints human-readable diagnostics
# (``print(f"gate_bootstrap_tick: unhandled error: {exc!r}", file=sys.stderr)``
# below) and Python's default traceback also lands there. An exception ``repr``
# is normally ASCII, but a localized file path or a wrapped MCP payload can
# carry native text that would crash a raw cp932 write. Setting
# ``errors="backslashreplace"`` — WITHOUT overriding the encoding — leaves
# encodable text untouched (Japanese in a path stays readable in the cp932
# console) and escapes only the characters that would otherwise raise. The
# ``getattr`` guard exists because ``reconfigure`` is a ``TextIOWrapper``
# method and mypy needs the runtime probe; the same convention lives in
# ``scripts/dogfood_smoke.py`` etc.
_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(errors="backslashreplace")


async def _run_tick(
    project: str,
    repo_dir: Path,
    *,
    owner: str,
    mcp_url: str | None,
    merge_commit_sha: str | None,
    mcp_factory: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Do one tick's work for one project. Returns (exit_code, output_object).

    Split from ``main`` so tests can drive it against a fake ``McpToolCaller``
    without going through the CLI.
    """
    inspection = inspect_gate(project, repo_dir)
    out: dict[str, Any] = {
        "project": inspection.project,
        "repo_dir": str(inspection.repo_dir),
        "status": inspection.status.value,
        "upstream_ref": inspection.upstream_ref,
        "reason": inspection.reason,
        "action": "no_action",
        "thread_id": None,
        "error": None,
    }

    # UNUSABLE fails closed (msg-1963 D-6): no MCP call at all.
    if inspection.status == GateStatus.UNUSABLE:
        return 0, out

    # The mcp_factory seam exists for tests only: production always uses the
    # real StreamableHttpChatroomMcp. A factory (not a caller) is injected so
    # `mcp_url` still means what it means in production.
    mcp: McpToolCaller = (
        mcp_factory(mcp_url) if mcp_factory is not None else StreamableHttpChatroomMcp(mcp_url)
    )
    thread_id = thread_id_for(project)
    out["thread_id"] = thread_id

    if should_alert(inspection.status):
        try:
            result = await open_alert(mcp, project=project, owner=owner)
        except MagickitMcpError as exc:
            out["error"] = f"open_alert failed: {exc}"
            return 1, out
        out["action"] = "already_open" if result.already_exists else "opened"
        return 0, out

    # DECLARED / STALE_WORKTREE — attempt idempotent close. Not-found /
    # already-resolved envelopes fold into ``already_closed`` (was_open=False).
    try:
        result = await close_alert(
            mcp, project=project, owner=owner, merge_commit_sha=merge_commit_sha
        )
    except GateBootstrapCloseError as exc:
        # Loud surface for the msg-1968 obligation: if the close is refused,
        # the operator sees this on the next tick's log, not never.
        out["action"] = "close_refused"
        out["error"] = str(exc)
        return 1, out
    except MagickitMcpError as exc:
        out["error"] = f"close_alert transport failure: {exc}"
        return 1, out
    out["action"] = "closed" if result.was_open else "already_closed"
    return 0, out


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="project id, matches the sweep-list entry")
    parser.add_argument(
        "--repo-dir",
        required=True,
        type=Path,
        help="repo_dir from the sweep-list entry (the implementer's working clone)",
    )
    parser.add_argument(
        "--owner",
        default=DEFAULT_SWEEPER_OWNER,
        help=(
            "owner name for the alert thread (chatroom identity). "
            "Default reuses the PR-review orchestrator's identity."
        ),
    )
    parser.add_argument(
        "--url",
        default=None,
        help="magickit MCP URL (default: in-code / MINDWIRE_MAGICKIT_MCP_URL env)",
    )
    parser.add_argument(
        "--merge-commit-sha",
        default=None,
        help=(
            "Optional evidence sha to write into decide_content when closing. "
            "Not required — the mechanism works without it — but including it "
            "makes the close self-explanatory in the ledger."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        exit_code, out = asyncio.run(
            _run_tick(
                args.project,
                args.repo_dir,
                owner=args.owner,
                mcp_url=args.url,
                merge_commit_sha=args.merge_commit_sha,
            )
        )
    except Exception as exc:  # top-level guard — the sweep must not hang on us
        # The stdout is the wrapper's parse target; the stderr line is the
        # human-readable "why". Both are needed: the wrapper still gets a
        # JSON object with an error field, and the operator sees the trace.
        # ``ensure_ascii=True`` is the load-bearing fix here (and on the
        # success-path ``print`` below): the JSON becomes pure ASCII, which
        # (a) encodes safely under any terminal codepage — including cp932,
        # the branch that broke msg-2290 — and (b) parses cleanly under
        # PowerShell's ``ConvertFrom-Json``, which is JSON-strict and rejects
        # anything outside the ``\uXXXX`` escape (surrogate pairs included).
        # No stream reconfiguration is used to defend stdout: it would only
        # add a second, redundant defence AND corrupt the operator's console
        # by writing raw UTF-8 bytes into a cp932 reader.
        print(
            json.dumps(
                {
                    "project": args.project,
                    "repo_dir": str(args.repo_dir),
                    "status": "error",
                    "upstream_ref": None,
                    "reason": f"gate_bootstrap_tick crashed: {exc!r}",
                    "action": "no_action",
                    "thread_id": None,
                    "error": str(exc),
                },
                ensure_ascii=True,
            )
        )
        print(f"gate_bootstrap_tick: unhandled error: {exc!r}", file=sys.stderr)
        return 1

    # See the crash-path comment above for why ``ensure_ascii=True`` here.
    print(json.dumps(out, ensure_ascii=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
