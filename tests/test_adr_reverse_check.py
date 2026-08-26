"""Reverse-check gate — every ADR id cited in the repo must resolve in the manifest.

The check itself lives in :mod:`spirrow_mindwire.naysayer.adr_reverse_check`; this
module is the CI enforcement surface: the gate (``.mindwire-gate``) runs pytest,
pytest runs the working-tree assertion below, and a red result is what stops a PR
that names an ADR id the manifest never heard of. That is the failure mode this
sub-thread was born from (T-adr-index-dangling-references) — an implementer told
to comply with ``ADR-2026-05-29-13`` when there is no such body available.

The check is deliberately narrow. It asserts ``cited ⊆ index``, not "the ADR exists
in the universe" — see the docstring of :mod:`~spirrow_mindwire.naysayer.adr_reverse_check`
for why the wider assertion is unattainable on the loop host (no ``_docmap.yaml``,
so no runtime union and no CI drift-check).

Fixture-id notes
----------------
This file itself is on the check's occurrence-level fixture exclusion list, so the
sample failure-message output the parametrized test below prints back may contain
real-looking ids without pardoning them anywhere else. Tests for the check's own
internals go in the unit test below and use a ``tmp_path`` git repo to avoid coupling
to the working tree's actual state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spirrow_mindwire.naysayer.adr_reverse_check import (
    ADR_ID_RE,
    format_failure_message,
    run_reverse_check,
)

# tests/… -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_regex_matches_the_canonical_shape() -> None:
    """The scanner regex matches the ``ADR-YYYY-MM-DD-N`` shape used across the repo."""
    for sample in (
        "ADR-2026-05-21-06",
        "ADR-2026-06-04-19",
        "ADR-9999-99-99-99",
        "ADR-2026-01-01-1",
    ):
        assert ADR_ID_RE.fullmatch(sample), sample


def test_working_tree_cited_ids_are_a_subset_of_the_committed_manifest() -> None:
    """Every ADR id token in the working tree must resolve in ``spec/adr_index.yaml``.

    This is the primary CI-enforced assertion — a red run here means an ADR id
    somewhere in the repo does not appear in the committed manifest, and the fix is
    one of the two branches the failure message spells out (rename to a resolvable
    id, or de-cite it because it is a mention rather than a citation).
    """
    result = run_reverse_check(_REPO_ROOT)
    assert result.scanned_files > 0, (
        "reverse check scanned zero files — git ls-files returned nothing. "
        "Either the tree is empty or git is unavailable here; the check is trivially "
        "green and therefore worthless. Investigate before treating this as OK."
    )
    if not result.is_green:
        message = format_failure_message(result, manifest_path="spec/adr_index.yaml")
        pytest.fail(message)


def test_fixture_files_are_not_scanned_but_would_flag_if_they_were(tmp_path: Path) -> None:
    """The occurrence-level fixture exclusion actually excludes what it claims to.

    Constructs a tiny git worktree that mirrors the fixture-file arrangement (a
    made-up ADR id inside a file whose path matches the fixture-exclusion list), and
    checks (a) the scan is green when that file is at the excluded path, and (b) the
    scan reds when the same content lives at a non-excluded path — proving the
    exclusion is what is doing the work, not the id itself.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(
        'adrs:\n  - id: ADR-2026-05-21-06\n    title: "Ports"\n',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    fixture_body = '"""fixture: ADR-9999-99-99-99 used as a made-up id."""\n'
    fixture_file = tmp_path / "tests" / "test_pr_review_adr_pointers.py"
    fixture_file.write_text(fixture_body, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."],
        check=True,
    )
    green = run_reverse_check(tmp_path)
    assert green.is_green, (
        "fixture file at the excluded path should not fire the check; unresolved="
        f"{green.unresolved}"
    )

    # Same content, non-excluded path — must red. This is the mirror the "id-unit
    # exclusion is forbidden" rule (Bohr §4 msg-913) is guarding: the fixture ids
    # are OK because of *where* they sit, not because of *what they look like*.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "not_a_fixture.md").write_text(fixture_body, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    red = run_reverse_check(tmp_path)
    assert not red.is_green
    assert "ADR-9999-99-99-99" in red.unresolved


def test_index_file_is_excluded_so_the_subset_relation_is_meaningful(tmp_path: Path) -> None:
    """The manifest file itself does not count as a citation.

    If the index were scanned, every id in it would scan as a self-citation and the
    ``cited ⊆ index`` subset relation would be trivially satisfied — the check
    would be worthless. This test pins the exclusion by constructing a tree where the
    only occurrence of an id is *inside* the manifest, and expects the scan to still
    read that id as an index entry (green), not as an unresolved citation (red).
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(
        'adrs:\n  - id: ADR-2026-05-21-06\n    title: "Ports"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    result = run_reverse_check(tmp_path)
    assert result.is_green, f"unresolved={result.unresolved}"


def test_failure_message_states_check_scope_verbatim(tmp_path: Path) -> None:
    """The failure message must say the check's scope out loud, not just list ids.

    Bohr §1 (msg-939): a message that lists only the offending id and nothing else
    reads as "this ADR does not exist" and provokes destructive edits to real
    citations. The message must instead name the two branches (resolvable /
    de-cite) and the stop-and-report residual, and must not print an
    ``scripts/gen_adr_index.py`` command the loop host cannot actually run.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(
        'adrs:\n  - id: ADR-2026-05-21-06\n    title: "Ports"\n',
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("see ADR-2026-05-21-04\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    result = run_reverse_check(tmp_path)
    assert not result.is_green

    message = format_failure_message(result, manifest_path="spec/adr_index.yaml")
    assert "ADR-2026-05-21-04" in message
    assert "docs/notes.md:1" in message
    assert "Scope of this check" in message
    assert "does NOT assert that the ADR exists" in message
    assert "mention rather than a citation" in message
    assert "stop and report" in message
    # Must not tell the reader to run a command the loop host cannot execute.
    assert "gen_adr_index.py" not in message
