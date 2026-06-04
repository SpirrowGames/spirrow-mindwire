"""Tests for the ADR index generator (ADR-2026-06-04-19 N-2, Tier B Finding-1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from spirrow_mindwire.naysayer.adr_index import load_adr_index
from spirrow_mindwire.naysayer.adr_index_gen import (
    build_manifest_index,
    extract_docmap_adrs,
    render_manifest,
)

# §M carries an identity ADR (09) the _docmap omits.
_CLAUDE_MD = (
    "## §M\n| ADR | x | y |\n|---|---|---|\n| ADR-2026-05-27-09 (T28) | identity 4 layers | T |\n"
)

# A plausible _docmap shape: a list of doc entries (nested under a top-level key), each
# with a path + title. Carries an architecture ADR (16) §M omits, plus a non-ADR doc.
_DOCMAP: dict[str, Any] = {
    "documents": [
        {
            "path": "adr/ADR-2026-06-03-16-ci-gate.md",
            "title": "naysayer CI-gate",
            "status": "accepted",
        },
        {"path": "guides/setup.md", "title": "Setup guide", "status": "draft"},
    ]
}


def test_extract_docmap_adrs_tolerant_walk() -> None:
    adrs = extract_docmap_adrs(_DOCMAP)
    assert adrs == {"ADR-2026-06-03-16": "naysayer CI-gate"}  # non-ADR doc ignored


def test_build_manifest_index_is_the_union() -> None:
    index = build_manifest_index(_CLAUDE_MD, _DOCMAP)
    ids = [adr_id for adr_id, _ in index]
    # The whole point: §M-only (09) AND _docmap-only (16) both present, sorted.
    assert ids == ["ADR-2026-05-27-09", "ADR-2026-06-03-16"]
    assert dict(index)["ADR-2026-05-27-09"] == "identity 4 layers"  # §M title kept
    assert dict(index)["ADR-2026-06-03-16"] == "naysayer CI-gate"  # _docmap title


def test_render_manifest_round_trips_through_loader(tmp_path: Path) -> None:
    index = build_manifest_index(_CLAUDE_MD, _DOCMAP)
    rendered = render_manifest(index)
    # Parses as YAML and matches the loader's view when written to spec/adr_index.yaml.
    assert yaml.safe_load(rendered)["adrs"][0]["id"] == "ADR-2026-05-27-09"
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(rendered, encoding="utf-8")
    assert load_adr_index(tmp_path) == index


def test_render_manifest_escapes_quotes() -> None:
    rendered = render_manifest((("ADR-2026-01-01-1", 'a "quoted" title'),))
    assert yaml.safe_load(rendered)["adrs"][0]["title"] == 'a "quoted" title'
