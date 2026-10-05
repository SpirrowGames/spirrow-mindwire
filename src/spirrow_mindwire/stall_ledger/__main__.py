"""CLI entry point: ``python -m spirrow_mindwire.stall_ledger [--input PATH]``.

Reads a ``session_log_tail`` (whole-blob mode) and writes the resolved
``failure_class`` label to stdout on one line. Nonzero exit is reserved for a
plumbing failure (unreadable / undecodable input) — an ``unknown`` classification
is a normal successful outcome, so it exits zero and prints ``unknown``.

Input source (T-parked-humans-probe-has-no-timeout, Bohr msg-5611 §1):

* ``--input PATH`` — read the tail from a UTF-8 file. This is the path the
  wrapper ``run-conductor-scheduled.ps1`` uses (``Get-FailureClass`` writes the tail
  to a probe temp file and runs this CLI through the bounded ``Invoke-BoundedUvProbe``
  helper, whose child stdin is closed at start). A stdin feed through the
  ``uv`` → trampoline → python chain is what hung for 20+ minutes in msg-5322.
* no argument — read from stdin, as before, so existing tests and hand runs keep
  working.

The PowerShell side reads the first non-empty stdout line and stores it in the
record's ``failure_class`` field. If the CLI fails, times out or is not on PATH,
the wrapper stores ``unknown`` — the ledger is designed to tolerate the field
being missing exactly because a partial wire-up is worse than none for its
noisiness invariant.
"""

from __future__ import annotations

import argparse
import sys

from spirrow_mindwire.stall_ledger.failure_class import classify_failure


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m spirrow_mindwire.stall_ledger",
        description="Classify a session-log tail into a failure_class label.",
    )
    parser.add_argument(
        "--input",
        metavar="PATH",
        default=None,
        help="read the tail from this UTF-8 file instead of stdin",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError`` — the two
    # exception hierarchies do not overlap, so a bare ``except OSError`` would let
    # a corrupted-encoding input crash the script with a stack trace. The PowerShell
    # wrapper (``Get-FailureClass``) does trap the resulting non-zero exit and
    # substitutes ``unknown``, so the shipped artefact never observes a raise —
    # but that is a safety net on top, not an excuse for the CLI to reach it. Both
    # exception types are enumerated below so the "print unknown, exit 2" contract
    # covers the two live input-side failure modes we know about, for both sources.
    source = "stdin" if args.input is None else f"input file {args.input!r}"
    try:
        if args.input is None:
            blob = sys.stdin.read()
        else:
            with open(args.input, encoding="utf-8") as fh:
                blob = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        # Stdout an ``unknown`` so the PowerShell side never has to distinguish "the
        # tool failed" from "the tool ran and said unknown" — the ledger's
        # noisiness invariant already covers unknown. Stderr the reason (naming the
        # source) so an operator debugging the wire-up sees it.
        print("unknown")
        print(f"stall-ledger classify_failure: {source} unreadable ({exc})", file=sys.stderr)
        return 2
    print(classify_failure(blob))
    return 0


if __name__ == "__main__":  # pragma: no cover — invoked only from the wrapper
    raise SystemExit(main())
