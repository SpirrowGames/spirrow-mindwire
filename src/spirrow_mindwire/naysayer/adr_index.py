"""Deterministic CLAUDE.md §M all-ADR title index (ADR-2026-06-04-19 N-2).

Salvaged from the retired ``context_bundle.py`` gather (which ADR-19 supersedes).
The one piece of that bundle the agentized naysayer cannot reproduce on its own is
the **complete** list of ADRs the project knows about: an LLM cannot search for an
ADR it does not know exists, so if the proposer frames a thread without mentioning
a conflicting historical ADR, a purely self-fetching naysayer would never look for
it (Einstein, ``T-naysayer-agentization`` msg-421 Obj-2; ACCEPTed in msg-422).

We therefore inject this deterministic, mechanically-parsed §M index into the
naysayer's system prompt on **every summon** — the agent then holds a complete map
and decides independently what to fetch, rather than relying on the proposer's
breadcrumbs. The thread's *own* references are left to the agent to enumerate (it
reads the thread); only the complete index must be supplied deterministically.

This module is the surviving home of the index parser: the ADR-19 N-4 follow-up
removes ``context_bundle.py``, so the parser lives here and ``context_bundle.py``
imports it until then.
"""

from __future__ import annotations

import re
from pathlib import Path

# adr_index.py -> naysayer -> spirrow_mindwire -> src -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]

# A CLAUDE.md §M table row: ``| ADR-2026-05-27-09 (T28) | <title> | <thread> |``
_ADR_INDEX_ROW_RE = re.compile(
    r"^\|\s*(ADR-\d{4}-\d{2}-\d{2}-\d+)[^|]*\|\s*([^|]+?)\s*\|", re.MULTILINE
)


def parse_adr_index(claude_md: str) -> tuple[tuple[str, str], ...]:
    """Parse the CLAUDE.md §M ADR table into ``(adr_id, title)`` rows (deduped, sorted)."""
    seen: dict[str, str] = {}
    for adr_id, title in _ADR_INDEX_ROW_RE.findall(claude_md):
        seen.setdefault(adr_id, title.strip())
    return tuple(sorted(seen.items()))


def _read_claude_md(repo_root: Path) -> str | None:
    """Read ``CLAUDE.md`` from ``repo_root``; ``None`` if it is absent/unreadable."""
    try:
        return (repo_root / "CLAUDE.md").read_text(encoding="utf-8")
    except OSError:
        return None


def build_adr_index_block(repo_root: Path | None = None) -> str:
    """Render the injectable all-ADR index block (deterministic, complete).

    Reads ``CLAUDE.md`` §M from ``repo_root`` (the reviewed repo; defaults to this
    repo) and renders the index plus the cross-check instruction. When no §M index
    is available the block says so **explicitly** rather than silently omitting it,
    so a missing index is visible to the reviewer (and auditable) instead of a
    silently narrowed worldview.
    """
    root = repo_root if repo_root is not None else _REPO_ROOT
    claude_md = _read_claude_md(root)
    index = parse_adr_index(claude_md) if claude_md else ()
    rows = (
        "\n".join(f"- {adr_id} — {title}" for adr_id, title in index)
        if index
        else "(no ADR index available — CLAUDE.md §M not found in the reviewed repo)"
    )
    return (
        "## All-ADR index (titles only, from CLAUDE.md §M) — deterministic, complete\n"
        "This is the COMPLETE list of ADRs the project knows about, injected on every "
        "summon so your review is not bounded by what this thread happens to cite. "
        "Enumerate the ADRs/docs the thread DOES reference, cross-check the design under "
        "review against the full list below, and flag any relevant ADR the discussion "
        "never referenced — you cannot search for an ADR you do not know exists:\n"
        f"{rows}"
    )


__all__ = ["build_adr_index_block", "parse_adr_index"]
