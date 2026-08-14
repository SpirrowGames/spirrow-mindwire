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

    The bodies must DIFFER here, or the test proves nothing about origin. The
    two signals in ``_match_rename`` are tried in order — matching origin, then
    identical body — so a fixture whose base and head share a body is matched
    by the fallback and the origin branch is never reached. (It was written
    that way originally: deleting the entire origin branch left this test, and
    the whole 1132-test suite, green. The follow-up review of the tail of
    PR #135 that no reviewer ever saw caught it by mutation.)

    Differing bodies with an identical origin block leave exactly one branch
    that can produce a RENAMED verdict.
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
            body: "the clause exactly as it was first worded"
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
            body: "the same clause, lightly rephrased in this commit"
        """
    )
    summary = _run(base, head, tmp_path, monkeypatch)
    assert "RENAMED: `OBL-OLD` → `OBL-NEW`" in summary
    assert "REMOVED: `OBL-OLD`" not in summary


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

    The fixture has to be built so that a *broken* head read is visible. Only
    ``removed_ids = set(base) - set(head)`` ever reaches the summary, so a test
    with an empty base can assert nothing about the head at all: every head —
    the right one, an empty one, a stale one — produces the identical "No
    obligation ids disappeared" text. (That was this test's original shape, and
    it passed with ``_read_head_manifest`` returning ``""``; the follow-up
    review of the never-reviewed tail of PR #135 caught it by mutation.)

    So the base carries two ids and the working tree keeps one of them. The
    surviving id is the probe: it stays out of the findings only if the head
    really was read from ``fake_repo``.
    """
    fake_repo = tmp_path / "fake_repo"
    (fake_repo / "spec" / "process").mkdir(parents=True)
    (fake_repo / "spec" / "process" / "obligations.yaml").write_text(
        dedent(
            """\
            version: 1
            obligations:
              - id: OBL-SURVIVES-IN-HEAD
                role: implementer
                body: "still here"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(_MODULE, "_REPO_ROOT", fake_repo)
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    # Base has both ids; the working tree keeps only OBL-SURVIVES-IN-HEAD.
    base_file = tmp_path / "base.yaml"
    base_file.write_text(
        dedent(
            """\
            version: 1
            obligations:
              - id: OBL-SURVIVES-IN-HEAD
                role: implementer
                body: "still here"
              - id: OBL-DROPPED-IN-HEAD
                role: implementer
                body: "gone"
            """
        ),
        encoding="utf-8",
    )

    exit_code = _MODULE.main(  # type: ignore[attr-defined]
        ["--base-file", str(base_file)]
    )
    assert exit_code == 0
    summary = summary_file.read_text(encoding="utf-8")
    # The real drift is reported...
    assert "REMOVED: `OBL-DROPPED-IN-HEAD`" in summary
    # ...and this is the load-bearing half: if the head had NOT come from the
    # working tree it would be empty, and the surviving id would be reported
    # removed too.
    assert "OBL-SURVIVES-IN-HEAD" not in summary


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


# --------------------------------------------------------------------------- #
# base-read discrimination (Tier B naysayer Finding 3 on PR #135 / msg-766)
# --------------------------------------------------------------------------- #
#
# `_read_base_manifest` used to `return ""` on ANY non-zero `git show` — which
# swallowed unknown/unfetched refs, missing git binaries, and every other real
# tool failure as "empty base = zero findings" (green). That defeated the
# msg-762 exit contract from inside the script the contract was designed
# around. The paired tests below hold the discrimination rule:
#
#   * `git show` returned "path does not exist in <rev>" style stderr →
#     empty base is the correct advisory verdict (this PR is adding a new
#     manifest; nothing to diff against).
#   * `git show` failed for any other reason →
#     ObligationsReadbackToolError, which propagates out of `main` and
#     reds the check.


def _make_completed_process(returncode: int, stderr: str, stdout: str = "") -> object:
    """Small stand-in for ``subprocess.CompletedProcess`` — only the fields we read."""
    from dataclasses import dataclass

    @dataclass
    class _P:
        returncode: int
        stdout: str
        stderr: str

    return _P(returncode=returncode, stdout=stdout, stderr=stderr)


def test_base_read_treats_file_missing_at_revision_as_empty_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git's "path does not exist in <rev>" stderr → advisory empty-base path.

    This is the added-in-this-PR-manifest case: the head has a manifest, the
    base does not, so the diff should show every head id as "added" (no
    removed/renamed findings) and the check goes green with a clean summary.
    """

    def _fake_git_show(*_args: object, **_kwargs: object) -> object:
        return _make_completed_process(
            returncode=128,
            stderr=("fatal: path 'spec/process/obligations.yaml' does not exist in 'origin/main'"),
        )

    monkeypatch.setattr(_MODULE.subprocess, "run", _fake_git_show)  # type: ignore[attr-defined]
    # Force the non-test-seam path so we exercise `_read_base_manifest` proper.
    head_file = tmp_path / "head.yaml"
    head_file.write_text(
        dedent(
            """\
            version: 1
            obligations:
              - id: OBL-BRAND-NEW
                role: implementer
                body: "just landed"
            """
        ),
        encoding="utf-8",
    )
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    exit_code = _MODULE.main(  # type: ignore[attr-defined]
        ["--base-ref", "origin/main", "--head-file", str(head_file)]
    )
    assert exit_code == 0
    summary = summary_file.read_text(encoding="utf-8")
    assert "No obligation ids disappeared" in summary


def test_base_read_treats_exists_on_disk_stderr_as_empty_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alternate git wording ("exists on disk, but not in <rev>") also
    counts as legitimate empty base, not a tool failure.
    """

    def _fake_git_show(*_args: object, **_kwargs: object) -> object:
        return _make_completed_process(
            returncode=128,
            stderr=(
                "fatal: path 'spec/process/obligations.yaml' exists on disk, "
                "but not in 'origin/main'"
            ),
        )

    monkeypatch.setattr(_MODULE.subprocess, "run", _fake_git_show)  # type: ignore[attr-defined]
    head_file = tmp_path / "head.yaml"
    head_file.write_text("version: 1\nobligations: []\n", encoding="utf-8")
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    exit_code = _MODULE.main(  # type: ignore[attr-defined]
        ["--base-ref", "origin/main", "--head-file", str(head_file)]
    )
    assert exit_code == 0


def test_base_read_unknown_ref_raises_tool_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unfetched / unknown / ambiguous base ref must red the check.

    This is the exact fail-open the Tier B naysayer flagged (Finding 3): the
    previous "swallow every non-zero" branch would have called this an empty
    base and returned green. Under the msg-762 contract it MUST raise so the
    implementer triages the mechanism (bad checkout, missing fetch, etc.)
    rather than trusting a lying green.
    """

    def _fake_git_show(*_args: object, **_kwargs: object) -> object:
        return _make_completed_process(
            returncode=128,
            stderr=(
                "fatal: ambiguous argument 'origin/no-such-branch': "
                "unknown revision or path not in the working tree."
            ),
        )

    monkeypatch.setattr(_MODULE.subprocess, "run", _fake_git_show)  # type: ignore[attr-defined]
    head_file = tmp_path / "head.yaml"
    head_file.write_text("version: 1\nobligations: []\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))

    with pytest.raises(_MODULE.ObligationsReadbackToolError, match="ambiguous argument"):  # type: ignore[attr-defined]
        _MODULE.main(  # type: ignore[attr-defined]
            ["--base-ref", "origin/no-such-branch", "--head-file", str(head_file)]
        )


def test_base_read_forces_c_locale_for_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_read_base_manifest` must invoke `git show` with `LC_ALL=C` / `LANG=C`.

    Tier B naysayer Finding 1 on msg-768: the "file missing at revision"
    substring markers are English (`"does not exist in"` / `"exists on
    disk, but not in"`). Without a forced C locale, git emits localised
    stderr on a developer host set to e.g. `ja_JP.UTF-8` (executing this
    script under `OBL-PRCHECK-READ` locally), the substring matcher
    misses, and a valid empty-base result is misreported as a tool crash.

    We capture the `env=` kwarg that `main` passes to `subprocess.run` and
    assert both locale variables are set to `C` regardless of what the
    ambient shell has.
    """
    captured_env: dict[str, str] = {}

    def _capture_env(*_args: object, **kwargs: object) -> object:
        env = kwargs.get("env")
        if isinstance(env, dict):
            captured_env.update({str(k): str(v) for k, v in env.items()})
        return _make_completed_process(
            returncode=128,
            stderr=("fatal: path 'spec/process/obligations.yaml' does not exist in 'origin/main'"),
        )

    # Set the ambient locale to something non-C so we can see the override wins.
    monkeypatch.setenv("LC_ALL", "ja_JP.UTF-8")
    monkeypatch.setenv("LANG", "ja_JP.UTF-8")
    monkeypatch.setattr(_MODULE.subprocess, "run", _capture_env)  # type: ignore[attr-defined]
    head_file = tmp_path / "head.yaml"
    head_file.write_text("version: 1\nobligations: []\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))

    exit_code = _MODULE.main(  # type: ignore[attr-defined]
        ["--base-ref", "origin/main", "--head-file", str(head_file)]
    )
    assert exit_code == 0
    assert captured_env.get("LC_ALL") == "C", (
        "git subprocess must run with LC_ALL=C so the 'file missing' "
        "substring matcher works regardless of the ambient locale"
    )
    assert captured_env.get("LANG") == "C"


# --------------------------------------------------------------------------- #
# reference-scan robustness (Tier B naysayer Finding 2 on msg-768)
# --------------------------------------------------------------------------- #


def test_reference_scan_survives_non_utf8_bytes_in_a_scanned_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-UTF-8 byte in a scanned file must NOT hide OBL-* references in it.

    Tier B naysayer Finding 2 on msg-768: swallowing `UnicodeDecodeError`
    and `continue` skipped the entire file, so a single ISO-8859-1 smart
    quote could silently drop every OBL-* reference in that file from the
    advisory. `read_text(errors="replace")` decodes the ASCII parts
    cleanly (the regex only needs the ASCII OBL-* tokens) while leaving
    U+FFFD in place of the bad bytes.

    We construct a fake repo containing (a) a manifest that removes an id
    and (b) a docs file with a non-UTF-8 byte plus a surviving reference
    to the removed id. The advisory MUST list the reference in its
    summary; before the fix it silently reported zero surviving
    references.
    """
    fake_repo = tmp_path / "fake_repo"
    (fake_repo / "spec" / "process").mkdir(parents=True)
    # Head manifest: OBL-STILL-HERE remains; OBL-JUST-GONE has been dropped.
    (fake_repo / "spec" / "process" / "obligations.yaml").write_text(
        dedent(
            """\
            version: 1
            obligations:
              - id: OBL-STILL-HERE
                role: implementer
                body: "still"
            """
        ),
        encoding="utf-8",
    )
    # Scanned file: a Markdown doc that mentions OBL-JUST-GONE next to a
    # non-UTF-8 byte. Written as bytes so we control the encoding exactly.
    docs_dir = fake_repo / "docs"
    docs_dir.mkdir()
    (docs_dir / "notes.md").write_bytes(
        b"See OBL-JUST-GONE for context. Author name: Andr\xe9 (ISO-8859-1 e-acute).\n"
    )
    monkeypatch.setattr(_MODULE, "_REPO_ROOT", fake_repo)

    # Base has both ids; head has only OBL-STILL-HERE → OBL-JUST-GONE = REMOVED.
    base_file = tmp_path / "base.yaml"
    base_file.write_text(
        dedent(
            """\
            version: 1
            obligations:
              - id: OBL-STILL-HERE
                role: implementer
                body: "still"
              - id: OBL-JUST-GONE
                role: implementer
                body: "bye"
            """
        ),
        encoding="utf-8",
    )
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    exit_code = _MODULE.main(  # type: ignore[attr-defined]
        ["--base-file", str(base_file)]
    )
    assert exit_code == 0
    summary = summary_file.read_text(encoding="utf-8")
    assert "REMOVED: `OBL-JUST-GONE`" in summary
    # The surviving reference to OBL-JUST-GONE MUST be listed — the whole
    # point of Finding 2 is that a non-UTF-8 byte in `notes.md` must not
    # cause the scan to skip the file entirely.
    assert "docs/notes.md:1" in summary


# --------------------------------------------------------------------------- #
# rename-heuristic robustness (Tier B naysayer Finding 3 on msg-768)
# --------------------------------------------------------------------------- #


def test_body_match_rename_survives_dropping_origin_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename that also drops the origin block on the same commit is a rename,
    not a delete + add.

    Tier B naysayer Finding 3 on msg-768: the body-match fallback used to
    gate on ``old.moved_from is None AND candidate.moved_from is None``.
    That was over-constrained — the very "reclassify a verbatim-move as
    net-new AND rename it in the same commit" case (exactly what this PR
    did for OBL-READBACK-ENTRY / OBL-READBACK-EXIT, minus the rename)
    would fall through both branches and be reported as a spurious
    deletion + addition.

    Fixed: the body-match branch is unconditional on the old side's origin
    state. We construct exactly the pathological case and assert on
    RENAMED, not REMOVED.
    """
    base = dedent(
        """\
        version: 1
        obligations:
          - id: OBL-WAS-A-MOVE
            role: implementer
            origin:
              moved_from: "some/place::LITERAL"
              original_length: 32
            body: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        """
    )
    head = dedent(
        """\
        version: 1
        obligations:
          - id: OBL-NOW-NET-NEW
            role: implementer
            body: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        """
    )
    summary = _run(base, head, tmp_path, monkeypatch)
    assert "RENAMED: `OBL-WAS-A-MOVE` → `OBL-NOW-NET-NEW`" in summary
    assert "REMOVED: `OBL-WAS-A-MOVE`" not in summary


def test_base_read_missing_git_binary_propagates_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ``git`` binary (OSError) must propagate — not be swallowed.

    We deliberately do NOT try/except OSError inside `_read_base_manifest`;
    the msg-762 contract requires infrastructure failures to red the check.
    """

    def _boom_no_git(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("No such file or directory: 'git'")

    monkeypatch.setattr(_MODULE.subprocess, "run", _boom_no_git)  # type: ignore[attr-defined]
    head_file = tmp_path / "head.yaml"
    head_file.write_text("version: 1\nobligations: []\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))

    with pytest.raises(FileNotFoundError):
        _MODULE.main(  # type: ignore[attr-defined]
            ["--base-ref", "origin/main", "--head-file", str(head_file)]
        )
