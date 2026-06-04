"""Tests for the deterministic in-repo ADR index manifest (ADR-2026-06-04-19 N-2)."""

from __future__ import annotations

from pathlib import Path

from spirrow_mindwire.naysayer.adr_index import (
    build_adr_index_block,
    load_adr_index,
    parse_adr_index,
)

_MANIFEST = (
    "adrs:\n"
    "  - id: ADR-2026-06-04-19\n"
    '    title: "naysayer agentization"\n'
    "  - id: ADR-2026-05-23-07\n"
    '    title: "Stage 3 gating"\n'
)


def _repo_with_manifest(tmp_path: Path) -> Path:
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(_MANIFEST, encoding="utf-8")
    return tmp_path


def test_load_adr_index_sorted_deduped(tmp_path: Path) -> None:
    rows = load_adr_index(_repo_with_manifest(tmp_path))
    assert [r[0] for r in rows] == ["ADR-2026-05-23-07", "ADR-2026-06-04-19"]  # sorted
    assert dict(rows)["ADR-2026-06-04-19"] == "naysayer agentization"


def test_load_adr_index_missing_returns_empty(tmp_path: Path) -> None:
    assert load_adr_index(tmp_path) == ()  # no spec/adr_index.yaml


def test_load_adr_index_malformed_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text("just a string", encoding="utf-8")
    assert load_adr_index(tmp_path) == ()  # not a {adrs: [...]} mapping


def test_build_block_lists_manifest_entries(tmp_path: Path) -> None:
    block = build_adr_index_block(_repo_with_manifest(tmp_path))
    assert "ADR index (id + title)" in block
    assert "ADR-2026-06-04-19 — naysayer agentization" in block
    assert "ADR-2026-05-23-07 — Stage 3 gating" in block
    # The cross-check instruction (the point of injecting the complete index).
    assert "cannot search for an ADR you do not know exists" in block


def test_build_block_without_manifest_is_explicit(tmp_path: Path) -> None:
    block = build_adr_index_block(tmp_path)  # no manifest
    assert "UNAVAILABLE" in block
    assert "could not cross-check" in block


def test_real_in_repo_manifest_loads() -> None:
    # The committed spec/adr_index.yaml must parse and carry the ADR-19 entry.
    rows = load_adr_index()  # default repo root
    ids = {r[0] for r in rows}
    assert "ADR-2026-06-04-19" in ids
    assert "ADR-2026-06-03-16" in ids  # an architecture ADR §M omits — must be present


def test_parse_adr_index_still_parses_claude_md_section_m() -> None:
    # Retained §M parser (for context_bundle until its Step ③ removal).
    claude_md = (
        "## §M\n| ADR | x | y |\n|---|---|---|\n"
        "| ADR-2026-05-31-15 | independence gradation | T |\n"
    )
    rows = parse_adr_index(claude_md)
    assert rows == (("ADR-2026-05-31-15", "independence gradation"),)
