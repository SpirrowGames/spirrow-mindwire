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

The manifest is a **derived view** (id + title + optional thread + body locator, never
the canonical ADR body). It is *generated*, not hand-maintained:
``scripts/gen_adr_index.py`` (logic in :mod:`spirrow_mindwire.naysayer.adr_index_gen`)
rebuilds id/title/thread from CLAUDE.md §M + the spirrow-docs ``_docmap``; the
``body:`` locator per entry is hand-maintained in the yaml and preserved on
regenerate (round-trip). A committed copy is unavoidable — the loop host has no docs
checkout and the deploy topology is undecided (ADR-18 / msg-438), so a runtime union
(which needs ``_docmap``) is not possible. CI cannot run a *full* drift-check either
(``_docmap`` is absent in CI), but it does enforce four things: the committed manifest
**parses and is well-formed** (``test_real_in_repo_manifest_loads_and_is_well_formed``),
every §M-referenced ADR is present in the manifest
(``test_section_m_adrs_are_a_subset_of_the_manifest``, a partial drift-check since
CLAUDE.md is in CI), every entry carries a **format-valid body locator**
(``test_real_manifest_body_locators_are_well_formed``; this is the fix for
T-adr-index-omits-chatroom-body-locator — before, the index silently pointed callers
to Drive when the actual body was a chatroom decide-close, and three turns of
"body unreadable" misjudgments followed), and every §M thread column matches the
yaml's ``thread`` field per id (``test_section_m_threads_match_manifest``; that guards
§M ↔ yaml agreement only — ``thread`` is *not* rendered into the injected block, which
carries id/title/``body:``, so the misdirection class rides on ``body:``). Two more
arrived with ``repo:``: every ``repo:`` target must exist
in the tree (``test_real_manifest_repo_locators_resolve``), and an ADR whose body file
IS in ``docs/adr/`` must not be left pointing elsewhere
(``test_in_repo_adr_bodies_are_registered_as_repo_locators``). A seventh binds the two
columns: a ``chatroom:`` locator must name the same thread as the ``thread`` column
(``test_chatroom_body_locators_name_the_thread_column``). All seven are hermetic —
same-tree reads only, never the network, Drive, or the chatroom.

The body locator format is one of:

* ``chatroom:<project>/<thread>#msg-<n>`` — canonical: the decide-close message.
* ``drive`` — weak: "the body lives in Drive/spirrow-docs, file unknown". Carried
  forward as debt.
* ``drive:<fileId-or-title>`` — Drive with a specific pointer (future use).
* ``repo:<repo-relative path>.md`` — the body file lives in **this** repository (under
  ``docs/adr/``). Guarded by the regex *plus* an out-of-regex path predicate (no
  absolute paths, no ``..`` segments) *plus* a same-tree existence check, because a
  precise-looking path that does not resolve is worse than the self-declaredly weak
  bare ``drive`` (msg-2671 D-3).

**A locator is not a canonicity claim.** It names where a reader can open the bytes,
not which copy is normative; ``repo:`` no more declares "Git is the source of truth"
than a bare ``drive`` declares it for Drive. Canonicity moved from Drive to the Git trees
on 2026-09-11 (ADR-2026-05-23-07 §6 Amendment, Takahito, Tier-C), superseding
``docs/spec/DOCS_DEVELOP_LAYOUT_CONVENTION.md``. That decision is the grounds for it —
``repo:`` never was, and still is not: do not cite a locator as precedent (msg-2671 D-2).

The naysayer/implementer injectors expand the locator into the prompt so a reviewer
can reach the body from the index alone — not knowing where a body lives is exactly
how the three-turn misjudgment happened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# adr_index.py -> naysayer -> spirrow_mindwire -> src -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST_REL = Path("spec") / "adr_index.yaml"

# A CLAUDE.md §M table row: ``| ADR-2026-05-27-09 (T28) | <title> | <thread> |``. The
# naysayer index reads the manifest (below), not §M; §M is parsed only to build/validate
# the manifest. What is optional about the third column (``thread``) is its *content*
# (``[^|]*?`` admits an empty cell), NOT the column: the closing pipe is mandatory. A row
# that drops the column outright (2 cells / 3 pipes) therefore does not match at all, and
# its ADR vanishes from the parse result in silence — and, since ``adr_index_gen`` folds
# §M into the generated manifest, out of the manifest too. §M is the only source for the
# §M-only identity ADRs, so for those the entry simply ceases to exist. The guard against
# that is ``test_section_m_rows_all_parse``; the two §M drift-checks cannot be, because
# both are subset checks over rows that already parsed.
_ADR_INDEX_ROW_RE = re.compile(
    r"^\|\s*(ADR-\d{4}-\d{2}-\d{2}-\d+)[^|]*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|",
    re.MULTILINE,
)

# Body locator format — the validation regex the CI uses. Kept module-level so the
# manifest test, the generator, and any future producer all read the same source.
# Accepts three shapes and only three:
#   - chatroom:<project>/<thread>#msg-<n>
#   - drive
#   - drive:<anything non-empty>
# ``[\w.-]+`` matches project/thread ids (letters/digits/_/./-); the anchor ``#msg-<n>``
# is required for the chatroom form (Einstein's O-1 in msg-2582 — an anchor-missing
# ``chatroom:proj/T-foo`` string must be rejected here, not silently passed through).
#
# The ``repo:`` arm deliberately does NOT try to exclude ``..`` or a leading ``/`` in the
# regex: ``.`` and ``/`` are both legal inside a path, so any character class that admits
# ``docs/adr/x.md`` necessarily admits ``../x.md`` too. Those are rejected by
# :func:`_repo_path_is_safe` instead (msg-2671 §4-2). Callers should therefore prefer
# :func:`body_locator_is_valid` over matching this regex directly — the regex alone is
# NOT the full validity rule.
BODY_LOCATOR_RE = re.compile(
    r"^(?:chatroom:[\w.-]+/[\w.-]+#msg-\d+|drive|drive:.+|repo:[A-Za-z0-9._/-]+\.md)$"
)

_REPO_LOCATOR_PREFIX = "repo:"


def repo_locator_path(body: str) -> str | None:
    """Return the repo-relative path a ``repo:`` locator names, else ``None``.

    ``None`` means "not a ``repo:`` locator" — it is not an error signal. Callers use it
    to decide whether the same-tree existence check applies to an entry.
    """
    if not body.startswith(_REPO_LOCATOR_PREFIX):
        return None
    return body[len(_REPO_LOCATOR_PREFIX) :]


def _repo_path_is_safe(path: str) -> bool:
    """Reject absolute paths and ``..`` segments in a ``repo:`` locator path.

    The out-of-regex half of the ``repo:`` grammar (msg-2671 §4-2). It exists because the
    path character class must admit ``.`` and ``/``, which makes ``../x.md`` and
    ``/abs/x.md`` regex-legal. Keeping the rule here rather than widening the regex keeps
    the failure explainable. Windows drive letters (``C:/x.md``) need no rule — ``:`` is
    already outside the regex's character class.
    """
    if path.startswith("/"):
        return False
    return ".." not in path.split("/")


def body_locator_is_valid(body: str) -> bool:
    """Full validity rule for a body locator: the regex AND the path predicate.

    Prefer this over ``BODY_LOCATOR_RE.match`` — the regex alone accepts ``repo:../x.md``
    and ``repo:/abs/x.md``. Producers and the CI check share this one function so the two
    halves cannot drift apart.
    """
    if not BODY_LOCATOR_RE.match(body):
        return False
    path = repo_locator_path(body)
    return path is None or _repo_path_is_safe(path)


@dataclass(frozen=True)
class AdrEntry:
    """A single ADR index entry: id + title + optional thread + body locator.

    ``thread`` is only present for §M-referenced ADRs (identity/role ADRs 09-13/15
    and any future §M row); architecture ADRs from ``_docmap`` have ``None``. ``body``
    is always present — a bare ``drive`` (weak locator, debt marker) is preferable to
    an absent field so the CI locator check has something to check.
    """

    adr_id: str
    title: str
    thread: str | None
    body: str


def parse_adr_index(claude_md: str) -> tuple[tuple[str, str], ...]:
    """Parse the CLAUDE.md §M ADR table into ``(adr_id, title)`` rows (deduped, sorted).

    Used by :mod:`~spirrow_mindwire.naysayer.adr_index_gen` (to fold §M into the generated
    manifest) and by the partial CI drift-check (§M ⊆ manifest); the naysayer system prompt
    itself sources its index from the manifest, not §M. For rows that also need the
    ``thread`` column (§4-1 of T-adr-index-omits-chatroom-body-locator), see
    :func:`parse_adr_index_with_thread`.
    """
    seen: dict[str, str] = {}
    for adr_id, title, _thread in _ADR_INDEX_ROW_RE.findall(claude_md):
        seen.setdefault(adr_id, title.strip())
    return tuple(sorted(seen.items()))


def parse_adr_index_with_thread(
    claude_md: str,
) -> tuple[tuple[str, str, str | None], ...]:
    """Parse §M into ``(adr_id, title, thread)`` rows (deduped by id, sorted).

    ``thread`` is the §M table's third column (the chatroom thread that carries the
    ADR's decide-close, when one exists). An *empty* third column (``| … | … |  |``)
    yields ``None``: that row is legal (though not currently used) and the caller reads
    the absence as "no chatroom body, look at Drive".

    A row that omits the column altogether (``| … | … |``) is NOT that case. It is a
    broken §M row: it does not match :data:`_ADR_INDEX_ROW_RE`, so the ADR drops out of
    this result — and out of the generated manifest — with no error. See the note there.
    """
    seen: dict[str, tuple[str, str | None]] = {}
    for adr_id, title, thread in _ADR_INDEX_ROW_RE.findall(claude_md):
        thread_clean = thread.strip() or None
        seen.setdefault(adr_id, (title.strip(), thread_clean))
    return tuple(sorted((aid, t, th) for aid, (t, th) in seen.items()))


def load_adr_index(repo_root: Path | None = None) -> tuple[tuple[str, str], ...]:
    """Load the in-repo ADR manifest as ``(id, title)`` rows (deduped, sorted).

    Reads ``<repo_root>/spec/adr_index.yaml``. ``repo_root`` defaults to **this** repo
    and production callers must leave it unset — the manifest is MindWire's derived view
    of MindWire's ADRs, so it does not move with whatever repo is under review. The
    parameter exists for tests that plant a fixture manifest in ``tmp_path``.

    Returns ``()`` if the manifest is absent or malformed — the caller surfaces that
    **explicitly** rather than passing off an empty/partial list as complete. Note the
    cost of that fail-open: a caller that passes the wrong root gets a *silently*
    index-less review, announced only inside the prompt. Do not re-introduce a
    "``repo_root`` = the reviewed repo" reading; that misreading cost the design-time
    naysayer its entire ADR index until 2026-08-02.

    For entries that also carry ``thread``/``body`` (see :class:`AdrEntry`), use
    :func:`load_adr_entries`. This function is retained as a thin wrapper so existing
    callers (id+title only) do not need to change on the schema extension.
    """
    return tuple((e.adr_id, e.title) for e in load_adr_entries(repo_root))


def load_adr_entries(repo_root: Path | None = None) -> tuple[AdrEntry, ...]:
    """Load the in-repo ADR manifest as full ``AdrEntry`` records (deduped, sorted).

    Same fail-open semantics as :func:`load_adr_index`: returns ``()`` on missing or
    malformed manifest. An entry missing ``body`` falls open to the string ``"drive"``
    (the weakest valid locator) so downstream renderers always have SOMETHING to print
    rather than a silent empty field — a silent empty is what let the original
    misjudgment happen. The CI test enforces that shipped entries carry a real, format-
    valid body locator; this fallback only exists so the loader itself does not raise
    during prompt construction (fail-open policy from Tier B Finding-2, msg-442).
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
    seen: dict[str, AdrEntry] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        adr_id = entry.get("id")
        if not (isinstance(adr_id, str) and adr_id):
            continue
        thread_val = entry.get("thread")
        thread = thread_val.strip() if isinstance(thread_val, str) and thread_val.strip() else None
        body_val = entry.get("body")
        body = body_val.strip() if isinstance(body_val, str) and body_val.strip() else "drive"
        seen.setdefault(
            adr_id,
            AdrEntry(
                adr_id=adr_id,
                title=str(entry.get("title", "")).strip(),
                thread=thread,
                body=body,
            ),
        )
    return tuple(sorted(seen.values(), key=lambda e: e.adr_id))


def build_adr_index_block(repo_root: Path | None = None) -> str:
    """Render the injectable ADR index block from the in-repo manifest (deterministic).

    When the manifest cannot be loaded the block says so **explicitly** — a missing
    index must be visible to the reviewer, never silently omitted nor mislabelled as
    complete. The rendered rows include the ``body:`` locator per entry so a reviewer
    can reach the ADR body from the index alone; before this field existed a reader
    would have to guess whether the body lived in Drive or in a chatroom decide-close,
    and three turns of "body unreadable" misjudgments were traced to that gap
    (T-adr-index-omits-chatroom-body-locator).
    """
    entries = load_adr_entries(repo_root)
    if not entries:
        return (
            "## ADR index — UNAVAILABLE\n"
            "The in-repo ADR manifest (spec/adr_index.yaml) could not be loaded. Proceed "
            "without a complete ADR map and note in your review that you could not "
            "cross-check the design against the full ADR set."
        )
    rows = "\n".join(f"- {e.adr_id} — {e.title} [body: {e.body}]" for e in entries)
    return (
        "## ADR index (id + title + body locator) — the project's known ADRs, "
        "injected deterministically\n"
        "A maintained in-repo derived view of every ADR the project knows about (bodies "
        "live where the `body:` locator says — a chatroom decide-close message, a file "
        "in this repository, or "
        "Drive/spirrow-docs), injected on every summon so your review is not bounded by "
        "what this thread happens to cite. Enumerate the ADRs/docs the thread DOES "
        "reference, cross-check the design under review against the full list below, and "
        "flag any relevant ADR the discussion never referenced — you cannot search for an "
        "ADR you do not know exists. When you need an ADR body, follow its `body:` "
        "locator: `chatroom:<project>/<thread>#msg-<n>` names the message that carries "
        "the decide-close; `repo:<path>` names a file inside this repository (read it "
        "directly); `drive` means the body is in spirrow-docs/Drive; a bare `drive` (no "
        "file pointer) means the specific file is unknown to this index — a weak "
        "locator and a known debt. A locator says where the bytes can be opened, not "
        "which copy is normative:\n"
        f"{rows}"
    )


__all__ = [
    "BODY_LOCATOR_RE",
    "AdrEntry",
    "body_locator_is_valid",
    "build_adr_index_block",
    "load_adr_entries",
    "load_adr_index",
    "parse_adr_index",
    "parse_adr_index_with_thread",
    "repo_locator_path",
]
