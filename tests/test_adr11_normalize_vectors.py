"""Conformance of :mod:`spirrow_mindwire.identity.normalize` against the shared ADR-11 vectors.

``tests/fixtures/adr11_normalize_vectors.json`` is the **canonical** copy of the vectors that
spirrow-magickit's port of the ADR-11 rule is checked against
(spirrow-magickit ``T-magickit-stop-disposition-intake``, DESIGN v6 §1 / v10 §1 / v12 §2).
magickit keeps a byte-identical copy and records, in a provenance file, the spirrow-mindwire
commit it was taken from plus ``sha256_lf_text`` below. The bytes were produced by running
this repository's ``normalize.py`` (blob ``57c43cb7``, unchanged since ``cbf9bb1``) over the
inputs, so this test asserts nothing new about the rule — it pins the file other repos copy.

What the hash pin is for: editing the vectors here does not reach magickit's copy (its test
only detects a hand edit to the copy). Changing this file therefore needs a deliberate update
of ``_SHA256_LF_TEXT`` and a matching refresh on the magickit side; the failing assertion
says so instead of letting the two drift silently. LF-normalised so ``core.autocrlf`` on
Windows cannot red the test. No network, no git.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.identity import find_collisions, normalize_identity_key

_VECTORS = Path(__file__).resolve().parent / "fixtures" / "adr11_normalize_vectors.json"

# Must equal ``sha256_lf_text`` in spirrow-magickit
# ``tests/fixtures/adr11_normalize_vectors.provenance.json``.
_SHA256_LF_TEXT = "450595dd2129bef0eec592bf81137343fea96556def77a95d2e9f837beac60d0"


def _vectors() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_VECTORS.read_text(encoding="utf-8"))
    return data


def test_canonical_vectors_match_the_hash_copies_are_pinned_to() -> None:
    text = _VECTORS.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == _SHA256_LF_TEXT, (
        "adr11_normalize_vectors.json changed. It is the canonical copy spirrow-magickit pins "
        "by hash: update _SHA256_LF_TEXT here AND refresh magickit's copy + provenance."
    )


def test_vectors_are_not_empty() -> None:
    data = _vectors()
    assert data["normalize"], "no normalize vectors — parametrised tests would pass vacuously"
    assert data["collisions"], "no collision vectors — parametrised tests would pass vacuously"


@pytest.mark.parametrize("raw,expected", _vectors()["normalize"])
def test_normalize_conforms(raw: str, expected: str) -> None:
    assert normalize_identity_key(raw) == expected


@pytest.mark.parametrize("case", _vectors()["collisions"], ids=lambda c: repr(c["input"]))
def test_find_collisions_conforms(case: dict[str, Any]) -> None:
    assert find_collisions(case["input"]) == case["expected"]
