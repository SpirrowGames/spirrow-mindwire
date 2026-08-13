"""Tests for the naysayer principles SOT loader + preamble builder (ADR-17 D-1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from spirrow_mindwire.naysayer.principles import (
    NAYSAYER_EXPECTED_BACKEND,
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


def test_expected_backend_is_the_ledger_field_not_the_model_id() -> None:
    """★ M5 (Tier-C msg-970 §3): the value of ``NAYSAYER_EXPECTED_BACKEND`` is pinned.

    A mutation probe on PR #143 changed this constant to
    ``NAYSAYER_UPSTREAM_MODEL`` and CI stayed green through 1109 tests — the only
    test that read the value was ``@pytest.mark.manual`` (deselected by
    ``addopts -m "not manual"``) and its assertion was a tautology. Reproduced on
    ``main``\\@``5529084`` before writing this: still 1109 passed, 8 deselected.

    What the survivor costs, if it is ever mutated for real: the two strings look
    like duplicates and inviting a "consolidate these" cleanup is exactly how it
    would happen. The gateway's accounting row carries ``backend: "gemini"`` — the
    backend *family*, never the model id (live row 6032, 2026-08-13). Compare
    against the model id and **every** attestation mismatches, every naysayer
    spawn raises, the daemon exits non-zero, and the sweep quarantines the
    candidate. Fail-loud is the right design (Tier-C msg-970 §2), which is
    precisely why the trigger must not be reachable by a plausible tidy-up.

    Two asserts, and the second is not implied by the first for a reader: the
    literal pin catches any change at all, the inequality names the specific
    change that motivated the pin.
    """
    assert NAYSAYER_EXPECTED_BACKEND == "gemini"
    assert NAYSAYER_EXPECTED_BACKEND != NAYSAYER_UPSTREAM_MODEL, (
        "NAYSAYER_EXPECTED_BACKEND is compared against the `backend` field of a Lexora "
        "accounting row, which names the backend family; NAYSAYER_UPSTREAM_MODEL is the "
        "finer-grained upstream model id the row does not carry. Collapsing the two fails "
        "every preflight attestation and takes the conductor daemon down with it."
    )


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
