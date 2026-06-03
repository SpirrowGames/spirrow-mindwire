"""Tests for the naysayer principles SOT loader + preamble builder (ADR-17 D-1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from spirrow_mindwire.naysayer.principles import (
    NAYSAYER_MODEL_TIER,
    NAYSAYER_UPSTREAM_MODEL,
    PrinciplesError,
    build_preamble,
    load_principles,
    principles_version,
)


def test_model_identity_pinned() -> None:
    # N-4: SOT = Gemini, single source for the tier name.
    assert NAYSAYER_MODEL_TIER == "naysayer"
    assert NAYSAYER_UPSTREAM_MODEL == "gemini-3.1-pro-preview"


def test_load_principles_is_verbatim_with_all_five() -> None:
    text = load_principles()
    assert text.lstrip().startswith("---")  # frontmatter preserved verbatim
    for principle in (
        "YAGNI / OverScope",
        "hybrid & dual-management complexity",
        "no opposition for opposition's sake",
        "explicitly endorse what should be endorsed",
        "silence is negligence",
    ):
        assert principle in text


def test_principles_version_parsed() -> None:
    assert principles_version() == 1


def test_build_preamble_injects_verbatim_and_tags_version() -> None:
    preamble = build_preamble()
    assert f"principles_version={principles_version()}" in preamble
    # The whole SOT is injected verbatim (not paraphrased).
    assert load_principles() in preamble


def test_missing_principles_is_fail_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "nope.md"
    monkeypatch.setenv("MINDWIRE_NAYSAYER_PRINCIPLES_PATH", str(missing))
    with pytest.raises(PrinciplesError):
        load_principles()


def test_empty_principles_is_fail_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blank = tmp_path / "blank.md"
    blank.write_text("   \n", encoding="utf-8")
    monkeypatch.setenv("MINDWIRE_NAYSAYER_PRINCIPLES_PATH", str(blank))
    with pytest.raises(PrinciplesError):
        load_principles()
