#!/usr/bin/env python3
"""Regenerate ``spec/adr_index.yaml`` — the naysayer's ADR index (ADR-2026-06-04-19 N-2).

Thin CLI over :mod:`spirrow_mindwire.naysayer.adr_index_gen`. The manifest is a derived
view (union of CLAUDE.md §M + the ADR bodies in ``docs/adr/``); shipping this generator
is what makes it a genuine derived view rather than a hand-maintained second source
(Tier B Finding-1, ``T-naysayer-unify-impl`` msg-442/443).

**Runs anywhere the repository is checked out, including CI.** It did not until
2026-09-11: the second source was ``spirrow-docs/_docmap.yaml``, a file on one machine in
a tree with no remote, so the union could not be rebuilt here and the committed copy could
not be drift-checked (ADR-2026-06-04-19 N-2 records that as unavoidable). The Drive→Git
migration put every ADR body in ``docs/adr/`` and ADR-2026-05-23-07 §6 moved canonicity
here, so the bodies are the source now. The id set came out identical on the day of the
switch — 19 either way, nothing gained or lost.

    python scripts/gen_adr_index.py
    python scripts/gen_adr_index.py --check   # exit 1 on drift, write nothing

The ``_docmap`` reader went with it. The one comparison it was wanted for — does the new
source produce the same index — was made on the day of the switch and is recorded in the
commit that made it: 19 ids either way, six titles longer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from spirrow_mindwire.naysayer.adr_index_gen import (
    adr_titles_from_repo,
    build_manifest_index,
    load_existing_body_locators,
    render_manifest,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CLAUDE_MD = _REPO_ROOT / "CLAUDE.md"
_DEFAULT_OUT = _REPO_ROOT / "spec" / "adr_index.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate spec/adr_index.yaml from CLAUDE.md §M + docs/adr/ bodies."
    )
    parser.add_argument("--claude-md", type=Path, default=_DEFAULT_CLAUDE_MD)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare with the committed manifest; exit 1 on drift, write nothing",
    )
    args = parser.parse_args(argv)

    claude_md = args.claude_md.read_text(encoding="utf-8")
    index = build_manifest_index(claude_md, adr_titles_from_repo(_REPO_ROOT))
    # Round-trip: hand-maintained ``body:`` locators (per T-adr-index-omits-chatroom-
    # body-locator §4-1) must survive regeneration. Missing ids fall back to the
    # ``drive`` default inside render_manifest.
    body_locators = load_existing_body_locators(args.out)
    rendered = render_manifest(index, body_locators)

    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != rendered:
            print(
                f"DRIFT: {args.out} is out of date — rerun without --check to regenerate",
                file=sys.stderr,
            )
            return 1
        print(f"{args.out} is up to date ({len(index)} ADRs)")
        return 0

    # newline="\n": Path.write_text translates to the platform default, so regenerating
    # on the loop host would rewrite every line as CRLF against an LF-committed file.
    # It never showed while this ran only on the Linux docs host; it does now that the
    # generator no longer needs a file that lives there.
    with args.out.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
    print(f"wrote {args.out} ({len(index)} ADRs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
