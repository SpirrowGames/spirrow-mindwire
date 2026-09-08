"""Tests for the ADR index generator (ADR-2026-06-04-19 N-2, Tier B Finding-1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from spirrow_mindwire.naysayer.adr_index import load_adr_entries, load_adr_index
from spirrow_mindwire.naysayer.adr_index_gen import (
    build_manifest_index,
    check_body_locator_formats,
    check_in_repo_bodies_are_registered,
    check_repo_locator_targets,
    extract_docmap_adrs,
    load_existing_body_locators,
    render_manifest,
)

# §M carries an identity ADR (09) the _docmap omits.
_CLAUDE_MD = (
    "## §M\n| ADR | x | y |\n|---|---|---|\n"
    "| ADR-2026-05-27-09 (T28) | identity 4 layers | T-T28-author-role-identity |\n"
)

# A plausible _docmap shape: a list of doc entries (nested under a top-level key), each
# with a path + title. Carries an architecture ADR (16) §M omits, plus a non-ADR doc.
_DOCMAP: dict[str, Any] = {
    "documents": [
        {
            "path": "adr/ADR-2026-06-03-16-ci-gate.md",
            "title": "naysayer CI-gate",
            "status": "accepted",
        },
        {"path": "guides/setup.md", "title": "Setup guide", "status": "draft"},
    ]
}


def test_extract_docmap_adrs_tolerant_walk() -> None:
    adrs = extract_docmap_adrs(_DOCMAP)
    assert adrs == {"ADR-2026-06-03-16": "naysayer CI-gate"}  # non-ADR doc ignored


def test_extract_docmap_adrs_strips_id_prefix_from_title() -> None:
    # Real _docmap titles carry the id as a prefix; the rendered "- {id} — {title}" line must
    # not double-print the id (Tier B re-review msg-446). The title's own parens are preserved.
    docmap = {
        "docs": [
            {
                "path": "adr/ADR-2026-05-21-06.md",
                "title": "ADR-2026-05-21-06 — mindwire Interface Contract (Ports)",
            }
        ]
    }
    assert extract_docmap_adrs(docmap) == {
        "ADR-2026-05-21-06": "mindwire Interface Contract (Ports)"
    }


def test_build_manifest_index_is_the_union() -> None:
    index = build_manifest_index(_CLAUDE_MD, _DOCMAP)
    # The whole point: §M-only (09) AND _docmap-only (16) both present, sorted.
    assert [row[0] for row in index] == ["ADR-2026-05-27-09", "ADR-2026-06-03-16"]
    assert index[0][1] == "identity 4 layers"  # §M title kept
    assert index[1][1] == "naysayer CI-gate"  # _docmap title


def test_build_manifest_index_carries_section_m_thread() -> None:
    # T-adr-index-omits-chatroom-body-locator §4-1: the §M thread column must be
    # preserved through generation (single-source with CLAUDE.md — §4-6).
    index = build_manifest_index(_CLAUDE_MD, _DOCMAP)
    by_id = {adr_id: (title, thread) for adr_id, title, thread in index}
    assert by_id["ADR-2026-05-27-09"][1] == "T-T28-author-role-identity"
    # Architecture ADRs (docmap-only, no §M row) have no thread.
    assert by_id["ADR-2026-06-03-16"][1] is None


def test_render_manifest_round_trips_through_loader(tmp_path: Path) -> None:
    index = build_manifest_index(_CLAUDE_MD, _DOCMAP)
    # No pre-existing body locators → every entry gets the ``drive`` default.
    rendered = render_manifest(index, body_locators={})
    # Parses as YAML and matches the loader's view when written to spec/adr_index.yaml.
    parsed = yaml.safe_load(rendered)
    assert parsed["adrs"][0]["id"] == "ADR-2026-05-27-09"
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(rendered, encoding="utf-8")
    loaded = load_adr_index(tmp_path)
    assert [row[0] for row in loaded] == [row[0] for row in index]
    assert [row[1] for row in loaded] == [row[1] for row in index]


def test_render_manifest_escapes_quotes() -> None:
    rendered = render_manifest(
        (("ADR-2026-01-01-1", 'a "quoted" title', None),),
        body_locators={"ADR-2026-01-01-1": "drive"},
    )
    parsed = yaml.safe_load(rendered)
    assert parsed["adrs"][0]["title"] == 'a "quoted" title'


def test_render_manifest_preserves_body_locators(tmp_path: Path) -> None:
    # T-adr-index-omits-chatroom-body-locator §4-1: hand-maintained body locators must
    # survive regeneration (round-trip). An id absent from the map gets ``drive``.
    index = build_manifest_index(_CLAUDE_MD, _DOCMAP)
    rendered = render_manifest(
        index,
        body_locators={
            "ADR-2026-05-27-09": "chatroom:spirrow-mindwire/T-T28-author-role-identity#msg-283"
        },
    )
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(rendered, encoding="utf-8")
    entries = load_adr_entries(tmp_path)
    by_id = {e.adr_id: e for e in entries}
    assert (
        by_id["ADR-2026-05-27-09"].body
        == "chatroom:spirrow-mindwire/T-T28-author-role-identity#msg-283"
    )
    # No pre-existing locator for the docmap-only entry → default.
    assert by_id["ADR-2026-06-03-16"].body == "drive"


def test_load_existing_body_locators_reads_committed_manifest(tmp_path: Path) -> None:
    manifest = (
        "adrs:\n"
        "  - id: ADR-1\n"
        '    title: "t"\n'
        "    body: chatroom:proj/T-foo#msg-1\n"
        "  - id: ADR-2\n"
        '    title: "t2"\n'
        # Missing body → not surfaced by the reader; the generator falls back to default.
    )
    p = tmp_path / "adr_index.yaml"
    p.write_text(manifest, encoding="utf-8")
    assert load_existing_body_locators(p) == {"ADR-1": "chatroom:proj/T-foo#msg-1"}


def test_load_existing_body_locators_missing_file_returns_empty(tmp_path: Path) -> None:
    # First-time generation (no committed manifest yet) must not crash.
    assert load_existing_body_locators(tmp_path / "does-not-exist.yaml") == {}


def test_check_body_locator_formats_accepts_valid_shapes() -> None:
    manifest = (
        "adrs:\n"
        "  - id: ADR-1\n"
        '    title: "t"\n'
        "    body: chatroom:spirrow-mindwire/T-foo.bar-baz#msg-42\n"
        "  - id: ADR-2\n"
        '    title: "t"\n'
        "    body: drive\n"
        "  - id: ADR-3\n"
        '    title: "t"\n'
        "    body: drive:some-file-id\n"
    )
    assert check_body_locator_formats(manifest) == []


def test_check_body_locator_formats_rejects_broken_shapes() -> None:
    # Einstein O-1 in msg-2582: empty / TODO / anchor-missing / project-missing MUST fail.
    manifest = (
        "adrs:\n"
        "  - id: ADR-1\n"
        '    title: "t"\n'
        "    body: TODO\n"
        "  - id: ADR-2\n"
        '    title: "t"\n'
        '    body: ""\n'
        "  - id: ADR-3\n"
        '    title: "t"\n'
        "    body: chatroom:proj/T-foo\n"  # anchor missing
        "  - id: ADR-4\n"
        '    title: "t"\n'
        "    body: chatroom:#msg-1\n"  # project/thread missing
    )
    bad = dict(check_body_locator_formats(manifest))
    assert set(bad.keys()) == {"ADR-1", "ADR-2", "ADR-3", "ADR-4"}


def test_check_body_locator_formats_accepts_and_rejects_repo_shapes() -> None:
    # msg-2671 D-1/§4-2. ``repo:`` is accepted; the two escapes the regex cannot express
    # are rejected by the path predicate that check_body_locator_formats now shares with
    # body_locator_is_valid, and a non-.md target falls at the regex itself.
    manifest = (
        "adrs:\n"
        "  - id: ADR-OK\n"
        '    title: "t"\n'
        "    body: repo:docs/adr/ADR-2026-08-25-20-independence-class-machine-value.md\n"
        "  - id: ADR-ABS\n"
        '    title: "t"\n'
        "    body: repo:/abs/x.md\n"
        "  - id: ADR-DOTDOT\n"
        '    title: "t"\n'
        "    body: repo:../x.md\n"
        "  - id: ADR-NOTMD\n"
        '    title: "t"\n'
        "    body: repo:docs/adr/x.txt\n"
    )
    bad = dict(check_body_locator_formats(manifest))
    assert set(bad) == {"ADR-ABS", "ADR-DOTDOT", "ADR-NOTMD"}


def _tree(tmp_path: Path, *names: str) -> Path:
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    for name in names:
        (tmp_path / "docs" / "adr" / name).write_text("# body\n", encoding="utf-8")
    return tmp_path


def test_check_repo_locator_targets_flags_only_the_missing_file(tmp_path: Path) -> None:
    # msg-2671 D-3: existence, in the same tree. A present file passes, an absent file
    # fails, and the non-repo forms are untouched (a bare ``drive`` has nothing to resolve).
    root = _tree(tmp_path, "ADR-1-here.md")
    manifest = (
        "adrs:\n"
        "  - id: ADR-1\n"
        '    title: "t"\n'
        "    body: repo:docs/adr/ADR-1-here.md\n"
        "  - id: ADR-2\n"
        '    title: "t"\n'
        "    body: repo:docs/adr/ADR-2-gone.md\n"
        "  - id: ADR-3\n"
        '    title: "t"\n'
        "    body: drive\n"
        "  - id: ADR-4\n"
        '    title: "t"\n'
        "    body: chatroom:proj/T-foo#msg-1\n"
    )
    assert check_repo_locator_targets(manifest, root) == [("ADR-2", "docs/adr/ADR-2-gone.md")]


def test_check_repo_locator_targets_skips_grammar_failures(tmp_path: Path) -> None:
    # A ``..`` locator must never reach the filesystem here: it belongs to
    # check_body_locator_formats, and resolving it is precisely what the path predicate
    # exists to prevent. Silence from THIS check is correct — the entry is not thereby
    # approved, it is rejected one check over. Both halves are asserted so a future edit
    # cannot quietly move the rejection out of the suite altogether.
    root = _tree(tmp_path)
    manifest = 'adrs:\n  - id: ADR-1\n    title: "t"\n    body: repo:../../secrets.md\n'
    assert check_repo_locator_targets(manifest, root) == []
    assert dict(check_body_locator_formats(manifest)) == {"ADR-1": "repo:../../secrets.md"}


def test_check_in_repo_bodies_are_registered_flags_unregistered_body(tmp_path: Path) -> None:
    # msg-2671 §4-9: a body file sitting in docs/adr/ while the index points at Drive is
    # exactly the state main shipped for ADR-20, and exactly the misdirection this thread
    # was opened about. It must fail.
    root = _tree(tmp_path, "ADR-1-body.md", "ADR-2-body.md")
    manifest = (
        "adrs:\n"
        "  - id: ADR-1\n"
        '    title: "t"\n'
        "    body: repo:docs/adr/ADR-1-body.md\n"
        "  - id: ADR-2\n"
        '    title: "t"\n'
        "    body: drive\n"
        "  - id: ADR-3\n"
        '    title: "t"\n'
        "    body: drive\n"  # no file in docs/adr/ -> legitimately still on Drive
    )
    assert check_in_repo_bodies_are_registered(manifest, root) == [
        ("ADR-2", "drive", "ADR-2-body.md")
    ]


def test_check_in_repo_bodies_are_registered_excludes_amendment_memos(tmp_path: Path) -> None:
    # The ADR-06 trap (msg-2671 §4-5) as a unit-level fixture: an amendment memo is a diff
    # against a body, not the body, so its presence must NOT force a repo: locator. Drop
    # the exclusion and this test goes red — which is the guard, because inferring
    # ``repo:`` from a filename is how ADR-06 would get pointed at a document that is not
    # ADR-06. A second amendment would be the signal to model amendments in the schema.
    root = _tree(tmp_path, "ADR-6-amendment-v2.2-thing.md")
    manifest = 'adrs:\n  - id: ADR-6\n    title: "t"\n    body: drive\n'
    assert check_in_repo_bodies_are_registered(manifest, root) == []
    # ... but a real body alongside the memo DOES have to be registered.
    (root / "docs" / "adr" / "ADR-6-real-body.md").write_text("# b\n", encoding="utf-8")
    assert check_in_repo_bodies_are_registered(manifest, root) == [
        ("ADR-6", "drive", "ADR-6-real-body.md")
    ]


def test_render_manifest_defaults_new_entries_to_drive_not_repo(tmp_path: Path) -> None:
    # msg-2671 §4-9: the generator must NOT infer ``repo:`` by scanning docs/adr/. A new
    # entry defaults to ``drive`` even when a same-id file exists, because inference
    # mis-files amendment memos (the ADR-06 trap). The drift check reports the gap; the
    # generator does not guess at it. Hand-maintained values still round-trip.
    _tree(tmp_path, "ADR-2026-01-01-1-body.md")
    rendered = render_manifest(
        (("ADR-2026-01-01-1", "new", None), ("ADR-2026-01-01-2", "kept", None)),
        body_locators={"ADR-2026-01-01-2": "repo:docs/adr/ADR-2026-01-01-2-x.md"},
    )
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(rendered, encoding="utf-8")
    by_id = {e.adr_id: e.body for e in load_adr_entries(tmp_path)}
    assert by_id["ADR-2026-01-01-1"] == "drive"
    assert by_id["ADR-2026-01-01-2"] == "repo:docs/adr/ADR-2026-01-01-2-x.md"
