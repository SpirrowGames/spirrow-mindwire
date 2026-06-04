"""Generator for ``spec/adr_index.yaml`` — the naysayer ADR index (ADR-2026-06-04-19 N-2).

Build-time logic behind ``scripts/gen_adr_index.py`` (kept in ``src`` so it is unit-tested
like the rest of the package; the script is a thin CLI wrapper). The committed manifest is
a **derived view**: this module regenerates it from the union of

  (a) the ADRs referenced in **CLAUDE.md §M** (in-repo, via :func:`parse_adr_index`), and
  (b) the ADR entries in the spirrow-docs **``_docmap.yaml``** (the canonical doc manifest).

Neither source alone is complete — §M omits the architecture ADRs (06/07/08/14/16-19) and
``_docmap`` omits the §M-only identity ADRs (09-13, which have no ``.md`` body) — so the
union is taken (Tier B Finding-1, ``T-naysayer-unify-impl`` msg-442/443). Shipping this
generator is what makes the committed file a genuine derived view rather than a
hand-maintained second source.

``_docmap`` schema: the exact shape is owned by spirrow-docs and is not available on the
loop host, so :func:`extract_docmap_adrs` is **schema-tolerant** — it walks the parsed
structure and picks up any mapping carrying a ``title`` plus an ADR id in one of its string
fields. Verify the regenerated file against the real ``_docmap`` on first run.
"""

from __future__ import annotations

import re
from typing import Any

from .adr_index import parse_adr_index

_ADR_ID_RE = re.compile(r"ADR-\d{4}-\d{2}-\d{2}-\d+")

# Emitted verbatim as the manifest's header; kept byte-identical to the committed
# spec/adr_index.yaml header so ``--check`` does not report spurious drift.
_HEADER = """\
# mindwire ADR index — union of CLAUDE.md §M references + spirrow-docs _docmap adr entries.
# GENERATED FILE — do not hand-edit. Regenerate with:
#     python scripts/gen_adr_index.py --docmap <path-to-spirrow-docs/_docmap.yaml>
# Run by the proposer on the docs host when an ADR is added/accepted (loop host/CI lacks
# _docmap, so this committed copy is unavoidable; ADR-2026-06-04-19 N-2 / msg-438/443).
# Derived view: id + title only (the canonical ADR body lives in Drive).
# CI does not regenerate this (no _docmap in CI); it only checks it parses + is well-formed."""

_ID_FIELDS = ("id", "adr_id", "path", "doc_id", "slug", "name", "file")


def _first_adr_id(node: dict[str, Any]) -> str | None:
    """First ADR id found in this mapping's string fields, else ``None``."""
    for key in _ID_FIELDS:
        value = node.get(key)
        if isinstance(value, str):
            match = _ADR_ID_RE.search(value)
            if match:
                return match.group(0)
    return None


def extract_docmap_adrs(docmap_data: Any) -> dict[str, str]:
    """Extract ``{adr_id: title}`` from a parsed ``_docmap`` structure (schema-tolerant).

    Walks the (possibly nested) structure and records any mapping that carries a non-empty
    ``title`` together with an ADR id in one of its string fields. Tolerant of the top-level
    shape (list / dict / nested) since the canonical ``_docmap`` schema lives in spirrow-docs.
    """
    found: dict[str, str] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            title = node.get("title")
            if isinstance(title, str) and title.strip():
                adr_id = _first_adr_id(node)
                if adr_id is not None:
                    found.setdefault(adr_id, title.strip())
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(docmap_data)
    return found


def build_manifest_index(claude_md: str, docmap_data: Any) -> tuple[tuple[str, str], ...]:
    """Union of CLAUDE.md §M references and ``_docmap`` ADR entries (deduped, sorted).

    Title preference: the §M summary when present (the in-repo curated text), else the
    ``_docmap`` title. The union is the point — it carries both the architecture ADRs §M
    omits and the identity ADRs ``_docmap`` omits.
    """
    section_m = dict(parse_adr_index(claude_md))
    docmap = extract_docmap_adrs(docmap_data)
    ids = set(section_m) | set(docmap)
    merged = {adr_id: (section_m.get(adr_id) or docmap.get(adr_id, "")) for adr_id in ids}
    return tuple(sorted(merged.items()))


def render_manifest(index: tuple[tuple[str, str], ...]) -> str:
    """Render the manifest YAML (header + ``adrs:`` list) — round-trips through PyYAML."""
    lines = [_HEADER, "adrs:"]
    for adr_id, title in index:
        escaped = title.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f"  - id: {adr_id}")
        lines.append(f'    title: "{escaped}"')
    return "\n".join(lines) + "\n"


__all__ = ["build_manifest_index", "extract_docmap_adrs", "render_manifest"]
