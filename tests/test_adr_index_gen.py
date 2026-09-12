"""Tests for the ADR index generator (ADR-2026-06-04-19 N-2, Tier B Finding-1)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

from spirrow_mindwire.naysayer.adr_index import (
    body_locator_is_valid,
    load_adr_entries,
    load_adr_index,
)
from spirrow_mindwire.naysayer.adr_index_gen import (
    build_manifest_index,
    check_body_locator_formats,
    check_in_repo_bodies_are_registered,
    check_repo_locator_targets,
    load_existing_body_locators,
    render_manifest,
)

# §M carries an identity ADR (09) the body scan omits (it has no .md body).
_CLAUDE_MD = (
    "## §M\n| ADR | x | y |\n|---|---|---|\n"
    "| ADR-2026-05-27-09 (T28) | identity 4 layers | T-T28-author-role-identity |\n"
)

# The second source: {id: title} as adr_titles_from_repo returns it. Carries an
# architecture ADR (16) §M omits, so the union below is doing real work.
_SECOND_SOURCE: dict[str, str] = {"ADR-2026-06-03-16": "naysayer CI-gate"}


def test_build_manifest_index_is_the_union() -> None:
    index = build_manifest_index(_CLAUDE_MD, _SECOND_SOURCE)
    # The whole point: §M-only (09) AND _docmap-only (16) both present, sorted.
    assert [row[0] for row in index] == ["ADR-2026-05-27-09", "ADR-2026-06-03-16"]
    assert index[0][1] == "identity 4 layers"  # §M title kept
    assert index[1][1] == "naysayer CI-gate"  # _docmap title


def test_build_manifest_index_carries_section_m_thread() -> None:
    # T-adr-index-omits-chatroom-body-locator §4-1: the §M thread column must be
    # preserved through generation (single-source with CLAUDE.md — §4-6).
    index = build_manifest_index(_CLAUDE_MD, _SECOND_SOURCE)
    by_id = {adr_id: (title, thread) for adr_id, title, thread in index}
    assert by_id["ADR-2026-05-27-09"][1] == "T-T28-author-role-identity"
    # Architecture ADRs (docmap-only, no §M row) have no thread.
    assert by_id["ADR-2026-06-03-16"][1] is None


def test_render_manifest_round_trips_through_loader(tmp_path: Path) -> None:
    index = build_manifest_index(_CLAUDE_MD, _SECOND_SOURCE)
    # No pre-existing body locators → every entry gets the ``unknown`` default.
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
    # survive regeneration (round-trip). An id absent from the map gets ``unknown``.
    index = build_manifest_index(_CLAUDE_MD, _SECOND_SOURCE)
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
    assert by_id["ADR-2026-06-03-16"].body == "unknown"


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


def test_amendment_marker_is_a_literal_segment_a_body_can_also_match(tmp_path: Path) -> None:
    # Both halves of the marker's literal rule, pinned side by side so that neither half
    # can be read as a safety claim about the other.
    #
    # (1) SEGMENT, not bare substring. The exclusion is documented as ``*-amendment-*`` in
    # adr_index_gen's module comment and in docs/adr/README.md, so it must match a hyphen-
    # delimited segment, not any occurrence of the word: a real body named
    # ``ADR-7-amendments-to-the-registry.md`` contains "amendment" but is a BODY, and
    # dropping it from the drift check would re-open the exact hole that check closes --
    # silently, since an excluded file is indistinguishable from an absent one. Widen the
    # marker back to a bare substring and this goes red.
    root = _tree(tmp_path, "ADR-7-amendments-to-the-registry.md")
    manifest = 'adrs:\n  - id: ADR-7\n    title: "t"\n    body: drive\n'
    assert check_in_repo_bodies_are_registered(manifest, root) == [
        ("ADR-7", "drive", "ADR-7-amendments-to-the-registry.md")
    ]
    # (2) The SINGULAR form collides, asserted outright rather than left to inference. A
    # real body titled "Amendment to X" is filed as ``ADR-8-amendment-to-x.md`` and the
    # literal rule skips it like a memo. Not desirable, and (1) was never a claim that the
    # marker is precise: this is an ACCEPTED collision of a string rule, written down so
    # nobody has to rediscover it. Not patched -- tightening the marker (to ``-amendment-v``,
    # say) would invent a convention out of the one real filename here and start mistaking
    # version-less memos for bodies, trading fail-open for fail-wrong. Escape hatch: an
    # explicit ``body: repo:docs/adr/<file>.md`` registers such a body anyway, so the
    # collision costs the prompt, not the entry. Made loud in
    # tests/test_naysayer_adr_index.py::test_amendment_marker_skip_set_is_pinned_to_this_tree.
    collided = _tree(tmp_path / "singular", "ADR-8-amendment-to-x.md")
    singular = 'adrs:\n  - id: ADR-8\n    title: "t"\n    body: drive\n'
    assert check_in_repo_bodies_are_registered(singular, collided) == []


def test_render_manifest_defaults_new_entries_to_unknown_not_repo(tmp_path: Path) -> None:
    # msg-2671 §4-9: the generator must NOT infer ``repo:`` by scanning docs/adr/. A new
    # entry defaults to ``unknown`` even when a same-id file exists, because inference
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
    assert by_id["ADR-2026-01-01-1"] == "unknown"
    assert by_id["ADR-2026-01-01-2"] == "repo:docs/adr/ADR-2026-01-01-2-x.md"


def test_default_body_names_no_medium() -> None:
    """The default must not point a new entry at a medium that is no longer canonical.

    It was ``drive`` until 2026-09-11. After ADR-2026-05-23-07 §6 Amendment moved
    canonicity to the Git trees, that default would have filed every new ADR under Drive
    — the one place the amendment says the body is not. This pins the replacement to a
    token that names no medium at all, which is what a weak locator should do.
    """
    from spirrow_mindwire.naysayer.adr_index_gen import _DEFAULT_BODY

    assert _DEFAULT_BODY == "unknown"
    assert "drive" not in _DEFAULT_BODY
    assert body_locator_is_valid(_DEFAULT_BODY)


def test_legacy_drive_locators_are_still_accepted() -> None:
    """Entries written before the canonicity move must not red the suite.

    Nothing new is emitted with a ``drive`` locator, but rejecting the form outright
    would turn a historical value into a CI failure for a decision made after it was
    written. Accepted, and preserved on regenerate like any other hand-set value — what
    changed is only that a new entry never defaults to it.
    """
    assert body_locator_is_valid("drive")
    assert body_locator_is_valid("drive:some-file-id")


def test_committed_manifest_matches_regeneration() -> None:
    """spec/adr_index.yaml is exactly what the generator produces from this tree.

    This is the drift-check ADR-2026-06-04-19 N-2 recorded as impossible. It was: the
    second source was spirrow-docs/_docmap.yaml, a file on one machine in a tree with no
    remote, so CI could only check that the committed copy parsed and covered §M — never
    that it was current. Since every ADR body landed in docs/adr/ and canonicity moved
    here (ADR-2026-05-23-07 §6), the generator reads only this repository, so the full
    comparison runs in the suite.

    If this reds after adding an ADR, the fix is to run the generator, not to edit the
    yaml: `python scripts/gen_adr_index.py`. Hand-set `body:` locators round-trip.
    """
    import subprocess

    repo_root = Path(__file__).resolve().parents[1]
    committed = (repo_root / "spec" / "adr_index.yaml").read_text(encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "gen_adr_index.py"), "--check"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "spec/adr_index.yaml is out of date. Run: python scripts/gen_adr_index.py"
        + chr(10)
        + result.stderr
    )
    assert "adrs:" in committed


def test_generator_writes_lf_on_this_platform() -> None:
    """Run the generator and assert the file it *wrote* has no CRLF.

    The first version of this test read the checked-in manifest instead, which proves
    nothing about the generator — git normalises on checkout, so it would have passed
    against the very bug it was named for. Path.write_text translates newlines to the
    platform default; on the loop host that turned all 98 lines into CRLF against an
    LF-committed file, and it could not show while the generator only ran on the Linux
    docs host. So the write path itself is exercised, on whatever platform runs the suite.
    """
    import subprocess

    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "adr_index.yaml"
        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts" / "gen_adr_index.py"),
                "--out",
                str(out),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        raw = out.read_bytes()
    assert raw, "generator wrote nothing"
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")


def test_adr_titles_from_repo_keeps_a_body_with_no_heading(tmp_path: Path) -> None:
    """A body file with no ``# `` line still gets an index entry, with an empty title.

    The first version keyed inclusion on finding the heading, so such a file vanished
    from the index entirely — which is the failure this index exists to prevent, and the
    one ADR-2026-08-25-20 demonstrated by sitting unregistered for half a year. An empty
    title is visible in the injected prompt; a missing row is not.
    """
    from spirrow_mindwire.naysayer.adr_index_gen import adr_titles_from_repo

    body_dir = tmp_path / "docs" / "adr"
    body_dir.mkdir(parents=True)
    (body_dir / "ADR-2026-01-01-1-titled.md").write_text("# Real title", encoding="utf-8")
    (body_dir / "ADR-2026-01-01-2-headless.md").write_text(
        "no heading here, just prose", encoding="utf-8"
    )
    (body_dir / "ADR-2026-01-01-3-amendment-memo.md").write_text("# Skipped", encoding="utf-8")

    titles = adr_titles_from_repo(tmp_path)

    assert titles == {"ADR-2026-01-01-1": "Real title", "ADR-2026-01-01-2": ""}


def test_a_headless_body_reaches_the_manifest(
    tmp_path: Path,
) -> None:
    """And it survives all the way into the rendered manifest, not just the title map."""
    from spirrow_mindwire.naysayer.adr_index_gen import adr_titles_from_repo

    body_dir = tmp_path / "docs" / "adr"
    body_dir.mkdir(parents=True)
    (body_dir / "ADR-2026-01-01-2-headless.md").write_text("prose only", encoding="utf-8")
    rendered = render_manifest(build_manifest_index("", adr_titles_from_repo(tmp_path)))
    parsed = yaml.safe_load(rendered)
    assert [e["id"] for e in parsed["adrs"]] == ["ADR-2026-01-01-2"]
    assert parsed["adrs"][0]["title"] == ""
