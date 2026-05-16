"""Tests for ``spirrow_mindwire.mcp_write_server.tools_read`` (Feature 3-C).

The 2 claude.ai-participant read tools (``mindwire_list_threads`` /
``mindwire_get_thread``, chatroom ``T-feat3-read-overview`` msg-136).
Mirrors the structure of ``test_mcp_write_server.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from factories import seed_thread_meta, write_message_file
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from ulid import ULID

from spirrow_mindwire.config import MindwireSettings
from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.mcp_write_server.http import build_app
from spirrow_mindwire.mcp_write_server.tools_read import (
    READ_API_SCHEMA_VERSION,
    ReadTools,
    register_read_tools,
)
from spirrow_mindwire.schema import Participant

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ULID_B = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
ULID_C = "01ARZ3NDEKTSV4RRFFQ69G5FC1"


@pytest.fixture
def read_tools(tmp_path: Path) -> ReadTools:
    """A :class:`ReadTools` rooted at ``tmp_path`` (= data_dir)."""
    return ReadTools(data_dir=tmp_path)


def _layout(tmp_path: Path, thread_id: str) -> ThreadDirLayout:
    return ThreadDirLayout(base_dir=tmp_path, thread_id=thread_id)


# ---------------------------------------------------------------------------
# list_threads
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_threads_empty_data_dir(read_tools: ReadTools) -> None:
    result = await read_tools.list_threads()
    assert result == {
        "schema_version": READ_API_SCHEMA_VERSION,
        "items": [],
        "total": 0,
        "limit": 100,
        "offset": 0,
    }


@pytest.mark.anyio
async def test_list_threads_summary_shape(read_tools: ReadTools, tmp_path: Path) -> None:
    """ThreadSummary carries the F3-C additive awaiting_from + message_count."""
    layout = _layout(tmp_path, ULID_A)
    seed_thread_meta(layout, title="t1", awaiting_from="claude.ai", tags=["x"])
    write_message_file(layout, seq=1, sender="claude.ai", atomic=False)
    write_message_file(layout, seq=2, sender="claude-code", atomic=False)

    result = await read_tools.list_threads()
    assert result["total"] == 1
    (summary,) = result["items"]
    assert summary == {
        "thread_id": ULID_A,
        "title": "t1",
        "status": "active",
        "awaiting_from": "claude.ai",
        "participants": ["claude.ai", "claude-code"],
        "created_at": "2026-05-07T08:43:07Z",
        "updated_at": "2026-05-07T08:43:07Z",
        "tags": ["x"],
        "message_count": 2,
    }


@pytest.mark.anyio
async def test_list_threads_awaiting_from_null_in_terminal(
    read_tools: ReadTools, tmp_path: Path
) -> None:
    """awaiting_from is null (None) for terminal states (string|null spec)."""
    layout = _layout(tmp_path, ULID_A)
    seed_thread_meta(layout, status="resolved", awaiting_from=None)
    result = await read_tools.list_threads()
    assert result["items"][0]["awaiting_from"] is None


@pytest.mark.anyio
async def test_list_threads_status_filter_or(read_tools: ReadTools, tmp_path: Path) -> None:
    seed_thread_meta(_layout(tmp_path, ULID_A), status="active", awaiting_from="claude-code")
    seed_thread_meta(_layout(tmp_path, ULID_B), status="retrying", awaiting_from="claude-code")
    seed_thread_meta(_layout(tmp_path, ULID_C), status="resolved", awaiting_from=None)

    result = await read_tools.list_threads(status_filter=["active", "retrying"])
    ids = {s["thread_id"] for s in result["items"]}
    assert ids == {ULID_A, ULID_B}
    assert result["total"] == 2


@pytest.mark.anyio
async def test_list_threads_archived_excluded_by_default(
    read_tools: ReadTools, tmp_path: Path
) -> None:
    seed_thread_meta(_layout(tmp_path, ULID_A), status="active", awaiting_from="claude-code")
    seed_thread_meta(_layout(tmp_path, ULID_B), status="archived", awaiting_from=None)

    default = await read_tools.list_threads()
    assert {s["thread_id"] for s in default["items"]} == {ULID_A}

    with_archived = await read_tools.list_threads(include_archived=True)
    assert {s["thread_id"] for s in with_archived["items"]} == {ULID_A, ULID_B}

    # Explicit status_filter=["archived"] is also honoured as intent.
    explicit = await read_tools.list_threads(status_filter=["archived"])
    assert {s["thread_id"] for s in explicit["items"]} == {ULID_B}


@pytest.mark.anyio
async def test_list_threads_tag_and_participant_filter_or(
    read_tools: ReadTools, tmp_path: Path
) -> None:
    seed_thread_meta(_layout(tmp_path, ULID_A), tags=["alpha"], awaiting_from="claude-code")
    seed_thread_meta(_layout(tmp_path, ULID_B), tags=["beta"], awaiting_from="claude-code")

    by_tag = await read_tools.list_threads(tag_filter=["beta", "gamma"])
    assert {s["thread_id"] for s in by_tag["items"]} == {ULID_B}

    # Both seeded threads have the default 2 participants.
    by_part = await read_tools.list_threads(participant_filter=["claude.ai"])
    assert {s["thread_id"] for s in by_part["items"]} == {ULID_A, ULID_B}
    none = await read_tools.list_threads(participant_filter=["nobody"])
    assert none["items"] == []


@pytest.mark.anyio
async def test_list_threads_id_filter_in(read_tools: ReadTools, tmp_path: Path) -> None:
    seed_thread_meta(_layout(tmp_path, ULID_A), awaiting_from="claude-code")
    seed_thread_meta(_layout(tmp_path, ULID_B), awaiting_from="claude-code")
    result = await read_tools.list_threads(id_filter=[ULID_B])
    assert {s["thread_id"] for s in result["items"]} == {ULID_B}


@pytest.mark.anyio
async def test_list_threads_id_filter_bad_ulid_raises(read_tools: ReadTools) -> None:
    with pytest.raises(ToolError, match="invalid_argument: id_filter entry"):
        await read_tools.list_threads(id_filter=["not-a-ulid"])


@pytest.mark.anyio
async def test_list_threads_created_bounds_inclusive(read_tools: ReadTools, tmp_path: Path) -> None:
    seed_thread_meta(
        _layout(tmp_path, ULID_A),
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-01T00:00:00Z",
        awaiting_from="claude-code",
    )
    seed_thread_meta(
        _layout(tmp_path, ULID_B),
        created_at="2026-05-10T00:00:00Z",
        updated_at="2026-05-10T00:00:00Z",
        awaiting_from="claude-code",
    )
    # created_after is inclusive (>=).
    after = await read_tools.list_threads(created_after="2026-05-10T00:00:00Z")
    assert {s["thread_id"] for s in after["items"]} == {ULID_B}
    # created_before is inclusive (<=).
    before = await read_tools.list_threads(created_before="2026-05-01T00:00:00Z")
    assert {s["thread_id"] for s in before["items"]} == {ULID_A}


@pytest.mark.anyio
async def test_list_threads_created_after_gt_before_raises(read_tools: ReadTools) -> None:
    with pytest.raises(ToolError, match="created_after must be <= created_before"):
        await read_tools.list_threads(
            created_after="2026-05-10T00:00:00Z",
            created_before="2026-05-01T00:00:00Z",
        )


@pytest.mark.anyio
async def test_list_threads_bad_timestamp_raises(read_tools: ReadTools) -> None:
    with pytest.raises(ToolError, match="not a valid UTC ISO 8601"):
        await read_tools.list_threads(created_after="yesterday")


@pytest.mark.anyio
async def test_list_threads_pagination(read_tools: ReadTools, tmp_path: Path) -> None:
    # ULIDs sort lexicographically; created_at is identical so the
    # thread_id tiebreak gives a deterministic order A < B < C.
    for tid in (ULID_A, ULID_B, ULID_C):
        seed_thread_meta(_layout(tmp_path, tid), awaiting_from="claude-code")

    page1 = await read_tools.list_threads(limit=2, offset=0)
    assert [s["thread_id"] for s in page1["items"]] == [ULID_A, ULID_B]
    assert page1["total"] == 3

    page2 = await read_tools.list_threads(limit=2, offset=2)
    assert [s["thread_id"] for s in page2["items"]] == [ULID_C]
    assert page2["total"] == 3


@pytest.mark.anyio
async def test_list_threads_bad_pagination_raises(read_tools: ReadTools) -> None:
    with pytest.raises(ToolError, match="limit must be in"):
        await read_tools.list_threads(limit=0)
    with pytest.raises(ToolError, match="limit must be in"):
        await read_tools.list_threads(limit=1001)
    with pytest.raises(ToolError, match="offset must be >= 0"):
        await read_tools.list_threads(offset=-1)


@pytest.mark.anyio
async def test_list_threads_skips_corrupt_meta(read_tools: ReadTools, tmp_path: Path) -> None:
    """A corrupt meta.yaml is skipped; other threads still list."""
    good = _layout(tmp_path, ULID_A)
    seed_thread_meta(good, awaiting_from="claude-code")
    bad = _layout(tmp_path, ULID_B)
    bad.thread_dir.mkdir(parents=True, exist_ok=True)
    bad.meta_path.write_text("{ not: valid: yaml :", encoding="utf-8")

    result = await read_tools.list_threads()
    assert {s["thread_id"] for s in result["items"]} == {ULID_A}


@pytest.mark.anyio
async def test_list_threads_skips_schema_invalid_meta(
    read_tools: ReadTools, tmp_path: Path
) -> None:
    """Parseable YAML but pydantic-schema-invalid meta.yaml is skipped (3a fix).

    Distinct path from test_list_threads_skips_corrupt_meta (YAML parse
    failure): pydantic v2 ValidationError is not a ValueError subclass,
    so this exercises the explicit ValidationError branch in
    _iter_thread_metas. One schema-invalid thread must not 500 the
    whole listing.
    """
    good = _layout(tmp_path, ULID_A)
    seed_thread_meta(good, awaiting_from="claude-code")
    bad = _layout(tmp_path, ULID_B)
    bad.thread_dir.mkdir(parents=True, exist_ok=True)
    # Valid YAML, valid required fields, but an extra=forbid violation.
    bad.meta_path.write_text(
        "schema_version: 2\n"
        f"thread_id: {ULID_B}\n"
        "title: bad\n"
        "status: active\n"
        "participants: [claude.ai, claude-code]\n"
        "created_at: '2026-05-07T08:43:07Z'\n"
        "updated_at: '2026-05-07T08:43:07Z'\n"
        "tags: []\n"
        "awaiting_from: claude-code\n"
        "retry_count: 0\n"
        "unknown_extra_field: nope\n",
        encoding="utf-8",
    )

    result = await read_tools.list_threads()
    assert {s["thread_id"] for s in result["items"]} == {ULID_A}


@pytest.mark.anyio
async def test_list_threads_skips_staging_and_non_ulid(
    read_tools: ReadTools, tmp_path: Path
) -> None:
    seed_thread_meta(_layout(tmp_path, ULID_A), awaiting_from="claude-code")
    (tmp_path / "threads" / f".staging-{ULID_B}").mkdir(parents=True)
    (tmp_path / "threads" / "not-a-ulid").mkdir(parents=True)

    result = await read_tools.list_threads()
    assert {s["thread_id"] for s in result["items"]} == {ULID_A}


# ---------------------------------------------------------------------------
# get_thread
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_thread_happy_path(read_tools: ReadTools, tmp_path: Path) -> None:
    layout = _layout(tmp_path, ULID_A)
    seed_thread_meta(layout, title="rt", awaiting_from="claude.ai", tags=["t"])
    write_message_file(layout, seq=1, sender="claude.ai", body="hi", atomic=False)
    write_message_file(layout, seq=2, sender="claude-code", body="yo", atomic=False)

    result = await read_tools.get_thread(ULID_A)
    assert result["schema_version"] == READ_API_SCHEMA_VERSION
    assert result["thread"] == {
        "schema_version": 2,
        "thread_id": ULID_A,
        "title": "rt",
        "status": "active",
        "awaiting_from": "claude.ai",
        "participants": ["claude.ai", "claude-code"],
        "created_at": "2026-05-07T08:43:07Z",
        "updated_at": "2026-05-07T08:43:07Z",
        "tags": ["t"],
        "retry_count": 0,
        "terminated_reason": None,
        "terminated_at": None,
    }
    msgs = result["messages"]
    assert [m["seq"] for m in msgs] == [1, 2]
    assert msgs[0] == {
        "schema_version": 1,
        "msg_id": f"{ULID_A}/001",
        "seq": 1,
        "from": "claude.ai",
        "to": "claude-code",
        "created_at": "2026-05-07T08:43:07Z",
        "reply_to": None,
        "body": "hi",
    }


@pytest.mark.anyio
async def test_get_thread_include_messages_false(read_tools: ReadTools, tmp_path: Path) -> None:
    layout = _layout(tmp_path, ULID_A)
    seed_thread_meta(layout, awaiting_from="claude-code")
    write_message_file(layout, seq=1, sender="claude.ai", atomic=False)

    result = await read_tools.get_thread(ULID_A, include_messages=False)
    assert result["messages"] is None
    assert result["thread"]["thread_id"] == ULID_A


@pytest.mark.anyio
async def test_get_thread_seq_slice_inclusive(read_tools: ReadTools, tmp_path: Path) -> None:
    layout = _layout(tmp_path, ULID_A)
    seed_thread_meta(layout, awaiting_from="claude-code")
    for seq in (1, 2, 3, 4):
        sender: Participant = "claude.ai" if seq % 2 else "claude-code"
        write_message_file(layout, seq=seq, sender=sender, atomic=False)

    sliced = await read_tools.get_thread(ULID_A, message_seq_from=2, message_seq_to=3)
    assert [m["seq"] for m in sliced["messages"]] == [2, 3]

    open_ended = await read_tools.get_thread(ULID_A, message_seq_from=3)
    assert [m["seq"] for m in open_ended["messages"]] == [3, 4]


@pytest.mark.anyio
async def test_get_thread_seq_from_gt_to_raises(read_tools: ReadTools) -> None:
    with pytest.raises(ToolError, match="message_seq_from must be <= message_seq_to"):
        await read_tools.get_thread(ULID_A, message_seq_from=5, message_seq_to=2)


@pytest.mark.anyio
async def test_get_thread_invalid_ulid_raises(read_tools: ReadTools) -> None:
    with pytest.raises(ToolError, match="invalid_argument"):
        await read_tools.get_thread("not-a-ulid")


@pytest.mark.anyio
async def test_get_thread_not_found_raises(read_tools: ReadTools) -> None:
    fresh = str(ULID())
    with pytest.raises(ToolError, match="thread_not_found"):
        await read_tools.get_thread(fresh)


@pytest.mark.anyio
async def test_get_thread_dir_without_meta_is_not_found(
    read_tools: ReadTools, tmp_path: Path
) -> None:
    layout = _layout(tmp_path, ULID_A)
    layout.thread_dir.mkdir(parents=True)
    with pytest.raises(ToolError, match="thread_not_found"):
        await read_tools.get_thread(ULID_A)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_register_read_tools_exposes_both_tools(tmp_path: Path) -> None:
    fastmcp = FastMCP(name="test")
    register_read_tools(fastmcp, ReadTools(data_dir=tmp_path))
    names = {t.name for t in await fastmcp.list_tools()}
    assert {"mindwire_list_threads", "mindwire_get_thread"} <= names


def test_build_app_smoke() -> None:
    """build_app wires the read tools into the Starlette app without error.

    Tool-level registration is verified separately in
    test_register_read_tools_exposes_both_tools; this is a smoke test
    that the read-tool wiring doesn't break build_app construction.
    """
    settings = MindwireSettings()
    app = build_app(settings, api_key="s3cret")
    assert app is not None
