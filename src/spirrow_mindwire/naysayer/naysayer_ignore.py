"""``.naysayer-ignore`` — data files the PR gate names instead of reading in full.

Thread T-diff-cap-has-no-escape-for-indivisible-data-artifacts: Takahito ruled (msg-8448) that
data whose correctness a machine check already guarantees (a sha pin in a test, a manifest) is not
something the gate should read, and that the dismiss + human-APPROVE workaround (msg-4423) is not
made a rule. The final design is Bohr msg-8470, approved by Einstein msg-8471. In short:

1. One file, ``.naysayer-ignore`` at the repository root, read from the PR **head**. A missing
   file (404) and an unreadable one both mean "no exclusion" — the gate then reads the full diff,
   so a failure only ever makes the review larger. Rules the base side does not have are named
   ``NEW RULE (this PR)`` to the model and in the review body.
2. Line form ``<glob>  # <guarantor>``. ``*`` stays inside one path segment, ``**`` crosses
   segments, ``!`` (negation) is not supported, and an empty guarantor makes the line invalid
   (``unchecked`` is how a repo says "nothing checks this, record only"). Invalid lines are not
   applied and are named with their line number and reason.
3. Never excluded, whatever a rule says: ``.naysayer-ignore`` itself, ``.gitattributes``,
   ``.github/**`` and every :data:`NEVER_IGNORABLE_SUFFIXES` file. ``.json`` stays excludable.
4. :func:`split_diff_by_file` cuts the diff at ``diff --git`` lines. A section is excluded only
   when EVERY path it names (both sides of a rename) matches a rule and none is in 3. Whether a
   section is excluded depends on the rules and 3 alone — never on a network read — so the gate
   and ``mindwire pr-diff-size`` always measure the same ``original_chars``.
5. Each excluded section is replaced IN the diff by a short, deterministic pointer line (path,
   rule line, diff_chars). The full summary line — bytes, sha256, blob, guarantor — is written by
   the driver into a separate list (prompt and review body). Keeping the hash out of the counted
   text is what keeps 4's equality true when a blob read fails.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable

IGNORE_FILE = ".naysayer-ignore"

# The blacklist (msg-8466 R2): a code or configuration file is reviewed in full even when a rule
# matches it. A blacklist, not an allow-list, so a downstream repo can exclude a data format
# mindwire has never heard of (``*.mp4``) without a mindwire change (msg-8464 Obj-2).
NEVER_IGNORABLE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".ps1",
        ".psm1",
        ".sh",
        ".bash",
        ".js",
        ".mjs",
        ".ts",
        ".tsx",
        ".rs",
        ".go",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".java",
        ".kt",
        ".lua",
        ".rb",
        ".toml",
        ".yml",
        ".yaml",
        ".cfg",
        ".ini",
    }
)
_NEVER_IGNORABLE_NAMES: frozenset[str] = frozenset({IGNORE_FILE, ".gitattributes"})
_NEVER_IGNORABLE_PREFIX = ".github/"

UNCHECKED = "unchecked"


def never_ignorable_reason(path: str) -> str | None:
    """Why ``path`` can never be excluded, or ``None`` when a rule may exclude it."""
    name = path.rsplit("/", 1)[-1]
    if name in _NEVER_IGNORABLE_NAMES:
        return f"`{name}` is never excluded"
    if path.startswith(_NEVER_IGNORABLE_PREFIX):
        return "`.github/**` is never excluded"
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in NEVER_IGNORABLE_SUFFIXES:
        return f"`{suffix}` is an extension the gate never excludes"
    return None


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Whole-path glob: ``*`` / ``?`` stay inside one segment, ``**`` crosses segments."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out), re.DOTALL)


@dataclass(frozen=True)
class IgnoreRule:
    line_no: int
    glob: str
    guarantor: str
    pattern: re.Pattern[str] = field(compare=False, repr=False)

    def matches(self, path: str) -> bool:
        return self.pattern.fullmatch(path) is not None


@dataclass(frozen=True)
class InvalidLine:
    line_no: int
    text: str
    reason: str


@dataclass(frozen=True)
class IgnoreRules:
    rules: tuple[IgnoreRule, ...] = ()
    invalid: tuple[InvalidLine, ...] = ()

    def match(self, path: str) -> IgnoreRule | None:
        for rule in self.rules:
            if rule.matches(path):
                return rule
        return None


NO_RULES = IgnoreRules()


def parse_ignore_file(text: str) -> IgnoreRules:
    """Parse ``.naysayer-ignore``. Blank lines and lines starting with ``#`` are comments."""
    rules: list[IgnoreRule] = []
    invalid: list[InvalidLine] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        glob, sep, guarantor = line.partition("#")
        glob, guarantor = glob.strip(), guarantor.strip()
        reason = ""
        if glob.startswith("!"):
            reason = "negation (`!`) is not supported"
        elif not sep:
            reason = f"no guarantor — write `<glob>  # <guarantor>` (or `# {UNCHECKED}`)"
        elif not guarantor:
            reason = f"empty guarantor — write `# {UNCHECKED}` when nothing checks this data"
        elif not glob:
            reason = "empty glob"
        if reason:
            invalid.append(InvalidLine(line_no=line_no, text=line, reason=reason))
            continue
        glob = glob.lstrip("/")
        rules.append(
            IgnoreRule(line_no=line_no, glob=glob, guarantor=guarantor, pattern=glob_to_regex(glob))
        )
    return IgnoreRules(rules=tuple(rules), invalid=tuple(invalid))


def new_rules(head: IgnoreRules, base_text: str | None) -> tuple[IgnoreRule, ...]:
    """Head rules the base side does not carry. ``base_text=None`` (unread) → every rule."""
    if base_text is None:
        return head.rules
    base = {(r.glob, r.guarantor) for r in parse_ignore_file(base_text).rules}
    return tuple(r for r in head.rules if (r.glob, r.guarantor) not in base)


# ---- splitting the diff -----------------------------------------------------------------------

_DIFF_HEADER = "diff --git "


@dataclass(frozen=True)
class DiffSection:
    """One ``diff --git`` section, or the preamble before the first one (``paths == ()``).

    ``paths`` is empty when the header could not be read (a quoted path, say): such a section is
    never excluded, which only makes the review larger.
    """

    text: str
    paths: tuple[str, ...]
    head_path: str
    deleted: bool


def _strip_side(value: str, prefix: str) -> str | None:
    value = value.rstrip("\r\n")
    if value == "/dev/null":
        return None
    if value.startswith(prefix):
        return value[len(prefix) :]
    return None


def _parse_section(text: str) -> DiffSection:
    lines = text.splitlines()
    header = lines[0][len(_DIFF_HEADER) :] if lines else ""
    a_side: str | None = None
    b_side: str | None = None
    renamed_from: str | None = None
    renamed_to: str | None = None
    deleted = False
    if not header.startswith('"') and header.startswith("a/") and (len(header) - 5) % 2 == 0:
        half = (len(header) - 5) // 2
        cand = header[2 : 2 + half]
        if header == f"a/{cand} b/{cand}":
            a_side = b_side = cand
    for line in lines[1:]:
        if line.startswith("@@") or line.startswith("Binary files") or line == "GIT binary patch":
            break
        if line.startswith("deleted file mode"):
            deleted = True
        elif line.startswith(("rename from ", "copy from ")):
            renamed_from = line.split(" ", 2)[2]
        elif line.startswith(("rename to ", "copy to ")):
            renamed_to = line.split(" ", 2)[2]
        elif line.startswith("--- "):
            side = _strip_side(line[4:], "a/")
            a_side = side if side is not None else a_side
        elif line.startswith("+++ "):
            side = _strip_side(line[4:], "b/")
            b_side = side if side is not None else b_side
    if renamed_from is not None and renamed_to is not None:
        a_side, b_side = renamed_from, renamed_to
    paths = tuple(dict.fromkeys(p for p in (a_side, b_side) if p))
    if any(p.startswith('"') for p in paths):
        paths = ()
    head_path = (a_side if deleted else b_side) or a_side or b_side or ""
    return DiffSection(text=text, paths=paths, head_path=head_path, deleted=deleted)


def split_diff_by_file(diff: str) -> list[DiffSection]:
    """Cut ``diff`` at each column-0 ``diff --git`` line; ``"".join`` of the texts is ``diff``."""
    sections: list[str] = []
    current: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith(_DIFF_HEADER) and current:
            sections.append("".join(current))
            current = []
        current.append(line)
    if current:
        sections.append("".join(current))
    out: list[DiffSection] = []
    for text in sections:
        if text.startswith(_DIFF_HEADER):
            out.append(_parse_section(text))
        else:
            out.append(DiffSection(text=text, paths=(), head_path="", deleted=False))
    return out


# ---- applying the rules -------------------------------------------------------------------------


@dataclass(frozen=True)
class ExcludedFile:
    path: str
    line_no: int
    guarantor: str
    diff_chars: int
    deleted: bool
    # Filled by the gate only (``pr-diff-size`` never hashes). ``None`` = not read.
    size: int | None = None
    sha256: str | None = None
    blob: str | None = None

    @property
    def hash_unavailable(self) -> bool:
        return not self.deleted and self.sha256 is None

    def pointer_line(self) -> str:
        """The in-diff stand-in. Deterministic: it counts toward ``original_chars``."""
        return (
            f"[excluded by {IGNORE_FILE}:L{self.line_no}] {self.path}  "
            f"diff_chars={self.diff_chars}\n"
        )

    def summary_line(self) -> str:
        """The full summary line of msg-8470 §5."""
        if self.deleted:
            size, digest = "0", "deleted"
        elif self.sha256 is None:
            size, digest = "unknown", "fetch_failed"
        else:
            size, digest = str(self.size), self.sha256
        return (
            f"[excluded by {IGNORE_FILE}:L{self.line_no}] {self.path}  bytes={size}  "
            f"sha256={digest}  blob={self.blob or 'unavailable'}  "
            f"diff_chars={self.diff_chars}  guarantor={self.guarantor}"
        )


@dataclass(frozen=True)
class RefusedMatch:
    """A rule matched a path in a section the gate still sends in full."""

    path: str
    line_no: int
    reason: str


@dataclass(frozen=True)
class KeptSpan:
    """Where a section that was sent in full sits in the post-exclusion diff."""

    path: str
    start: int
    end: int


@dataclass(frozen=True)
class Exclusion:
    text: str
    pre_exclusion_chars: int
    excluded: tuple[ExcludedFile, ...] = ()
    refused: tuple[RefusedMatch, ...] = ()
    kept: tuple[KeptSpan, ...] = ()

    @property
    def excluded_chars(self) -> int:
        return self.pre_exclusion_chars - len(self.text)


def apply_ignore(diff: str, rules: IgnoreRules) -> Exclusion:
    """Replace each excludable section of ``diff`` by its pointer line (module docstring §4)."""
    parts: list[str] = []
    excluded: list[ExcludedFile] = []
    refused: list[RefusedMatch] = []
    kept: list[KeptSpan] = []
    offset = 0
    for section in split_diff_by_file(diff):
        hit = _excluded(section, rules, refused) if rules.rules else None
        if hit is not None:
            excluded.append(hit)
            piece = hit.pointer_line()
        else:
            piece = section.text
            if section.paths:
                kept.append(KeptSpan(section.head_path, offset, offset + len(piece)))
        parts.append(piece)
        offset += len(piece)
    return Exclusion(
        text="".join(parts),
        pre_exclusion_chars=len(diff),
        excluded=tuple(excluded),
        refused=tuple(refused),
        kept=tuple(kept),
    )


def _excluded(
    section: DiffSection, rules: IgnoreRules, refused: list[RefusedMatch]
) -> ExcludedFile | None:
    if not section.paths:
        return None
    matched = {p: rules.match(p) for p in section.paths}
    hits = {p: r for p, r in matched.items() if r is not None}
    if not hits:
        return None
    for path, rule in hits.items():
        reason = never_ignorable_reason(path)
        if reason is not None:
            refused.append(RefusedMatch(path=path, line_no=rule.line_no, reason=reason))
            return None
    if len(hits) != len(matched):
        path, rule = next(iter(hits.items()))
        refused.append(
            RefusedMatch(
                path=path,
                line_no=rule.line_no,
                reason="a rename or copy whose other side no rule matches is sent in full",
            )
        )
        return None
    rule = hits.get(section.head_path) or next(iter(hits.values()))
    return ExcludedFile(
        path=section.head_path,
        line_no=rule.line_no,
        guarantor=rule.guarantor,
        diff_chars=len(section.text),
        deleted=section.deleted,
    )


def files_beyond(kept: tuple[KeptSpan, ...], limit: int) -> tuple[list[str], list[str]]:
    """``(never_sent, partial)`` path lists for a diff cut at ``limit`` chars (msg-4152 §4)."""
    never_sent = [s.path for s in kept if s.start >= limit]
    partial = [s.path for s in kept if s.start < limit < s.end]
    return never_sent, partial


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- the driver-written report ------------------------------------------------------------------


@dataclass(frozen=True)
class IgnoreReport:
    excluded: tuple[ExcludedFile, ...] = ()
    refused: tuple[RefusedMatch, ...] = ()
    invalid: tuple[InvalidLine, ...] = ()
    new: tuple[IgnoreRule, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.excluded or self.refused or self.invalid or self.new)

    def render(self) -> str:
        """Markdown section written by the driver, not the model. ``""`` when there is nothing."""
        if self.empty:
            return ""
        lines = [
            f"**`{IGNORE_FILE}` report** (written by the gate driver, not by the model)",
        ]
        if self.excluded:
            lines += ["", "Excluded from this review (not sent in full):"]
            lines += [f"- `{f.summary_line()}`" for f in self.excluded]
        for rule in self.new:
            count = sum(1 for f in self.excluded if f.line_no == rule.line_no)
            lines += [
                "",
                f"NEW RULE (this PR): L{rule.line_no} {rule.glob} → {count} files excluded",
            ]
        unavailable = [f.path for f in self.excluded if f.hash_unavailable]
        if unavailable:
            lines.append("")
            lines += [f"HASH UNAVAILABLE: {p}" for p in unavailable]
        if self.refused:
            lines += ["", "Matched a rule but sent in full:"]
            lines += [f"- L{r.line_no} `{r.path}`: {r.reason}" for r in self.refused]
        if self.invalid:
            lines += ["", f"Invalid `{IGNORE_FILE}` lines (not applied):"]
            lines += [f"- L{i.line_no} `{i.text}`: {i.reason}" for i in self.invalid]
        return "\n".join(lines)


# ---- reading over the network -------------------------------------------------------------------
#
# Kept apart from ``GitHubReviewClient`` on purpose: a client that does not implement these (the
# test fakes of every older suite) simply has no ignore file, which is exactly the pre-change
# behaviour. ``GitHubClient`` implements all three.

logger = logging.getLogger(__name__)


@runtime_checkable
class IgnoreFileSource(Protocol):
    async def fetch_file_bytes_at(
        self, owner: str, repo: str, *, path: str, ref: str
    ) -> bytes | None: ...


@runtime_checkable
class BlobSource(Protocol):
    async def fetch_compare_blob_shas(
        self, owner: str, repo: str, base_ref: str, head_sha: str
    ) -> dict[str, str]: ...

    async def fetch_blob_bytes(self, owner: str, repo: str, sha: str) -> bytes: ...


async def read_ignore_text(client: object, owner: str, repo: str, ref: str) -> str | None:
    """``.naysayer-ignore`` at ``ref``: its text, ``""`` when absent, ``None`` when unread.

    A client without :class:`IgnoreFileSource` reads as "absent". Every failure is logged and
    returns ``None``; the caller decides what unread means (head: no exclusion; base: every rule
    is NEW).
    """
    if not isinstance(client, IgnoreFileSource):
        return ""
    try:
        data = await client.fetch_file_bytes_at(owner, repo, path=IGNORE_FILE, ref=ref)
    except Exception as exc:  # fail-open toward a LARGER review
        logger.warning("%s: cannot read %s at %s (%s)", IGNORE_FILE, owner + "/" + repo, ref, exc)
        return None
    if data is None:
        return ""
    return data.decode("utf-8", errors="replace")


async def read_head_rules(client: object, owner: str, repo: str, head_ref: str) -> IgnoreRules:
    """The head-side rules; an absent or unread file is :data:`NO_RULES` (full review)."""
    text = await read_ignore_text(client, owner, repo, head_ref)
    return parse_ignore_file(text) if text else NO_RULES


async def hash_excluded(
    client: object,
    owner: str,
    repo: str,
    base_ref: str,
    head_sha: str,
    excluded: tuple[ExcludedFile, ...],
) -> tuple[ExcludedFile, ...]:
    """Fill ``size`` / ``sha256`` / ``blob`` for the summary lines (msg-8468 R3).

    A failed read leaves ``sha256`` unset (``fetch_failed`` in the line, ``HASH UNAVAILABLE`` in
    the report) and NEVER undoes the exclusion: what is excluded is decided before this runs.
    """
    if not excluded:
        return excluded
    blobs: dict[str, str] = {}
    if isinstance(client, BlobSource):
        try:
            blobs = await client.fetch_compare_blob_shas(owner, repo, base_ref, head_sha)
        except Exception as exc:
            logger.warning("%s: compare file list unavailable (%s)", IGNORE_FILE, exc)
    out: list[ExcludedFile] = []
    for item in excluded:
        blob = blobs.get(item.path)
        if item.deleted:
            out.append(replace(item, size=0, blob=blob))
            continue
        if blob is None or not isinstance(client, BlobSource):
            logger.warning("%s: no blob for %s; sha256 unavailable", IGNORE_FILE, item.path)
            out.append(replace(item, blob=blob))
            continue
        try:
            data = await client.fetch_blob_bytes(owner, repo, blob)
        except Exception as exc:
            logger.warning("%s: blob %s for %s unreadable (%s)", IGNORE_FILE, blob, item.path, exc)
            out.append(replace(item, blob=blob))
            continue
        out.append(replace(item, size=len(data), sha256=sha256_hex(data), blob=blob))
    return tuple(out)
