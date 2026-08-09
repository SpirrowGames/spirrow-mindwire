"""Tests for the ``obligations-readback (advisory)`` script.

The script (``scripts/obligations_readback.py``) is a non-blocking advisory
diff of ``spec/process/obligations.yaml`` between the PR base and head. These
tests use the ``--base-file`` / ``--head-file`` test seams so we can exercise
the drift-detection logic without needing a real ``git show`` — the git-read
path is a thin subprocess wrapper covered by CI on the actual PR.

We deliberately DO NOT re-test the "always exit 0" contract by aggregating a
sample of scary manifests: exit 0 is asserted structurally (``return 0`` in
``main``) and belt-and-braced by ``continue-on-error: true`` in the workflow.
Chasing every path here would rebuild the same "shadow list" of behaviour the
canary-① removal explicitly rejected.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from textwrap import dedent

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "obligations_readback.py"


def _load_script_module() -> object:
    """Load the standalone script as a module (it is not on the import path)."""
    spec = importlib.util.spec_from_file_location("_obligations_readback_test_module", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_script_module()


def _run(base: str, head: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Invoke ``main`` with base/head as inline manifests and return the summary text."""
    base_file = tmp_path / "base.yaml"
    head_file = tmp_path / "head.yaml"
    base_file.write_text(base, encoding="utf-8")
    head_file.write_text(head, encoding="utf-8")

    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    exit_code = _MODULE.main(  # type: ignore[attr-defined]
        ["--base-file", str(base_file), "--head-file", str(head_file)]
    )
    assert exit_code == 0, "advisory script must always exit 0"
    return summary_file.read_text(encoding="utf-8")


def test_no_drift_reports_no_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Identical base and head yields the "no findings" summary and never blocks."""
    manifest = dedent(
        """\
        version: 1
        obligations:
          - id: OBL-DECLARE-UNREADABLE
            role: implementer
            body: "hello"
        """
    )
    summary = _run(manifest, manifest, tmp_path, monkeypatch)
    assert "No obligation ids disappeared or were renamed" in summary
    # The advisory disclosure must travel with the "no findings" summary too —
    # a reader who lands on a green run should still see the "not a gate" fact
    # rather than infer gating from the absence of a warning.
    assert "does not block a merge" in summary


def test_removed_id_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An id present in base and absent in head is flagged as REMOVED."""
    base = dedent(
        """\
        version: 1
        obligations:
          - id: OBL-DECLARE-UNREADABLE
            role: implementer
            body: "hello"
          - id: OBL-GONE
            role: implementer
            body: "byebye"
        """
    )
    head = dedent(
        """\
        version: 1
        obligations:
          - id: OBL-DECLARE-UNREADABLE
            role: implementer
            body: "hello"
        """
    )
    summary = _run(base, head, tmp_path, monkeypatch)
    assert "REMOVED: `OBL-GONE`" in summary
    assert "Minimum remediation" in summary


def test_renamed_id_is_reported_via_body_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disappeared id whose body survives under a new id is flagged as RENAMED."""
    base = dedent(
        """\
        version: 1
        obligations:
          - id: OBL-OLD-NAME
            role: implementer
            body: "same body text, different id"
        """
    )
    head = dedent(
        """\
        version: 1
        obligations:
          - id: OBL-NEW-NAME
            role: implementer
            body: "same body text, different id"
        """
    )
    summary = _run(base, head, tmp_path, monkeypatch)
    assert "RENAMED: `OBL-OLD-NAME` → `OBL-NEW-NAME`" in summary


def test_renamed_via_origin_moved_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Origin (moved_from + original_length) is the strongest rename signal.

    Body may legitimately differ across a rename in edge cases; the origin
    identity is what carries the moved-clause identity through canary
    two-double-prime.
    """
    base = dedent(
        """\
        version: 1
        obligations:
          - id: OBL-OLD
            role: implementer
            origin:
              moved_from: "some/path::LITERAL"
              original_length: 42
            body: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        """
    )
    head = dedent(
        """\
        version: 1
        obligations:
          - id: OBL-NEW
            role: implementer
            origin:
              moved_from: "some/path::LITERAL"
              original_length: 42
            body: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        """
    )
    summary = _run(base, head, tmp_path, monkeypatch)
    assert "RENAMED: `OBL-OLD` → `OBL-NEW`" in summary


def test_malformed_base_manifest_is_reported_as_empty_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed base is treated as an empty set — the advisory reports drift, not schema.

    The fail-closed loader in ``obligations.py`` is what validates schema at
    daemon startup; here we deliberately keep the check tolerant so a
    transient parse surprise on either side does not turn the advisory into a
    silent gate.
    """
    base = "!!! not yaml at all !!!\n:\n:\n"
    head = dedent(
        """\
        version: 1
        obligations:
          - id: OBL-NEW-ONLY
            role: implementer
            body: "arrived"
        """
    )
    summary = _run(base, head, tmp_path, monkeypatch)
    # Nothing disappeared (base was empty) → clean summary, no crash.
    assert "No obligation ids disappeared" in summary
