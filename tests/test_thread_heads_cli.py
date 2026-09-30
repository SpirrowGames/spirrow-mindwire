"""Tests for the sweep's head probe (``scripts/thread_heads.py``).

The probe moved from ``chatroom_my_unread`` to ``chatroom_list_threads``
(T-unread-correlated-count-scale): the inbox path makes conclair evaluate a per-thread unread
count for every thread, and the probe never used that count. These tests pin the parts of the
move that a regression would silently undo:

  * the probe calls the listing, not the inbox, and filters to every status except ``resolved``
    (the inbox's ``include_resolved=false`` set it replaced);
  * the head id is read from ``last_msg_id`` (the listing's field name, not the inbox's
    ``latest_msg_id``);
  * a malformed item or a thread with no head id is omitted, so the caller fails open;
  * the stdout shape ``{"heads": {...}, "count": n}`` is unchanged, and a probe failure exits
    non-zero (the wrapper's fail-open trigger).

The MCP constructor is monkey-patched, so no network I/O happens — same pattern as
``test_head_skip_cli.py``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from spirrow_mindwire.magickit.client import MagickitMcpError

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "thread_heads.py"


def _load_cli_module() -> object:
    """Load the standalone CLI script as a module (it is not on the import path)."""
    spec = importlib.util.spec_from_file_location("_thread_heads_cli_test_module", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_cli_module()


class _FakeMcp:
    """Stand-in for :class:`StreamableHttpChatroomMcp` that records every call."""

    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, name: str, params: dict[str, object]) -> object:
        self.calls.append((name, params))
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _patch_mcp(monkeypatch: pytest.MonkeyPatch, payload: object) -> _FakeMcp:
    fake = _FakeMcp(payload)
    monkeypatch.setattr(_MODULE, "StreamableHttpChatroomMcp", lambda *_a, **_k: fake)
    return fake


def _fetch(project: str = "p") -> dict[str, str]:
    result: dict[str, str] = asyncio.run(
        _MODULE.fetch_heads(project, None, 1000)  # type: ignore[attr-defined]
    )
    return result


def test_probe_calls_the_listing_with_every_status_but_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _patch_mcp(monkeypatch, {"items": []})
    _fetch("spirrow-x")
    assert len(fake.calls) == 1
    name, params = fake.calls[0]
    assert name == "chatroom_list_threads"
    assert params["project"] == "spirrow-x"
    assert params["limit"] == 1000
    statuses = params["status_filter"]
    assert isinstance(statuses, list)
    assert set(statuses) == {"active", "awaiting_reply", "parked", "superseded"}
    assert "resolved" not in statuses


def test_probe_never_calls_the_inbox(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_mcp(monkeypatch, {"items": []})
    _fetch()
    assert all(name != "chatroom_my_unread" for name, _ in fake.calls)


def test_head_id_comes_from_last_msg_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mcp(
        monkeypatch,
        {
            "items": [
                {"thread_id": "T-a", "last_msg_id": "msg-010", "status": "active"},
                {"thread_id": "T-b", "last_msg_id": "msg-020", "status": "awaiting_reply"},
                # the inbox's field name must NOT be read any more
                {"thread_id": "T-c", "latest_msg_id": "msg-030", "status": "active"},
            ],
            "total": 3,
        },
    )
    assert _fetch() == {"T-a": "msg-010", "T-b": "msg-020"}


def test_items_without_a_usable_head_are_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mcp(
        monkeypatch,
        {
            "items": [
                {"thread_id": "T-empty", "last_msg_id": None},
                {"thread_id": "T-blank", "last_msg_id": ""},
                {"last_msg_id": "msg-001"},
                "not-a-dict",
                {"thread_id": "T-ok", "last_msg_id": "msg-002"},
            ]
        },
    )
    assert _fetch() == {"T-ok": "msg-002"}


def test_payload_without_item_list_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mcp(monkeypatch, {"error": "nope"})
    with pytest.raises(MagickitMcpError, match="chatroom_list_threads"):
        _fetch()


def test_main_prints_unchanged_output_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_mcp(monkeypatch, {"items": [{"thread_id": "T-a", "last_msg_id": "msg-007"}]})
    monkeypatch.setattr(sys, "argv", ["thread_heads.py", "--project", "p"])
    assert _MODULE.main() == 0  # type: ignore[attr-defined]
    out = json.loads(capsys.readouterr().out)
    assert out == {"heads": {"T-a": "msg-007"}, "count": 1}


def test_main_fails_non_zero_when_the_probe_breaks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_mcp(monkeypatch, MagickitMcpError("boom"))
    monkeypatch.setattr(sys, "argv", ["thread_heads.py", "--project", "p"])
    assert _MODULE.main() == 1  # type: ignore[attr-defined]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "probe failed" in captured.err
