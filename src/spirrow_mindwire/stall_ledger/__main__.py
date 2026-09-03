"""CLI entry point: ``python -m spirrow_mindwire.stall_ledger.failure_class``.

Reads a ``session_log_tail`` from stdin (whole-blob mode) and writes the resolved
``failure_class`` label to stdout on one line. Nonzero exit is reserved for a
plumbing failure (unreadable stdin) — an ``unknown`` classification is a normal
successful outcome, so it exits zero and prints ``unknown``.

The wrapper ``run-conductor-scheduled.ps1`` calls this at quarantine time. The
PowerShell side reads one line of stdout and stores it in the record's
``failure_class`` field. If the CLI fails or is not on PATH, the wrapper stores
``unknown`` — the ledger is designed to tolerate the field being missing exactly
because a partial wire-up is worse than none for its noisiness invariant.
"""

from __future__ import annotations

import sys

from spirrow_mindwire.stall_ledger.failure_class import classify_failure


def main() -> int:
    try:
        blob = sys.stdin.read()
    except OSError as exc:
        # Stdout an ``unknown`` so the PowerShell side never has to distinguish "the
        # tool failed" from "the tool ran and said unknown" — the ledger's
        # noisiness invariant already covers unknown. Stderr the reason so an
        # operator debugging the wire-up sees it.
        print("unknown")
        print(f"stall-ledger classify_failure: stdin unreadable ({exc})", file=sys.stderr)
        return 2
    print(classify_failure(blob))
    return 0


if __name__ == "__main__":  # pragma: no cover — invoked only from the wrapper
    raise SystemExit(main())
