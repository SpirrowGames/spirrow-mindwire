"""Generator for ``spec/adr_index.yaml`` — the naysayer ADR index (ADR-2026-06-04-19 N-2).

Build-time logic behind ``scripts/gen_adr_index.py`` (kept in ``src`` so it is unit-tested
like the rest of the package; the script is a thin CLI wrapper). The committed manifest is
a **derived view**: this module regenerates it from the union of

  (a) the ADRs referenced in **CLAUDE.md §M** (in-repo, via
      :func:`parse_adr_index_with_thread`), and
  (b) the ADR entries in the spirrow-docs **``_docmap.yaml``** (the canonical doc manifest).

Neither source alone is complete — §M omits the architecture ADRs (06/07/08/14/16-19) and
``_docmap`` omits the §M-only identity ADRs (09-13, which have no ``.md`` body) — so the
union is taken (Tier B Finding-1, ``T-naysayer-unify-impl`` msg-442/443). Shipping this
generator is what makes the committed file a genuine derived view rather than a
hand-maintained second source.

Two extra fields per entry (added in ``T-adr-index-omits-chatroom-body-locator``):

* ``thread`` — the chatroom thread from CLAUDE.md §M column 3, when the ADR is a §M
  entry. Generated (single-source = CLAUDE.md §M) so ``§4-6`` needs no drift check.
* ``body`` — the concrete locator for the ADR body (chatroom message or Drive), hand-
  maintained per entry. The generator **preserves** existing ``body`` values on
  regenerate (round-trip) and defaults new entries to ``drive`` (a weak, format-valid
  placeholder). Bohr §4-4 in msg-2583: a bare ``drive`` is a debt line, not a bug.
  The default stays ``drive`` even though ``repo:`` now exists: inferring ``repo:`` by
  scanning ``docs/adr/`` would mis-file ADR-06, whose only in-repo file is an *amendment
  memo*, not the body (msg-2671 §4-5). Inference is replaced by the drift check below,
  which reports the gap instead of guessing at it.

Three hermetic checks live here and are called from the tests (never from the naysayer's
hot path): :func:`check_body_locator_formats` (grammar), :func:`check_repo_locator_targets`
(a ``repo:`` path must resolve in the same tree — msg-2671 D-3), and
:func:`check_in_repo_bodies_are_registered` (an ADR whose body file sits in ``docs/adr/``
must not be left pointing at Drive — the failure main's own commit message named,
"ADR-20 was never registered"). None of them touch the network, Drive, or the chatroom.

``_docmap`` schema: the exact shape is owned by spirrow-docs and is not available on the
loop host, so :func:`extract_docmap_adrs` is **schema-tolerant** — it walks the parsed
structure and picks up any mapping carrying a ``title`` plus an ADR id in one of its string
fields. Verify the regenerated file against the real ``_docmap`` on first run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .adr_index import body_locator_is_valid, parse_adr_index_with_thread, repo_locator_path

_ADR_ID_RE = re.compile(r"ADR-\d{4}-\d{2}-\d{2}-\d+")

# Some _docmap titles carry the ADR id as a prefix ("ADR-YYYY-MM-DD-N - Title"); strip it so
# the rendered "- {id} - {title}" line does not print the id twice (Tier B re-review msg-446).
# Strips the id plus one separator char (any dash/colon/etc.). ASCII-only pattern (carries no
# ambiguous unicode); [^\w\s]? eats a single separator without over-eating the title's own text.
_TITLE_ID_PREFIX_RE = re.compile(r"^ADR-\d{4}-\d{2}-\d{2}-\d+\s*[^\w\s]?\s*")

# Emitted verbatim as the manifest's header; kept byte-identical to the committed
# spec/adr_index.yaml header so ``--check`` does not report spurious drift.
_HEADER = """\
# mindwire ADR index — union of CLAUDE.md §M references + spirrow-docs _docmap adr entries.
# GENERATED FILE — do not hand-edit id/title/thread. Regenerate with:
#     python scripts/gen_adr_index.py --docmap <path-to-spirrow-docs/_docmap.yaml>
# Run by the proposer on the docs host when an ADR is added/accepted (loop host/CI lacks
# _docmap, so this committed copy is unavoidable; ADR-2026-06-04-19 N-2 / msg-438/443).
# Derived view: id + title (+ thread when the ADR is a §M entry) — the ADR body lives in
# ONE OF three places: Drive/spirrow-docs, THIS repository under `docs/adr/`, or the
# chatroom decide-close message that resolved the thread (§M-only ADRs 09-13 have no
# Drive body; their body IS the decide-close). The `body:` field per entry names the
# concrete locator so the reader can reach the body from the index alone; see
# T-adr-index-omits-chatroom-body-locator for why this field exists (three turns of
# "body unreadable" misjudgments in T-not-waiting-conclair-contract-…).
# A locator is NOT a canonicity claim. It says where a reader can open the bytes, not
# which copy is normative — `repo:` no more declares "Git is the source of truth" than a
# bare `drive` declares it for Drive. Canonicity moved from Drive to the Git trees on
# 2026-09-11 (ADR-2026-05-23-07 §6 Amendment, Takahito, Tier-C), which also superseded
# `docs/spec/DOCS_DEVELOP_LAYOUT_CONVENTION.md`. That decision is the grounds for it —
# `repo:` never was, and still is not: do not cite a locator as precedent (msg-2671 D-2).
# `body:` is hand-maintained per-entry and the generator preserves existing values on
# regenerate; new entries default to `unknown` (a weak, format-valid placeholder — carry it
# forward as debt, do not leave it silent). It used to default to `drive`, which after the
# 2026-09-11 canonicity move would have pointed every new entry at a medium that is no
# longer canonical.
# Locator format (validated by tests/test_naysayer_adr_index.py):
#     chatroom:<project>/<thread>#msg-<n>   — canonical: the decide-close message
#     unknown                               — weak: this index does not record the location
#     repo:<repo-relative path>.md          — the body file in THIS repository
#     drive / drive:<fileId-or-title>       — legacy, still accepted, never emitted
# CI does not regenerate this (no _docmap in CI); it checks that the file parses and is
# well-formed, that every `body:` matches the locator format above, that every `repo:`
# target actually exists in the tree, and that an ADR whose body file IS in `docs/adr/`
# is not left pointing elsewhere. All four are hermetic — same-tree reads only, never
# the network, Drive, or the chatroom."""

_ID_FIELDS = ("id", "adr_id", "path", "doc_id", "slug", "name", "file")

# The default body locator for a new entry: weak but format-valid so CI stays green.
# Bohr §4-4 in msg-2583 called this an accepted-but-debt state; keeping a placeholder
# rather than an empty string is deliberate — the whole reason this field exists is
# that a silent empty is what let the original misjudgment happen.
_DEFAULT_BODY = "unknown"

# Where in-repo ADR bodies live, and the filename marker the drift check skips over
# (see :func:`check_in_repo_bodies_are_registered`).
#
# The rule is LITERAL, and the literal is the whole rule: a filename is skipped when it
# contains ``-amendment-`` as a hyphen-delimited segment. The marker does not, and cannot,
# decide whether a file *is* an amendment memo. Two consequences, both accepted:
#   * A real BODY can match it. A body titled "Amendment to X" lands on disk as
#     ``...-amendment-to-x.md`` and is skipped exactly like a memo. The plural
#     ``...-amendments-to-x.md`` is NOT skipped -- that asymmetry is the segment-vs-substring
#     property (widening the marker back to a bare substring re-opens the drift hole for
#     every such body), and tests/test_adr_index_gen.py pins both directions.
#   * The direction of that miss is FAIL-OPEN. A skipped entry keeps whatever weak locator
#     it already had -- normally bare ``drive`` -- which is the state that predates this
#     check. No wrong locator is ever produced; the check only ever withholds a prompt.
# The escape hatch stays open: writing ``body: repo:docs/adr/<file>.md`` by hand registers
# the body regardless of its name. A name collision suppresses the nagging, never the
# registration. What goes red when the skipped set changes is
# tests/test_naysayer_adr_index.py::test_amendment_marker_skip_set_is_pinned_to_this_tree,
# which pins the exact set of in-tree filenames this marker removes from the scan.
_ADR_BODY_DIR = "docs/adr"
_AMENDMENT_MARKER = "-amendment-"


def _first_adr_id(node: dict[str, Any]) -> str | None:
    """First ADR id found in this mapping's string fields, else ``None``."""
    for key in _ID_FIELDS:
        value = node.get(key)
        if isinstance(value, str):
            match = _ADR_ID_RE.search(value)
            if match:
                return match.group(0)
    return None


def extract_docmap_adrs(docmap_data: Any) -> dict[str, str]:
    """Extract ``{adr_id: title}`` from a parsed ``_docmap`` structure (schema-tolerant).

    Walks the (possibly nested) structure and records any mapping that carries a non-empty
    ``title`` together with an ADR id in one of its string fields. Tolerant of the top-level
    shape (list / dict / nested) since the canonical ``_docmap`` schema lives in spirrow-docs.
    """
    found: dict[str, str] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            title = node.get("title")
            if isinstance(title, str) and title.strip():
                adr_id = _first_adr_id(node)
                if adr_id is not None:
                    clean = _TITLE_ID_PREFIX_RE.sub("", title.strip()).strip()
                    found.setdefault(adr_id, clean)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(docmap_data)
    return found


def build_manifest_index(
    claude_md: str, docmap_data: Any
) -> tuple[tuple[str, str, str | None], ...]:
    """Union of CLAUDE.md §M references and ``_docmap`` ADR entries, with §M thread column.

    Returns ``(adr_id, title, thread)`` rows. Title preference: the §M summary when
    present (the in-repo curated text), else the ``_docmap`` title. ``thread`` comes
    from §M's third column when the ADR is a §M entry, else ``None`` (architecture ADRs
    have no chatroom thread — their body lives in Drive/spirrow-docs). The union is the
    point — it carries both the architecture ADRs §M omits and the identity ADRs
    ``_docmap`` omits.
    """
    section_m_rows = parse_adr_index_with_thread(claude_md)
    section_m_titles = {adr_id: title for adr_id, title, _ in section_m_rows}
    section_m_threads = {adr_id: thread for adr_id, _, thread in section_m_rows}
    docmap = extract_docmap_adrs(docmap_data)
    ids = set(section_m_titles) | set(docmap)
    merged: list[tuple[str, str, str | None]] = []
    for adr_id in sorted(ids):
        title = section_m_titles.get(adr_id) or docmap.get(adr_id, "")
        thread = section_m_threads.get(adr_id)
        merged.append((adr_id, title, thread))
    return tuple(merged)


def load_existing_body_locators(out_path: Any) -> dict[str, str]:
    """Read existing per-id ``body`` values from the committed manifest for round-trip.

    ``out_path`` should be the ``spec/adr_index.yaml`` path (``pathlib.Path``-like).
    Missing/malformed files → ``{}`` so a first-time generation still succeeds; the
    caller then falls back to :data:`_DEFAULT_BODY` for every entry. This is the seam
    that makes ``body:`` hand-maintainable in the yaml while ``id/title/thread`` stay
    generated: the generator does not know body locators, it only preserves them.
    """
    try:
        raw = out_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError, AttributeError):
        return {}
    if not isinstance(data, dict):
        return {}
    entries = data.get("adrs")
    if not isinstance(entries, list):
        return {}
    result: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        adr_id = entry.get("id")
        body_val = entry.get("body")
        if isinstance(adr_id, str) and adr_id and isinstance(body_val, str) and body_val.strip():
            result[adr_id] = body_val.strip()
    return result


def render_manifest(
    index: tuple[tuple[str, str, str | None], ...],
    body_locators: dict[str, str] | None = None,
) -> str:
    """Render the manifest YAML (header + ``adrs:`` list) — round-trips through PyYAML.

    ``body_locators`` maps adr id → existing body locator string (preserved on
    regenerate). An id absent from the map gets :data:`_DEFAULT_BODY` (``unknown``), a
    weak but format-valid placeholder that does not name a medium. Emitting a body value
    on every entry keeps the CI locator-format check meaningful (it always has something
    to check) and stops the
    generator from silently dropping a hand-maintained locator when the input changes.
    """
    body_locators = body_locators or {}
    lines = [_HEADER, "adrs:"]
    for adr_id, title, thread in index:
        escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f"  - id: {adr_id}")
        lines.append(f'    title: "{escaped_title}"')
        if thread:
            lines.append(f"    thread: {thread}")
        lines.append(f"    body: {body_locators.get(adr_id, _DEFAULT_BODY)}")
    return "\n".join(lines) + "\n"


def check_body_locator_formats(
    manifest_text: str,
) -> list[tuple[str, str]]:
    """Return ``(adr_id, bad_body)`` for entries whose ``body:`` fails the locator grammar.

    An empty result means every entry has a format-valid locator (Bohr §4-4 in
    msg-2583 — hermetic format check, no reachability). ``manifest_text`` is the raw
    YAML text so the check does not care about the read path (tests can pass a
    string; CI passes the committed file's contents).

    The rule is :func:`~spirrow_mindwire.naysayer.adr_index.body_locator_is_valid`, not
    the bare regex: for ``repo:`` locators the path predicate (no absolute path, no
    ``..``) is part of the grammar and is deliberately not expressible in the pattern.
    """
    return [
        (adr_id, body)
        for adr_id, body in _manifest_bodies(manifest_text)
        if not body_locator_is_valid(body)
    ]


def _manifest_bodies(manifest_text: str) -> list[tuple[str, str]]:
    """``(adr_id, body)`` for every well-identified entry in the raw manifest text.

    Shared by the three hermetic checks so they agree on what an entry is. A malformed
    manifest yields ``[]`` — the "does it parse at all" question belongs to
    ``test_real_in_repo_manifest_loads_and_is_well_formed``, not to these checks, and
    returning ``[]`` here keeps a parse failure from being reported three more times as
    a locator problem.
    """
    try:
        data = yaml.safe_load(manifest_text)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("adrs")
    if not isinstance(entries, list):
        return []
    out: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        adr_id = entry.get("id")
        if not isinstance(adr_id, str) or not adr_id:
            continue
        body = entry.get("body")
        out.append((adr_id, body if isinstance(body, str) else ""))
    return out


def check_repo_locator_targets(manifest_text: str, repo_root: Path) -> list[tuple[str, str]]:
    """Return ``(adr_id, path)`` for ``repo:`` locators whose file is not in the tree.

    D-3 of msg-2671. This is an existence check, **not** a reachability check: it is a
    single ``is_file()`` against the same working tree the manifest is committed in, so
    Einstein's O-1 boundary (msg-2582 — no network I/O in CI) is untouched. It is the
    same class as the CLAUDE.md-vs-yaml thread cross-check that O-1's author endorsed.

    Without it ``repo:`` would be the worst of the three forms: precise enough to look
    authoritative, free to be wrong. A bare ``drive`` at least announces its own weakness.
    Entries that fail the grammar are skipped — :func:`check_body_locator_formats` owns
    those, and resolving an unsafe path (``..``) is exactly what must not happen here.
    """
    bad: list[tuple[str, str]] = []
    for adr_id, body in _manifest_bodies(manifest_text):
        if not body_locator_is_valid(body):
            continue
        rel = repo_locator_path(body)
        if rel is not None and not (repo_root / rel).is_file():
            bad.append((adr_id, rel))
    return bad


def check_in_repo_bodies_are_registered(
    manifest_text: str, repo_root: Path
) -> list[tuple[str, str, str]]:
    """Return ``(adr_id, body, filename)`` where an in-repo body exists but is unregistered.

    Unregistered = a file matching ``docs/adr/<id>-*.md`` exists, yet the entry's
    ``body:`` points somewhere other than ``repo:``.

    §4-9 of msg-2671, the drift check. ``docs/adr/`` gained real ADR bodies while this
    index still pointed them at Drive; main's own commit message named the symptom
    ("ADR-20 was never registered"). This moves that from something a human must notice
    to something the suite fails on.

    Filenames carrying ``-amendment-`` as a hyphen-delimited segment are skipped. That is
    a rule about the *string*: it holds no opinion on whether the file is an amendment
    memo, so a real body titled "Amendment to X" is skipped too. That collision is known
    and accepted, and it fails OPEN — a skipped entry just keeps the weak locator it
    already had, this function never writes one, and an explicit ``body: repo:…`` registers
    such a body anyway. Full rule: the ``_AMENDMENT_MARKER`` comment above. Guard against
    the skipped set changing: ``test_amendment_marker_skip_set_is_pinned_to_this_tree``.

    The skip exists for ADR-2026-05-21-06, whose only file under ``docs/adr/`` says of
    itself "本メモは Drive 反映時に ADR-06 本体へマージする差分": mapping id to file
    mechanically would point ADR-06 at a document that is not ADR-06 — the same
    misdirection this thread exists to remove. A *second* amendment would be the signal to
    model amendments in the schema instead; that is out of scope here.
    """
    body_dir = repo_root / _ADR_BODY_DIR
    stale: list[tuple[str, str, str]] = []
    for adr_id, body in _manifest_bodies(manifest_text):
        if body.startswith("repo:"):
            continue
        for path in sorted(body_dir.glob(f"{adr_id}-*.md")):
            if _AMENDMENT_MARKER in path.name.lower():
                continue
            stale.append((adr_id, body, path.name))
            break
    return stale


__all__ = [
    "build_manifest_index",
    "check_body_locator_formats",
    "check_in_repo_bodies_are_registered",
    "check_repo_locator_targets",
    "extract_docmap_adrs",
    "load_existing_body_locators",
    "render_manifest",
]
