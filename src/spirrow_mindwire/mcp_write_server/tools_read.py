"""The 2 read tools exposed for the claude.ai-participant audience.

Feature 3-C (chatroom ``T-feat3-read-overview`` msg-136 = integrator
decide, trilateral-convergent + user final approval 2026-05-16; GitHub
tracker Issue #48). The dogfooding harvest localised the residual relay
friction to a single path: claude.ai-side has no way to *read* a thread
back, so the operator hand-pastes claude-code's reply into Claude
Desktop. These two tools close that path:

- :meth:`ReadTools.list_threads` — enumerate threads with filter /
  pagination so claude.ai can find "my active thread" without an
  out-of-band thread_id paste (= ``mindwire_list_threads``, §3.1 of
  ``docs/mcp-interface.md``).
- :meth:`ReadTools.get_thread` — fetch a thread's meta + messages (the
  relay body itself), with an optional seq slice for incremental reads
  (= ``mindwire_get_thread``, §3.2).

**Scope = 2 tool minimal** (msg-136 論点 2). The §3 spec also defines
``mindwire_get_events`` / ``mindwire_status``; those have no
claude.ai-participant driver (event-log polling is a Connector concern,
status metrics an Operator concern). They stay spec-only until a
Phase 2+ Connector / Operator driver emerges, at which point they
activate *additively* on this same surface — no rename, no spec
re-scope. This is the same "driver-absent ⇒ not-adding is the
YAGNI-aligned choice" frame as ``tools_write`` 's deferred 4th tool.

**Signature SOT** = ``docs/mcp-interface.md`` §3.1 / §3.2, reused 100%
(msg-136 論点 5). The only additive spec change F3-C makes is
``ThreadSummary.awaiting_from: string | null`` (a return field — the §3
``status_filter`` example was written before Feature 2 split
``awaiting_from`` into its own meta field, so turn-state was genuinely
unrepresentable in the listing). The mooted ``awaiting_from_filter``
*input* filter is deferred: at dogfooding scale a participant has a
handful of threads, so client-side filtering of the returned
``awaiting_from`` is sufficient and a server-side filter would be
speculative surface.

**Audience grouping** (msg-136 論点 1 + 論点 4): these read tools ride
on the same server process as the write tools because the audience —
claude.ai-participant — is identical. A single audience legitimately
needing both read and write is exactly why the server is renamed from
``mindwire-write`` to ``mindwire-participant``: the api-key boundary is
re-framed from the *contract* axis (write) to the *audience* axis
(participant), which is what restores the D2-3 "no scope-based access
control needed" property (there is no read-only third party).

**Race contract** (msg-136 論点 5, ``docs/mcp-interface.md`` §5): reads
are a best-effort snapshot. A read concurrent with a watcher /
write-tool write on the same thread can observe a stale-but-complete
state (``atomic_write_text`` 's ``*.tmp`` → ``os.replace`` guarantees
the loader never sees a torn file — see ``loader`` module docstring).
The next call converges. Transactional reads are out of scope; the
participant relay use case does not need them. These tools therefore
perform **zero file writes** — they never call ``atomic_write_text`` /
``os.replace`` / ``toggle_awaiting_from`` — so the sub-PR 3 startup
race-gap metric is unaffected (review §3b non-interference check).

**Error model** ([[feedback_trust_llm_for_tool_errors]]): user-actionable
failures (bad ULID, nonexistent thread, ``created_after`` >
``created_before``, ``message_seq_from`` > ``message_seq_to``, bad
pagination) raise :class:`ToolError` with a verbatim message; FastMCP
returns ``isError=True`` and the LLM decides how to react. Internal
filesystem faults propagate as 500-level errors so operators are
alerted.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from spirrow_mindwire._time import iso_z
from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.schema import Message, ThreadMeta
from spirrow_mindwire.watcher.loader import load_messages, load_thread_meta

logger = logging.getLogger(__name__)

READ_API_SCHEMA_VERSION = 1
"""Version of the *read-API return envelope* (``docs/mcp-interface.md``
§5.4: every return carries ``schema_version: int``).

INDEPENDENT from the per-resource on-disk schema versions
(:data:`spirrow_mindwire.schema._common.SCHEMA_VERSION` for ThreadMeta,
``Message`` / ``_BaseEvent`` for their own files). This number versions
the *shape of the dict these tools return*; a backward-compatible
additive change (a new field, e.g. ``ThreadSummary.awaiting_from`` in
F3-C) holds the version, a breaking change bumps it (§5.4 policy).
"""

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
DEFAULT_EVENT_LIMIT = 1000  # reserved for future get_events; unused here.


def _parse_ts_filter(value: str, *, field: str) -> datetime:
    """Parse a UTC ISO-8601 filter argument into an aware datetime.

    Accepts the canonical ``...Z`` form (architecture.md §3) as well as
    explicit ``+00:00`` offsets. A naive value is treated as UTC. A
    value that doesn't parse is a caller error → :class:`ToolError`.
    """
    raw = value.strip()
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError as e:
        raise ToolError(
            f"invalid_argument: {field}={value!r} is not a valid UTC ISO 8601 timestamp"
        ) from e
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class ReadTools:
    """Bundle of the 2 read tool handlers, bound to a ``data_dir``.

    Constructed once at server startup with ``settings.paths.data_dir``
    and shared across all incoming MCP requests. Stateless beyond the
    immutable ``data_dir`` (reads hold no locks — the best-effort
    snapshot race contract makes per-thread serialisation unnecessary,
    unlike :class:`~spirrow_mindwire.mcp_write_server.tools_write.WriteTools`).
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def _layout(self, thread_id: str) -> ThreadDirLayout:
        """Build a :class:`ThreadDirLayout`, surfacing bad IDs as :class:`ToolError`."""
        try:
            return ThreadDirLayout(base_dir=self._data_dir, thread_id=thread_id)
        except ValueError as e:
            raise ToolError(f"invalid_argument: {e}") from e

    # ------------------------------------------------------------------
    # list_threads
    # ------------------------------------------------------------------

    async def list_threads(
        self,
        status_filter: list[str] | None = None,
        tag_filter: list[str] | None = None,
        participant_filter: list[str] | None = None,
        id_filter: list[str] | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        include_archived: bool = False,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Enumerate threads with OR filters + inclusive date bounds + pagination.

        Filter semantics (``docs/mcp-interface.md`` §5.2): ``*_filter``
        args are OR (match if the thread overlaps any element);
        ``created_after`` / ``created_before`` are inclusive (≥ / ≤).
        ``archived`` threads are excluded unless ``include_archived`` is
        true or ``"archived"`` is explicitly in ``status_filter``
        (§3.1: "省略時 archived 除く全 status").

        Returns ``{schema_version, items: [ThreadSummary], total,
        limit, offset}`` where ``total`` is the post-filter count
        *before* pagination.
        """
        if limit < 1 or limit > MAX_LIMIT:
            raise ToolError(f"invalid_argument: limit must be in [1, {MAX_LIMIT}], got {limit}")
        if offset < 0:
            raise ToolError(f"invalid_argument: offset must be >= 0, got {offset}")

        after = _parse_ts_filter(created_after, field="created_after") if created_after else None
        before = (
            _parse_ts_filter(created_before, field="created_before") if created_before else None
        )
        if after is not None and before is not None and after > before:
            raise ToolError(
                "invalid_argument: created_after must be <= created_before "
                f"({created_after!r} > {created_before!r})"
            )

        # Validate id_filter ULIDs eagerly so a malformed id is a clear
        # caller error rather than a silent no-match (§3.1 error contract).
        id_set: set[str] | None = None
        if id_filter is not None:
            for tid in id_filter:
                try:
                    ThreadDirLayout(base_dir=self._data_dir, thread_id=tid)
                except ValueError as e:
                    raise ToolError(f"invalid_argument: id_filter entry {tid!r}: {e}") from e
            id_set = set(id_filter)

        status_set = set(status_filter) if status_filter is not None else None
        tag_set = set(tag_filter) if tag_filter is not None else None
        participant_set = set(participant_filter) if participant_filter is not None else None
        archived_allowed = include_archived or (status_set is not None and "archived" in status_set)

        summaries: list[dict[str, Any]] = []
        for thread_id, meta, layout in self._iter_thread_metas():
            if id_set is not None and thread_id not in id_set:
                continue
            if meta.status == "archived" and not archived_allowed:
                continue
            if status_set is not None and meta.status not in status_set:
                continue
            if tag_set is not None and not (set(meta.tags) & tag_set):
                continue
            if participant_set is not None and not (set(meta.participants) & participant_set):
                continue
            if after is not None and meta.created_at < after:
                continue
            if before is not None and meta.created_at > before:
                continue
            summaries.append(_thread_summary(meta, layout))

        # Deterministic order: creation time, ULID tiebreak (ULIDs are
        # lexicographically time-ordered, so this is stable across calls).
        summaries.sort(key=lambda s: (s["created_at"], s["thread_id"]))
        total = len(summaries)
        page = summaries[offset : offset + limit]
        return {
            "schema_version": READ_API_SCHEMA_VERSION,
            "items": page,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def _iter_thread_metas(
        self,
    ) -> Iterator[tuple[str, ThreadMeta, ThreadDirLayout]]:
        """Yield ``(thread_id, ThreadMeta, layout)`` for every valid thread dir.

        Net-new enumeration: ``loader`` only has single-thread
        primitives, so this walks ``threads/`` directly — same iterdir
        skip rules as :func:`startup_full_scan` /
        :func:`migrate_data_dir` (skip non-dirs, dot-dirs like
        ``.staging-<ULID>``, non-ULID names, dirs without ``meta.yaml``).
        A meta.yaml that fails to parse *or* fails schema validation is
        logged at WARNING and skipped rather than failing the whole
        listing (one corrupt thread must not blind the participant to
        every other thread). Note ``pydantic.ValidationError`` is not a
        ``ValueError`` subclass in pydantic v2 (it is a Rust-backed
        ``pydantic_core`` exception), so it is caught explicitly.

        No ``sorted()`` here: :meth:`list_threads` re-sorts the
        projected summaries by ``(created_at, thread_id)``, so ordering
        the iterdir would be dead work.
        """
        threads_root = self._data_dir / "threads"
        if not threads_root.is_dir():
            return
        for thread_dir in threads_root.iterdir():
            if not thread_dir.is_dir() or thread_dir.name.startswith("."):
                continue
            try:
                layout = ThreadDirLayout(base_dir=self._data_dir, thread_id=thread_dir.name)
            except ValueError:
                continue
            if not layout.meta_path.is_file():
                continue
            try:
                meta = load_thread_meta(layout)
            except (OSError, ValueError, yaml.YAMLError, ValidationError) as e:
                logger.warning(
                    "list_threads: skipping %s (meta.yaml unreadable: %s)",
                    thread_dir.name,
                    e,
                )
                continue
            yield thread_dir.name, meta, layout

    # ------------------------------------------------------------------
    # get_thread
    # ------------------------------------------------------------------

    async def get_thread(
        self,
        thread_id: str,
        include_messages: bool = True,
        message_seq_from: int | None = None,
        message_seq_to: int | None = None,
    ) -> dict[str, Any]:
        """Fetch one thread's full meta + (optionally sliced) messages.

        ``message_seq_from`` / ``message_seq_to`` are both inclusive
        (§5.2). ``include_messages=False`` returns ``messages: null``
        (lightweight meta-only fetch). Errors: nonexistent thread
        (``thread_not_found``), bad ULID or ``message_seq_from >
        message_seq_to`` (``invalid_argument``).
        """
        if (
            message_seq_from is not None
            and message_seq_to is not None
            and message_seq_from > message_seq_to
        ):
            raise ToolError(
                "invalid_argument: message_seq_from must be <= message_seq_to "
                f"({message_seq_from} > {message_seq_to})"
            )
        layout = self._layout(thread_id)
        if not layout.thread_dir.is_dir() or not layout.meta_path.is_file():
            raise ToolError(
                f"thread_not_found: thread {thread_id!r} does not exist under {self._data_dir}"
            )
        meta = load_thread_meta(layout)

        messages: list[dict[str, Any]] | None
        if not include_messages:
            messages = None
        else:
            msgs = load_messages(layout)
            if message_seq_from is not None:
                msgs = [m for m in msgs if m.seq >= message_seq_from]
            if message_seq_to is not None:
                msgs = [m for m in msgs if m.seq <= message_seq_to]
            messages = [_message_dict(m) for m in msgs]

        return {
            "schema_version": READ_API_SCHEMA_VERSION,
            "thread": _thread_detail(meta),
            "messages": messages,
        }


def _thread_detail(meta: ThreadMeta) -> dict[str, Any]:
    """Project ThreadMeta to the ``ThreadDetail`` shape (§3.2, meta.yaml-equivalent).

    Returns the full meta surface (a superset of the §3.2 enumerated
    fields — ``awaiting_from`` / ``retry_count`` / terminated audit
    fields included), since §3.2 describes it as "meta.yaml フル相当".
    Timestamps use the ``Z`` suffix (architecture.md §3) via
    :func:`iso_z`.
    """
    return {
        "schema_version": meta.schema_version,
        "thread_id": meta.thread_id,
        "title": meta.title,
        "status": meta.status,
        "awaiting_from": meta.awaiting_from,
        "participants": list(meta.participants),
        "created_at": iso_z(meta.created_at),
        "updated_at": iso_z(meta.updated_at),
        "tags": list(meta.tags),
        "retry_count": meta.retry_count,
        "terminated_reason": meta.terminated_reason,
        "terminated_at": iso_z(meta.terminated_at) if meta.terminated_at is not None else None,
    }


def _thread_summary(meta: ThreadMeta, layout: ThreadDirLayout) -> dict[str, Any]:
    """Project ThreadMeta to the ``ThreadSummary`` shape (§3.1).

    ``awaiting_from`` is the F3-C additive return field (``string |
    null`` — ``None`` in terminal states). ``message_count`` is a
    fresh on-disk count (O(n) per thread; fine at dogfooding scale).
    """
    return {
        "thread_id": meta.thread_id,
        "title": meta.title,
        "status": meta.status,
        "awaiting_from": meta.awaiting_from,
        "participants": list(meta.participants),
        "created_at": iso_z(meta.created_at),
        "updated_at": iso_z(meta.updated_at),
        "tags": list(meta.tags),
        "message_count": len(load_messages(layout)),
    }


def _message_dict(msg: Message) -> dict[str, Any]:
    """Project a :class:`~spirrow_mindwire.schema.Message` to the §3.2 shape.

    Uses the on-disk ``from`` key (not the Python ``from_`` attribute)
    and the ``Z``-suffixed timestamp form.
    """
    return {
        "schema_version": msg.schema_version,
        "msg_id": msg.msg_id,
        "seq": msg.seq,
        "from": msg.from_,
        "to": msg.to,
        "created_at": iso_z(msg.created_at),
        "reply_to": msg.reply_to,
        "body": msg.body,
    }


def register_read_tools(fastmcp: FastMCP, tools: ReadTools) -> None:
    """Register the 2 read tools on the FastMCP server.

    Tool names are the ``docs/mcp-interface.md`` §3 SOT names verbatim
    (``mindwire_list_threads`` / ``mindwire_get_thread``, msg-136 論点 5
    = signature/naming 100% reuse), distinct from the write tools'
    bare names on the same server.
    """

    @fastmcp.tool(
        name="mindwire_list_threads",
        description=(
            "List threads with OR filters and pagination. All filter "
            "args (status_filter / tag_filter / participant_filter / "
            "id_filter) are OR; created_after / created_before are "
            "inclusive UTC ISO 8601 bounds. Archived threads are "
            "excluded unless include_archived=true. Returns "
            "{schema_version, items:[ThreadSummary], total, limit, "
            "offset}; each ThreadSummary includes awaiting_from "
            "(string|null) so the caller can find its own turn without "
            "an extra get_thread call."
        ),
    )
    async def mindwire_list_threads(
        status_filter: list[str] | None = None,
        tag_filter: list[str] | None = None,
        participant_filter: list[str] | None = None,
        id_filter: list[str] | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        include_archived: bool = False,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        return await tools.list_threads(
            status_filter=status_filter,
            tag_filter=tag_filter,
            participant_filter=participant_filter,
            id_filter=id_filter,
            created_after=created_after,
            created_before=created_before,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )

    @fastmcp.tool(
        name="mindwire_get_thread",
        description=(
            "Fetch one thread's full meta + messages. Pass `thread_id` "
            "(a ULID). `include_messages=false` returns messages:null "
            "(meta-only). `message_seq_from` / `message_seq_to` are "
            "both inclusive for incremental relay reads. Errors: "
            "thread_not_found (no such thread) or invalid_argument "
            "(bad ULID, message_seq_from > message_seq_to)."
        ),
    )
    async def mindwire_get_thread(
        thread_id: str,
        include_messages: bool = True,
        message_seq_from: int | None = None,
        message_seq_to: int | None = None,
    ) -> dict[str, Any]:
        return await tools.get_thread(
            thread_id,
            include_messages=include_messages,
            message_seq_from=message_seq_from,
            message_seq_to=message_seq_to,
        )


__all__ = ["READ_API_SCHEMA_VERSION", "ReadTools", "register_read_tools"]
