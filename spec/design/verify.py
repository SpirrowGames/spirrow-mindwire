#!/usr/bin/env python3
"""spec/design/verify.py — diagnostic for the spec-delivery mechanism.

Implements V-1..V-14 from SPEC-2026-09-20-pin-hardening-and-id-audit §5
(inheriting SPEC-2026-08-11-design-spec-delivery via ``supersedes``) and the
fail-closed pin-resolution procedure from §3.  This script is a diagnostic,
not a CI gate (D-10 / D-36): `main` is expected to be error-0 / warning-0
(A-12); errors and warnings from V-1..V-14 do NOT affect the exit code —
only usage / environment failures do.  See D-36 (§5-E) for the exit-code
independence rationale.

Usage:
    python spec/design/verify.py [--repo-root PATH] [--json] [--pin-only] [--no-fetch]

Exit codes:
    0 — script executed to completion (regardless of errors / warnings from
        checks; per D-36 verify.py is diagnostic, not gate)
    2 — usage / environment error (not a git repo, unusable arguments,
        YAML load failure of a required file)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Constants — spec §3 (pin schema) and §5 (checks)
# ---------------------------------------------------------------------------

# The thirteen pin-resolution reason codes, in the order §3-D lists them
# (12 FAULT codes then the sole BOOTSTRAP proceed-code).  A-31 / V-13 require
# every one of these to appear verbatim in OBL-SPEC-PIN's body when the entry
# exists.  BOOTSTRAP and PROHIBITED_FIELD are new in
# SPEC-2026-09-20-pin-hardening-and-id-audit; unknown ``mode`` values re-use
# MISSING_FIELD (justified body-side by the "any value other than resolved
# or bootstrap is also MISSING_FIELD" sentence), so the enumeration stays at
# 13 rather than adding a UNKNOWN_MODE code (D-35 / §9 rejected).
PIN_REASON_CODES: tuple[str, ...] = (
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
)

# §3-A: seven fields whose presence turns a ``mode: bootstrap`` pin into
# PROHIBITED_FIELD.  A bootstrap pin must carry none of the resolved-form
# identity/reachability fields; a mixed pin is neither one nor the other.
_BOOTSTRAP_PROHIBITED_FIELDS: tuple[str, ...] = (
    "spec_id",
    "thread",
    "repo",
    "branch",
    "path",
    "blob_sha",
    "commit",
)

# Manifest front-matter — required top-level keys and their expected types.
_REQUIRED_MANIFEST_KEYS: dict[str, type | tuple[type, ...]] = {
    "spec_id": str,
    "thread": str,
    "target_repo": str,
    "base_branch": str,
    "status": str,
    "canary": str,
    "supersedes": list,
    "obligations": list,
    "items": list,
}

# §5-C — optional top-level keys.  When present, V-1 checks the type; when
# absent, V-1 is silent.  Unknown keys not in either dict are silently
# tolerated (forward-compat: adding a new optional key in a successor spec
# must not fail V-1 on older manifests that do not know about it).
_OPTIONAL_MANIFEST_KEYS: dict[str, type | tuple[type, ...]] = {
    "verify_exempt_ids": list,
}

# §6 tripartite model of item-level fields.  Each item-level field has TWO
# independent properties, and each of the three sets below captures ONE
# property.  Do not conflate them.
#
#   inherited=YES  -> item may OMIT the field; V-6 falls back to the root
#                     value and errors iff neither side supplies it.
#   overridable=YES -> item MAY declare the field; when it does, that value
#                      applies to that item only.  V-12 is silent.
#   overridable=NO  -> item must NOT declare the field; V-12 errors if it
#                      does.  Root supplies the only legal value.
#
# Per-field decomposition (spec §6 lines 411 / 413 / 414):
#
#   field          inherited   overridable    lives in
#   -----------    ---------   -----------    -----------------------------
#   target_repo    YES         NO (D-22)      _INHERITED_ITEM_FIELDS
#                                              + _ROOT_ONLY_ITEM_FIELDS
#   base_branch    YES         YES            _INHERITED_ITEM_FIELDS
#                                              + _OVERRIDABLE_ITEM_FIELDS
#   canary         YES         YES            _INHERITED_ITEM_FIELDS
#                                              + _OVERRIDABLE_ITEM_FIELDS
#   spec_id        n/a         NO             _ROOT_ONLY_ITEM_FIELDS
#   thread         n/a         NO             _ROOT_ONLY_ITEM_FIELDS
#   supersedes     n/a         NO             _ROOT_ONLY_ITEM_FIELDS
#
# `target_repo` intentionally appears in BOTH _INHERITED_ITEM_FIELDS and
# _ROOT_ONLY_ITEM_FIELDS.  That is not a contradiction: the spec (D-22)
# defines it as "inherited-but-not-overridable" -- item must not declare
# it (V-12), yet V-6 still checks that root supplies it and reports the
# failure with per-item context when root is missing it.

# §6 line 411 — fields V-6 must resolve for each item (item may omit;
# resolution falls back to manifest root).
_INHERITED_ITEM_FIELDS: tuple[str, ...] = ("target_repo", "base_branch", "canary")

# §6 line 413 — fields the item MAY declare to override root for that item
# only.  Not currently consumed by a check (item-side declaration is silently
# honored by _resolved_item_field for these two fields); kept as documentation
# of the tripartite model and to catch drift if the spec adds more overridable
# fields.
_OVERRIDABLE_ITEM_FIELDS: tuple[str, ...] = ("base_branch", "canary")

# §6 line 414 — fields V-12 must reject when an item declares them.  Root is
# the only legal home.
_ROOT_ONLY_ITEM_FIELDS: tuple[str, ...] = (
    "spec_id",
    "thread",
    "supersedes",
    "target_repo",
)

# §5 V-3 — enum values.
_STATUS_ENUM: frozenset[str] = frozenset({"active", "withdrawn"})
_CANARY_ENUM: frozenset[str] = frozenset({"required", "not-applicable"})

_HEX40 = re.compile(r"\A[0-9a-f]{40}\Z")

_YAML_FRONTMATTER = re.compile(r"\A---\r?\n(?P<body>.*?)\r?\n---\r?\n?", re.DOTALL)

# ---------------------------------------------------------------------------
# V-14 patterns — §5-D of SPEC-2026-09-20-pin-hardening-and-id-audit
# ---------------------------------------------------------------------------
#
# Bounded id pattern.  Prefix is bound to [A-Z]{1,2} to structurally exclude
# 3+ letter acronyms (SHA / UTF / ADR / SPEC / HTTP / JSON / XML / HTML / CSS
# / TCP / DNS / JWT / ISO / ASCII); digit run is bound to \d{1,3} to exclude
# 4+ digit terms (ES-2015).  The negative lookarounds prevent substring
# extraction from longer tokens like SPEC-2026-... or ADR-2026-... whose
# surrounding chars are ``[A-Z0-9-]``.  Prime (U+2032) is preserved as part
# of the id (the spec's "D-25 <U+2032 PRIME>" convention).  The rare
# 2-letter false-positives that slip past the bound (IL-6, S-1) are
# handled via ``verify_exempt_ids`` (D-38) — see D-37 for the
# responsibility split rationale.
# The literal ``U+2032 PRIME`` and ``U+FF08 FULLWIDTH LEFT PARENTHESIS``
# below are spelled with ``\u`` escapes so the ruff RUF001 / RUF003 lints
# stay silent — the compiled patterns behave identically to the equivalent
# raw-string forms the spec text uses.
_PRIME = "\N{PRIME}"
_FULLWIDTH_LPAREN = "\N{FULLWIDTH LEFT PARENTHESIS}"

_ID_REFERENCE = re.compile("(?<![A-Z0-9-])[A-Z]{1,2}-\\d{1,3}" + _PRIME + "?(?![A-Z0-9-])")

# Definition patterns (SPEC-2026-09-20 section 5-D 定義子抽出).  Bold form:
# first occurrence per line of ``**X-nn**`` or ``**X-nn(`` (either the
# half-width U+0028 or the full-width U+FF08 that the spec's own
# decision-list titles use).  Table form: row starting with ``| X-nn |``.
# Symmetrically bounded with the reference pattern so a definition that
# is bounded out on one side is not extracted on the other (which would
# create a "defined but not referenced" ghost).
_DEF_BOLD = re.compile(
    "\\*\\*([A-Z]{1,2}-\\d{1,3}" + _PRIME + "?)(?:\\*\\*|[" + _FULLWIDTH_LPAREN + "(])"
)
_DEF_TABLE = re.compile("^\\|\\s*([A-Z]{1,2}-\\d{1,3}" + _PRIME + "?)\\s*\\|")

# Inline-code stripper.  Fenced blocks are handled by a line-based scanner
# in ``_strip_fenced_blocks`` (regex-based matching is too fragile against
# prose that mentions backtick sequences, e.g., the spec's own §5-D
# discussion of what a fence *looks like*).  The inline pattern matches a
# backtick pair that does not cross a newline.
_INLINE_CODE = re.compile(r"`[^`\n]+`")

# A fence opener: line whose first non-whitespace run is 3+ backticks.  The
# fence extends to the next line whose first non-whitespace run is 3+
# backticks (of at least the same count as the opener, per CommonMark).
# Anything before the fence closes — or before end of file, if unclosed —
# is treated as fenced content.
_FENCE_OPENER = re.compile(r"^\s*(`{3,})")

# ---------------------------------------------------------------------------
# Finding — a single line of output
# ---------------------------------------------------------------------------


class Finding:
    """A single diagnostic message.

    ``level`` is one of {"ERROR", "WARNING", "INFO"}.  ``check`` is the
    check id (V-* / PIN).  ``target`` identifies the object under check
    (usually a spec_id + item id, or a path).
    """

    __slots__ = ("check", "level", "message", "target")

    def __init__(self, level: str, check: str, target: str, message: str) -> None:
        self.level = level
        self.check = check
        self.target = target
        self.message = message

    def render(self) -> str:
        return f"{self.level} {self.check} {self.target}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "check": self.check,
            "target": self.target,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# git helpers — all fail-closed, none raise on subprocess errors
# ---------------------------------------------------------------------------


def _git(
    repo_root: Path,
    *args: str,
    check: bool = False,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run git and return the completed process.

    Never raises for non-zero exit unless ``check`` is True.  Uses UTF-8 to
    decode output.  When ``capture`` is False, output is inherited.
    """

    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
    )


def _git_ok(repo_root: Path, *args: str) -> bool:
    try:
        proc = _git(repo_root, *args)
    except (FileNotFoundError, OSError):
        return False
    return proc.returncode == 0


def _git_out(repo_root: Path, *args: str) -> str | None:
    """Return stripped stdout on success; None on any failure."""

    try:
        proc = _git(repo_root, *args)
    except (FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


def _load_yaml_frontmatter(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Return (front_matter_dict, error_message).

    A missing ``---`` front-matter block is reported as ``None, None`` —
    the file is not a manifest and callers may skip it silently.
    A parse failure inside a present block returns ``None, message``.
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"cannot read file: {exc}"
    match = _YAML_FRONTMATTER.match(raw)
    if match is None:
        return None, None  # not a manifest — skip
    body = match.group("body")
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        return None, f"YAML parse error in front-matter: {exc}"
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, "front-matter is not a YAML mapping"
    return data, None


def _discover_manifests(repo_root: Path) -> list[Path]:
    design_dir = repo_root / "spec" / "design"
    if not design_dir.is_dir():
        return []
    return sorted(design_dir.glob("*.md"))


# ---------------------------------------------------------------------------
# Pin resolution — spec §3 procedure, fail-closed
# ---------------------------------------------------------------------------


class PinResult:
    """State of pin resolution for one repo/turn."""

    __slots__ = (
        "blob_sha",
        "branch",
        "commit",
        "path",
        "reason",
        "spec_id",
        "state",
        "thread",
    )

    def __init__(self, state: str, reason: str | None = None) -> None:
        self.state = state  # "RESOLVED" or "NO-PIN"
        self.reason: str | None = reason
        self.spec_id: str | None = None
        self.blob_sha: str | None = None
        self.path: str | None = None
        self.thread: str | None = None
        self.branch: str | None = None
        self.commit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "spec_id": self.spec_id,
            "blob_sha": self.blob_sha,
            "path": self.path,
            "thread": self.thread,
            "branch": self.branch,
            "commit": self.commit,
        }


def _resolve_pin(repo_root: Path, no_fetch: bool) -> PinResult:
    """Execute §3 procedure verbatim.  All branches → NO-PIN(<code>) or RESOLVED."""

    pin_path = repo_root / ".mindwire" / "pin"
    # 1 — file absent
    if not pin_path.is_file():
        return PinResult("NO-PIN", "ABSENT")

    # 2 — YAML parse
    try:
        raw = pin_path.read_text(encoding="utf-8")
    except OSError:
        return PinResult("NO-PIN", "PARSE_ERROR")
    try:
        pin: Any = yaml.safe_load(raw)
    except yaml.YAMLError:
        return PinResult("NO-PIN", "PARSE_ERROR")
    if not isinstance(pin, dict):
        return PinResult("NO-PIN", "PARSE_ERROR")

    # 3 — schema_version
    if pin.get("schema_version") != 1:
        return PinResult("NO-PIN", "SCHEMA_VERSION")

    # 3.5 — mode branch (§3-B).  BOOTSTRAP judgement runs AFTER schema_version
    # (D-35): a future v2 bootstrap-shaped pin must not be waved through by a
    # v1 agent.  Three branches:
    #   1. mode == "bootstrap"  → check limited-field constraints, emit
    #      BOOTSTRAP / MISSING_FIELD / PROHIBITED_FIELD
    #   2. mode is None or "resolved" → fall through to step 4 (legacy path)
    #   3. anything else (int, empty string, other string) → MISSING_FIELD.
    #      Justified body-side by the §4-1 "any value other than resolved
    #      or bootstrap is also MISSING_FIELD" sentence.  fail-closed: the
    #      agent does NOT infer the dispatcher's intent.
    mode = pin.get("mode")
    if mode == "bootstrap":
        # Branch 1 — bootstrap form.  Required: pinned_at, pinned_by (both
        # non-empty str, with datetime coercion for pinned_at per YAML).
        pinned_by = pin.get("pinned_by")
        if not isinstance(pinned_by, str) or not pinned_by:
            return PinResult("NO-PIN", "MISSING_FIELD")
        if "pinned_at" not in pin:
            return PinResult("NO-PIN", "MISSING_FIELD")
        pinned_at_bs = pin["pinned_at"]
        if not isinstance(pinned_at_bs, str):
            try:
                pinned_at_bs = pinned_at_bs.isoformat()
            except AttributeError:
                return PinResult("NO-PIN", "MISSING_FIELD")
            if not pinned_at_bs:
                return PinResult("NO-PIN", "MISSING_FIELD")
        # Any of the 7 resolved-form fields present → PROHIBITED_FIELD.
        # `pin.get(field) is not None` treats YAML null as absent (a
        # dispatcher writing ``spec_id: ~`` is not carrying a value).
        for field in _BOOTSTRAP_PROHIBITED_FIELDS:
            if pin.get(field) is not None:
                return PinResult("NO-PIN", "PROHIBITED_FIELD")
        return PinResult("NO-PIN", "BOOTSTRAP")
    elif mode is not None and mode != "resolved":
        # Branch 3 — unknown mode value.  Any non-None value that is not
        # "resolved" or "bootstrap" (including empty string, non-string
        # types like int, or unknown string values like "future_feature")
        # halts as MISSING_FIELD.  No fall-through to the resolved path
        # (would defeat the fail-closed guarantee for future mode values).
        return PinResult("NO-PIN", "MISSING_FIELD")
    # Branch 2 — mode is None or "resolved": continue to step 4.

    # 4 — required fields + type + hex40.  `pinned_at` may be parsed by
    # yaml.safe_load as a datetime (unquoted ISO 8601 timestamps are the
    # canonical YAML form and the spec's own §3 example is unquoted); we
    # accept datetime for that field and coerce.  Every other required field
    # must be a non-empty string.
    required_str = (
        "spec_id",
        "thread",
        "repo",
        "branch",
        "path",
        "blob_sha",
        "commit",
        "pinned_by",
    )
    for key in required_str:
        val = pin.get(key)
        if not isinstance(val, str) or not val:
            return PinResult("NO-PIN", "MISSING_FIELD")
    if "pinned_at" not in pin:
        return PinResult("NO-PIN", "MISSING_FIELD")
    pinned_at = pin["pinned_at"]
    if not isinstance(pinned_at, str):
        try:
            pinned_at = pinned_at.isoformat()  # datetime → str
        except AttributeError:
            return PinResult("NO-PIN", "MISSING_FIELD")
        if not pinned_at:
            return PinResult("NO-PIN", "MISSING_FIELD")
    blob_sha = pin["blob_sha"]
    commit = pin["commit"]
    if not _HEX40.fullmatch(blob_sha) or not _HEX40.fullmatch(commit):
        return PinResult("NO-PIN", "MISSING_FIELD")

    # 5 — detached HEAD
    branch = _git_out(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None or branch == "" or branch == "HEAD":
        return PinResult("NO-PIN", "DETACHED_HEAD")

    # 6 — branch match
    if branch != pin["branch"]:
        return PinResult("NO-PIN", "BRANCH_MISMATCH")

    # 7 — repo name match (basename of origin url, strip .git)
    origin_url = _git_out(repo_root, "remote", "get-url", "origin")
    if origin_url is None:
        return PinResult("NO-PIN", "REPO_MISMATCH")
    repo_name = _origin_basename(origin_url)
    if repo_name != pin["repo"]:
        return PinResult("NO-PIN", "REPO_MISMATCH")

    # 8 — reachability from origin/main (positive side never fetches; negative side
    # tries fetch once, then re-judges)
    def _is_ancestor() -> bool:
        return _git_ok(repo_root, "merge-base", "--is-ancestor", commit, "origin/main")

    have_origin_main = _git_ok(repo_root, "rev-parse", "--verify", "--quiet", "origin/main")
    if have_origin_main and _is_ancestor():
        pass  # proceed
    else:
        if no_fetch:
            return PinResult("NO-PIN", "FETCH_UNAVAILABLE")
        fetch_ok = _git_ok(
            repo_root,
            "fetch",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        if not fetch_ok:
            return PinResult("NO-PIN", "FETCH_UNAVAILABLE")
        if not _is_ancestor():
            return PinResult("NO-PIN", "COMMIT_UNREACHABLE")

    # 9 — blob lookup by commit:path
    blob_at_commit = _git_out(repo_root, "rev-parse", f"{commit}:{pin['path']}")
    if blob_at_commit is None or not _HEX40.fullmatch(blob_at_commit):
        return PinResult("NO-PIN", "BLOB_UNREADABLE")

    # 10 — sha equality
    if blob_at_commit != blob_sha:
        return PinResult("NO-PIN", "SHA_MISMATCH")

    # 11 — cat-file readable → RESOLVED
    try:
        proc = _git(repo_root, "cat-file", "blob", f"{commit}:{pin['path']}")
    except (FileNotFoundError, OSError):
        return PinResult("NO-PIN", "BLOB_UNREADABLE")
    if proc.returncode != 0:
        return PinResult("NO-PIN", "BLOB_UNREADABLE")

    result = PinResult("RESOLVED", None)
    result.spec_id = pin["spec_id"]
    result.blob_sha = blob_sha
    result.path = pin["path"]
    result.thread = pin["thread"]
    result.branch = pin["branch"]
    result.commit = commit
    return result


def _origin_basename(url: str) -> str:
    """Return basename of a git remote URL, stripping trailing .git."""

    url = url.strip().rstrip("/")
    # SSH form:  git@github.com:owner/repo.git
    # HTTPS:    https://github.com/owner/repo.git
    tail = url.rsplit("/", 1)[-1]
    tail = tail.rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return tail


# ---------------------------------------------------------------------------
# Manifest checks — V-1 .. V-13
# ---------------------------------------------------------------------------


class Manifest:
    """Loaded manifest plus derived info used by cross-file checks."""

    __slots__ = ("data", "path", "spec_id")

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data
        raw_id = data.get("spec_id")
        self.spec_id: str = raw_id if isinstance(raw_id, str) else str(path.name)


def _target_label(manifest: Manifest, item_id: str | None = None) -> str:
    parts = [manifest.spec_id]
    if item_id:
        parts.append(item_id)
    return " ".join(parts)


def _check_v1(manifest: Manifest) -> list[Finding]:
    findings: list[Finding] = []
    for key, expected_type in _REQUIRED_MANIFEST_KEYS.items():
        if key not in manifest.data:
            findings.append(
                Finding(
                    "ERROR",
                    "V-1",
                    _target_label(manifest),
                    f"missing required front-matter key `{key}`",
                )
            )
            continue
        val = manifest.data[key]
        if not isinstance(val, expected_type):
            exp = expected_type.__name__ if isinstance(expected_type, type) else str(expected_type)
            findings.append(
                Finding(
                    "ERROR",
                    "V-1",
                    _target_label(manifest),
                    f"front-matter key `{key}` has wrong type "
                    f"(expected {exp}, got {type(val).__name__})",
                )
            )
    # §5-C: optional keys — checked ONLY when present.  Absence is silent;
    # unknown keys (in neither dict) are also silent (forward-compat).
    for key, expected_type in _OPTIONAL_MANIFEST_KEYS.items():
        if key not in manifest.data:
            continue
        val = manifest.data[key]
        if not isinstance(val, expected_type):
            exp = expected_type.__name__ if isinstance(expected_type, type) else str(expected_type)
            findings.append(
                Finding(
                    "ERROR",
                    "V-1",
                    _target_label(manifest),
                    f"optional front-matter key `{key}` has wrong type "
                    f"(expected {exp}, got {type(val).__name__})",
                )
            )
            continue
        # `verify_exempt_ids` must be list[str] (element-level check).  The
        # individual ids are NOT checked against the id-pattern here — a
        # deliberately-permissive entry (e.g., a non-conforming string
        # kept for audit trail) is not V-1's concern.
        if key == "verify_exempt_ids":
            for idx, entry in enumerate(val):
                if not isinstance(entry, str):
                    findings.append(
                        Finding(
                            "ERROR",
                            "V-1",
                            _target_label(manifest),
                            f"verify_exempt_ids[{idx}] is not a string "
                            f"(got {type(entry).__name__})",
                        )
                    )
    return findings


def _check_v2(manifests: list[Manifest]) -> list[Finding]:
    findings: list[Finding] = []
    seen_spec_ids: dict[str, str] = {}
    for m in manifests:
        thread = m.data.get("thread")
        if isinstance(thread, str) and m.path.name != f"{thread}.md":
            findings.append(
                Finding(
                    "ERROR",
                    "V-2",
                    _target_label(m),
                    f"file name `{m.path.name}` does not match `<thread>.md` "
                    f"(thread = `{thread}`, expected `{thread}.md`)",
                )
            )
        spec_id = m.data.get("spec_id")
        if isinstance(spec_id, str):
            prior = seen_spec_ids.get(spec_id)
            if prior:
                findings.append(
                    Finding(
                        "ERROR",
                        "V-2",
                        _target_label(m),
                        f"duplicate spec_id `{spec_id}` (also in `{prior}`)",
                    )
                )
            else:
                seen_spec_ids[spec_id] = m.path.name
    return findings


def _check_v3(manifest: Manifest) -> list[Finding]:
    findings: list[Finding] = []
    status = manifest.data.get("status")
    if isinstance(status, str) and status not in _STATUS_ENUM:
        findings.append(
            Finding(
                "ERROR",
                "V-3",
                _target_label(manifest),
                f"status `{status}` not in {sorted(_STATUS_ENUM)}",
            )
        )
    canary = manifest.data.get("canary")
    if isinstance(canary, str) and canary not in _CANARY_ENUM:
        findings.append(
            Finding(
                "ERROR",
                "V-3",
                _target_label(manifest),
                f"canary `{canary}` not in {sorted(_CANARY_ENUM)}",
            )
        )
    return findings


def _check_v4(manifest: Manifest, all_spec_ids: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    supersedes = manifest.data.get("supersedes")
    if not isinstance(supersedes, list):
        return findings
    for entry in supersedes:
        if not isinstance(entry, str):
            findings.append(
                Finding(
                    "ERROR",
                    "V-4",
                    _target_label(manifest),
                    f"supersedes entry is not a string: {entry!r}",
                )
            )
            continue
        if entry == manifest.spec_id:
            findings.append(
                Finding(
                    "ERROR",
                    "V-4",
                    _target_label(manifest),
                    f"supersedes references self (`{entry}`)",
                )
            )
        elif entry not in all_spec_ids:
            findings.append(
                Finding(
                    "ERROR",
                    "V-4",
                    _target_label(manifest),
                    f"supersedes references unknown spec_id `{entry}`",
                )
            )
    return findings


def _check_v5(manifest: Manifest) -> list[Finding]:
    findings: list[Finding] = []
    items = manifest.data.get("items")
    if not isinstance(items, list):
        return findings
    seen_ids: set[str] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            findings.append(
                Finding(
                    "ERROR",
                    "V-5",
                    _target_label(manifest),
                    f"items[{idx}] is not a mapping",
                )
            )
            continue
        iid = item.get("id")
        if not isinstance(iid, str) or not iid:
            findings.append(
                Finding(
                    "ERROR",
                    "V-5",
                    _target_label(manifest),
                    f"items[{idx}] missing or invalid `id`",
                )
            )
            continue
        if iid in seen_ids:
            findings.append(
                Finding(
                    "ERROR",
                    "V-5",
                    _target_label(manifest, iid),
                    "duplicate item id within manifest",
                )
            )
        seen_ids.add(iid)
        if not isinstance(item.get("title"), str) or not item["title"]:
            findings.append(
                Finding(
                    "ERROR",
                    "V-5",
                    _target_label(manifest, iid),
                    "missing or invalid `title`",
                )
            )
        paths = item.get("paths")
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(p, str) and p for p in paths)
        ):
            findings.append(
                Finding(
                    "ERROR",
                    "V-5",
                    _target_label(manifest, iid),
                    "missing or invalid `paths`",
                )
            )
        if "status" in item:
            istatus = item["status"]
            if istatus not in _STATUS_ENUM:
                findings.append(
                    Finding(
                        "ERROR",
                        "V-5",
                        _target_label(manifest, iid),
                        f"item status `{istatus}` not in {sorted(_STATUS_ENUM)}",
                    )
                )
            elif istatus == "withdrawn":
                reason = item.get("withdrawn_reason")
                if not isinstance(reason, str) or not reason.strip():
                    findings.append(
                        Finding(
                            "ERROR",
                            "V-5",
                            _target_label(manifest, iid),
                            "withdrawn item requires non-empty `withdrawn_reason`",
                        )
                    )
    return findings


def _resolved_item_field(manifest: Manifest, item: dict[str, Any], field: str) -> Any:
    """§6 resolution: return the item value if present, else the root value.

    For fields in _OVERRIDABLE_ITEM_FIELDS (``base_branch`` / ``canary``) the
    item value is a legal override.  For ``target_repo`` the item value is
    illegal — V-12 will independently error on it — but we still return it
    here so V-6 can report "resolved" from the item's own (illegal) value
    without double-reporting a resolution failure on top of the V-12 error.
    For fields in _ROOT_ONLY_ITEM_FIELDS that are not also inherited (``spec_id``
    / ``thread`` / ``supersedes``) this function is not called by V-6 at all;
    those fields have no item-level notion of resolution.
    """

    if field in item:
        return item[field]
    return manifest.data.get(field)


def _check_v6(manifest: Manifest) -> list[Finding]:
    findings: list[Finding] = []
    items = manifest.data.get("items")
    if not isinstance(items, list):
        return findings
    for item in items:
        if not isinstance(item, dict):
            continue
        iid = item.get("id") if isinstance(item.get("id"), str) else "<no-id>"
        for field in _INHERITED_ITEM_FIELDS:
            resolved = _resolved_item_field(manifest, item, field)
            if resolved is None:
                findings.append(
                    Finding(
                        "ERROR",
                        "V-6",
                        _target_label(manifest, iid),
                        f"cannot resolve `{field}` (missing at manifest root and item)",
                    )
                )
    return findings


def _check_v7_v8(
    manifest: Manifest, known_obligation_ids: set[str]
) -> tuple[list[Finding], list[dict[str, Any]]]:
    findings: list[Finding] = []
    resolved: list[dict[str, Any]] = []
    items = manifest.data.get("items")
    if not isinstance(items, list):
        return findings, resolved
    root_obl = manifest.data.get("obligations") or []
    root_obl_list = [x for x in root_obl if isinstance(x, str)]
    for item in items:
        if not isinstance(item, dict):
            continue
        iid = item.get("id") if isinstance(item.get("id"), str) else "<no-id>"
        item_obl = item.get("obligations") or []
        item_obl_list = [x for x in item_obl if isinstance(x, str)]
        # union with stable order: root first, then item extras (item is "add-only")
        seen: set[str] = set()
        union: list[str] = []
        for oid in root_obl_list + item_obl_list:
            if oid not in seen:
                seen.add(oid)
                union.append(oid)
        resolved.append({"id": iid, "obligations": union})
        # V-7 info
        findings.append(
            Finding(
                "INFO",
                "V-7",
                _target_label(manifest, iid),
                "obligations (root + item union) = " + (", ".join(union) if union else "(empty)"),
            )
        )
        # V-8 error for any id not defined on-disk
        for oid in union:
            if oid not in known_obligation_ids:
                findings.append(
                    Finding(
                        "ERROR",
                        "V-8",
                        _target_label(manifest, iid),
                        f"unknown obligation `{oid}`",
                    )
                )
    return findings, resolved


def _check_v10(manifests: list[Manifest], current_branch: str | None) -> list[Finding]:
    """warning-only. only fires when current branch is `main`."""

    findings: list[Finding] = []
    if current_branch != "main":
        return findings
    superseded_ids: set[str] = set()
    for m in manifests:
        supersedes = m.data.get("supersedes")
        if isinstance(supersedes, list):
            for entry in supersedes:
                if isinstance(entry, str):
                    superseded_ids.add(entry)
    for m in manifests:
        status = m.data.get("status")
        if status == "withdrawn":
            findings.append(
                Finding(
                    "WARNING",
                    "V-10",
                    _target_label(m),
                    "manifest is withdrawn — must not be the target of a new pin",
                )
            )
        if m.spec_id in superseded_ids:
            findings.append(
                Finding(
                    "WARNING",
                    "V-10",
                    _target_label(m),
                    "manifest is superseded (referenced by another `supersedes`) — "
                    "must not be the target of a new pin",
                )
            )
    return findings


def _check_v11(manifest: Manifest, origin_basename: str | None) -> list[Finding]:
    findings: list[Finding] = []
    target_repo = manifest.data.get("target_repo")
    if not isinstance(target_repo, str):
        return findings  # V-1 already errored
    if origin_basename is None:
        # No `origin` remote — diagnostic tools do not fail on the environment.
        findings.append(
            Finding(
                "INFO",
                "V-11",
                _target_label(manifest),
                f"no `origin` remote — skipping target_repo check (target_repo = `{target_repo}`)",
            )
        )
        return findings
    if target_repo != origin_basename:
        findings.append(
            Finding(
                "ERROR",
                "V-11",
                _target_label(manifest),
                f"target_repo `{target_repo}` does not match origin basename `{origin_basename}`",
            )
        )
    return findings


def _check_v12(manifest: Manifest) -> list[Finding]:
    findings: list[Finding] = []
    items = manifest.data.get("items")
    if not isinstance(items, list):
        return findings
    for item in items:
        if not isinstance(item, dict):
            continue
        iid = item.get("id") if isinstance(item.get("id"), str) else "<no-id>"
        for field in _ROOT_ONLY_ITEM_FIELDS:
            if field in item:
                findings.append(
                    Finding(
                        "ERROR",
                        "V-12",
                        _target_label(manifest, iid),
                        f"field `{field}` may not appear on an item (root-only)",
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# V-14 helpers — internal id reference audit (SPEC-2026-09-20 §5-D)
# ---------------------------------------------------------------------------


def _read_manifest_body(path: Path) -> str:
    """Return the body of a manifest (text after the closing ``---``).

    Returns the empty string if the file cannot be read or has no
    front-matter.  Chain-walk callers report the missing-file case
    separately with a V-14 error.
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _YAML_FRONTMATTER.match(raw)
    if match is None:
        return raw
    return raw[match.end() :]


def _strip_fenced_blocks(body: str) -> str:
    """Remove fenced code blocks by scanning line by line.

    A regex-based match is too fragile: the spec's own §5-D discusses what
    a fence *looks like* using inline prose that carries backtick runs, and
    a naive multi-line regex reads that prose as an opening fence with no
    closer — swallowing the rest of the file.  The scanner requires a
    fence opener to be the leading token of its line and requires the
    closer to carry at least as many backticks as the opener.
    """

    lines = body.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _FENCE_OPENER.match(line)
        if m is None:
            out.append(line)
            i += 1
            continue
        opener_len = len(m.group(1))
        # Scan forward until a line whose leading backtick run is >= opener_len.
        i += 1
        while i < len(lines):
            close = _FENCE_OPENER.match(lines[i])
            i += 1
            if close is not None and len(close.group(1)) >= opener_len:
                break
        # else: unterminated fence — treat rest as fenced (drop it).
    return "".join(out)


def _strip_code(body: str) -> str:
    """Strip fenced code blocks and inline code from a manifest body.

    Per §5-D: reference extraction excludes ``` fences and single-backtick
    inline code.  Stripping fences first avoids interpreting backticks
    that live inside a fenced block as inline code.
    """

    body = _strip_fenced_blocks(body)
    body = _INLINE_CODE.sub("", body)
    return body


def _extract_references(body: str) -> set[str]:
    """Extract bounded internal-id references from a body (post-strip)."""

    stripped = _strip_code(body)
    return set(_ID_REFERENCE.findall(stripped))


def _extract_definitions(body: str, items: list[Any]) -> set[str]:
    """Extract definition-side ids from a body plus front-matter items.

    Bold form takes the FIRST match per line only (§5-D 定義子抽出).
    Table form matches any row whose leading cell is a bounded id.
    Front-matter ``items[].id`` values are trivially definitional (I-N).
    """

    defs: set[str] = set()
    for item in items or []:
        if isinstance(item, dict):
            iid = item.get("id")
            if isinstance(iid, str) and _ID_REFERENCE.fullmatch(iid):
                defs.add(iid)
    for raw_line in body.splitlines():
        m_bold = _DEF_BOLD.search(raw_line)
        if m_bold:
            defs.add(m_bold.group(1))
        m_table = _DEF_TABLE.match(raw_line)
        if m_table:
            defs.add(m_table.group(1))
    return defs


def _resolve_supersedes_chain(
    manifest: Manifest,
    manifests_by_spec_id: dict[str, Manifest],
) -> tuple[set[str], set[str], list[Finding]]:
    """Walk the ``supersedes`` chain and return (defs_union, exempts_union, errors).

    Each ancestor contributes its own definitions and ``verify_exempt_ids``
    to the current manifest's exemption / definition sets.  Cycles are
    prevented by tracking visited spec_ids; an ancestor file that cannot
    be located in ``manifests_by_spec_id`` produces a V-14 error (per
    D-37) but does not stop the walk.
    """

    findings: list[Finding] = []
    defs: set[str] = set()
    exempts: set[str] = set()
    visited: set[str] = set()

    def _walk(m: Manifest) -> None:
        if m.spec_id in visited:
            return
        visited.add(m.spec_id)
        body = _read_manifest_body(m.path)
        items = m.data.get("items") if isinstance(m.data.get("items"), list) else []
        defs.update(_extract_definitions(body, items))
        raw_exempts = m.data.get("verify_exempt_ids") or []
        if isinstance(raw_exempts, list):
            for entry in raw_exempts:
                if isinstance(entry, str):
                    exempts.add(entry)
        supersedes = m.data.get("supersedes") or []
        if not isinstance(supersedes, list):
            return
        for parent_id in supersedes:
            if not isinstance(parent_id, str):
                continue
            parent = manifests_by_spec_id.get(parent_id)
            if parent is None:
                findings.append(
                    Finding(
                        "ERROR",
                        "V-14",
                        _target_label(m),
                        f"supersedes ancestor `{parent_id}` file not found "
                        "(cannot resolve chain-walked definitions)",
                    )
                )
                continue
            _walk(parent)

    _walk(manifest)
    return defs, exempts, findings


def _check_v14(
    manifest: Manifest,
    manifests_by_spec_id: dict[str, Manifest],
) -> list[Finding]:
    """V-14 — internal id references must resolve to a definition or exemption.

    Errors flow into the ``errors`` array but do NOT affect the exit code
    (D-36 / §5-E): verify.py stays a diagnostic even after V-14 lands.
    """

    findings: list[Finding] = []
    body = _read_manifest_body(manifest.path)
    references = _extract_references(body)
    defs, exempts, walk_findings = _resolve_supersedes_chain(manifest, manifests_by_spec_id)
    findings.extend(walk_findings)
    unresolved = sorted(references - defs - exempts)
    for ref in unresolved:
        findings.append(
            Finding(
                "ERROR",
                "V-14",
                _target_label(manifest),
                f"reference `{ref}` has no definition in this manifest, "
                "no chain-walked definition in a `supersedes` ancestor, "
                "and no `verify_exempt_ids` entry",
            )
        )
    return findings


def _check_v13(
    obligations_by_id: dict[str, dict[str, Any]],
) -> list[Finding]:
    """If OBL-SPEC-PIN is present, every §3 reason code must appear verbatim."""

    findings: list[Finding] = []
    entry = obligations_by_id.get("OBL-SPEC-PIN")
    if entry is None:
        return findings  # V-8 catches this per-spec; do not double-report.
    body = entry.get("body")
    if not isinstance(body, str):
        findings.append(
            Finding(
                "ERROR",
                "V-13",
                "OBL-SPEC-PIN",
                "obligation body is missing or not a string",
            )
        )
        return findings
    for code in PIN_REASON_CODES:
        if code not in body:
            findings.append(
                Finding(
                    "ERROR",
                    "V-13",
                    "OBL-SPEC-PIN",
                    f"reason code `{code}` not present verbatim in body",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Obligations loader
# ---------------------------------------------------------------------------


def _load_obligations(repo_root: Path) -> tuple[dict[str, dict[str, Any]], list[Finding]]:
    findings: list[Finding] = []
    path = repo_root / "spec" / "process" / "obligations.yaml"
    if not path.is_file():
        findings.append(
            Finding(
                "ERROR",
                "V-8",
                path.as_posix(),
                "obligations manifest not found",
            )
        )
        return {}, findings
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        findings.append(Finding("ERROR", "V-8", path.as_posix(), f"cannot read: {exc}"))
        return {}, findings
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        findings.append(Finding("ERROR", "V-8", path.as_posix(), f"YAML parse error: {exc}"))
        return {}, findings
    if not isinstance(data, dict):
        findings.append(Finding("ERROR", "V-8", path.as_posix(), "top-level is not a mapping"))
        return {}, findings
    entries = data.get("obligations") or []
    by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(entries, list):
        findings.append(
            Finding(
                "ERROR",
                "V-8",
                path.as_posix(),
                "`obligations` is not a list",
            )
        )
        return {}, findings
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        oid = entry.get("id")
        if isinstance(oid, str) and oid:
            by_id[oid] = entry
    return by_id, findings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _run(
    repo_root: Path,
    json_output: bool,
    pin_only: bool,
    no_fetch: bool,
) -> int:
    all_findings: list[Finding] = []
    resolved_items: list[dict[str, Any]] = []

    pin = _resolve_pin(repo_root, no_fetch=no_fetch)
    if pin.state == "RESOLVED":
        all_findings.append(
            Finding(
                "INFO",
                "V-9",
                "pin",
                f"RESOLVED spec_id={pin.spec_id} blob_sha={pin.blob_sha} path={pin.path}",
            )
        )
    else:
        all_findings.append(
            Finding(
                "INFO",
                "V-9",
                "pin",
                f"NO-PIN({pin.reason})",
            )
        )

    if pin_only:
        return _emit(all_findings, resolved_items, pin, json_output)

    # obligations
    obligations_by_id, obl_findings = _load_obligations(repo_root)
    all_findings.extend(obl_findings)
    known_obl_ids = set(obligations_by_id.keys())

    # V-13 — cross-file check on OBL-SPEC-PIN body
    all_findings.extend(_check_v13(obligations_by_id))

    # origin basename for V-11
    origin_url = _git_out(repo_root, "remote", "get-url", "origin")
    origin_basename = _origin_basename(origin_url) if origin_url else None

    # current branch — for V-10 gating
    current_branch = _git_out(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if current_branch == "HEAD":
        current_branch = None  # detached HEAD — V-10 not applicable

    # manifests
    manifest_paths = _discover_manifests(repo_root)
    manifests: list[Manifest] = []
    for path in manifest_paths:
        data, err = _load_yaml_frontmatter(path)
        if data is None and err is None:
            # No front-matter — not a manifest we own; skip silently.
            continue
        if err is not None:
            all_findings.append(
                Finding(
                    "ERROR",
                    "V-1",
                    path.relative_to(repo_root).as_posix(),
                    err,
                )
            )
            continue
        assert data is not None
        manifests.append(Manifest(path, data))

    # V-1, V-3, V-5, V-6, V-11, V-12 — per-manifest
    for m in manifests:
        all_findings.extend(_check_v1(m))
        all_findings.extend(_check_v3(m))
        all_findings.extend(_check_v5(m))
        all_findings.extend(_check_v6(m))
        all_findings.extend(_check_v11(m, origin_basename))
        all_findings.extend(_check_v12(m))

    # V-2 — cross-manifest uniqueness + filename match
    all_findings.extend(_check_v2(manifests))

    # V-4 — supersedes references
    all_spec_ids = {m.spec_id for m in manifests}
    for m in manifests:
        all_findings.extend(_check_v4(m, all_spec_ids))

    # V-7 (info) + V-8 (error) — resolved obligation union per item
    for m in manifests:
        item_findings, item_resolved = _check_v7_v8(m, known_obl_ids)
        all_findings.extend(item_findings)
        for entry in item_resolved:
            resolved_items.append(
                {"spec_id": m.spec_id, "id": entry["id"], "obligations": entry["obligations"]}
            )

    # V-10 — warnings for withdrawn / superseded on `main`
    all_findings.extend(_check_v10(manifests, current_branch))

    # V-14 — internal id reference audit (SPEC-2026-09-20 §5-D).  Errors
    # here flow into `errors` but do NOT affect the exit code (D-36 /
    # §5-E): verify.py stays diagnostic.
    manifests_by_spec_id: dict[str, Manifest] = {}
    for m in manifests:
        if isinstance(m.spec_id, str) and m.spec_id and m.spec_id not in manifests_by_spec_id:
            manifests_by_spec_id[m.spec_id] = m
    for m in manifests:
        all_findings.extend(_check_v14(m, manifests_by_spec_id))

    return _emit(all_findings, resolved_items, pin, json_output)


def _emit(
    findings: list[Finding],
    resolved_items: list[dict[str, Any]],
    pin: PinResult,
    json_output: bool,
) -> int:
    errors = [f for f in findings if f.level == "ERROR"]
    warnings = [f for f in findings if f.level == "WARNING"]
    infos = [f for f in findings if f.level == "INFO"]

    if json_output:
        payload = {
            "errors": [f.to_dict() for f in errors],
            "warnings": [f.to_dict() for f in warnings],
            "info": [f.to_dict() for f in infos],
            "items": resolved_items,
            "pin": pin.to_dict(),
        }
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        for f in findings:
            print(f.render())

    # D-36 / §5-E: verify.py is a diagnostic, not a gate.  Check-driven
    # findings (V-1..V-14 errors / warnings) do NOT affect the exit code —
    # only usage / environment failures (handled in ``main``) do.  A-12
    # keeps ``main`` at error-0 / warning-0 by discipline, not by gate.
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnostic for the spec-delivery mechanism "
        "(SPEC-2026-08-11-design-spec-delivery §5)."
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--pin-only", action="store_true")
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip the single fetch that §3 step 8 allows; the negative side of "
        "reachability becomes NO-PIN(FETCH_UNAVAILABLE).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    repo_root: Path
    if args.repo_root is not None:
        repo_root = args.repo_root.resolve()
        if not repo_root.is_dir():
            print(f"ERROR usage: --repo-root path is not a directory: {repo_root}", file=sys.stderr)
            return 2
    else:
        top = _git_out(Path.cwd(), "rev-parse", "--show-toplevel")
        if not top:
            print("ERROR usage: not inside a git repository", file=sys.stderr)
            return 2
        repo_root = Path(top).resolve()
    return _run(
        repo_root=repo_root,
        json_output=args.json_output,
        pin_only=args.pin_only,
        no_fetch=args.no_fetch,
    )


if __name__ == "__main__":
    raise SystemExit(main())
