"""Tests for the V-14 definition-form revision (SPEC-2026-11-19-v14-definition-forms).

Covers the successor manifest's acceptance conditions:

* AC-1 — the plain run on this repo leaves exactly the 13 recorded V-14 errors
* AC-2 — positive / negative / prime-coexistence fixtures for the new forms
  (heading D-40, suffix D-41, bold table cell D-42, CommonMark indentation)
* AC-3 — a genuinely undefined reference is still a V-14 error
* AC-4 — the parent (SPEC-2026-09-20) and the successor each pass V-14 error-0
  in the plain run, asserted separately
* AC-5 — exit code stays 0 (covered by ``test_a41_*`` in
  ``test_spec_design_verify.py``; re-asserted here on this repo)
* A-35' — design-spec-delivery under external ``["U-1"]`` override leaves G-4
"""

from __future__ import annotations

import importlib.util
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VERIFY_PATH = _REPO_ROOT / "spec" / "design" / "verify.py"
_DESIGN = _REPO_ROOT / "spec" / "design"
_SUCCESSOR_ID = "SPEC-2026-11-19-v14-definition-forms"
_PIN_HARDENING_ID = "SPEC-2026-09-20-pin-hardening-and-id-audit"
_DELIVERY_ID = "SPEC-2026-08-11-design-spec-delivery"
_TRUNCATED_ID = "SPEC-2026-08-30-gate-silently-suppresses-approve-on-truncated-diff"
_STALE_BASE_ID = "SPEC-2026-08-31-gate-reads-stale-base-diff"

PRIME = "\N{PRIME}"
DPRIME = "\N{DOUBLE PRIME}"
TPRIME = "\N{TRIPLE PRIME}"
QPRIME = "\N{QUADRUPLE PRIME}"
SUP5 = "\N{SUPERSCRIPT FIVE}"
KAI = "改"
EM = "\N{EM DASH}"
FWL = "\N{FULLWIDTH LEFT PARENTHESIS}"
FWR = "\N{FULLWIDTH RIGHT PARENTHESIS}"


def _load_verify_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_spec_design_verify_v14_forms", _VERIFY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFY = _load_verify_module()


def _defs(body: str) -> set[str]:
    result: set[str] = VERIFY._extract_definitions(body, [])
    return result


def _repo_manifests() -> dict[str, Any]:
    by_id: dict[str, Any] = {}
    for path in VERIFY._discover_manifests(_REPO_ROOT):
        data, err = VERIFY._load_yaml_frontmatter(path)
        if data is None or err is not None:
            continue
        m = VERIFY.Manifest(path, data)
        if isinstance(m.spec_id, str) and m.spec_id and m.spec_id not in by_id:
            by_id[m.spec_id] = m
    return by_id


def _unresolved(manifest: Any, by_id: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for f in VERIFY._check_v14(manifest, by_id):
        if f.level != "ERROR":
            continue
        msg = f.message
        assert msg.startswith("reference `"), msg
        out.add(msg.split("`")[1])
    return out


# ---------------------------------------------------------------------------
# AC-2 — positive fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (f"### D-1 {EM} heading definition", "D-1"),
        ("## B-1 TOMBSTONED", "B-1"),
        ("# C-2", "C-2"),
        ("   ### D-7: three-space indented heading", "D-7"),
        (f"### D-2{DPRIME} {EM} suffixed heading", "D-2"),
        (f"### D-8{SUP5} {EM} superscript suffix", "D-8"),
        (f"### D-25{PRIME} {EM} prime is part of the id", f"D-25{PRIME}"),
    ],
)
def test_heading_form_is_a_definition(line: str, expected: str) -> None:
    assert expected in _defs(line + "\n")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (f"| **AC-14{TPRIME}** | x |", "AC-14"),
        (f"| **AC-16{QPRIME}** | x |", "AC-16"),
        (f"| **AC-8{KAI}** | x |", "AC-8"),
        (f"| **AC-25{KAI}'** | x |", "AC-25"),
        (f"| **AC-26{KAI}(b)** | x |", "AC-26"),
        ("| **V-14** | bold cell without suffix |", "V-14"),
        (f"| AC-13{DPRIME} | plain cell with suffix |", "AC-13"),
        ("   | V-9 | three-space indented table row |", "V-9"),
    ],
)
def test_table_cell_forms_are_definitions(line: str, expected: str) -> None:
    assert expected in _defs(line + "\n")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (f"- **AC-14{TPRIME}** inline bold with suffix", "AC-14"),
        (f"- **D-2{DPRIME}\N{FULLWIDTH LEFT PARENTHESIS}title", "D-2"),
        ("- **D-3(a)** letter tag", "D-3"),
    ],
)
def test_inline_bold_tolerates_suffix(line: str, expected: str) -> None:
    assert expected in _defs(line + "\n")


# ---------------------------------------------------------------------------
# AC-2 — negative fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        f"## \N{SECTION SIGN}8 運用{FWL}順序 {EM} D-15{FWR}",  # id mid-heading
        "### see D-15 for details",  # id not the first token
        "**D-15 は強調された地の文**",  # emphasis with no delimiter after the id
        "    ### D-15 four-space indent is a code block",
        "    | D-15 | four-space indent is a code block |",
        "| **D-15 | opening bold only |",
        "| D-15** | closing bold only |",
        "###D-15 no space after hashes",
        "### D-15x trailing letter is not a delimiter",
    ],
)
def test_non_definition_forms_are_rejected(line: str) -> None:
    assert "D-15" not in _defs(line + "\n")


# ---------------------------------------------------------------------------
# AC-2 — prime coexistence (D-41: no normalisation of PRIME)
# ---------------------------------------------------------------------------


def test_prime_only_definition_does_not_resolve_bare_reference() -> None:
    body = f"- **D-3{PRIME}** revised\n\nSee D-3 here.\n"
    defs = _defs(body)
    refs = VERIFY._extract_references(body)
    assert "D-3" in refs
    assert "D-3" not in defs
    assert f"D-3{PRIME}" in defs


def test_base_and_prime_definitions_stay_distinct() -> None:
    body = f"- **D-3** base\n- **D-3{PRIME}** revised\n\nSee D-3 and D-3{PRIME}.\n"
    defs = _defs(body)
    refs = VERIFY._extract_references(body)
    assert {"D-3", f"D-3{PRIME}"} <= defs
    assert refs - defs == set()


def test_non_prime_suffix_definition_resolves_bare_reference() -> None:
    body = f"### D-8{SUP5} {EM} revised\n\nSee D-8{SUP5} and D-8.\n"
    assert VERIFY._extract_references(body) == {"D-8"}
    assert _defs(body) == {"D-8"}


# ---------------------------------------------------------------------------
# AC-3 — a genuinely undefined reference still errors
# ---------------------------------------------------------------------------


def test_undefined_reference_is_still_v14_error(tmp_path: Path) -> None:
    path = tmp_path / "T-fixture.md"
    path.write_text(
        "---\nspec_id: SPEC-X\nsupersedes: []\n---\n"
        f"### D-1 {EM} defined\n"
        f"| **AC-2{TPRIME}** | defined |\n"
        "Refers to D-1, AC-2 and the undefined A-99.\n",
        encoding="utf-8",
    )
    m = VERIFY.Manifest(path, {"spec_id": "SPEC-X", "supersedes": []})
    assert _unresolved(m, {"SPEC-X": m}) == {"A-99"}


# ---------------------------------------------------------------------------
# AC-1 / AC-4 / A-35' — this repository's manifests
# ---------------------------------------------------------------------------

_EXPECTED_REMAINING: dict[str, set[str]] = {
    _DELIVERY_ID: {"G-4", "U-1"},
    _TRUNCATED_ID: {
        "AC-1",
        "AC-2",
        f"D-2{PRIME}",
        "O-1",
        "O-2",
        "O-3",
        "V-1",
        "V-2",
        "V-3",
        "V-5",
        "V-10",
    },
    _STALE_BASE_ID: set(),
    _PIN_HARDENING_ID: set(),
    _SUCCESSOR_ID: set(),
}


def test_ac1_plain_run_leaves_exactly_the_recorded_errors() -> None:
    by_id = _repo_manifests()
    got = {sid: _unresolved(by_id[sid], by_id) for sid in _EXPECTED_REMAINING}
    assert got == _EXPECTED_REMAINING
    assert sum(len(v) for v in got.values()) == 13


def test_ac4a_parent_pin_hardening_passes_v14_in_plain_run() -> None:
    by_id = _repo_manifests()
    assert _unresolved(by_id[_PIN_HARDENING_ID], by_id) == set()


def test_ac4b_successor_manifest_passes_all_checks() -> None:
    by_id = _repo_manifests()
    successor = by_id[_SUCCESSOR_ID]
    assert successor.path.name == "T-verify-v14-errors-on-main-and-spec-pin-residue.md"
    assert _unresolved(successor, by_id) == set()


def test_a35_prime_override_u1_leaves_only_g4() -> None:
    by_id = _repo_manifests()
    delivery = by_id[_DELIVERY_ID]
    override = dict(delivery.data)
    override["verify_exempt_ids"] = ["U-1"]
    overridden = VERIFY.Manifest(delivery.path, override)
    assert _unresolved(overridden, {_DELIVERY_ID: overridden}) == {"G-4"}


def test_ac5_exit_code_zero_on_this_repo() -> None:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        code = VERIFY._run(repo_root=_REPO_ROOT, json_output=True, pin_only=False, no_fetch=True)
    assert code == 0
