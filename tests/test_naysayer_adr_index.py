"""Tests for the deterministic CLAUDE.md §M ADR index (ADR-2026-06-04-19 N-2)."""

from __future__ import annotations

from pathlib import Path

from spirrow_mindwire.naysayer.adr_index import build_adr_index_block, parse_adr_index


def _repo(tmp_path: Path) -> Path:
    """A minimal repo with a §M ADR table (two rows, one carrying a (T-id) tag)."""
    (tmp_path / "CLAUDE.md").write_text(
        "## §M\n"
        "| ADR | 内容 | thread |\n"
        "|---|---|---|\n"
        "| ADR-2026-05-31-15 | independence gradation | T-t15 |\n"
        "| ADR-2026-05-27-09 (T28) | identity 4 layers | T-t28 |\n",
        encoding="utf-8",
    )
    return tmp_path


def test_parse_adr_index_sorted_deduped(tmp_path: Path) -> None:
    rows = parse_adr_index((_repo(tmp_path) / "CLAUDE.md").read_text(encoding="utf-8"))
    assert [r[0] for r in rows] == ["ADR-2026-05-27-09", "ADR-2026-05-31-15"]  # sorted
    assert dict(rows)["ADR-2026-05-27-09"] == "identity 4 layers"  # (T28) tag stripped


def test_parse_adr_index_ignores_header_rows(tmp_path: Path) -> None:
    # The header / separator rows (``| ADR | 内容 |`` and ``|---|``) must not match.
    rows = parse_adr_index((_repo(tmp_path) / "CLAUDE.md").read_text(encoding="utf-8"))
    assert len(rows) == 2


def test_build_block_injects_full_index(tmp_path: Path) -> None:
    block = build_adr_index_block(_repo(tmp_path))
    assert "All-ADR index" in block
    assert "ADR-2026-05-27-09 — identity 4 layers" in block
    assert "ADR-2026-05-31-15 — independence gradation" in block
    # The cross-check instruction (the point of injecting the complete index).
    assert "cannot search for an ADR you do not know exists" in block


def test_build_block_without_claude_md_is_explicit(tmp_path: Path) -> None:
    # An empty repo (no CLAUDE.md) must surface the gap, not silently omit it.
    block = build_adr_index_block(tmp_path)
    assert "(no ADR index available" in block
