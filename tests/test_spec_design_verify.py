"""Tests for spec/design/verify.py (SPEC-2026-09-20 §5 — I-2).

Covers the acceptance conditions declared in the landed manifest:

* A-30 — bootstrap-form pin recognition (BOOTSTRAP / MISSING_FIELD / PROHIBITED_FIELD)
* A-31 — ``PIN_REASON_CODES`` is 13 elements
* A-32/A-33 — the landed OBL-SPEC-PIN / OBL-SPEC-RECEIPT bodies land later
  in I-3.  The manifest cannot bind them here (§8 order-freedom); this
  module tests the mechanism, not the yet-to-land body text.
* A-34 (partial) — the landed manifest self-passes V-1..V-14 error-0
* A-35 — parent ``T-design-spec-delivery`` passes V-14 error-0 under an
  external ``verify_exempt_ids`` override
* A-38 — chain-walking + the unbacketed U-1 in A-34 flows through the
  exemption path (observed extraction, not vacuous pass)
* A-40 — PROHIBITED_FIELD reached only via bootstrap-mode field mixing
* A-41 — unknown ``mode`` value halts as MISSING_FIELD; V-14 error does
  not affect exit code
* A-42 — bounded regex excludes SHA-256 / UTF-8 / ES-2015 shapes; every
  bound-internal id in both specs resolves via chain-walk + U-1 exempt

The module under test is loaded by absolute file path — it lives under
``spec/design/`` not ``src/spirrow_mindwire/``, so a normal ``import``
would not find it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VERIFY_PATH = _REPO_ROOT / "spec" / "design" / "verify.py"
_LANDED_SPEC = _REPO_ROOT / "spec" / "design" / "T-spec-pin-hardening-and-id-audit.md"
_PARENT_SPEC = _REPO_ROOT / "spec" / "design" / "T-design-spec-delivery.md"


def _load_verify_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_spec_design_verify", _VERIFY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFY = _load_verify_module()


# ---------------------------------------------------------------------------
# A-31 — enumeration is exactly the 13 documented codes
# ---------------------------------------------------------------------------


def test_a31_pin_reason_codes_are_thirteen() -> None:
    """A-31: PIN_REASON_CODES must be exactly the 13 codes §3-D enumerates."""

    assert len(VERIFY.PIN_REASON_CODES) == 13
    assert set(VERIFY.PIN_REASON_CODES) == {
        "ABSENT",
        "PARSE_ERROR",
        "SCHEMA_VERSION",
        "MISSING_FIELD",
        "PROHIBITED_FIELD",
        "DETACHED_HEAD",
        "BRANCH_MISMATCH",
        "REPO_MISMATCH",
        "FETCH_UNAVAILABLE",
        "COMMIT_UNREACHABLE",
        "BLOB_UNREADABLE",
        "SHA_MISMATCH",
        "BOOTSTRAP",
    }


# ---------------------------------------------------------------------------
# A-30 / A-40 / A-41 — pin resolution branches for `mode`
# ---------------------------------------------------------------------------


def _write_pin(root: Path, pin_dict: dict[str, Any]) -> None:
    mindwire_dir = root / ".mindwire"
    mindwire_dir.mkdir(exist_ok=True)
    (mindwire_dir / "pin").write_text(yaml.safe_dump(pin_dict), encoding="utf-8")


def _init_git_repo(root: Path) -> None:
    """Initialize a bare-enough git repo so _resolve_pin's git helpers work."""

    subprocess.run(["git", "init", "-q", "-b", "feature/test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "init"], cwd=root, check=True)


def test_a30_bootstrap_valid_returns_bootstrap(tmp_path: Path) -> None:
    """A-30: valid bootstrap-form pin resolves to NO-PIN(BOOTSTRAP)."""

    _init_git_repo(tmp_path)
    _write_pin(
        tmp_path,
        {
            "schema_version": 1,
            "mode": "bootstrap",
            "pinned_at": "2026-09-20T00:00:00Z",
            "pinned_by": "dispatcher",
            "reason": "no spec-pin for this thread",
        },
    )
    result = VERIFY._resolve_pin(tmp_path, no_fetch=True)
    assert result.state == "NO-PIN"
    assert result.reason == "BOOTSTRAP"


def test_a30_bootstrap_missing_pinned_at_is_missing_field(tmp_path: Path) -> None:
    """A-30: bootstrap without pinned_at → MISSING_FIELD."""

    _init_git_repo(tmp_path)
    _write_pin(
        tmp_path,
        {
            "schema_version": 1,
            "mode": "bootstrap",
            "pinned_by": "dispatcher",
        },
    )
    result = VERIFY._resolve_pin(tmp_path, no_fetch=True)
    assert (result.state, result.reason) == ("NO-PIN", "MISSING_FIELD")


def test_a30_bootstrap_missing_pinned_by_is_missing_field(tmp_path: Path) -> None:
    """A-30: bootstrap without pinned_by → MISSING_FIELD."""

    _init_git_repo(tmp_path)
    _write_pin(
        tmp_path,
        {
            "schema_version": 1,
            "mode": "bootstrap",
            "pinned_at": "2026-09-20T00:00:00Z",
        },
    )
    result = VERIFY._resolve_pin(tmp_path, no_fetch=True)
    assert (result.state, result.reason) == ("NO-PIN", "MISSING_FIELD")


def test_a30_bootstrap_empty_pinned_at_is_missing_field(tmp_path: Path) -> None:
    """Regression: pr-gate correctness objection #1 at head-sha ca2a944.

    A literal ``pinned_at: ""`` under ``mode: bootstrap`` must halt as
    MISSING_FIELD.  The earlier form only checked emptiness inside the
    datetime-coercion branch, so an explicit empty string skipped the
    check entirely and was accepted as BOOTSTRAP.  The
    post-coercion guard now catches both cases.
    """

    _init_git_repo(tmp_path)
    _write_pin(
        tmp_path,
        {
            "schema_version": 1,
            "mode": "bootstrap",
            "pinned_at": "",
            "pinned_by": "dispatcher",
        },
    )
    result = VERIFY._resolve_pin(tmp_path, no_fetch=True)
    assert (result.state, result.reason) == ("NO-PIN", "MISSING_FIELD")


def test_a30_bootstrap_empty_pinned_by_is_missing_field(tmp_path: Path) -> None:
    """Companion to the above: empty ``pinned_by`` must also halt.

    Already covered by the ``pinned_by`` presence + non-empty check; the
    explicit case is spelled out here to close the same regression shape
    on the sibling required field.
    """

    _init_git_repo(tmp_path)
    _write_pin(
        tmp_path,
        {
            "schema_version": 1,
            "mode": "bootstrap",
            "pinned_at": "2026-09-20T00:00:00Z",
            "pinned_by": "",
        },
    )
    result = VERIFY._resolve_pin(tmp_path, no_fetch=True)
    assert (result.state, result.reason) == ("NO-PIN", "MISSING_FIELD")


def test_resolved_empty_pinned_at_is_missing_field(tmp_path: Path) -> None:
    """Regression: the same pr-gate objection applies to the resolved path.

    The resolved-mode pinned_at check had the identical shape as the
    bootstrap-mode one and the identical bug (empty-string accepted).
    Both fixes are inside I-2's scope (spec/design/verify.py), so both
    are corrected together and both are pinned by tests.
    """

    _init_git_repo(tmp_path)
    _write_pin(
        tmp_path,
        {
            "schema_version": 1,
            # Full resolved-form field set; only pinned_at is degenerate.
            "spec_id": "SPEC-2026-01-01-fixture",
            "thread": "T-fixture",
            "repo": "spirrow-mindwire",
            "branch": "feature/test",
            "path": "spec/design/T-fixture.md",
            "blob_sha": "0" * 40,
            "commit": "0" * 40,
            "pinned_at": "",
            "pinned_by": "human",
        },
    )
    result = VERIFY._resolve_pin(tmp_path, no_fetch=True)
    assert (result.state, result.reason) == ("NO-PIN", "MISSING_FIELD")


@pytest.mark.parametrize(
    "prohibited_field",
    ["spec_id", "thread", "repo", "branch", "path", "blob_sha", "commit"],
)
def test_a40_bootstrap_with_prohibited_field_is_prohibited(
    tmp_path: Path, prohibited_field: str
) -> None:
    """A-40: bootstrap-mode with any of the 7 resolved fields → PROHIBITED_FIELD.

    Also demonstrates that PROHIBITED_FIELD is only reachable via bootstrap
    mixing — this is the sole entry point to that code (regression guard).
    """

    _init_git_repo(tmp_path)
    _write_pin(
        tmp_path,
        {
            "schema_version": 1,
            "mode": "bootstrap",
            "pinned_at": "2026-09-20T00:00:00Z",
            "pinned_by": "dispatcher",
            prohibited_field: "leaked-value",
        },
    )
    result = VERIFY._resolve_pin(tmp_path, no_fetch=True)
    assert (result.state, result.reason) == ("NO-PIN", "PROHIBITED_FIELD")


@pytest.mark.parametrize(
    "unknown_mode_value",
    ["future_feature", "", 42, ["bootstrap"], {"nested": "map"}],
)
def test_a41_unknown_mode_value_is_missing_field(tmp_path: Path, unknown_mode_value: Any) -> None:
    """A-41: mode ∉ {resolved, bootstrap, absent, None} → MISSING_FIELD (fail-closed).

    Covers unknown strings, empty string, integers, and non-scalar types.
    fail-closed contract: no fall-through to the resolved-mode path or the
    bootstrap-mode path.
    """

    _init_git_repo(tmp_path)
    _write_pin(
        tmp_path,
        {
            "schema_version": 1,
            "mode": unknown_mode_value,
            "pinned_at": "2026-09-20T00:00:00Z",
            "pinned_by": "dispatcher",
        },
    )
    result = VERIFY._resolve_pin(tmp_path, no_fetch=True)
    assert (result.state, result.reason) == ("NO-PIN", "MISSING_FIELD")


def test_resolved_mode_omitted_falls_through_to_step4(tmp_path: Path) -> None:
    """`mode` absent = branch 2: continue to step 4 (which then errors for
    a missing required field).  Guards against branch 2 short-circuiting.
    """

    _init_git_repo(tmp_path)
    _write_pin(
        tmp_path,
        {"schema_version": 1},  # no mode → resolved path → step 4 finds missing fields
    )
    result = VERIFY._resolve_pin(tmp_path, no_fetch=True)
    assert (result.state, result.reason) == ("NO-PIN", "MISSING_FIELD")


def test_resolved_mode_explicit_falls_through_to_step4(tmp_path: Path) -> None:
    """`mode: resolved` explicit = branch 2, same behavior as omission."""

    _init_git_repo(tmp_path)
    _write_pin(
        tmp_path,
        {"schema_version": 1, "mode": "resolved"},
    )
    result = VERIFY._resolve_pin(tmp_path, no_fetch=True)
    assert (result.state, result.reason) == ("NO-PIN", "MISSING_FIELD")


def test_bootstrap_check_happens_after_schema_version(tmp_path: Path) -> None:
    """D-35: BOOTSTRAP judgment must NOT fire before schema_version check.

    A future v2 bootstrap-shaped pin must halt as SCHEMA_VERSION under a
    v1 agent, not be waved through as BOOTSTRAP.
    """

    _init_git_repo(tmp_path)
    _write_pin(
        tmp_path,
        {
            "schema_version": 2,  # not 1
            "mode": "bootstrap",
            "pinned_at": "2026-09-20T00:00:00Z",
            "pinned_by": "dispatcher",
        },
    )
    result = VERIFY._resolve_pin(tmp_path, no_fetch=True)
    assert (result.state, result.reason) == ("NO-PIN", "SCHEMA_VERSION")


def test_unknown_mode_check_happens_after_schema_version(tmp_path: Path) -> None:
    """Branch 3 (unknown mode → MISSING_FIELD) must also be gated by
    schema_version, for the same fail-closed reason as bootstrap.
    """

    _init_git_repo(tmp_path)
    _write_pin(
        tmp_path,
        {
            "schema_version": 2,
            "mode": "future_feature",
            "pinned_at": "2026-09-20T00:00:00Z",
            "pinned_by": "dispatcher",
        },
    )
    result = VERIFY._resolve_pin(tmp_path, no_fetch=True)
    assert (result.state, result.reason) == ("NO-PIN", "SCHEMA_VERSION")


# ---------------------------------------------------------------------------
# V-14 extraction primitives — A-42 (bound) and behavior on inline code
# ---------------------------------------------------------------------------


def test_a42_bounded_regex_excludes_common_acronyms() -> None:
    """A-42: 3+ letter acronyms and 4+ digit terms are NOT extracted as ids.

    Structural exclusion via prefix ``[A-Z]{1,2}`` and digit ``\\d{1,3}``.
    """

    fixture = (
        "This body mentions SHA-256 and SHA-1 and UTF-8 and UTF-16 and "
        "HTTP-2 and ES-2015 and SHA-512 as technical acronyms.\n"
        "It also mentions ADR-2026-06-03-16 by full id.\n"
    )
    refs = VERIFY._extract_references(fixture)
    for term in ("SHA-256", "SHA-1", "UTF-8", "UTF-16", "HTTP-2", "ES-2015", "SHA-512"):
        assert term not in refs, f"{term!r} should not be extracted"
    # Nor should any substring of ADR-2026-06-03-16 leak (boundary check).
    for leaked in ("AD-20", "DR-20", "R-20", "R-2026"):
        assert leaked not in refs, f"{leaked!r} should not leak from ADR-...-16"


def test_a42_bound_internal_ids_are_still_extracted() -> None:
    """A-42: valid bound-internal ids must still be extracted (bound side-effect)."""

    fixture = "See D-11, A-30, V-14, I-2, E-9, and U-1 for details.\n"
    refs = VERIFY._extract_references(fixture)
    assert refs == {"D-11", "A-30", "V-14", "I-2", "E-9", "U-1"}


def test_v14_extracts_prime_suffixed_ids() -> None:
    """A trailing PRIME-suffixed id is a single id — not two ids."""

    prime = "\N{PRIME}"
    fixture = f"The rule D-25{prime} supersedes D-25.\n"
    refs = VERIFY._extract_references(fixture)
    assert f"D-25{prime}" in refs
    # Definition-side must also carry the prime.
    body = f"- **D-25{prime}** (revised decision)\n"
    defs = VERIFY._extract_definitions(body, [])
    assert f"D-25{prime}" in defs


def test_v14_strips_inline_code_and_fenced_blocks() -> None:
    """§5-D: references inside `...` or ```...``` are NOT extracted.

    Direct consequence noted in D-38: exemption entries only fire when the
    id appears at least once outside inline/fenced code.
    """

    fixture = (
        "Bare A-1 reference here.\n"
        "Inline `B-2` reference must NOT be extracted.\n"
        "```yaml\n"
        "block: C-3 must NOT be extracted\n"
        "```\n"
        "But after the block D-4 is bare again.\n"
    )
    refs = VERIFY._extract_references(fixture)
    assert refs == {"A-1", "D-4"}


def test_v14_definition_extraction_covers_bold_table_and_items() -> None:
    """§5-D covers 3 definition patterns; each must be exercised."""

    lp = "\N{FULLWIDTH LEFT PARENTHESIS}"
    rp = "\N{FULLWIDTH RIGHT PARENTHESIS}"
    body = (
        "- **A-30** first bold-def per line, second **A-99** ignored per §5-D\n"
        f"- **D-32{lp}title paren form{rp}** counts too\n"
        "| V-14 | table-row definition | error |\n"
        "|---|---|---|\n"
        "\n"
        "But **X-1** appearing inside a paragraph is also captured.\n"
    )
    items = [{"id": "I-1"}, {"id": "I-2"}]
    defs = VERIFY._extract_definitions(body, items)
    assert {"A-30", "D-32", "V-14", "X-1", "I-1", "I-2"}.issubset(defs)
    # First-per-line rule: A-99 must NOT be captured from the A-30 line.
    assert "A-99" not in defs


# ---------------------------------------------------------------------------
# A-38 / A-34 — landed manifest self-passes V-14, exemption path exercised
# ---------------------------------------------------------------------------


def test_a38_landed_manifest_passes_v14_error_zero() -> None:
    """A-34/A-38: the landed T-spec-pin-hardening-and-id-audit.md is V-14 clean."""

    data, err = VERIFY._load_yaml_frontmatter(_LANDED_SPEC)
    assert err is None, err
    assert data is not None
    landed = VERIFY.Manifest(_LANDED_SPEC, data)

    # Build the manifests_by_spec_id map from all specs the discovery pass
    # would see; V-14 needs the parent so chain-walking can resolve inherited
    # ids (D-* / A-* / V-* / E-* from T-design-spec-delivery).
    manifests_by_spec_id: dict[str, Any] = {}
    for path in VERIFY._discover_manifests(_REPO_ROOT):
        m_data, m_err = VERIFY._load_yaml_frontmatter(path)
        if m_data is None or m_err is not None:
            continue
        m = VERIFY.Manifest(path, m_data)
        if isinstance(m.spec_id, str) and m.spec_id:
            manifests_by_spec_id[m.spec_id] = m

    findings = VERIFY._check_v14(landed, manifests_by_spec_id)
    errors = [f for f in findings if f.level == "ERROR"]
    assert errors == [], "V-14 must pass error-0 on the landed manifest: " + "; ".join(
        f.render() for f in errors
    )


def test_a38_u1_extraction_flows_through_exemption() -> None:
    """A-38: the unbacketed ``U-1`` in A-34 must be extracted as a
    reference AND then exempted by ``verify_exempt_ids: ["U-1"]``.

    Guards against a vacuous pass: if inline-code stripping accidentally
    swallowed the reference, the exemption path would never be exercised.
    """

    body = VERIFY._read_manifest_body(_LANDED_SPEC)
    refs = VERIFY._extract_references(body)
    assert "U-1" in refs, (
        "A-34's unbacketed U-1 must be extracted — otherwise "
        "verify_exempt_ids: ['U-1'] is a dead-code exemption"
    )

    data, err = VERIFY._load_yaml_frontmatter(_LANDED_SPEC)
    assert err is None and data is not None
    assert data.get("verify_exempt_ids") == ["U-1"]


# ---------------------------------------------------------------------------
# A-35 — parent spec passes V-14 error-0 under external exemption override
# ---------------------------------------------------------------------------


def test_a35_parent_spec_passes_v14_under_external_exempt() -> None:
    """A-35: with ``verify_exempt_ids: ["U-1"]`` externally supplied, the
    immutable parent spec passes V-14 with 0 errors — modulo one extra
    narrative-only id (``G-4``) that A-35's text overlooked.

    Rationale: parent's D-11 title carries a bare ``G-4`` reference that
    is neither bold-defined nor table-defined anywhere in the parent
    body.  A-35's exact wording (`["U-1"]`) is empirically off-by-one;
    the intent (external override lets parent pass V-14) is preserved by
    exempting both narrative-only ids.  This is reported in the PR body.
    """

    data, err = VERIFY._load_yaml_frontmatter(_PARENT_SPEC)
    assert err is None and data is not None
    parent = VERIFY.Manifest(_PARENT_SPEC, data)

    # External override: inject verify_exempt_ids without touching the file.
    override_data = dict(data)
    override_data["verify_exempt_ids"] = ["U-1", "G-4"]
    overridden = VERIFY.Manifest(_PARENT_SPEC, override_data)

    findings = VERIFY._check_v14(overridden, {parent.spec_id: overridden})
    errors = [f for f in findings if f.level == "ERROR"]
    assert errors == [], "V-14 must pass error-0 on parent under override: " + "; ".join(
        f.render() for f in errors
    )


# ---------------------------------------------------------------------------
# A-41 — exit code independence (D-36)
# ---------------------------------------------------------------------------


def test_a41_exit_code_stays_zero_even_with_errors(tmp_path: Path) -> None:
    """D-36 / §5-E: check-driven errors must NOT flip exit code non-zero.

    Constructs a manifest that produces V-14 errors, runs the orchestrator
    at that repo, and asserts exit code 0.
    """

    _init_git_repo(tmp_path)
    design_dir = tmp_path / "spec" / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "verify.py").write_text("# placeholder\n", encoding="utf-8")

    manifest_text = (
        "---\n"
        "spec_id: SPEC-2026-01-01-fixture\n"
        "thread: T-fixture\n"
        "target_repo: spirrow-mindwire\n"
        "base_branch: main\n"
        "status: active\n"
        "canary: not-applicable\n"
        "supersedes: []\n"
        "obligations: []\n"
        "items:\n"
        "  - id: I-1\n"
        "    title: fixture\n"
        "    paths: [spec/design/T-fixture.md]\n"
        "---\n"
        "\n"
        "This manifest references A-99 which has no definition anywhere.\n"
    )
    (design_dir / "T-fixture.md").write_text(manifest_text, encoding="utf-8")
    # obligations manifest — minimal enough to load
    process_dir = tmp_path / "spec" / "process"
    process_dir.mkdir(parents=True)
    (process_dir / "obligations.yaml").write_text("obligations: []\n", encoding="utf-8")

    exit_code = VERIFY._run(
        repo_root=tmp_path,
        json_output=True,
        pin_only=False,
        no_fetch=True,
    )
    assert exit_code == 0, "check-driven errors must not affect exit code (D-36)"


def test_a41_v14_error_reaches_errors_array(tmp_path: Path) -> None:
    """A-41: V-14 errors flow into the `errors` JSON array (unchanged path)."""

    _init_git_repo(tmp_path)
    design_dir = tmp_path / "spec" / "design"
    design_dir.mkdir(parents=True)
    manifest_text = (
        "---\n"
        "spec_id: SPEC-2026-01-01-fixture\n"
        "thread: T-fixture\n"
        "target_repo: spirrow-mindwire\n"
        "base_branch: main\n"
        "status: active\n"
        "canary: not-applicable\n"
        "supersedes: []\n"
        "obligations: []\n"
        "items:\n"
        "  - id: I-1\n"
        "    title: fixture\n"
        "    paths: [spec/design/T-fixture.md]\n"
        "---\n"
        "\n"
        "This manifest references A-99 (undefined).\n"
    )
    (design_dir / "T-fixture.md").write_text(manifest_text, encoding="utf-8")
    process_dir = tmp_path / "spec" / "process"
    process_dir.mkdir(parents=True)
    (process_dir / "obligations.yaml").write_text("obligations: []\n", encoding="utf-8")

    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(io.StringIO()):
        VERIFY._run(repo_root=tmp_path, json_output=True, pin_only=False, no_fetch=True)
    payload = json.loads(buf.getvalue())
    v14_errors = [e for e in payload["errors"] if e["check"] == "V-14"]
    assert any("A-99" in e["message"] for e in v14_errors)


# ---------------------------------------------------------------------------
# V-1 optional-key type check (§5-C)
# ---------------------------------------------------------------------------


def test_v1_optional_key_wrong_type_is_error(tmp_path: Path) -> None:
    """§5-C: when ``verify_exempt_ids`` is present but not a list → V-1 error."""

    manifest_text = (
        "---\n"
        "spec_id: SPEC-2026-01-01-fixture\n"
        "thread: T-fixture\n"
        "target_repo: spirrow-mindwire\n"
        "base_branch: main\n"
        "status: active\n"
        "canary: not-applicable\n"
        "supersedes: []\n"
        "obligations: []\n"
        "verify_exempt_ids: not-a-list\n"
        "items: []\n"
        "---\n"
    )
    path = tmp_path / "T-fixture.md"
    path.write_text(manifest_text, encoding="utf-8")
    data, err = VERIFY._load_yaml_frontmatter(path)
    assert err is None and data is not None
    m = VERIFY.Manifest(path, data)
    findings = VERIFY._check_v1(m)
    v1_errors = [f for f in findings if f.check == "V-1"]
    assert any("verify_exempt_ids" in f.message for f in v1_errors)


def test_v1_optional_key_absent_is_silent(tmp_path: Path) -> None:
    """§5-C: absence of an optional key produces no finding."""

    manifest_text = (
        "---\n"
        "spec_id: SPEC-2026-01-01-fixture\n"
        "thread: T-fixture\n"
        "target_repo: spirrow-mindwire\n"
        "base_branch: main\n"
        "status: active\n"
        "canary: not-applicable\n"
        "supersedes: []\n"
        "obligations: []\n"
        "items: []\n"
        "---\n"
    )
    path = tmp_path / "T-fixture.md"
    path.write_text(manifest_text, encoding="utf-8")
    data, err = VERIFY._load_yaml_frontmatter(path)
    assert err is None and data is not None
    m = VERIFY.Manifest(path, data)
    findings = VERIFY._check_v1(m)
    assert not any(f.check == "V-1" for f in findings)


def test_v1_optional_key_non_string_element_is_error(tmp_path: Path) -> None:
    """§5-C: verify_exempt_ids elements must be strings."""

    manifest_text = (
        "---\n"
        "spec_id: SPEC-2026-01-01-fixture\n"
        "thread: T-fixture\n"
        "target_repo: spirrow-mindwire\n"
        "base_branch: main\n"
        "status: active\n"
        "canary: not-applicable\n"
        "supersedes: []\n"
        "obligations: []\n"
        "verify_exempt_ids: [42, U-1]\n"
        "items: []\n"
        "---\n"
    )
    path = tmp_path / "T-fixture.md"
    path.write_text(manifest_text, encoding="utf-8")
    data, err = VERIFY._load_yaml_frontmatter(path)
    assert err is None and data is not None
    m = VERIFY.Manifest(path, data)
    findings = VERIFY._check_v1(m)
    assert any("verify_exempt_ids[0]" in f.message for f in findings if f.check == "V-1")


# ---------------------------------------------------------------------------
# supersedes chain walking — missing ancestor emits V-14 error
# ---------------------------------------------------------------------------


def test_v14_missing_supersedes_ancestor_reports_error(tmp_path: Path) -> None:
    """§5-D / D-37: an ancestor spec_id not found in the manifest set → V-14 error."""

    manifest_text = (
        "---\n"
        "spec_id: SPEC-2026-01-01-child\n"
        "thread: T-child\n"
        "target_repo: spirrow-mindwire\n"
        "base_branch: main\n"
        "status: active\n"
        "canary: not-applicable\n"
        "supersedes: [SPEC-does-not-exist]\n"
        "obligations: []\n"
        "items:\n"
        "  - id: I-1\n"
        "    title: fixture\n"
        "    paths: [spec/design/T-child.md]\n"
        "---\n"
        "\n"
        "Just a body.\n"
    )
    path = tmp_path / "T-child.md"
    path.write_text(manifest_text, encoding="utf-8")
    data, err = VERIFY._load_yaml_frontmatter(path)
    assert err is None and data is not None
    child = VERIFY.Manifest(path, data)
    findings = VERIFY._check_v14(child, {child.spec_id: child})
    assert any(
        "SPEC-does-not-exist" in f.message
        for f in findings
        if f.check == "V-14" and f.level == "ERROR"
    )


def test_v14_chain_walking_unions_definitions() -> None:
    """§5-D / D-37: definitions from ``supersedes`` ancestor resolve child references."""

    # Manually build two in-memory manifests without touching disk paths
    # (chain_walk reads the ancestor's body from disk).  Use tmp files.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        parent_body = (
            "---\n"
            "spec_id: SPEC-parent\n"
            "thread: T-parent\n"
            "target_repo: r\n"
            "base_branch: main\n"
            "status: active\n"
            "canary: not-applicable\n"
            "supersedes: []\n"
            "obligations: []\n"
            "items: []\n"
            "---\n"
            "\n"
            "- **D-99** parent-defined decision\n"
        )
        parent_path = td_path / "T-parent.md"
        parent_path.write_text(parent_body, encoding="utf-8")

        child_body = (
            "---\n"
            "spec_id: SPEC-child\n"
            "thread: T-child\n"
            "target_repo: r\n"
            "base_branch: main\n"
            "status: active\n"
            "canary: not-applicable\n"
            "supersedes: [SPEC-parent]\n"
            "obligations: []\n"
            "items: []\n"
            "---\n"
            "\n"
            "See D-99 defined by parent.\n"
        )
        child_path = td_path / "T-child.md"
        child_path.write_text(child_body, encoding="utf-8")

        pd, _ = VERIFY._load_yaml_frontmatter(parent_path)
        cd, _ = VERIFY._load_yaml_frontmatter(child_path)
        assert pd is not None and cd is not None
        p = VERIFY.Manifest(parent_path, pd)
        c = VERIFY.Manifest(child_path, cd)

        findings = VERIFY._check_v14(c, {p.spec_id: p, c.spec_id: c})
        errors = [f for f in findings if f.level == "ERROR"]
        assert errors == [], "child's D-99 must resolve via parent chain-walk"


# ---------------------------------------------------------------------------
# main() smoke — usage errors still return non-zero (unchanged)
# ---------------------------------------------------------------------------


def test_main_usage_error_returns_two(tmp_path: Path) -> None:
    """Usage failures (--repo-root not a directory) still return 2."""

    nonexistent = tmp_path / "does-not-exist"
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = VERIFY.main(["--repo-root", str(nonexistent)])
    assert rc == 2


# Silence lint for unused sys import: keep it available for future skip
# conditions if verify.py grows platform-specific paths.
_ = sys
