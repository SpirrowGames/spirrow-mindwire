"""Tests for ``spirrow_mindwire.mcp_write_server`` (Feature 3-A sub-PR 2).

Commit 2 scope: bootstrap + api-key middleware. The 3 write tools
(``send_message`` / ``open_thread`` / ``resolve_thread``) and their
in-server unit tests (= C1 reuse, C4 concurrency) are added in commit 3.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from factories import seed_thread_meta, write_message_file
from mcp.server.fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from ulid import ULID

from spirrow_mindwire.config import MindwireSettings
from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.mcp_write_server.auth import (
    ApiKeyMiddleware,
    MissingApiKeyError,
    read_api_key,
)
from spirrow_mindwire.mcp_write_server.http import MCP_PATH, build_app
from spirrow_mindwire.mcp_write_server.tools_write import WriteTools
from spirrow_mindwire.watcher.loader import load_messages, load_thread_meta

# ---------------------------------------------------------------------------
# read_api_key
# ---------------------------------------------------------------------------


def test_read_api_key_returns_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWIRE_MCP_API_KEY", "s3cret")
    settings = MindwireSettings()
    assert read_api_key(settings) == "s3cret"


def test_read_api_key_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDWIRE_MCP_API_KEY", raising=False)
    settings = MindwireSettings()
    with pytest.raises(MissingApiKeyError, match="MINDWIRE_MCP_API_KEY"):
        read_api_key(settings)


def test_read_api_key_empty_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWIRE_MCP_API_KEY", "")
    settings = MindwireSettings()
    with pytest.raises(MissingApiKeyError):
        read_api_key(settings)


def test_read_api_key_honours_custom_env_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_KEY_ENV", "alt-secret")
    monkeypatch.delenv("MINDWIRE_MCP_API_KEY", raising=False)
    settings = MindwireSettings(mcp_server={"api_key_env": "CUSTOM_KEY_ENV"})  # type: ignore[arg-type]
    assert read_api_key(settings) == "alt-secret"


# ---------------------------------------------------------------------------
# ApiKeyMiddleware (unit-level, isolated from FastMCP)
# ---------------------------------------------------------------------------


def _make_stub_app(api_key: str) -> Starlette:
    """Build a minimal Starlette app guarded by :class:`ApiKeyMiddleware`."""

    async def ok(_request: object) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", ok)])
    app.add_middleware(ApiKeyMiddleware, api_key=api_key)
    return app


def test_middleware_accepts_valid_bearer_token() -> None:
    client = TestClient(_make_stub_app("s3cret"))
    response = client.get("/", headers={"Authorization": "Bearer s3cret"})
    assert response.status_code == 200
    assert response.text == "ok"


def test_middleware_accepts_lowercase_bearer_scheme() -> None:
    """The HTTP scheme token is case-insensitive (RFC 7235)."""
    client = TestClient(_make_stub_app("s3cret"))
    response = client.get("/", headers={"Authorization": "bearer s3cret"})
    assert response.status_code == 200


def test_middleware_rejects_missing_header() -> None:
    client = TestClient(_make_stub_app("s3cret"))
    response = client.get("/")
    assert response.status_code == 401
    assert response.json()["detail"] == "missing or malformed Authorization header"


def test_middleware_rejects_wrong_scheme() -> None:
    client = TestClient(_make_stub_app("s3cret"))
    response = client.get("/", headers={"Authorization": "Basic s3cret"})
    assert response.status_code == 401
    assert response.json()["detail"] == "missing or malformed Authorization header"


def test_middleware_rejects_empty_bearer_token() -> None:
    client = TestClient(_make_stub_app("s3cret"))
    response = client.get("/", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


def test_middleware_rejects_wrong_token() -> None:
    client = TestClient(_make_stub_app("s3cret"))
    response = client.get("/", headers={"Authorization": "Bearer not-the-key"})
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid API key"


# ---------------------------------------------------------------------------
# build_app integration
# ---------------------------------------------------------------------------


def test_build_app_returns_starlette() -> None:
    """``build_app`` returns a Starlette ASGI app."""
    settings = MindwireSettings()
    app = build_app(settings, api_key="s3cret")
    assert isinstance(app, Starlette)


def test_build_app_mcp_endpoint_requires_auth() -> None:
    """The MCP endpoint is gated by :class:`ApiKeyMiddleware`."""
    settings = MindwireSettings()
    app = build_app(settings, api_key="s3cret")
    with TestClient(app) as client:
        response = client.post(MCP_PATH, json={})
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# WriteTools — send_message / open_thread / resolve_thread
# ---------------------------------------------------------------------------


ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _read_events(layout: ThreadDirLayout) -> list[dict[str, Any]]:
    """Parse all events from the thread's events.jsonl."""
    if not layout.event_log_path.is_file():
        return []
    text = layout.event_log_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture
def write_tools(tmp_path: Path) -> WriteTools:
    """A :class:`WriteTools` rooted at ``tmp_path`` (= data_dir)."""
    return WriteTools(data_dir=tmp_path)


# ----- send_message --------------------------------------------------------


@pytest.mark.anyio
async def test_send_message_happy_path(write_tools: WriteTools, tmp_path: Path) -> None:
    """send_message writes 002-from-cai, toggles awaiting_from, emits 2 events."""
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    seed_thread_meta(layout, awaiting_from="claude.ai")
    write_message_file(layout, seq=1, sender="claude.ai", atomic=False)

    result = await write_tools.send_message(ULID_A, "hello from claude.ai")

    assert result["thread_id"] == ULID_A
    assert result["seq"] == 2
    assert result["msg_id"] == f"{ULID_A}/002"
    assert result["awaiting_from"] == "claude-code"

    # On-disk verification: the new message parses as a Message and meta
    # toggled.
    messages = load_messages(layout)
    assert len(messages) == 2
    assert messages[1].seq == 2
    assert messages[1].from_ == "claude.ai"
    assert messages[1].to == "claude-code"
    assert messages[1].body == "hello from claude.ai"

    meta = load_thread_meta(layout)
    assert meta.awaiting_from == "claude-code"

    # Two events appended: MessageReceived + AwaitingFromChanged.
    events = _read_events(layout)
    types = [e["type"] for e in events]
    assert types == ["message.received", "thread.awaiting_from.changed"]
    assert events[0]["seq"] == 2
    assert events[0]["from"] == "claude.ai"
    assert events[1]["from_participant"] == "claude.ai"
    assert events[1]["to_participant"] == "claude-code"


@pytest.mark.anyio
async def test_send_message_invalid_thread_id_raises_tool_error(
    write_tools: WriteTools,
) -> None:
    with pytest.raises(ToolError, match="must be a ULID"):
        await write_tools.send_message("not-a-ulid", "body")


@pytest.mark.anyio
async def test_send_message_thread_not_found_raises_tool_error(
    write_tools: WriteTools,
) -> None:
    fresh_ulid = str(ULID())
    with pytest.raises(ToolError, match="does not exist"):
        await write_tools.send_message(fresh_ulid, "body")


@pytest.mark.anyio
async def test_send_message_terminal_state_raises_tool_error(
    write_tools: WriteTools, tmp_path: Path
) -> None:
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    seed_thread_meta(
        layout,
        status="resolved",
        awaiting_from=None,
    )
    with pytest.raises(ToolError, match="terminal state"):
        await write_tools.send_message(ULID_A, "body")


@pytest.mark.anyio
async def test_send_message_wrong_turn_raises_tool_error(
    write_tools: WriteTools, tmp_path: Path
) -> None:
    """If meta.awaiting_from is claude-code, send_message must refuse."""
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    seed_thread_meta(layout, awaiting_from="claude-code")
    with pytest.raises(ToolError, match="claude-code's turn"):
        await write_tools.send_message(ULID_A, "body")


@pytest.mark.anyio
async def test_send_message_concurrent_writes(write_tools: WriteTools, tmp_path: Path) -> None:
    """C4 (msg-127 §4): two concurrent send_message calls on the same thread.

    The per-thread asyncio.Lock serializes them; whichever wins toggles
    awaiting_from to claude-code, so the loser hits the turn-discipline
    guard and raises ToolError. The on-disk state must reflect exactly
    one write (= one new message at seq 2, no torn frontmatter).
    """
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    seed_thread_meta(layout, awaiting_from="claude.ai")
    write_message_file(layout, seq=1, sender="claude.ai", atomic=False)

    coros = [
        write_tools.send_message(ULID_A, "first attempt"),
        write_tools.send_message(ULID_A, "second attempt"),
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)

    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ToolError)

    # Exactly one new message landed; no torn writes.
    messages = load_messages(layout)
    assert len(messages) == 2
    assert messages[1].seq == 2


# ----- open_thread ---------------------------------------------------------


@pytest.mark.anyio
async def test_open_thread_happy_path(write_tools: WriteTools, tmp_path: Path) -> None:
    """open_thread creates a thread dir with meta + first message + 2 events."""
    result = await write_tools.open_thread(
        initial_message="hello world",
        title="my-thread",
        tags=["x", "y"],
    )
    thread_id = result["thread_id"]
    assert result["msg_id"] == f"{thread_id}/001"
    assert result["awaiting_from"] == "claude-code"

    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=thread_id)
    assert layout.thread_dir.is_dir()
    # Staging directory was atomically renamed away.
    assert not layout.staging_dir.exists()

    meta = load_thread_meta(layout)
    assert meta.thread_id == thread_id
    assert meta.schema_version == 2
    assert meta.status == "active"
    assert meta.awaiting_from == "claude-code"
    assert meta.title == "my-thread"
    assert meta.tags == ("x", "y")

    messages = load_messages(layout)
    assert len(messages) == 1
    assert messages[0].seq == 1
    assert messages[0].from_ == "claude.ai"
    assert messages[0].body == "hello world"

    events = _read_events(layout)
    types = [e["type"] for e in events]
    assert types == ["thread.created", "message.received"]


@pytest.mark.anyio
async def test_open_thread_defaults(write_tools: WriteTools) -> None:
    """open_thread accepts no title / no tags."""
    result = await write_tools.open_thread(initial_message="body")
    assert "thread_id" in result
    layout = ThreadDirLayout(base_dir=write_tools._data_dir, thread_id=result["thread_id"])
    meta = load_thread_meta(layout)
    assert meta.title == ""
    assert meta.tags == ()


# ----- resolve_thread ------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_thread_happy_path(write_tools: WriteTools, tmp_path: Path) -> None:
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    seed_thread_meta(layout, awaiting_from="claude-code")

    result = await write_tools.resolve_thread(ULID_A)
    assert result == {"thread_id": ULID_A, "status": "resolved"}

    meta = load_thread_meta(layout)
    assert meta.status == "resolved"
    assert meta.awaiting_from is None

    events = _read_events(layout)
    types = [e["type"] for e in events]
    assert types == ["thread.status.changed", "thread.resolved"]
    assert events[0]["from_status"] == "active"
    assert events[0]["to_status"] == "resolved"


@pytest.mark.anyio
async def test_resolve_thread_idempotent(write_tools: WriteTools, tmp_path: Path) -> None:
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    seed_thread_meta(layout, status="resolved", awaiting_from=None)

    result = await write_tools.resolve_thread(ULID_A)
    assert result == {"thread_id": ULID_A, "status": "resolved", "noop": True}

    # No events appended on noop.
    assert _read_events(layout) == []


@pytest.mark.anyio
async def test_resolve_thread_invalid_transition_raises_tool_error(
    write_tools: WriteTools, tmp_path: Path
) -> None:
    """archived → resolved is forbidden by the transition table."""
    layout = ThreadDirLayout(base_dir=tmp_path, thread_id=ULID_A)
    seed_thread_meta(layout, status="archived", awaiting_from=None)
    with pytest.raises(ToolError, match="not allowed"):
        await write_tools.resolve_thread(ULID_A)


@pytest.mark.anyio
async def test_resolve_thread_not_found_raises_tool_error(
    write_tools: WriteTools,
) -> None:
    fresh_ulid = str(ULID())
    with pytest.raises(ToolError, match="does not exist"):
        await write_tools.resolve_thread(fresh_ulid)
