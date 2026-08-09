#!/usr/bin/env python3
"""``obligations-readback (advisory)`` — non-blocking PR check.

Diffs the loop-readable obligations manifest (``spec/process/obligations.yaml``)
between the PR **base revision** and the PR head. Reports:

- obligation ids that disappeared (present in base, absent in head)
- obligation ids that were renamed (heuristic: a disappeared id whose body
  survives — same ``origin.moved_from`` and ``origin.original_length``, or same
  body text — under a new id)
- for each disappearing/renamed id, any references to the *old* id that still
  live in the head repository (Python string literals, docs mentions, test
  hardcodings) — reported as ``file:line`` so the reviewer does not have to
  hunt them by hand
- a one-line minimum-remediation suggestion for each finding

**This check is advisory. It does not block a merge.** This repository has no
GitHub branch protection under the current plan (Pro-upgrade rejected — see
msg-761 environment facts), so the authoritative merge guard is Takahito at
the PR merge-box, not a required check. Naming the check ``(advisory)`` is
where that fact is cheapest to state honestly: it appears in the merge-box
next to the green/red badge.

**Scope — what this check does NOT catch:**

- Direct pushes to ``main`` (this check only fires on the ``pull_request``
  workflow trigger). Once a drift lands in ``main`` via direct push it becomes
  the new base, so subsequent PRs see it as the "correct" baseline and the
  drift is never surfaced again. This is a structural consequence of deriving
  the expected set from the base revision rather than carrying a shadow list;
  it is also unavoidable without branch protection — the check side cannot
  close it.
- Semantic drift where the id is preserved but the body is rewritten. That is
  what canary two-double-prime (``tests/test_obligations.py``) is for: any
  moved-from body whose length no longer matches ``origin.original_length``
  reds the gate. This advisory check is scoped to id-level topology (add /
  remove / rename), because reasoning about "did the meaning change" from a
  diff would require this script to hold a shadow of the reviewed meaning —
  the very shadow-list dual-management the design deliberately rejected.

**Readers:** Takahito at PR-merge time (the merge-box badge + the
``$GITHUB_STEP_SUMMARY`` details this script writes), and the implementer via
``OBL-PRCHECK-READ`` before it hands ``NEXT: human``.

CLI:

    obligations_readback.py --base-ref origin/main
    obligations_readback.py --base-file <path> --head-file <path>   # test seam

The head side ALWAYS reads from the working tree (both the manifest and the
reference scan) — never from ``git show``. This is deliberate (Tier B naysayer
Finding 2 on PR #135): if the manifest was read from a git object while
``_scan_references`` walked the filesystem, the two would diverge on a dirty
working tree, silently reporting references from one revision against a
manifest from another. Unifying both reads on the working tree eliminates that
hybrid state. In CI ``actions/checkout@v4`` puts the PR merge commit into the
working tree, so the working-tree read is exactly the head; there is no
``--head-ref`` argument because the redundancy would only re-open the hybrid.

Exit code contract (Einstein / msg-762 objection):

- **Exits 0** on the normal path, regardless of whether any findings were
  produced. Advisory findings are surfaced through the summary text, not
  the exit code — turning them into a gate is exactly what this design
  rejects.
- **Exits non-zero** (via an unhandled exception propagating out of
  ``main``) on a real tool failure — a Python error, a broken import, a
  malformed CLI invocation. The workflow deliberately does NOT set
  ``continue-on-error: true``, so a tool crash reds the check and the
  implementer executing ``OBL-PRCHECK-READ`` triages the mechanism itself
  rather than misreading "green + empty summary" as "zero findings".

Tolerance boundary: a malformed manifest on either side (base or head) is
treated as an empty set and reported as a clean advisory run, *not* a tool
failure. That is deliberate — the fail-closed loader in
``obligations.py`` is what validates schema at daemon startup, and this
advisory must not double-gate the same invariant.

**Base-read discrimination (Tier B naysayer Finding 3 on PR #135):**
"empty base" (returned as empty string, feeds the diff, exits 0) means
strictly *"git reports the manifest file did not exist at that revision"*
— the added-in-this-PR-manifest case, matched by
:data:`_GIT_FILE_MISSING_STDERR_MARKERS`. Everything else that ``git
show`` can fail with (missing binary, unknown ref, unfetched revision,
ambiguous argument, network / permission) is a real tool failure and
raises :class:`ObligationsReadbackToolError`, propagating out of
``main`` and reding the check. The previous "swallow every non-zero"
recreated the exact fail-open the msg-762 exit-contract exists to
prevent — it would have interpreted a broken checkout as "empty base =
zero findings" and reported green.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

# Repo layout: scripts/ -> <repo root>. Kept as a plain resolve so the script
# also works in a shallow CI checkout as long as the working directory is the
# repo root (the workflow does `actions/checkout@v4` before invoking it).
_REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_REL = Path("spec") / "process" / "obligations.yaml"


@dataclass(frozen=True)
class _ObligationSnapshot:
    """A minimal, id-keyed view of one manifest entry — enough to detect drift.

    We deliberately do NOT reuse ``spirrow_mindwire.obligations`` here: this
    check must read the manifest at the base revision, and importing the
    package would tie us to the head-revision loader (whose schema may have
    moved). Reading the YAML directly keeps base and head symmetric.
    """

    id: str
    role: str
    body: str
    moved_from: str | None
    original_length: int | None


def _load_snapshot(text: str) -> dict[str, _ObligationSnapshot]:
    """Parse a manifest's YAML text into ``{id: snapshot}``; tolerant of shape drift.

    Returns an empty mapping on any structural surprise — the advisory job's
    contract is "report what changed", not "validate the manifest" (the
    fail-closed loader in ``obligations.py`` is what validates it). A structural
    surprise here is reported as an empty set rather than blowing up the check.
    """
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    entries = data.get("obligations")
    if not isinstance(entries, list):
        return {}
    snapshots: dict[str, _ObligationSnapshot] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        obligation_id = entry.get("id")
        if not isinstance(obligation_id, str):
            continue
        role = entry.get("role") if isinstance(entry.get("role"), str) else ""
        body = entry.get("body") if isinstance(entry.get("body"), str) else ""
        origin = entry.get("origin")
        moved_from: str | None = None
        original_length: int | None = None
        if isinstance(origin, dict):
            mf = origin.get("moved_from")
            ol = origin.get("original_length")
            if isinstance(mf, str):
                moved_from = mf
            if isinstance(ol, int):
                original_length = ol
        snapshots[obligation_id] = _ObligationSnapshot(
            id=obligation_id,
            role=role,
            body=body.rstrip("\n"),
            moved_from=moved_from,
            original_length=original_length,
        )
    return snapshots


# Substrings that appear in git's stderr when the requested path did not
# exist at the requested revision (an added-in-this-PR manifest — a
# legitimate empty base, not a tool failure). Case-insensitive match on the
# lowered stderr; both wordings ship with modern git.
#
#   "fatal: path 'X' does not exist in 'Y'"
#   "fatal: path 'X' exists on disk, but not in 'Y'"
#
# EVERY other git failure (missing binary → OSError; unknown/unfetched ref →
# "fatal: bad revision"; ambiguous argument; network failure; permission
# denied) is a tool / infrastructure failure and MUST propagate so the CI
# check reds under the msg-762 advisory-vs-tool contract. Tier B naysayer
# Finding 3 on PR #135 called out the previous "swallow every non-zero" as
# the exact fail-open the contract was designed to prevent.
_GIT_FILE_MISSING_STDERR_MARKERS: tuple[str, ...] = (
    "does not exist in",
    "exists on disk, but not in",
)


class ObligationsReadbackToolError(RuntimeError):
    """A tool / infrastructure failure that must red the CI check.

    Raised for anything that is NOT a "file missing at a valid revision"
    result. Kept separate from :class:`RuntimeError` at the type level so a
    caller (or a test) can assert on the discrimination itself — see
    ``tests/test_obligations_readback.py::test_unknown_base_ref_is_treated_as_tool_failure``.
    """


def _read_base_manifest(base_ref: str) -> str:
    """Read ``spec/process/obligations.yaml`` at ``base_ref`` via ``git show``.

    Returns the empty string ONLY when git reports "path did not exist at
    that revision" (matched by :data:`_GIT_FILE_MISSING_STDERR_MARKERS`) —
    that is the added-in-this-PR-manifest case, which is not a drift and
    correctly compares against an empty base. Every other git failure is a
    tool / infrastructure failure and is raised as
    :class:`ObligationsReadbackToolError` so it propagates out of ``main``
    and reds the CI check (msg-762 advisory-vs-tool contract; Tier B
    naysayer Finding 3 on PR #135). ``OSError`` from ``subprocess.run``
    (missing ``git`` binary, permission denied) is left to propagate for the
    same reason — we deliberately do not catch it here.
    """
    rel = _MANIFEST_REL.as_posix()
    result = subprocess.run(
        ["git", "show", f"{base_ref}:{rel}"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    stderr_lower = (result.stderr or "").lower()
    if any(marker in stderr_lower for marker in _GIT_FILE_MISSING_STDERR_MARKERS):
        return ""
    raise ObligationsReadbackToolError(
        f"`git show {base_ref}:{rel}` failed with return code {result.returncode} "
        f"and this is NOT a 'file missing at valid revision' result — this is a "
        f"tool / infrastructure failure (unknown or unfetched ref, ambiguous "
        f"argument, environment issue). Under the msg-762 advisory-vs-tool "
        f"contract this MUST red the CI check rather than be silently swallowed "
        f"as 'empty base = zero findings' (Tier B naysayer Finding 3 on PR #135). "
        f"git stderr: {result.stderr.strip()!r}"
    )


def _read_head_manifest() -> str:
    """Read the manifest from the working tree (never from ``git show``).

    The reference scan (:func:`_scan_references`) also walks the working tree,
    so unifying both reads on the same source of truth eliminates the
    hybrid-state class Tier B naysayer Finding 2 flagged on PR #135. In CI
    ``actions/checkout@v4`` puts the PR merge commit into the working tree;
    locally, "head" means "whatever is on disk right now" — which is also
    exactly what the reference scan sees.
    """
    try:
        return (_REPO_ROOT / _MANIFEST_REL).read_text(encoding="utf-8")
    except OSError:
        return ""


# A pattern that matches an obligation id as a whole token in code / docs.
# Kept intentionally simple: ``OBL-`` followed by ASCII uppercase / digits /
# hyphens, not preceded or followed by another such char. Locale-insensitive
# on purpose (we do not want a locale that classifies ``-`` as a word char to
# silently over-match).
_ID_TOKEN_TEMPLATE = r"(?<![A-Za-z0-9_-]){id}(?![A-Za-z0-9_-])"

# File extensions we scan for references. Deliberately excludes the manifest
# itself (the id "reference" there IS the definition) and binary/vendored dirs.
_SCAN_EXTS = frozenset({".py", ".md", ".yaml", ".yml", ".toml", ".txt"})
_SCAN_EXCLUDE_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}
)


def _scan_references(obligation_id: str) -> list[tuple[str, int]]:
    """Find ``file:line`` sites in the head repo that mention ``obligation_id``.

    Skips the manifest itself and ignored dirs. Returns an empty list if no
    references exist — a disappeared id with no surviving references is a
    clean removal; one with surviving references is a partial removal to flag.
    """
    pattern = re.compile(_ID_TOKEN_TEMPLATE.format(id=re.escape(obligation_id)))
    manifest_abs = (_REPO_ROOT / _MANIFEST_REL).resolve()
    hits: list[tuple[str, int]] = []
    for root, dirs, files in os.walk(_REPO_ROOT):
        # In-place prune of ignored dirs so os.walk does not recurse into them.
        dirs[:] = [d for d in dirs if d not in _SCAN_EXCLUDE_DIRS]
        for name in files:
            path = Path(root) / name
            if path.suffix not in _SCAN_EXTS:
                continue
            try:
                if path.resolve() == manifest_abs:
                    continue
            except OSError:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    rel = path.relative_to(_REPO_ROOT).as_posix()
                    hits.append((rel, lineno))
    return hits


@dataclass(frozen=True)
class _Finding:
    """A single advisory-check finding — one row in the summary."""

    kind: str  # "removed" | "renamed"
    old_id: str
    new_id: str | None  # only set for kind == "renamed"
    references: tuple[tuple[str, int], ...]
    remediation: str


def _match_rename(
    old: _ObligationSnapshot, added: dict[str, _ObligationSnapshot]
) -> _ObligationSnapshot | None:
    """Heuristic: is ``old`` really the same clause under a new id in ``added``?

    Two signals — the origin block (``moved_from`` + ``original_length``) is
    the strongest (a moved clause carries its identity through the length
    invariant), and the body text is the fallback for net-new formulations
    without an origin. First-match wins; a second match would be noise and is
    left as a plain removal for a reviewer to disambiguate.
    """
    for candidate in added.values():
        if (
            old.moved_from is not None
            and candidate.moved_from == old.moved_from
            and candidate.original_length == old.original_length
            and candidate.original_length is not None
        ):
            return candidate
        if old.moved_from is None and candidate.moved_from is None and candidate.body == old.body:
            return candidate
    return None


def _build_findings(
    base: dict[str, _ObligationSnapshot], head: dict[str, _ObligationSnapshot]
) -> list[_Finding]:
    removed_ids = set(base) - set(head)
    added_ids = set(head) - set(base)
    added_snapshots = {i: head[i] for i in added_ids}
    findings: list[_Finding] = []
    for old_id in sorted(removed_ids):
        old = base[old_id]
        rename_target = _match_rename(old, added_snapshots)
        references = tuple(_scan_references(old_id))
        if rename_target is not None:
            # Only consume the added snapshot if we treat this as a rename, so a
            # second removal cannot double-claim the same new id.
            added_snapshots.pop(rename_target.id, None)
            remediation = (
                f"add a redirect / release-note that {old_id} → {rename_target.id}, or "
                f"update the {len(references)} surviving reference(s) to the new id"
                if references
                else f"no surviving references to {old_id}; rename is clean"
            )
            findings.append(
                _Finding(
                    kind="renamed",
                    old_id=old_id,
                    new_id=rename_target.id,
                    references=references,
                    remediation=remediation,
                )
            )
        else:
            remediation = (
                f"delete or update the {len(references)} surviving reference(s) to {old_id}"
                if references
                else f"no surviving references to {old_id}; removal is clean"
            )
            findings.append(
                _Finding(
                    kind="removed",
                    old_id=old_id,
                    new_id=None,
                    references=references,
                    remediation=remediation,
                )
            )
    return findings


def _format_summary(findings: list[_Finding]) -> str:
    """Format the findings as a Markdown block for ``$GITHUB_STEP_SUMMARY``."""
    lines: list[str] = ["## obligations-readback (advisory)", ""]
    if not findings:
        lines.append(
            "No obligation ids disappeared or were renamed between the PR base and head. "
            "**This check does not block a merge — the authoritative guard is the human at "
            "merge-time (no branch protection on this repo under the current plan).**"
        )
        return "\n".join(lines) + "\n"
    lines.append(
        "One or more obligation ids in ``spec/process/obligations.yaml`` disappeared or "
        "were renamed between the PR base and head. **This check does not block a merge** — "
        "the authoritative guard is the human at merge-time. The findings below name the "
        "consequences and a one-line remediation each; the reviewer does not need to grep "
        "the repo to see what still points at a removed id."
    )
    lines.append("")
    for finding in findings:
        if finding.kind == "renamed":
            lines.append(f"### RENAMED: `{finding.old_id}` → `{finding.new_id}`")
        else:
            lines.append(f"### REMOVED: `{finding.old_id}`")
        lines.append("")
        if finding.references:
            lines.append("Surviving references in the head repo:")
            for rel, lineno in finding.references:
                lines.append(f"- `{rel}:{lineno}`")
        else:
            lines.append("No surviving references in the head repo.")
        lines.append("")
        lines.append(f"**Minimum remediation:** {finding.remediation}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _write_summary(text: str) -> None:
    """Write ``text`` to ``$GITHUB_STEP_SUMMARY`` if set, otherwise stdout.

    A broken step-summary sink is intentionally handled by *also* mirroring to
    stdout — GitHub captures stdout in the raw job log, so the finding text is
    never lost even if the summary target cannot be opened. The advisory
    contract (Einstein / msg-762) is that a genuine tool failure (import
    error, unhandled exception) reds the check via a non-zero exit; a
    successful analysis with a busted sink still counts as a successful
    analysis and must not be conflated with a crash.
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    wrote_to_summary = False
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as fp:
                fp.write(text)
                wrote_to_summary = True
        except OSError:
            # Fall through to stdout — advisory must not fail on a broken step-summary sink.
            pass
    if not wrote_to_summary:
        sys.stdout.write(text)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Advisory diff of the loop-readable obligations manifest between "
        "a PR base and head.",
    )
    parser.add_argument(
        "--base-ref",
        default=None,
        help="git ref for the PR base (e.g. origin/main). Read via `git show <ref>:<manifest>`.",
    )
    parser.add_argument(
        "--base-file",
        default=None,
        help="test seam: read the base manifest from this file instead of via git.",
    )
    parser.add_argument(
        "--head-file",
        default=None,
        help=(
            "test seam: read the head manifest from this file instead of the working tree. "
            "There is deliberately no `--head-ref` — the head side always reads from the "
            "working tree so it is guaranteed to match what `_scan_references` walks "
            "(Tier B naysayer Finding 2 on PR #135)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    base_text = (
        Path(args.base_file).read_text(encoding="utf-8")
        if args.base_file
        else _read_base_manifest(args.base_ref or "origin/main")
    )
    head_text = (
        Path(args.head_file).read_text(encoding="utf-8")
        if args.head_file
        else _read_head_manifest()
    )
    base = _load_snapshot(base_text)
    head = _load_snapshot(head_text)
    findings = _build_findings(base, head)
    _write_summary(_format_summary(findings))
    # Exit 0 on the normal path regardless of findings — the workflow does NOT
    # set `continue-on-error: true`, so tool crashes propagate non-zero and
    # red the check (Einstein / msg-762). See module docstring §「Exit code
    # contract」.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
