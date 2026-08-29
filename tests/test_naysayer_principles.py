"""Tests for the naysayer principles SOT loader + preamble builder (ADR-17 D-1)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from spirrow_mindwire.naysayer.principles import (
    EXPECTED_PRINCIPLES_VERSION,
    NAYSAYER_EXPECTED_BACKEND,
    NAYSAYER_MODEL_TIER,
    NAYSAYER_UPSTREAM_MODEL,
    PrinciplesError,
    build_preamble,
    load_principles,
    objection_classes,
    principles_path,
    principles_version,
)

_V2_BLOCKING = 6
_V2_ADVISORY = 5
# The size discipline the SOT states about itself (v2, msg-2033 J-1(e)). Asserted here because
# a limit only a document mentions is a limit nothing enforces: the file is injected verbatim
# into EVERY naysayer call, so growth is paid on every review forever.
_MAX_PRINCIPLES_BYTES = 8000


def _write_principles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    """Point the loader at a throwaway SOT (and clear the path-keyed read cache)."""
    path = tmp_path / f"principles-{abs(hash(text))}.md"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("MINDWIRE_NAYSAYER_PRINCIPLES_PATH", str(path))
    return path


def _frontmatter(**overrides: str) -> str:
    body = overrides.pop("body", "version: 2\nobjection_classes:\n  docs:\n    blocks: false\n")
    return f"---\n{body}---\n\n# test SOT\n"


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
    assert principles_version() == EXPECTED_PRINCIPLES_VERSION == 2


def test_objection_classes_are_read_from_the_sot() -> None:
    """The 11 classes, their blocking split, and the absence of an escape hatch (J-6-1).

    The counts are pinned rather than merely "some classes exist" because the design's whole
    claim is that the vocabulary is CLOSED: adding a twelfth class, or flipping one from
    advisory to blocking, is a principles revision (a Tier-C decision that bumps ``version:``),
    not an edit that CI should wave through.
    """
    classes = objection_classes()
    blocking = {name for name, entry in classes.items() if entry.blocks}
    advisory = {name for name, entry in classes.items() if not entry.blocks}

    assert len(classes) == _V2_BLOCKING + _V2_ADVISORY == 11
    assert len(blocking) == _V2_BLOCKING
    assert len(advisory) == _V2_ADVISORY
    assert blocking.isdisjoint(advisory)

    # Every blocking class states what evidence discharges it; no advisory class needs one.
    for name in blocking:
        assert classes[name].evidence, f"blocking class {name!r} has no evidence obligation"

    # No escape hatch: nothing named "other*" and nothing blocking that means "everything else".
    assert not [name for name in classes if name.startswith("other")], (
        "an 'other-*' class is the escape hatch msg-2031 explicitly did not approve; without "
        "it, an objection that fits no class must fall to the closest ADVISORY class"
    )


def test_principles_sot_stays_within_its_own_size_limit() -> None:
    """The SOT declares an 8000-byte ceiling; this is the thing that enforces it (J-1(e)).

    It is injected verbatim into every naysayer invocation, so the cost of an addition is
    paid on every review. The document says a revision that would cross the limit must cut
    something instead of raising it — that instruction has no force unless a test fails.
    """
    size = principles_path().read_bytes().__len__()
    assert size <= _MAX_PRINCIPLES_BYTES, (
        f"the principles SOT is {size} bytes, over its declared {_MAX_PRINCIPLES_BYTES}; "
        f"cut something (a worked example first) rather than raising the limit"
    )


def test_no_src_file_duplicates_the_objection_class_vocabulary() -> None:
    """No Python source may carry a second copy of the class names (J-6-3).

    This asserts the ABSENCE of a duplicate, not the agreement of two copies. A sync test
    (\"the literal list matches the SOT\") would still leave two lists to maintain, which is
    the P-2 defect the class system exists to remove; Einstein's msg-1927 §3 is the same point
    ("detection is not prevention").

    The threshold is TWO distinct class names in one file, because a duplicated vocabulary is
    always a *list*. One name alone can be an innocent unrelated literal — ``"docs"`` and
    ``"structure"`` are ordinary English — and failing on it would make the test a nuisance
    that gets deleted rather than a guard that holds.
    """
    names = sorted(objection_classes())
    quoted = re.compile("|".join(rf"[\"']{re.escape(n)}[\"']" for n in names))

    # Guard against a vacuous pass: the scanner must actually fire on a duplicate. Without
    # this, a broken regex or an empty vocabulary would leave the test green forever.
    sample = f'CLASSES = ["{names[0]}", "{names[1]}"]'
    assert len({m.strip("\"'") for m in quoted.findall(sample)}) == 2, (
        "the duplicate-vocabulary scanner does not detect a literal enum copy"
    )

    src = Path(__file__).resolve().parents[1] / "src"
    assert src.is_dir(), f"source tree not found at {src}"
    offenders: list[tuple[str, list[str]]] = []
    for path in sorted(src.rglob("*.py")):
        found = {m.strip("\"'") for m in quoted.findall(path.read_text(encoding="utf-8"))}
        if len(found) >= 2:
            offenders.append((str(path.relative_to(src)).replace("\\", "/"), sorted(found)))
    assert not offenders, (
        "objection class names are enumerated in Python source; the vocabulary lives once, in "
        f"the frontmatter of the principles SOT, and is read via objection_classes(): {offenders}"
    )


def test_version_mismatch_is_fail_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A SOT the code was not written against must refuse to load, not degrade (J-6-2).

    ``objection_classes()`` is version-specific: run it against v1 and it finds no map, which
    would make every objection unclassifiable and the derivation silently permissive.
    """
    _write_principles(tmp_path, monkeypatch, _frontmatter(body="version: 1\nstatus: canonical\n"))
    with pytest.raises(PrinciplesError, match="version 1"):
        principles_version()


def test_missing_frontmatter_is_fail_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_principles(tmp_path, monkeypatch, "# no frontmatter here\n\nversion: 2\n")
    with pytest.raises(PrinciplesError, match="frontmatter"):
        principles_version()


def test_missing_objection_classes_is_fail_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_principles(tmp_path, monkeypatch, _frontmatter(body="version: 2\nstatus: canonical\n"))
    with pytest.raises(PrinciplesError, match="objection_classes"):
        objection_classes()


def test_empty_objection_classes_is_fail_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_principles(
        tmp_path, monkeypatch, _frontmatter(body="version: 2\nobjection_classes: {}\n")
    )
    with pytest.raises(PrinciplesError, match="objection_classes"):
        objection_classes()


def test_blocking_class_without_evidence_is_fail_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocking class with no evidence obligation IS the escape hatch, so it cannot load.

    It would let an objection force REQUEST_CHANGES while stating nothing a reader can check
    — the exact shape ``other-blocking`` would have had, arriving by omission instead.
    """
    _write_principles(
        tmp_path,
        monkeypatch,
        _frontmatter(body="version: 2\nobjection_classes:\n  correctness:\n    blocks: true\n"),
    )
    with pytest.raises(PrinciplesError, match="evidence"):
        objection_classes()


def test_non_boolean_blocks_is_fail_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_principles(
        tmp_path,
        monkeypatch,
        _frontmatter(body='version: 2\nobjection_classes:\n  docs:\n    blocks: "no"\n'),
    )
    with pytest.raises(PrinciplesError, match="blocks"):
        objection_classes()


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
