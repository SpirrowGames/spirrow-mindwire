"""Reverse check for ADR references — every cited ADR id must resolve in the manifest.

This module implements the ``cited ⊆ index`` check on top of the same in-repo manifest
the naysayer and implementer already read (``spec/adr_index.yaml``, via
:func:`~spirrow_mindwire.naysayer.adr_index.load_adr_index`).

Scope of what this asserts
--------------------------
The check asserts *only* that any ADR id token in the repository resolves in the
committed manifest. It does **not** assert that the ADR exists. The manifest is a
derived view (union of ``CLAUDE.md §M`` + ``spirrow-docs/_docmap.yaml``) and neither
input covers the full ADR set — architecture ADRs live only in ``_docmap`` and
identity ADRs live only in §M — so an ADR can genuinely exist in spirrow-docs while
being absent from what CI sees. When this check fires, the correct question is
"does this reference resolve in *this* index" — not "does this ADR exist".

That framing matters because the failure message steers the fix. The message here
deliberately says the check's scope out loud so a reader does not silently rewrite
a real citation into ``This ADR does not exist`` — the exact failure this whole
sub-thread was born from.

Fixture exclusion — occurrence-unit, not id-unit
------------------------------------------------
Two test files carry synthetic ADR ids by design (they are testing the ADR machinery
itself) and would trigger this check with false positives: ``tests/test_adr_index_gen.py``
uses the placeholder id ``2026-01-01-1`` (an "ADR-"-prefixed literal that is not a real
ADR) as a rendering fixture, and ``tests/test_pr_review_adr_pointers.py`` uses the
made-up id ``9999-99-99-99`` for its unknown-id fixtures. Excluding those two files at
the *occurrence* level is safe; excluding those ids at the *id* level would silently
miss the same id if it ever appeared in production code — fixture ids are fixture ids
because of where they sit, not because of what they look like (Bohr §4 in msg-913).
This module also skips its own test file and its own source for the same reason: the
paired test's failure-message sample carries real-looking ids, and this module's own
prose describes fixture ids as bare tokens ``2026-01-01-1`` / ``9999-99-99-99``
(written here without the ``ADR-`` prefix so the check does not scan its own
documentation as citations).

The manifest file (``spec/adr_index.yaml``) is excluded because it is the *index*
itself, not a citation of it. Failing on the index would make ``cited ⊆ index``
trivially satisfied by construction and hide real misses.

CI integration
--------------
This module ships with a paired pytest test (``tests/test_adr_reverse_check.py``)
that runs the check against the working tree. The gate (``.mindwire-gate``) runs the
same pytest, so the check is enforced in CI and locally by the same path.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .adr_index import load_adr_index

# Matches an ADR id token anywhere in a file: ``ADR-YYYY-MM-DD-N`` where N is one or
# more digits. Kept byte-identical to the id regex used by :mod:`adr_index_gen`
# (:data:`_ADR_ID_RE`) so this check reads exactly the same set of tokens the naysayer
# index generator does — a divergence here would mean one process cites what the other
# never sees.
ADR_ID_RE = re.compile(r"ADR-\d{4}-\d{2}-\d{2}-\d+")

# Occurrence-level exclusions (see module docstring). These are file *paths*, not ids —
# the point is that a fixture id at *this location* is documented and expected, while
# the same id appearing elsewhere is not silently pardoned.
_FIXTURE_FILES: frozenset[str] = frozenset(
    {
        # Fixture: 2026-01-01-1 as a render_manifest input.
        "tests/test_adr_index_gen.py",
        # Fixture: 9999-99-99-99 as a made-up id used across several review tests.
        "tests/test_pr_review_adr_pointers.py",
        # This module's own paired test — the failure-message sample it prints back
        # contains real-looking ids and must not itself trigger the check.
        "tests/test_adr_reverse_check.py",
    }
)

# The index itself is not a citation; excluding it is what makes the ``cited ⊆ index``
# subset relation meaningful. Without this exclusion the check is trivially satisfied
# because every id in the index scans as a self-citation.
_INDEX_FILE = "spec/adr_index.yaml"


@dataclass(frozen=True)
class Occurrence:
    """A single ADR id token found at ``path``:``line`` (1-indexed line number)."""

    path: str
    line: int
    adr_id: str
    line_text: str


@dataclass(frozen=True)
class ReverseCheckResult:
    """Outcome of a single reverse-check run against a repository tree.

    ``unresolved`` groups every occurrence of an ADR id that is NOT in the manifest,
    keyed by adr id. ``scanned_files`` is the number of tracked files actually read
    (a small denominator would suggest ``git ls-files`` was empty and the check was
    trivially green).
    """

    unresolved: dict[str, tuple[Occurrence, ...]]
    scanned_files: int

    @property
    def is_green(self) -> bool:
        """Green iff every cited id resolves in the committed manifest."""
        return not self.unresolved


def _tracked_files(repo_root: Path) -> tuple[str, ...]:
    """Return the repo's tracked file paths (relative, forward-slash), or ``()``.

    Uses ``git ls-files`` so the check reads *exactly* the files the repository tracks
    — untracked scratch files (``.git/mindwire-scratch/…``) and generated caches
    (``__pycache__/``, ``.venv/``) never enter the scan. Returns ``()`` on failure (no
    git binary, non-git tree) so the caller surfaces "the scan was empty" rather than
    crashing; the scanned_files denominator makes an empty scan visible.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ()
    return tuple(line for line in result.stdout.splitlines() if line)


def _iter_occurrences(repo_root: Path, files: tuple[str, ...]) -> list[Occurrence]:
    occurrences: list[Occurrence] = []
    for rel_path in files:
        # The exclusions live here (not in the tracked-files list) because they are
        # *content* decisions, not "these paths are not tracked". Keeping the file
        # visible while skipping its occurrences would be lying by another name;
        # skipping the file entirely says out loud what is going on.
        if rel_path == _INDEX_FILE or rel_path in _FIXTURE_FILES:
            continue
        full = repo_root / rel_path
        try:
            # UTF-8 with ``replace`` mirrors the same tolerance rule
            # ``scripts/obligations_readback.py`` adopted after Tier B PR #135 msg-768
            # Finding 2: a single non-UTF-8 byte anywhere in a scanned file must not
            # silently drop the file's ADR ids from the scan.
            content = full.read_text(encoding="utf-8", errors="replace")
        except (OSError, IsADirectoryError):
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            for match in ADR_ID_RE.finditer(line):
                occurrences.append(
                    Occurrence(
                        path=rel_path,
                        line=line_number,
                        adr_id=match.group(0),
                        line_text=line.rstrip(),
                    )
                )
    return occurrences


def run_reverse_check(repo_root: Path) -> ReverseCheckResult:
    """Scan ``repo_root`` for ADR id occurrences and group the unresolved ones.

    ``repo_root`` must be the root of a git worktree; the scan uses ``git ls-files``
    so it never picks up scratch files or generated caches. Fixture files and the
    manifest itself are excluded per the module docstring; that exclusion is at the
    *occurrence* level (a file, not an id).
    """
    files = _tracked_files(repo_root)
    index_ids = {adr_id for adr_id, _ in load_adr_index(repo_root)}
    occurrences = _iter_occurrences(repo_root, files)
    unresolved: dict[str, list[Occurrence]] = {}
    for occ in occurrences:
        if occ.adr_id in index_ids:
            continue
        unresolved.setdefault(occ.adr_id, []).append(occ)
    return ReverseCheckResult(
        unresolved={aid: tuple(occs) for aid, occs in unresolved.items()},
        scanned_files=len(files),
    )


def format_failure_message(result: ReverseCheckResult, *, manifest_path: str) -> str:
    """Render the human-facing failure message for a red result — verbatim per Bohr §4.

    The message says the check's scope out loud (only "resolves in this index", never
    "exists in the universe") and lists the two branches a fix can take, plus a
    stop-and-report clause for the residual case. The one thing it deliberately does
    NOT do is tell the reader to run ``scripts/gen_adr_index.py`` — the loop host has
    no ``_docmap.yaml`` and cannot regenerate the index there, and printing an
    unrunnable command on every red run would reproduce this sub-thread's own root
    cause at the CI-output layer.
    """
    lines = ["ADR reference does not resolve in the committed ADR index.", ""]
    for adr_id in sorted(result.unresolved):
        for occ in result.unresolved[adr_id]:
            lines.append(f"  {adr_id}   referenced at {occ.path}:{occ.line}")
    lines.extend(
        [
            f"  index: {manifest_path}",
            "         (generated from CLAUDE.md §M + spirrow-docs/_docmap.yaml)",
            "",
            "Scope of this check: it asserts only that a referenced number resolves in the",
            "committed index. It does NOT assert that the ADR exists. An ADR can exist in",
            "spirrow-docs and still be absent from this index — the index does not cover",
            "every ADR.",
            "",
            "If the reference is meant to resolve: the index must gain the entry. That is a",
            "spirrow-docs-side change and cannot be made from this repository.",
            "",
            "If it is a mention rather than a citation: write the number without the ADR-",
            "prefix and state, in the same sentence, where it resolves or that it does not.",
            "",
            "If neither applies: stop and report it in the thread, with file:line and which",
            "of the two cases you ruled out.",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "ADR_ID_RE",
    "Occurrence",
    "ReverseCheckResult",
    "format_failure_message",
    "run_reverse_check",
]
