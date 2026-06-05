"""Deterministic in-repo ADR index manifest for the naysayer (ADR-2026-06-04-19 N-2).

An agentized naysayer that only self-fetches context has a blind spot: **it cannot
search for an ADR it does not know exists**. A thread framed without a conflicting
historical ADR would never prompt the naysayer to look for it (Einstein,
`T-naysayer-agentization` msg-421 Obj-2; ACCEPTed msg-422). So a complete, mechanical
ADR index is injected into the naysayer's system prompt on every summon — the agent
then holds a complete map and decides independently what to fetch.

**Source = the in-repo manifest** ``spec/adr_index.yaml`` (decided in
`T-naysayer-unify-impl` msg-438). Why in-repo rather than parsing CLAUDE.md §M or an
out-of-repo ``_docmap.yaml``:

* §M is a *curated identity/role subset* (it omits the naysayer/architecture ADRs —
  06/07/08/14/16-19), so a §M-only index lacks exactly the ADRs a naysayer reviewing a
  naysayer design must cross-check (the original Step ① defect).
* ``_docmap.yaml`` is *also* incomplete (it omits the §M-only identity ADRs 09-13,
  which have no ``.md`` body). The complete set is the **union** of both.
* An out-of-repo source (``MINDWIRE_DOCS_ROOT`` → spirrow-docs) is fragile: the loop
  host has no docs checkout and the deploy topology is undecided (ADR-18). In-repo is
  present on every host, deterministic, and needs no env wiring.

The manifest is a **derived view** (id + title only, never the canonical ADR body,
which lives in Drive / the scattered ADR set). It is *generated*, not hand-maintained:
``scripts/gen_adr_index.py`` (logic in :mod:`spirrow_mindwire.naysayer.adr_index_gen`)
rebuilds it from CLAUDE.md §M + the spirrow-docs ``_docmap``, run by the proposer on the
docs host when an ADR is added/accepted. A committed copy is unavoidable — the loop host
has no docs checkout and the deploy topology is undecided (ADR-18 / msg-438), so a runtime
union (which needs ``_docmap``) is not possible. CI cannot run a *full* drift-check either
(``_docmap`` is absent in CI), but it does enforce two things: the committed manifest
**parses and is well-formed** (``test_real_in_repo_manifest_loads_and_is_well_formed``), and
— a **partial drift-check**, since CLAUDE.md *is* in CI — that every §M-referenced ADR is
present in the manifest (``test_section_m_adrs_are_a_subset_of_the_manifest``), catching an
identity ADR added to §M without regenerating (Tier B re-review msg-448).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# adr_index.py -> naysayer -> spirrow_mindwire -> src -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST_REL = Path("spec") / "adr_index.yaml"

# A CLAUDE.md §M table row: ``| ADR-2026-05-27-09 (T28) | <title> | <thread> |``.
# Retained for ``context_bundle.py`` only, until the ADR-19 N-4 (Step ③) removal —
# the naysayer index itself no longer uses §M (it reads the manifest below).
_ADR_INDEX_ROW_RE = re.compile(
    r"^\|\s*(ADR-\d{4}-\d{2}-\d{2}-\d+)[^|]*\|\s*([^|]+?)\s*\|", re.MULTILINE
)


def parse_adr_index(claude_md: str) -> tuple[tuple[str, str], ...]:
    """Parse the CLAUDE.md §M ADR table into ``(adr_id, title)`` rows (deduped, sorted).

    Used by ``context_bundle.py`` (the retired relay) until its Step ③ removal; the
    naysayer system prompt now sources its index from the manifest, not §M.
    """
    seen: dict[str, str] = {}
    for adr_id, title in _ADR_INDEX_ROW_RE.findall(claude_md):
        seen.setdefault(adr_id, title.strip())
    return tuple(sorted(seen.items()))


def load_adr_index(repo_root: Path | None = None) -> tuple[tuple[str, str], ...]:
    """Load the in-repo ADR manifest as ``(id, title)`` rows (deduped, sorted).

    Reads ``<repo_root>/spec/adr_index.yaml`` (``repo_root`` defaults to this repo;
    the adapter passes the reviewed repo's cwd). Returns ``()`` if the manifest is
    absent or malformed — the caller surfaces that **explicitly** rather than passing
    off an empty/partial list as complete.
    """
    root = repo_root if repo_root is not None else _REPO_ROOT
    try:
        raw = (root / _MANIFEST_REL).read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError):
        # Missing file (OSError) OR a malformed/un-parseable manifest (YAMLError, e.g. a
        # typo'd bracket/indent in the hand-editable YAML) → fail open to (), so the
        # caller emits the explicit "UNAVAILABLE" block rather than crashing the naysayer
        # during prompt construction (Tier B Finding-2, msg-442).
        return ()
    if not isinstance(data, dict):
        return ()
    entries = data.get("adrs")
    if not isinstance(entries, list):
        return ()
    seen: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        adr_id = entry.get("id")
        if isinstance(adr_id, str) and adr_id:
            seen.setdefault(adr_id, str(entry.get("title", "")).strip())
    return tuple(sorted(seen.items()))


def build_adr_index_block(repo_root: Path | None = None) -> str:
    """Render the injectable ADR index block from the in-repo manifest (deterministic).

    When the manifest cannot be loaded the block says so **explicitly** — a missing
    index must be visible to the reviewer, never silently omitted nor mislabelled as
    complete.
    """
    index = load_adr_index(repo_root)
    if not index:
        return (
            "## ADR index — UNAVAILABLE\n"
            "The in-repo ADR manifest (spec/adr_index.yaml) could not be loaded. Proceed "
            "without a complete ADR map and note in your review that you could not "
            "cross-check the design against the full ADR set."
        )
    rows = "\n".join(f"- {adr_id} — {title}" for adr_id, title in index)
    return (
        "## ADR index (id + title) — the project's known ADRs, injected deterministically\n"
        "A maintained in-repo derived view of every ADR the project knows about (bodies "
        "live elsewhere), injected on every summon so your review is not bounded by what "
        "this thread happens to cite. Enumerate the ADRs/docs the thread DOES reference, "
        "cross-check the design under review against the full list below, and flag any "
        "relevant ADR the discussion never referenced — you cannot search for an ADR you "
        "do not know exists:\n"
        f"{rows}"
    )


__all__ = ["build_adr_index_block", "load_adr_index", "parse_adr_index"]
