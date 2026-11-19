"""Cross-thread references as observed facts.

Design: chatroom ``spirrow-mindwire`` thread
``T-thread-relations-unused-so-parallel-rule-cannot-be-checked``, Bohr msg-9729 (v0.4) as
corrected by msg-9731 (Einstein go on that correction); D-1 approved by the human in msg-9691.

What this is, and what it is not
--------------------------------

This module shows which threads *reference* which, and which of them had a PR merged. It does
NOT define which threads are "related" in the sense of the 2026-09-05 rule (D3, "only unrelated
threads may be developed in parallel"): that definition belongs on the Conclair side (D4) and is
left to a later design turn (msg-9544 §2). So the words "related", "undirected" and "depth" are
deliberately absent here; every line keeps its direction (``this → T-x`` / ``T-y → this``) and its
source message. Nothing here stops, delays or reorders a dispatch, and nothing here posts into any
thread (msg-9544 §3): the outputs are a prompt block (D-2) and digest lines (D-3).

The one edge set (msg-9544 §1, Obj-2-1)
--------------------------------------

An :class:`Edge` is ``(from, to)``: ``from`` is the thread whose message carries the reference,
``to`` the thread referenced. :func:`collect_edges` reads EVERY thread of the project, closed ones
included, and every message of each — the ``references_threads`` of each message, plus the
``affects_threads`` of a closed thread. A reference added after the opening message counts the
same as one in it. :func:`thread_references` then takes both ``from == t`` and ``to == t`` from
that same set, so when A references B and A is later closed, B still sees A. Narrowing to open
threads is done by the display side only (the digest lists open threads; the dispatch block shows
every counterpart with its status), never when collecting.

Merged events (msg-9729 §1)
---------------------------

:func:`merged_events` reads a thread's own messages for the line the 1b relay
(:data:`~spirrow_mindwire.pr_event_advance.decide.PR_EVENT_RELAY_AUTHOR`) writes on a merged PR,
``PR <ref> が merge された`` (then the merge commit). No GitHub call. The digest (D-3) and
the dispatch block (D-2) both call it, so they cannot disagree about what was merged.

Merge display limit (msg-9731, verbatim)
----------------------------------------

時間で区切る窓は設けません。そのため、時間が経っただけで表示から消えることはありません。ただし、参照先
1 本につき表示するのは最新 3 件までです。それより新しい merge が 3 件入ると、
古いものは表示から外れます。
これは、プロンプトの量を抑えるための意図した上限です。上限を超えた分まで届けることは保証しません。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .chatroom.status import is_terminal_status
from .github.client import parse_pr_ref
from .pr_event_advance.decide import PR_EVENT_RELAY_AUTHOR

#: Per counterpart, the dispatch block shows at most this many merges, newest first (msg-9731).
MERGED_DISPLAY_LIMIT = 3

#: The merged-fact sentence :func:`~.pr_event_advance.decide.lifecycle_post` writes (row 3).
#: Anchored at a line start so a quote of it inside other text (``> PR …``) is not read.
_MERGED_LINE_RE = re.compile(r"^PR (?P<ref>\S+) が merge された", re.MULTILINE)


@dataclass(frozen=True)
class ThreadSnapshot:
    """What this module needs from one ``chatroom_get_thread(mode="full")`` result."""

    thread_id: str
    status: str
    messages: tuple[Mapping[str, Any], ...] = ()
    affects_threads: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    last_msg_id: str | None = None

    @property
    def is_open(self) -> bool:
        """Not finished (:data:`~.chatroom.status.FINISHED_STATUSES`). Unknown status → open."""
        return not is_terminal_status(self.status)

    @property
    def last_msg_at(self) -> str | None:
        return _text(self.messages[-1].get("timestamp")) if self.messages else None

    @property
    def last_msg_by(self) -> str | None:
        return _text(self.messages[-1].get("author")) if self.messages else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _thread_ids(value: Any) -> tuple[str, ...]:
    """A list of thread ids, tolerantly: absent / ``None`` / non-list → ``()``; non-str dropped."""
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str) and v)


def snapshot_of(result: Any) -> ThreadSnapshot | None:
    """Map a ``chatroom_get_thread(mode="full")`` result; ``None`` when it is not that shape.

    ``status`` and ``affects_threads`` live under ``result["thread"]`` (msg-4749 measurement), the
    messages under ``result["messages"]``. A message without ``references_threads`` simply carries
    no reference.
    """
    if not isinstance(result, Mapping):
        return None
    thread = result.get("thread")
    messages = result.get("messages")
    if not isinstance(thread, Mapping) or not isinstance(messages, list):
        return None
    thread_id = _text(thread.get("thread_id"))
    if thread_id is None:
        return None
    return ThreadSnapshot(
        thread_id=thread_id,
        status=_text(thread.get("status")) or "",
        messages=tuple(m for m in messages if isinstance(m, Mapping)),
        affects_threads=_thread_ids(thread.get("affects_threads")),
        tags=_thread_ids(thread.get("tags")),
        last_msg_id=_text(thread.get("last_msg_id")),
    )


@dataclass(frozen=True, order=True)
class Edge:
    """One reference: ``from_thread`` mentions ``to_thread``. ``msg_id`` is ``None`` for a closed
    thread's ``affects_threads`` entry, which belongs to the thread, not to a message."""

    from_thread: str
    to_thread: str
    msg_id: str | None


def collect_edges(snapshots: Iterable[ThreadSnapshot]) -> frozenset[Edge]:
    """THE edge set of a project. Every thread, every message; closed ``affects_threads`` too.

    A thread naming itself is not an edge. Duplicates collapse because the result is a set.
    """
    edges: set[Edge] = set()
    for snap in snapshots:
        for msg in snap.messages:
            msg_id = _text(msg.get("msg_id"))
            for target in _thread_ids(msg.get("references_threads")):
                if target != snap.thread_id:
                    edges.add(Edge(snap.thread_id, target, msg_id))
        if not snap.is_open:
            for target in snap.affects_threads:
                if target != snap.thread_id:
                    edges.add(Edge(snap.thread_id, target, None))
    return frozenset(edges)


@dataclass(frozen=True)
class Reference:
    """One counterpart of a thread in one direction, with every source message, deduplicated."""

    other: str
    msg_ids: tuple[str, ...]


@dataclass(frozen=True)
class ThreadReferences:
    """``outgoing``: this thread → other. ``incoming``: other → this thread. Each sorted by id."""

    thread_id: str
    outgoing: tuple[Reference, ...]
    incoming: tuple[Reference, ...]

    def counterparts(self) -> tuple[str, ...]:
        """Every other thread on either side, once, sorted."""
        return tuple(sorted({r.other for r in (*self.outgoing, *self.incoming)}))


def _group(pairs: Iterable[tuple[str, str | None]]) -> tuple[Reference, ...]:
    by_other: dict[str, set[str]] = {}
    for other, msg_id in pairs:
        ids = by_other.setdefault(other, set())
        if msg_id is not None:
            ids.add(msg_id)
    return tuple(
        Reference(o, tuple(sorted(by_other[o], key=_msg_sort_key))) for o in sorted(by_other)
    )


def _msg_sort_key(msg_id: str) -> tuple[int, str]:
    tail = msg_id.rsplit("-", 1)[-1]
    return (int(tail), msg_id) if tail.isdigit() else (-1, msg_id)


def thread_references(edges: Iterable[Edge], thread_id: str) -> ThreadReferences:
    """Both directions for ``thread_id`` from ONE edge set (msg-9544 §1). One line per counterpart
    and direction, however many messages carry the reference (msg-9729 §3 test 2)."""
    edge_list = list(edges)
    return ThreadReferences(
        thread_id=thread_id,
        outgoing=_group((e.to_thread, e.msg_id) for e in edge_list if e.from_thread == thread_id),
        incoming=_group((e.from_thread, e.msg_id) for e in edge_list if e.to_thread == thread_id),
    )


@dataclass(frozen=True)
class MergedEvent:
    """A merged-PR fact the 1b relay wrote into a thread. ``at`` is the message timestamp."""

    repo: str
    pr: int
    msg_id: str
    at: str | None

    @property
    def slug(self) -> str:
        return f"{self.repo}#{self.pr}"


def merged_events(messages: Sequence[Mapping[str, Any]]) -> tuple[MergedEvent, ...]:
    """Every merged-PR fact :data:`PR_EVENT_RELAY_AUTHOR` wrote in ``messages``, newest first.

    Only that author's messages are read, so a person quoting the sentence is not an event. The
    same PR reported twice (e.g. a re-run) is kept once, at its newest report.
    """
    seen: set[tuple[str, int]] = set()
    found: list[MergedEvent] = []
    for msg in reversed(messages):
        if msg.get("author") != PR_EVENT_RELAY_AUTHOR:
            continue
        body = msg.get("content")
        if not isinstance(body, str):
            continue
        for match in _MERGED_LINE_RE.finditer(body):
            ref = parse_pr_ref(match.group("ref"))
            if ref is None:
                continue
            key = (f"{ref.owner}/{ref.repo}", ref.number)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                MergedEvent(
                    repo=key[0],
                    pr=ref.number,
                    msg_id=_text(msg.get("msg_id")) or "?",
                    at=_text(msg.get("timestamp")),
                )
            )
    return tuple(found)


_NOTICE_HEADER = "=== Cross-thread references (observed facts) ==="
_NOTICE_FRAMING = (
    "Threads that reference this one, or that this one references, in the same project. These\n"
    "are recorded references and merge facts only: they do not say whether anyone is working on\n"
    "the other thread, they are not a ruling that the threads are related, and they do not stop\n"
    "or change this turn. Read them as context and decide yourself whether they matter."
)


def _ids(ref: Reference) -> str:
    return ", ".join(ref.msg_ids) if ref.msg_ids else "affects_threads"


def _counterpart_facts(
    other: str, snapshots: Mapping[str, ThreadSnapshot]
) -> tuple[str, list[str]]:
    snap = snapshots.get(other)
    if snap is None:
        return "status=unknown last_msg=? by ?", []
    facts = (
        f"status={snap.status or 'unknown'} last_msg={snap.last_msg_at or '?'} "
        f"by {snap.last_msg_by or '?'}"
    )
    merges = [
        f"  merged: {e.slug}（{e.msg_id}, {e.at or '時刻不明'}）"  # noqa: RUF001 (msg-9729 §1 wording)
        for e in merged_events(snap.messages)[:MERGED_DISPLAY_LIMIT]
    ]
    return facts, merges


def render_references_notice(
    refs: ThreadReferences, snapshots: Mapping[str, ThreadSnapshot]
) -> str | None:
    """The D-2 prompt block, or ``None`` when the thread has no reference in either direction.

    Every counterpart is shown, open or closed, with its status: a closed thread's merge is the
    case Obj-2-1 kept visible. Per counterpart at most :data:`MERGED_DISPLAY_LIMIT` merges, newest
    first, with no time window — see the module docstring (msg-9731) for what that does and does
    not guarantee.
    """
    if not refs.outgoing and not refs.incoming:
        return None
    lines: list[str] = []
    for ref in refs.outgoing:
        facts, merges = _counterpart_facts(ref.other, snapshots)
        lines.append(f"参照: このスレッド → {ref.other}（{_ids(ref)}） {facts}")  # noqa: RUF001
        lines.extend(merges)
    for ref in refs.incoming:
        facts, merges = _counterpart_facts(ref.other, snapshots)
        lines.append(f"参照: {ref.other} → このスレッド（{_ids(ref)}） {facts}")  # noqa: RUF001
        lines.extend(merges)
    return "\n".join([_NOTICE_HEADER, _NOTICE_FRAMING, *lines])


def _parse_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def digest_merged_lines(
    snapshots: Sequence[ThreadSnapshot], edges: Iterable[Edge], *, since: datetime
) -> list[str]:
    """D-3: one line per merge reported at or after ``since`` whose thread has an open counterpart.

    ``T-a の PR owner/repo#N merged → 参照のある open スレッド: T-b, T-c``. Rebuilt from the threads
    every time, so it holds no state and writes nothing (msg-9544 §3). A merge whose timestamp is
    missing or unreadable is not "recent" and is left out. ``since`` must be timezone-aware.
    """
    edge_list = list(edges)
    by_id = {s.thread_id: s for s in snapshots}
    lines: list[str] = []
    for snap in sorted(snapshots, key=lambda s: s.thread_id):
        events = [e for e in merged_events(snap.messages) if (t := _parse_at(e.at)) and t >= since]
        if not events:
            continue
        others = thread_references(edge_list, snap.thread_id).counterparts()
        open_others = [o for o in others if o in by_id and by_id[o].is_open]
        if not open_others:
            continue
        targets = ", ".join(open_others)
        lines.extend(
            f"{snap.thread_id} の PR {e.slug} merged → 参照のある open スレッド: {targets}"
            for e in events
        )
    return lines


# --- D-4 candidates (msg-9729 §2 D-4; reused by the D-1 skill step) ------------------------------

#: ``関連: …`` / ``Related: …`` at a line start (after whitespace). A ``>`` quote does not match.
_RELATED_LINE_RE = re.compile(r"^\s*(?:関連|Related)\s*[:：]\s*(?P<rest>.*)$", re.MULTILINE)  # noqa: RUF001
_THREAD_ID_RE = re.compile(r"(?<![A-Za-z0-9_-])T-[A-Za-z0-9_-]*[A-Za-z0-9_]")
#: A tag of the ``T229`` kind (the project's work-item number), compared case-insensitively.
_ITEM_TAG_RE = re.compile(r"^T\d+$", re.IGNORECASE)
#: What a ``Related:`` line says when the thread was checked and nothing was found.
_NONE_VALUES = frozenset({"none", "なし", "無し"})


def _related_lines(snap: ThreadSnapshot) -> list[tuple[str | None, str]]:
    out: list[tuple[str | None, str]] = []
    for msg in snap.messages:
        body = msg.get("content")
        if isinstance(body, str):
            out.extend(
                (_text(msg.get("msg_id")), m.group("rest").strip())
                for m in _RELATED_LINE_RE.finditer(body)
            )
    return out


def related_none_msg(snap: ThreadSnapshot) -> str | None:
    """The newest message carrying ``Related: none`` (checked, nothing found), or ``None``."""
    found = [mid or "?" for mid, rest in _related_lines(snap) if rest.lower() in _NONE_VALUES]
    return found[-1] if found else None


@dataclass(frozen=True)
class Candidate:
    """One possible counterpart of a thread, and why. ``referenced`` = already an edge."""

    other: str
    reasons: tuple[str, ...]
    referenced: bool


def reference_candidates(
    snapshots: Sequence[ThreadSnapshot], edges: Iterable[Edge], thread_id: str
) -> tuple[Candidate, ...]:
    """Candidates for ``thread_id`` among the project's OPEN threads, sorted by id.

    Sources (msg-9729 D-4): a shared ``T###`` tag, a ``関連:`` / ``Related:`` line in either thread
    naming the other, and an existing reference in either direction. Output only: nothing is
    written anywhere. Closed threads are not offered, because a declaration is made against open
    work (msg-9521 P-1).
    """
    by_id = {s.thread_id: s for s in snapshots}
    me = by_id.get(thread_id)
    reasons: dict[str, list[str]] = {}

    def add(other: str, reason: str) -> None:
        if other != thread_id and other in by_id and by_id[other].is_open:
            bucket = reasons.setdefault(other, [])
            if reason not in bucket:
                bucket.append(reason)

    referenced = set(thread_references(edges, thread_id).counterparts())
    for other in sorted(referenced):
        add(other, "reference")
    if me is not None:
        my_tags = {t.upper() for t in me.tags if _ITEM_TAG_RE.match(t)}
        for snap in snapshots:
            their = {t.upper() for t in snap.tags if _ITEM_TAG_RE.match(t)}
            for tag in sorted(my_tags & their):
                add(snap.thread_id, f"tag:{tag}")
        for _mid, rest in _related_lines(me):
            for tid in _THREAD_ID_RE.findall(rest):
                add(tid, "related-line:this")
    for snap in snapshots:
        if snap.thread_id == thread_id:
            continue
        for _mid, rest in _related_lines(snap):
            if thread_id in _THREAD_ID_RE.findall(rest):
                add(snap.thread_id, "related-line:other")
    return tuple(Candidate(o, tuple(reasons[o]), o in referenced) for o in sorted(reasons))


# --- the per-tick cache and the D-2 index ------------------------------------------------------


def slim_message(msg: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a message this module reads, so the per-tick cache stays small.

    ``content`` keeps only the lines read here: the relay's merged line and ``関連:`` /
    ``Related:`` lines. The rest of the body is dropped.
    """
    body = msg.get("content")
    kept: list[str] = []
    if isinstance(body, str):
        relay = msg.get("author") == PR_EVENT_RELAY_AUTHOR
        kept = [
            line
            for line in body.splitlines()
            if _RELATED_LINE_RE.match(line) or (relay and _MERGED_LINE_RE.match(line))
        ]
    out: dict[str, Any] = {
        "msg_id": _text(msg.get("msg_id")),
        "author": _text(msg.get("author")),
        "timestamp": _text(msg.get("timestamp")),
        "content": "\n".join(kept),
    }
    refs = _thread_ids(msg.get("references_threads"))
    if refs:
        out["references_threads"] = list(refs)
    return out


def snapshot_to_json(snap: ThreadSnapshot) -> dict[str, Any]:
    """The cache form of a snapshot (messages slimmed by :func:`slim_message`)."""
    return {
        "thread_id": snap.thread_id,
        "status": snap.status,
        "last_msg_id": snap.last_msg_id,
        "tags": list(snap.tags),
        "affects_threads": list(snap.affects_threads),
        "messages": [slim_message(m) for m in snap.messages],
    }


def snapshot_from_json(raw: Any) -> ThreadSnapshot | None:
    """Inverse of :func:`snapshot_to_json`; ``None`` for anything that is not that shape."""
    if not isinstance(raw, Mapping):
        return None
    thread_id = _text(raw.get("thread_id"))
    messages = raw.get("messages")
    if thread_id is None or not isinstance(messages, list):
        return None
    return ThreadSnapshot(
        thread_id=thread_id,
        status=_text(raw.get("status")) or "",
        messages=tuple(m for m in messages if isinstance(m, Mapping)),
        affects_threads=_thread_ids(raw.get("affects_threads")),
        tags=_thread_ids(raw.get("tags")),
        last_msg_id=_text(raw.get("last_msg_id")),
    )


#: Version of the index file the sweep writes and the conductor reads.
INDEX_SCHEMA = 1


def build_notice_index(
    projects: Mapping[str, Sequence[ThreadSnapshot]], *, generated_at: datetime
) -> dict[str, Any]:
    """The D-2 index: per project, per thread, its rendered notice (threads without one omitted).

    Built once per tick by ``scripts/thread_references.py`` from ONE edge set per project, so
    every dispatch of the tick reads the same facts (msg-9729: the read happens once per tick).
    """
    out: dict[str, dict[str, str]] = {}
    for project, snaps in projects.items():
        edges = collect_edges(snaps)
        by_id = {s.thread_id: s for s in snaps}
        notices: dict[str, str] = {}
        for snap in snaps:
            notice = render_references_notice(thread_references(edges, snap.thread_id), by_id)
            if notice is not None:
                notices[snap.thread_id] = notice
        out[project] = notices
    return {"schema": INDEX_SCHEMA, "generated_at": generated_at.isoformat(), "projects": out}


def notice_from_index(index: Any, project: str, thread_id: str) -> str | None:
    """The notice for one thread from a loaded index, stamped with when it was observed.

    Anything unexpected (wrong schema, missing project, non-string) is ``None``: this block is
    observation only, so a bad index costs the block, never the dispatch.
    """
    if not isinstance(index, Mapping) or index.get("schema") != INDEX_SCHEMA:
        return None
    projects = index.get("projects")
    if not isinstance(projects, Mapping):
        return None
    notices = projects.get(project)
    if not isinstance(notices, Mapping):
        return None
    notice = notices.get(thread_id)
    if not isinstance(notice, str) or not notice:
        return None
    generated_at = _text(index.get("generated_at")) or "?"
    return f"{notice}\n(observed at {generated_at})"


__all__ = [
    "INDEX_SCHEMA",
    "MERGED_DISPLAY_LIMIT",
    "Candidate",
    "Edge",
    "MergedEvent",
    "Reference",
    "ThreadReferences",
    "ThreadSnapshot",
    "build_notice_index",
    "collect_edges",
    "digest_merged_lines",
    "merged_events",
    "notice_from_index",
    "reference_candidates",
    "related_none_msg",
    "render_references_notice",
    "slim_message",
    "snapshot_from_json",
    "snapshot_of",
    "snapshot_to_json",
    "thread_references",
]
