"""Deterministic design-thread context bundle (ADR-2026-06-03-17 N-1③ / N-5 / N-6).

The design-time twin of the PR-review's "raw diff" input: given
``(project, thread_id, decision_msg)`` this gathers the context the independent
naysayer reviews. The gather is a **deterministic function** of the thread
content + in-repo files (N-5): the launching session never hand-picks what the
naysayer sees, which is what keeps the independence real ("author must not
curate the context"). A :class:`BundleManifest` records exactly what was
gathered so a Tier C reviewer can audit it (N-5 / msg-404 指摘3).

What goes in (all mechanical, no human selection):

* the **full thread** (every message), with an N-6 size guard: structural
  messages (``propose`` / ``decide`` / ``handoff``) and the named decision
  message are kept whole; other messages are capped — preserving the decisions
  while bounding the prompt so a long thread does not blow the Gemini context
  window / timeout (msg-404 指摘A);
* every **ADR / local-doc reference** found in the thread, extracted by regex
  (complete, not proposer-chosen — msg-404 指摘2). Local ``spec/`` & ``docs/``
  files are inlined (capped); ADR bodies live in spirrow-docs/Drive and are
  **not** in this repo, so their IDs are listed for the naysayer's
  "did the design overlook ADR-X?" check rather than inlined (MVP limitation);
* an **all-ADR title index** parsed from ``CLAUDE.md`` §M, so the naysayer can
  flag a relevant ADR the discussion never referenced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..magickit.client import McpToolCaller
from .adr_index import parse_adr_index
from .principles import principles_version

# context_bundle.py -> naysayer -> spirrow_mindwire -> src -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]

# N-6 size guards (deterministic, per-message — no ordering heuristics):
_PER_MSG_CHARS = 4000  # cap for non-structural messages
_LOCAL_DOC_CHARS = 8000  # cap per inlined local doc
_TRUNCATION_MARK = "\n…[truncated for bundle size — see the thread for the full message]"

# Messages whose content is always kept whole (they carry the decisions/handoffs
# the naysayer must judge); everything else is capped at _PER_MSG_CHARS.
_STRUCTURAL_TYPES: frozenset[str] = frozenset({"propose", "decide", "handoff"})

_ADR_RE = re.compile(r"ADR-\d{4}-\d{2}-\d{2}-\d+")
_LOCAL_DOC_RE = re.compile(r"(?:spec|docs)/[A-Za-z0-9._/-]+\.[A-Za-z0-9]+")


@dataclass(frozen=True)
class BundleManifest:
    """Audit record of exactly what the deterministic gather included (N-5)."""

    project: str
    thread_id: str
    decision_msg_id: str | None
    title: str
    principles_version: int
    msg_ids: tuple[str, ...]
    truncated_msg_ids: tuple[str, ...]
    referenced_adrs: tuple[str, ...]
    inlined_docs: tuple[str, ...]
    missing_docs: tuple[str, ...]
    adr_index_count: int

    def as_header(self) -> str:
        """One-block provenance header (posted with the relay for auditability)."""
        trunc = (
            f"{len(self.truncated_msg_ids)} truncated"
            if self.truncated_msg_ids
            else "none truncated"
        )
        return (
            f"bundle manifest — thread={self.thread_id} project={self.project} "
            f"decision={self.decision_msg_id or '(whole thread)'} "
            f"principles_version={self.principles_version}\n"
            f"  messages: {len(self.msg_ids)} ({trunc})\n"
            f"  referenced ADRs: {', '.join(self.referenced_adrs) or '(none)'}\n"
            f"  inlined local docs: {', '.join(self.inlined_docs) or '(none)'}"
            + (
                f"\n  referenced-but-missing docs: {', '.join(self.missing_docs)}"
                if self.missing_docs
                else ""
            )
            + f"\n  all-ADR index entries (CLAUDE.md §M): {self.adr_index_count}"
        )


@dataclass(frozen=True)
class ContextBundle:
    """The naysayer's user-message body + its audit manifest."""

    text: str
    manifest: BundleManifest


def _msg_field(msg: dict[str, Any], key: str, default: str = "") -> str:
    value = msg.get(key, default)
    return value if isinstance(value, str) else default


def extract_references(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Mechanically extract (ADR ids, local-doc paths) from text — complete + sorted.

    Deterministic and exhaustive (every match, deduped, sorted) so the set the
    naysayer sees cannot be narrowed by whoever fires the review (N-5).
    """
    adrs = sorted(set(_ADR_RE.findall(text)))
    docs = sorted(set(_LOCAL_DOC_RE.findall(text)))
    return tuple(adrs), tuple(docs)


def _render_message(msg: dict[str, Any], *, keep_full: bool) -> tuple[str, bool]:
    """Render one message block; returns (text, was_truncated)."""
    msg_id = _msg_field(msg, "msg_id", "(no-id)")
    author = _msg_field(msg, "author", "(unknown)")
    mtype = _msg_field(msg, "type", "(none)")
    reply_to = _msg_field(msg, "reply_to")
    content = _msg_field(msg, "content")
    truncated = False
    if not keep_full and len(content) > _PER_MSG_CHARS:
        content = content[:_PER_MSG_CHARS] + _TRUNCATION_MARK
        truncated = True
    head = f"### {msg_id} [{author} / {mtype}]" + (f" reply_to={reply_to}" if reply_to else "")
    return f"{head}\n{content}", truncated


def _read_local_doc(repo_root: Path, rel_path: str) -> str | None:
    """Read an in-repo doc, capped. Returns None if absent or outside the repo."""
    candidate = (repo_root / rel_path).resolve()
    try:
        candidate.relative_to(repo_root)  # reject path traversal (e.g. ../..)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    text = candidate.read_text(encoding="utf-8")
    if len(text) > _LOCAL_DOC_CHARS:
        text = text[:_LOCAL_DOC_CHARS] + _TRUNCATION_MARK
    return text


async def build_context_bundle(
    mcp: McpToolCaller,
    *,
    project: str,
    thread_id: str,
    decision_msg_id: str | None = None,
    repo_root: Path | None = None,
) -> ContextBundle:
    """Gather the deterministic design-review context bundle for a thread.

    The only I/O is the ``chatroom_get_thread`` MCP call and reading in-repo
    docs; given identical thread content + repo files the output (text +
    manifest) is identical (DoD: determinism).
    """
    root = repo_root if repo_root is not None else _REPO_ROOT
    result = await mcp.call_tool(
        "chatroom_get_thread",
        {"project": project, "thread_id": thread_id, "mode": "full"},
    )
    thread = result.get("thread", {}) if isinstance(result, dict) else {}
    title = thread.get("title", "") if isinstance(thread, dict) else ""
    messages: list[dict[str, Any]] = result.get("messages", []) if isinstance(result, dict) else []

    # Reference extraction runs over the FULL (pre-truncation) text so a ref
    # buried in a long message is never lost to the size guard.
    full_text = "\n".join(_msg_field(m, "content") for m in messages)
    referenced_adrs, referenced_docs = extract_references(full_text)

    rendered: list[str] = []
    msg_ids: list[str] = []
    truncated_ids: list[str] = []
    for msg in messages:
        msg_id = _msg_field(msg, "msg_id", "(no-id)")
        msg_ids.append(msg_id)
        keep_full = _msg_field(msg, "type") in _STRUCTURAL_TYPES or msg_id == decision_msg_id
        block, was_trunc = _render_message(msg, keep_full=keep_full)
        rendered.append(block)
        if was_trunc:
            truncated_ids.append(msg_id)

    inlined: list[str] = []
    missing: list[str] = []
    doc_sections: list[str] = []
    for rel_path in referenced_docs:
        content = _read_local_doc(root, rel_path)
        if content is None:
            missing.append(rel_path)
        else:
            inlined.append(rel_path)
            doc_sections.append(f"### {rel_path}\n{content}")

    claude_md = _read_local_doc(root, "CLAUDE.md")
    adr_index = parse_adr_index(claude_md) if claude_md else ()

    manifest = BundleManifest(
        project=project,
        thread_id=thread_id,
        decision_msg_id=decision_msg_id,
        title=title,
        principles_version=principles_version(),
        msg_ids=tuple(msg_ids),
        truncated_msg_ids=tuple(truncated_ids),
        referenced_adrs=referenced_adrs,
        inlined_docs=tuple(inlined),
        missing_docs=tuple(missing),
        adr_index_count=len(adr_index),
    )

    decision_line = (
        f"You are reviewing the design decision at **{decision_msg_id}** in this thread."
        if decision_msg_id
        else "You are reviewing this design thread as a whole."
    )
    adr_ref_block = (
        "\n".join(f"- {a}" for a in referenced_adrs)
        if referenced_adrs
        else "(no ADRs referenced in the thread)"
    )
    adr_index_block = (
        "\n".join(f"- {adr_id} — {title}" for adr_id, title in adr_index)
        if adr_index
        else "(no ADR index available)"
    )
    docs_block = "\n\n".join(doc_sections) if doc_sections else "(no in-repo docs referenced)"

    text = (
        f"# Design-review context bundle\n"
        f"{manifest.as_header()}\n\n"
        f"{decision_line}\n\n"
        f"## Thread {thread_id} — {title}\n\n"
        + "\n\n".join(rendered)
        + "\n\n## Referenced ADRs (mechanically extracted from the thread)\n"
        "ADR bodies live in spirrow-docs/Drive (not in this repo); IDs are listed so you "
        "can flag whether the design honoured / overlooked them:\n"
        f"{adr_ref_block}\n\n"
        "## All-ADR index (titles only, from CLAUDE.md §M)\n"
        "Use this to flag a relevant ADR the discussion never referenced:\n"
        f"{adr_index_block}\n\n"
        "## Inlined in-repo docs referenced by the thread\n"
        f"{docs_block}\n"
    )
    return ContextBundle(text=text, manifest=manifest)


__all__ = [
    "BundleManifest",
    "ContextBundle",
    "build_context_bundle",
    "extract_references",
    "parse_adr_index",
]
