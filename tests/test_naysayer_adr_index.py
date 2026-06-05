"""Tests for the deterministic in-repo ADR index manifest (ADR-2026-06-04-19 N-2)."""

from __future__ import annotations

import re
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


def test_load_adr_index_yaml_syntax_error_returns_empty(tmp_path: Path) -> None:
    # Tier B Finding-2 (msg-442): a syntax-broken manifest must fail open to () — never
    # raise yaml.YAMLError up into prompt construction and crash the naysayer.
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(
        'adrs:\n  - id: ADR-1\n    title: "unclosed\n  - bad: [indent',  # ScannerError/ParserError
        encoding="utf-8",
    )
    assert load_adr_index(tmp_path) == ()


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


_ADR_ID_RE = re.compile(r"^ADR-\d{4}-\d{2}-\d{2}-\d+$")


def test_real_in_repo_manifest_loads_and_is_well_formed() -> None:
    # CI schema/parse validation of the committed spec/adr_index.yaml (the only CI check
    # possible — _docmap is absent in CI, so no drift-check; msg-443).
    rows = load_adr_index()  # default repo root
    assert rows, "committed manifest must be non-empty"
    ids = {r[0] for r in rows}
    assert "ADR-2026-06-04-19" in ids
    assert "ADR-2026-06-03-16" in ids  # an architecture ADR §M omits — must be present
    # Well-formed: every entry has a well-shaped ADR id and a non-empty title.
    for adr_id, title in rows:
        assert _ADR_ID_RE.match(adr_id), f"malformed ADR id: {adr_id!r}"
        assert title.strip(), f"empty title for {adr_id}"


def test_section_m_adrs_are_a_subset_of_the_manifest() -> None:
    # Partial CI drift-check (Tier B re-review msg-448): _docmap is absent in CI but CLAUDE.md IS
    # present, so every §M-referenced ADR must already be in the committed manifest — catching an
    # identity ADR added to §M without rerunning gen_adr_index.py. (A *full* drift-check, covering
    # the docs-only architecture ADRs, would need _docmap, which CI does not have.)
    repo_root = Path(__file__).resolve().parents[1]
    claude_md = (repo_root / "CLAUDE.md").read_text(encoding="utf-8")
    section_m = {adr_id for adr_id, _ in parse_adr_index(claude_md)}
    manifest = {adr_id for adr_id, _ in load_adr_index()}
    missing = section_m - manifest
    assert not missing, (
        f"CLAUDE.md §M references ADRs absent from spec/adr_index.yaml "
        f"(rerun scripts/gen_adr_index.py): {sorted(missing)}"
    )


def test_parse_adr_index_still_parses_claude_md_section_m() -> None:
    # Retained §M parser (for context_bundle until its Step ③ removal).
    claude_md = (
        "## §M\n| ADR | x | y |\n|---|---|---|\n"
        "| ADR-2026-05-31-15 | independence gradation | T |\n"
    )
    rows = parse_adr_index(claude_md)
    assert rows == (("ADR-2026-05-31-15", "independence gradation"),)
