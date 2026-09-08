"""Tests for the deterministic in-repo ADR index manifest (ADR-2026-06-04-19 N-2)."""

from __future__ import annotations

import re
from pathlib import Path

from spirrow_mindwire.naysayer.adr_index import (
    BODY_LOCATOR_RE,
    body_locator_is_valid,
    build_adr_index_block,
    load_adr_entries,
    load_adr_index,
    parse_adr_index,
    parse_adr_index_with_thread,
    repo_locator_path,
)
from spirrow_mindwire.naysayer.adr_index_gen import (
    check_body_locator_formats,
    check_in_repo_bodies_are_registered,
    check_repo_locator_targets,
)

_MANIFEST = (
    "adrs:\n"
    "  - id: ADR-2026-06-04-19\n"
    '    title: "naysayer agentization"\n'
    "    body: drive\n"
    "  - id: ADR-2026-05-23-07\n"
    '    title: "Stage 3 gating"\n'
    "    body: drive\n"
)


def _repo_with_manifest(tmp_path: Path) -> Path:
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(_MANIFEST, encoding="utf-8")
    return tmp_path


def test_load_adr_index_sorted_deduped(tmp_path: Path) -> None:
    rows = load_adr_index(_repo_with_manifest(tmp_path))
    assert [r[0] for r in rows] == ["ADR-2026-05-23-07", "ADR-2026-06-04-19"]  # sorted
    assert dict(rows)["ADR-2026-06-04-19"] == "naysayer agentization"


def test_load_adr_index_missing_returns_empty(tmp_path: Path) -> None:
    assert load_adr_index(tmp_path) == ()  # no spec/adr_index.yaml


def test_load_adr_index_malformed_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text("just a string", encoding="utf-8")
    assert load_adr_index(tmp_path) == ()  # not a {adrs: [...]} mapping


def test_load_adr_index_yaml_syntax_error_returns_empty(tmp_path: Path) -> None:
    # Tier B Finding-2 (msg-442): a syntax-broken manifest must fail open to () — never
    # raise yaml.YAMLError up into prompt construction and crash the naysayer.
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(
        'adrs:\n  - id: ADR-1\n    title: "unclosed\n  - bad: [indent',  # ScannerError/ParserError
        encoding="utf-8",
    )
    assert load_adr_index(tmp_path) == ()


def test_load_adr_entries_carries_thread_and_body(tmp_path: Path) -> None:
    manifest = (
        "adrs:\n"
        "  - id: ADR-2026-05-29-12\n"
        '    title: "embodiment"\n'
        "    thread: T-embodiment-self-declared\n"
        "    body: chatroom:spirrow-mindwire/T-embodiment-self-declared#msg-325\n"
        "  - id: ADR-2026-06-03-16\n"
        '    title: "naysayer ci gate"\n'
        "    body: drive\n"
    )
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(manifest, encoding="utf-8")
    entries = load_adr_entries(tmp_path)
    by_id = {e.adr_id: e for e in entries}
    assert by_id["ADR-2026-05-29-12"].thread == "T-embodiment-self-declared"
    assert (
        by_id["ADR-2026-05-29-12"].body
        == "chatroom:spirrow-mindwire/T-embodiment-self-declared#msg-325"
    )
    # Architecture ADRs (no §M thread) → thread is None, body defaults to drive.
    assert by_id["ADR-2026-06-03-16"].thread is None
    assert by_id["ADR-2026-06-03-16"].body == "drive"


def test_load_adr_entries_missing_body_falls_back_to_drive(tmp_path: Path) -> None:
    # Loader fail-open: an entry without ``body:`` renders as ``drive`` so downstream
    # rendering never sees an empty field (the CI check catches shipped manifests that
    # actually lack it; this fallback is for prompt-construction safety only).
    manifest = 'adrs:\n  - id: ADR-1\n    title: "t"\n'
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "adr_index.yaml").write_text(manifest, encoding="utf-8")
    entries = load_adr_entries(tmp_path)
    assert entries[0].body == "drive"


def test_build_block_lists_manifest_entries(tmp_path: Path) -> None:
    block = build_adr_index_block(_repo_with_manifest(tmp_path))
    assert "ADR index (id + title + body locator)" in block
    assert "ADR-2026-06-04-19 — naysayer agentization [body: drive]" in block
    assert "ADR-2026-05-23-07 — Stage 3 gating [body: drive]" in block
    # The cross-check instruction (the point of injecting the complete index).
    assert "cannot search for an ADR you do not know exists" in block
    # T-adr-index-omits-chatroom-body-locator: the locator format guidance the naysayer
    # needs to interpret ``chatroom:...#msg-N`` vs ``drive``.
    assert "chatroom:<project>/<thread>#msg-<n>" in block


def test_build_block_without_manifest_is_explicit(tmp_path: Path) -> None:
    block = build_adr_index_block(tmp_path)  # no manifest
    assert "UNAVAILABLE" in block
    assert "could not cross-check" in block


_ADR_ID_RE = re.compile(r"^ADR-\d{4}-\d{2}-\d{2}-\d+$")


def test_real_in_repo_manifest_loads_and_is_well_formed() -> None:
    # CI schema/parse validation of the committed spec/adr_index.yaml (the only CI check
    # possible — _docmap is absent in CI, so no drift-check; msg-443).
    rows = load_adr_index()  # default repo root
    assert rows, "committed manifest must be non-empty"
    ids = {r[0] for r in rows}
    assert "ADR-2026-06-04-19" in ids
    assert "ADR-2026-06-03-16" in ids  # an architecture ADR §M omits — must be present
    # Well-formed: every entry has a well-shaped ADR id and a non-empty title.
    for adr_id, title in rows:
        assert _ADR_ID_RE.match(adr_id), f"malformed ADR id: {adr_id!r}"
        assert title.strip(), f"empty title for {adr_id}"


def test_real_manifest_body_locators_are_well_formed() -> None:
    # T-adr-index-omits-chatroom-body-locator §4-4 (Einstein O-1 in msg-2582): the CI check
    # is a hermetic FORMAT check — no reachability, no Drive lookup, no chatroom fetch.
    # It only verifies that every ``body:`` string matches the locator grammar so a
    # ``TODO`` / empty / anchor-missing string never ships. Failing here means the yaml
    # got a placeholder that the schema rejects; fix the string or remove the entry.
    repo_root = Path(__file__).resolve().parents[1]
    manifest_text = (repo_root / "spec" / "adr_index.yaml").read_text(encoding="utf-8")
    bad = check_body_locator_formats(manifest_text)
    assert not bad, (
        f"spec/adr_index.yaml carries body locators that fail the format check: {bad}. "
        f"Valid shapes: chatroom:<project>/<thread>#msg-<n> | drive | drive:<pointer>"
    )


def test_body_locator_regex_shape() -> None:
    # Guardrails on the regex itself so a future edit does not accidentally widen it.
    assert BODY_LOCATOR_RE.match("chatroom:spirrow-mindwire/T-foo#msg-1")
    assert BODY_LOCATOR_RE.match("chatroom:proj.name/T.thread-x#msg-999")
    assert BODY_LOCATOR_RE.match("drive")
    assert BODY_LOCATOR_RE.match("drive:1a2b3c-file-id")
    # Rejections listed by Bohr in msg-2583 §4-4:
    assert not BODY_LOCATOR_RE.match("TODO")
    assert not BODY_LOCATOR_RE.match("")
    assert not BODY_LOCATOR_RE.match("chatroom:proj/T-foo")  # anchor missing
    assert not BODY_LOCATOR_RE.match("chatroom:/T-foo#msg-1")  # project missing
    # msg-2671 D-1: the third form.
    assert BODY_LOCATOR_RE.match("repo:docs/adr/ADR-2026-05-24-08-instance-identity-model.md")
    assert not BODY_LOCATOR_RE.match("repo:docs/adr/x.txt")  # not a .md body
    assert not BODY_LOCATOR_RE.match("repo:")  # empty path


def test_repo_locator_path_predicate_rejects_escapes() -> None:
    # msg-2671 §4-2: absolute paths and ``..`` segments are rejected by a predicate
    # OUTSIDE the regex, because ``.`` and ``/`` must both be legal inside a path. This
    # test pins the split: the regex ALONE accepts these two, the full rule does not. If
    # someone later "simplifies" body_locator_is_valid down to a bare regex match, the
    # first two asserts still pass and the last two fail — which is the point.
    assert BODY_LOCATOR_RE.match("repo:/abs/x.md")
    assert BODY_LOCATOR_RE.match("repo:../x.md")
    assert not body_locator_is_valid("repo:/abs/x.md")
    assert not body_locator_is_valid("repo:../x.md")
    assert not body_locator_is_valid("repo:docs/../../etc/x.md")
    assert not body_locator_is_valid("repo:x.txt")  # falls at the regex, not the predicate
    # A ``..`` that is not its own path segment is a legal filename, not an escape.
    assert body_locator_is_valid("repo:docs/adr/weird..name.md")
    assert body_locator_is_valid(
        "repo:docs/adr/ADR-2026-08-25-20-independence-class-machine-value.md"
    )
    # The three older forms are untouched by the predicate.
    assert body_locator_is_valid("drive")
    assert body_locator_is_valid("drive:file-id")
    assert body_locator_is_valid("chatroom:spirrow-mindwire/T-foo#msg-1")
    assert not body_locator_is_valid("TODO")


def test_repo_locator_path_extracts_only_repo_form() -> None:
    assert repo_locator_path("repo:docs/adr/x.md") == "docs/adr/x.md"
    # ``None`` means "not a repo: locator" — not an error. The existence check keys off it.
    assert repo_locator_path("drive") is None
    assert repo_locator_path("chatroom:proj/T-foo#msg-1") is None


def test_real_manifest_repo_locators_resolve() -> None:
    # msg-2671 D-3: every ``repo:`` locator must name a file that actually exists in this
    # tree. Hermetic — one is_file() against the working tree, no network, no Drive, no
    # chatroom (Einstein O-1's boundary in msg-2582 is about network I/O, and this is the
    # same class as the CLAUDE.md-vs-yaml cross-check O-1's author endorsed in msg-2584).
    # Without this a ``repo:`` locator would be the worst of the three forms: precise
    # enough to look authoritative, free to be wrong. A bare ``drive`` at least says so.
    repo_root = Path(__file__).resolve().parents[1]
    manifest_text = (repo_root / "spec" / "adr_index.yaml").read_text(encoding="utf-8")
    missing = check_repo_locator_targets(manifest_text, repo_root)
    assert not missing, (
        f"spec/adr_index.yaml has repo: locators pointing at files that do not exist: "
        f"{missing}. Fix the path, or move the entry back to `drive`."
    )


def test_in_repo_adr_bodies_are_registered_as_repo_locators() -> None:
    # msg-2671 §4-9, the drift check. ``docs/adr/`` gained real ADR bodies while the index
    # still pointed them at Drive; main's own commit message named the symptom ("ADR-20 was
    # never registered"). This turns "someone notices" into "the suite fails".
    repo_root = Path(__file__).resolve().parents[1]
    manifest_text = (repo_root / "spec" / "adr_index.yaml").read_text(encoding="utf-8")
    stale = check_in_repo_bodies_are_registered(manifest_text, repo_root)
    assert not stale, (
        f"these ADRs have a body file under docs/adr/ but their `body:` points elsewhere "
        f"(adr_id, body, filename): {stale}. Set body to repo:docs/adr/<filename>."
    )


def test_amendment_memo_does_not_force_a_repo_locator_on_adr_06() -> None:
    # msg-2671 §4-5, pinned as a fixture because it is the exact trap this whole thread
    # exists to remove. ADR-2026-05-21-06's ONLY file under docs/adr/ is an *amendment
    # memo* which says of itself that it is a diff to be merged into the ADR-06 body on
    # Drive reflection. Mapping id -> file mechanically would point ADR-06 at a document
    # that is not ADR-06 — a new instance of the misdirection, dressed as a fix. So the
    # drift check excludes ``*-amendment-*`` and ADR-06 must stay on bare ``drive``.
    repo_root = Path(__file__).resolve().parents[1]
    body_dir = repo_root / "docs" / "adr"
    files = sorted(p.name for p in body_dir.glob("ADR-2026-05-21-06-*.md"))
    assert files, "fixture gone: docs/adr/ no longer carries an ADR-06 file at all"
    assert all("amendment" in f for f in files), (
        f"docs/adr/ now has a NON-amendment ADR-06 file ({files}); that would be the real "
        f"body, and ADR-2026-05-21-06 should move from `drive` to a repo: locator."
    )
    bodies = {e.adr_id: e.body for e in load_adr_entries()}
    assert bodies["ADR-2026-05-21-06"] == "drive", (
        "ADR-06 must stay on `drive` while its only in-repo file is an amendment memo."
    )


def test_section_m_adrs_are_a_subset_of_the_manifest() -> None:
    # Partial CI drift-check (Tier B re-review msg-448): _docmap is absent in CI but CLAUDE.md IS
    # present, so every §M-referenced ADR must already be in the committed manifest — catching an
    # identity ADR added to §M without rerunning gen_adr_index.py. (A *full* drift-check, covering
    # the docs-only architecture ADRs, would need _docmap, which CI does not have.)
    repo_root = Path(__file__).resolve().parents[1]
    claude_md = (repo_root / "CLAUDE.md").read_text(encoding="utf-8")
    section_m = {adr_id for adr_id, _ in parse_adr_index(claude_md)}
    manifest = {adr_id for adr_id, _ in load_adr_index()}
    missing = section_m - manifest
    assert not missing, (
        f"CLAUDE.md §M references ADRs absent from spec/adr_index.yaml "
        f"(rerun scripts/gen_adr_index.py): {sorted(missing)}"
    )


def test_section_m_threads_match_manifest() -> None:
    # T-adr-index-omits-chatroom-body-locator §4-6 (defensive): CLAUDE.md §M is the single
    # source for the ``thread`` column, and the generator preserves it — but the yaml is
    # in principle editable by hand, so we cross-check that every §M row's third column
    # equals the yaml's ``thread`` field for the same id. A mismatch means either §M or
    # the yaml drifted; fixing the yaml is a rerun of scripts/gen_adr_index.py.
    repo_root = Path(__file__).resolve().parents[1]
    claude_md = (repo_root / "CLAUDE.md").read_text(encoding="utf-8")
    section_m = {adr_id: thread for adr_id, _, thread in parse_adr_index_with_thread(claude_md)}
    manifest = {e.adr_id: e.thread for e in load_adr_entries()}
    mismatches: dict[str, tuple[str | None, str | None]] = {}
    for adr_id, m_thread in section_m.items():
        y_thread = manifest.get(adr_id)
        if m_thread != y_thread:
            mismatches[adr_id] = (m_thread, y_thread)
    assert not mismatches, (
        "CLAUDE.md §M ``thread`` column disagrees with spec/adr_index.yaml ``thread`` "
        f"field for these ADR ids (§M value, yaml value): {mismatches}. "
        "Rerun scripts/gen_adr_index.py to regenerate the yaml from §M."
    )


def test_parse_adr_index_still_parses_claude_md_section_m() -> None:
    # §M parser (used by adr_index_gen to build the manifest + the §M-subset drift-check).
    claude_md = (
        "## §M\n| ADR | x | y |\n|---|---|---|\n"
        "| ADR-2026-05-31-15 | independence gradation | T |\n"
    )
    rows = parse_adr_index(claude_md)
    assert rows == (("ADR-2026-05-31-15", "independence gradation"),)


def test_parse_adr_index_with_thread_returns_third_column() -> None:
    claude_md = (
        "## §M\n| ADR | x | y |\n|---|---|---|\n"
        "| ADR-2026-05-29-12 | embodiment | T-embodiment-self-declared |\n"
        "| ADR-2026-05-29-13 | readback | T-implementer-spec-readback-checklist |\n"
    )
    rows = parse_adr_index_with_thread(claude_md)
    assert rows == (
        ("ADR-2026-05-29-12", "embodiment", "T-embodiment-self-declared"),
        ("ADR-2026-05-29-13", "readback", "T-implementer-spec-readback-checklist"),
    )
