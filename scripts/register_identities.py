"""Register every classified identity in the identity store (T-role-null write half).

Reads :file:`spec/identity/legitimate_roles.yaml` (the source of truth). For each
entry it builds the ``upsert_identity`` arguments with
:func:`~spirrow_mindwire.identity.build_upsert_identity_args`, the one
constructor, which refuses any payload the two-way guard rejects. With
``--apply`` it sends each payload through the magickit MCP surface. It then reads
the record back with ``get_identity`` and runs the same guard on what the store
actually holds.

Without ``--apply`` this is a dry run. It prints the planned payloads and makes
no network call.

**This is the PR-A deploy check (msg-1706 §3 DoD 2).** Prismind rejects an
``independence_class`` outside its enum with a clean ``success=False``. If
``"machine"`` is not deployed, the first machine upsert fails that way. The
script then STOPS and exits 3, and it registers nothing after that entry. No
separate pre-probe exists, because the live result is the probe. The script
keeps no local copy of the enum (msg-1706 §2).

Output (stdout JSON)::

    {"mode": "apply" | "dry-run",
     "classification_path": "...",
     "results": [{"identity_name": ..., "args": {...},
                  "upsert": {<raw upsert_identity response>} | null,
                  "readback": {<check_store_record row>} | null}],
     "stopped_at": null | "<identity_name>",
     "ok": true | false}

Exit codes: 0 all registered and every read-back is guard-clean (or dry run);
1 the script failed (classification unreadable, constructor refused, transport
error); 3 an upsert returned ``success=False`` (stopped there); 4 every upsert
succeeded but a read-back found a guard violation.

Re-running is idempotent. ``upsert_identity`` updates in place, so a second
``--apply`` rewrites the same values.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from spirrow_mindwire.identity import (
    ClassificationError,
    IdentityCollisionError,
    LegitimateRolesFile,
    RegistrationRefusedError,
    build_upsert_identity_args,
    check_store_record,
    default_classification_path,
    load_legitimate_roles,
)
from spirrow_mindwire.magickit.client import McpToolCaller, StreamableHttpChatroomMcp

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UPSERT_REFUSED = 3
EXIT_READBACK_VIOLATION = 4


def plan(classification: LegitimateRolesFile) -> list[dict[str, Any]]:
    """Build every payload up front, so one refused entry aborts before any write."""
    return [build_upsert_identity_args(entry) for entry in classification.entries]


async def apply(
    mcp: McpToolCaller, classification: LegitimateRolesFile, planned: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], str | None, int]:
    """Upsert each planned payload in order; read back and guard each one.

    Returns ``(results, stopped_at, exit_code)``.
    """
    results: list[dict[str, Any]] = []
    readback_dirty = False
    for entry, args in zip(classification.entries, planned, strict=True):
        row: dict[str, Any] = {
            "identity_name": entry.name,
            "args": args,
            "upsert": None,
            "readback": None,
        }
        results.append(row)
        upsert: Any = await mcp.call_tool("upsert_identity", args)
        row["upsert"] = upsert
        if not isinstance(upsert, dict) or upsert.get("success") is not True:
            # DoD 2 / msg-4902 §4 step 3: stop and report. Everything after this entry
            # stays unregistered, so the store never holds a partial set past a refusal.
            return results, entry.name, EXIT_UPSERT_REFUSED
        got: Any = await mcp.call_tool("get_identity", {"identity_name": entry.name})
        readback = check_store_record(entry, got if isinstance(got, dict) else {})
        row["readback"] = readback
        if readback["status"] != "found" or readback["violations"]:
            readback_dirty = True
    return results, None, EXIT_READBACK_VIOLATION if readback_dirty else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually call upsert_identity (default: dry run, no network)",
    )
    parser.add_argument("--classification", type=Path, default=None)
    parser.add_argument("--url", default=None, help="magickit MCP URL (default: env)")
    args = parser.parse_args(argv)

    classification_path = args.classification or default_classification_path()
    try:
        classification = load_legitimate_roles(classification_path)
        planned = plan(classification)
    except (ClassificationError, IdentityCollisionError, RegistrationRefusedError, OSError) as exc:
        print(f"register_identities: {exc}", file=sys.stderr)
        return EXIT_FAILED

    out: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "classification_path": str(classification_path),
        "results": [
            {"identity_name": e.name, "args": a, "upsert": None, "readback": None}
            for e, a in zip(classification.entries, planned, strict=True)
        ],
        "stopped_at": None,
        "ok": True,
    }
    code = EXIT_OK
    if args.apply:
        try:
            results, stopped_at, code = asyncio.run(
                apply(StreamableHttpChatroomMcp(args.url), classification, planned)
            )
        except Exception as exc:
            print(f"register_identities: apply failed: {exc}", file=sys.stderr)
            return EXIT_FAILED
        out["results"] = results
        out["stopped_at"] = stopped_at
        out["ok"] = code == EXIT_OK
    print(json.dumps(out, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
