#!/usr/bin/env python3
"""Regenerate ``spec/adr_index.yaml`` — the naysayer's ADR index (ADR-2026-06-04-19 N-2).

Thin CLI over :mod:`spirrow_mindwire.naysayer.adr_index_gen`. The manifest is a derived
view (union of CLAUDE.md §M + spirrow-docs ``_docmap`` adr entries); shipping this
generator is what makes it a genuine derived view rather than a hand-maintained second
source (Tier B Finding-1, ``T-naysayer-unify-impl`` msg-442/443).

Run by the **proposer on the docs host** (where ``spirrow-docs/_docmap.yaml`` lives) when
an ADR is added/accepted — the loop host/CI has no ``_docmap``, so neither runtime union
nor a CI drift-check is possible and the committed copy is unavoidable:

    python scripts/gen_adr_index.py --docmap /path/to/spirrow-docs/_docmap.yaml
    python scripts/gen_adr_index.py --docmap ... --check   # exit 1 on drift, no write

The ``_docmap`` reader is schema-tolerant (see ``adr_index_gen``); verify the regenerated
file against the real ``_docmap`` on first run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from spirrow_mindwire.naysayer.adr_index_gen import build_manifest_index, render_manifest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CLAUDE_MD = _REPO_ROOT / "CLAUDE.md"
_DEFAULT_OUT = _REPO_ROOT / "spec" / "adr_index.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate spec/adr_index.yaml from CLAUDE.md §M + spirrow-docs _docmap."
    )
    parser.add_argument(
        "--docmap", required=True, type=Path, help="path to spirrow-docs/_docmap.yaml"
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
    docmap_data = yaml.safe_load(args.docmap.read_text(encoding="utf-8"))
    index = build_manifest_index(claude_md, docmap_data)
    rendered = render_manifest(index)

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

    args.out.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.out} ({len(index)} ADRs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
