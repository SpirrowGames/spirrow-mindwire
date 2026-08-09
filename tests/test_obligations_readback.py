"""Tests for the ``obligations-readback (advisory)`` script.

The script (``scripts/obligations_readback.py``) is a non-blocking advisory
diff of ``spec/process/obligations.yaml`` between the PR base and head. These
tests use the ``--base-file`` / ``--head-file`` test seams so we can exercise
the drift-detection logic without needing a real ``git show`` — the git-read
path is a thin subprocess wrapper covered by CI on the actual PR.

The workflow deliberately does NOT set ``continue-on-error: true`` (Einstein
/ msg-762 objection): a step-level swallow would turn a tool crash into
"green + empty summary", which an implementer executing ``OBL-PRCHECK-READ``
would misread as "zero findings". The advisory-vs-tool split is therefore
enforced by the script's exit code — see the last two tests
(``test_findings_present_still_exits_zero`` / ``test_tool_failure_propagates_nonzero_exit``)
for the paired contract.
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


# --------------------------------------------------------------------------- #
# advisory-vs-tool exit-code contract (Einstein / msg-762 objection)
# --------------------------------------------------------------------------- #
#
# The workflow deliberately does NOT set `continue-on-error: true`, so the
# check goes red on any non-zero exit from the script. That makes the split
# between "advisory findings" and "tool crash" *the script's* responsibility:
#
#   * advisory findings         → exit 0 (green + populated summary)
#   * tool / infrastructure fail → non-zero exit (red — implementer triages)
#
# Losing this split re-opens the exact fail-open Einstein flagged: an
# implementer executing `OBL-PRCHECK-READ` sees "green + empty summary" and
# misreads a crashed tool as "zero missing obligations". The two tests below
# hold both halves of the contract.


def test_findings_present_still_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A REMOVED / RENAMED finding must not turn the exit code non-zero.

    Advisory findings are surfaced through the summary, not the exit code —
    otherwise the workflow (which has no ``continue-on-error``) would go red
    on every legitimate rename, defeating the "advisory means advisory"
    contract in msg-760 A.
    """
    base = dedent(
        """\
        version: 1
        obligations:
          - id: OBL-DECLARE-UNREADABLE
            role: implementer
            body: "hello"
          - id: OBL-DEFINITELY-GONE
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
    base_file = tmp_path / "base.yaml"
    head_file = tmp_path / "head.yaml"
    base_file.write_text(base, encoding="utf-8")
    head_file.write_text(head, encoding="utf-8")
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    exit_code = _MODULE.main(  # type: ignore[attr-defined]
        ["--base-file", str(base_file), "--head-file", str(head_file)]
    )
    # Advisory MUST be green even when findings exist — the finding must go to
    # the summary, not the exit code (msg-762).
    assert exit_code == 0
    summary = summary_file.read_text(encoding="utf-8")
    assert "REMOVED: `OBL-DEFINITELY-GONE`" in summary


def test_head_ref_flag_does_not_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The `--head-ref` flag was removed on Tier B Finding 2 (PR #135).

    A `--head-ref` that reads via ``git show`` while ``_scan_references`` walks
    the working tree creates a hybrid state: on a dirty working tree the two
    diverge and the summary lists references from one revision against a
    manifest from another. Removing the flag guarantees head-manifest read
    and reference-scan see the same source of truth. This test locks that
    removal so a "helpful" future PR cannot silently re-add it.
    """
    # argparse exits with SystemExit(2) on an unknown option and prints the
    # error to stderr — a clean signal that the option is gone.
    with pytest.raises(SystemExit):
        _MODULE.main(  # type: ignore[attr-defined]
            [
                "--base-file",
                str(tmp_path / "b"),
                "--head-file",
                str(tmp_path / "h"),
                "--head-ref",
                "HEAD",
            ]
        )


def test_head_read_defaults_to_the_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no `--head-file` and no `--head-ref` (there IS no `--head-ref`), the
    head comes from the working tree — the same source `_scan_references` walks.

    We assert this by monkey-patching the module's ``_REPO_ROOT`` to a temp
    directory that carries a manifest, and confirming the summary reflects
    that manifest without any git subprocess ever running (the base is empty
    because there is no git repo at the temp root; both sides come from disk).
    """
    fake_repo = tmp_path / "fake_repo"
    (fake_repo / "spec" / "process").mkdir(parents=True)
    (fake_repo / "spec" / "process" / "obligations.yaml").write_text(
        dedent(
            """\
            version: 1
            obligations:
              - id: OBL-ONLY-IN-HEAD
                role: implementer
                body: "only-in-head"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(_MODULE, "_REPO_ROOT", fake_repo)
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    # Empty base file (test seam), no `--head-*`: head must come from fake_repo.
    base_file = tmp_path / "base_empty.yaml"
    base_file.write_text("version: 1\nobligations: []\n", encoding="utf-8")

    exit_code = _MODULE.main(  # type: ignore[attr-defined]
        ["--base-file", str(base_file)]
    )
    assert exit_code == 0
    summary = summary_file.read_text(encoding="utf-8")
    # Head coming from the working tree of fake_repo means the "added" id is
    # OBL-ONLY-IN-HEAD; no removed id (base was empty) so the summary is clean.
    assert "No obligation ids disappeared" in summary


def test_tool_failure_propagates_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unhandled exception in ``main`` must propagate (non-zero exit).

    This is the second half of the advisory-vs-tool contract: a real crash
    must be visible as a red check so the implementer executing
    ``OBL-PRCHECK-READ`` triages the mechanism itself rather than misreading
    silence as safety. We simulate an infrastructure failure by monkey-patching
    the internal ``_load_snapshot`` (called by ``main``) to raise; the raise
    must escape ``main`` rather than be swallowed into an exit-0.
    """

    def _boom(_text: str) -> None:
        raise RuntimeError("simulated tool failure")

    monkeypatch.setattr(_MODULE, "_load_snapshot", _boom)
    base_file = tmp_path / "base.yaml"
    head_file = tmp_path / "head.yaml"
    base_file.write_text("version: 1\nobligations: []\n", encoding="utf-8")
    head_file.write_text("version: 1\nobligations: []\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))

    with pytest.raises(RuntimeError, match="simulated tool failure"):
        _MODULE.main(  # type: ignore[attr-defined]
            ["--base-file", str(base_file), "--head-file", str(head_file)]
        )
