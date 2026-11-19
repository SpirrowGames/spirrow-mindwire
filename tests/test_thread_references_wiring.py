# ruff: noqa: RUF001  (Japanese fixtures use full-width parentheses verbatim)
"""D-1..D-4 wiring of thread references (T-thread-relations-unused-so-parallel-rule-...)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from spirrow_mindwire.adapters.claude_code_sdk import (
    _DEFAULT_SYSTEM_PROMPT,
    THREAD_OPEN_REFERENCES_BLOCK,
)
from spirrow_mindwire.conductor.core import Conductor
from spirrow_mindwire.magickit.client import MagickitMcpError
from spirrow_mindwire.pr_event_advance.decide import PR_EVENT_RELAY_AUTHOR
from spirrow_mindwire.thread_context import build_turn_prompt
from spirrow_mindwire.thread_references_tick import refresh_project
from spirrow_mindwire.thread_relations import (
    ThreadSnapshot,
    build_notice_index,
    collect_edges,
    notice_from_index,
    reference_candidates,
    related_none_msg,
    slim_message,
    snapshot_from_json,
    snapshot_to_json,
)
from spirrow_mindwire.value_objects import (
    ChatroomEvent,
    EventType,
    NewMessagePayload,
    Role,
    ThreadContext,
    ThreadContextMessage,
    ThreadRef,
)

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "thread_references.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("thread_references_cli", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["thread_references_cli"] = module
    spec.loader.exec_module(module)
    return module


def _msg(
    mid: str, content: str = "x", refs: list[str] | None = None, author: str = "Bohr"
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "msg_id": mid,
        "author": author,
        "content": content,
        "timestamp": "2026-11-19T01:00:00+00:00",
    }
    if refs is not None:
        m["references_threads"] = refs
    return m


def _snap(
    tid: str,
    status: str = "active",
    msgs: list[dict[str, Any]] | None = None,
    tags: tuple[str, ...] = (),
) -> ThreadSnapshot:
    return ThreadSnapshot(thread_id=tid, status=status, messages=tuple(msgs or []), tags=tags)


# --- D-4 candidates ------------------------------------------------------------------------------


def test_candidates_from_tag_related_line_and_reference_open_only() -> None:
    me = _snap(
        "T-a", msgs=[_msg("msg-1", "本文\n関連: `T-c`、T-gone\n> 関連: T-q")], tags=("T229", "n3")
    )
    b = _snap("T-b", tags=("t229",))
    c = _snap("T-c")
    d = _snap("T-d", msgs=[_msg("msg-2", "Related: T-a")])
    e = _snap("T-e", msgs=[_msg("msg-3", refs=["T-a"])])
    closed = _snap("T-f", status="resolved", tags=("T229",))
    q = _snap("T-q")
    snaps = [me, b, c, d, e, closed, q]
    got = {
        c.other: (c.reasons, c.referenced)
        for c in reference_candidates(snaps, collect_edges(snaps), "T-a")
    }
    assert got == {
        "T-b": (("tag:T229",), False),
        "T-c": (("related-line:this",), False),
        "T-d": (("related-line:other",), False),
        "T-e": (("reference",), True),
    }


def test_related_none_is_recorded_only_from_an_explicit_line() -> None:
    assert related_none_msg(_snap("T-a", msgs=[_msg("msg-1", "Related: none")])) == "msg-1"
    assert related_none_msg(_snap("T-a", msgs=[_msg("msg-1", "> Related: none")])) is None
    assert related_none_msg(_snap("T-a", msgs=[_msg("msg-1", "no related line")])) is None


def test_slim_message_keeps_only_read_lines() -> None:
    relay = _msg(
        "msg-1", "heading\n\nPR o/r#3 が merge された（x）。\nother", author=PR_EVENT_RELAY_AUTHOR
    )
    assert slim_message(relay)["content"] == "PR o/r#3 が merge された（x）。"
    person = _msg("msg-2", "PR o/r#3 が merge された\n関連: T-x", refs=["T-y"])
    slim = slim_message(person)
    assert slim["content"] == "関連: T-x"
    assert slim["references_threads"] == ["T-y"]


def test_snapshot_cache_roundtrip() -> None:
    snap = ThreadSnapshot(
        "T-a", "resolved", (_msg("msg-1", refs=["T-b"]),), ("T-c",), ("T1",), "msg-1"
    )
    back = snapshot_from_json(json.loads(json.dumps(snapshot_to_json(snap))))
    assert back is not None
    assert back.thread_id == "T-a" and back.affects_threads == ("T-c",) and back.tags == ("T1",)
    assert collect_edges([back]) == collect_edges([snap])


# --- D-2 index → conductor → prompt ------------------------------------------------------------


def test_index_roundtrip_and_stamp() -> None:
    a = _snap("T-a", msgs=[_msg("msg-1", refs=["T-b"])])
    b = _snap("T-b")
    index = build_notice_index({"p": [a, b]}, generated_at=datetime(2026, 11, 19, tzinfo=UTC))
    loaded = json.loads(json.dumps(index))
    notice = notice_from_index(loaded, "p", "T-b")
    assert notice is not None
    assert "参照: T-a → このスレッド（msg-1）" in notice
    assert notice.endswith("(observed at 2026-11-19T00:00:00+00:00)")
    assert notice_from_index(loaded, "p", "T-zzz") is None
    assert notice_from_index(loaded, "other", "T-b") is None
    assert notice_from_index({**loaded, "schema": 99}, "p", "T-b") is None


def test_conductor_reads_index_and_fails_open(tmp_path: Path) -> None:
    path = tmp_path / "idx.json"
    fake = SimpleNamespace(
        _references_index_path=path,
        _thread_ref=ThreadRef(project_id="p", thread_id="T-b", chatroom_uri="u"),
    )
    assert Conductor._references_notice(fake) is None  # type: ignore[arg-type]  # missing file
    path.write_text("{not json", encoding="utf-8")
    assert Conductor._references_notice(fake) is None  # type: ignore[arg-type]  # unreadable
    index = build_notice_index(
        {"p": [_snap("T-a", msgs=[_msg("msg-1", refs=["T-b"])]), _snap("T-b")]},
        generated_at=datetime(2026, 11, 19, tzinfo=UTC),
    )
    path.write_text(json.dumps(index), encoding="utf-8")
    notice = Conductor._references_notice(fake)  # type: ignore[arg-type]
    assert notice is not None and "T-a → このスレッド" in notice
    fake._references_index_path = None
    assert Conductor._references_notice(fake) is None  # type: ignore[arg-type]  # not wired


def _event(notice: str | None) -> ChatroomEvent:
    return ChatroomEvent(
        event_id="e",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=ThreadRef(project_id="p", thread_id="T-b", chatroom_uri="u"),
        occurred_at=datetime(2026, 11, 19, tzinfo=UTC),
        payload=NewMessagePayload(msg_id="msg-9", author="Bohr", body="go", parent_msg_id=None),
        thread_context=ThreadContext(
            opener=ThreadContextMessage(msg_id="msg-1", author="Fermi", body="open"),
            recent=(),
            omitted_count=0,
            total_count=1,
        ),
        references_notice=notice,
    )


def test_prompt_places_block_before_history_and_omits_when_absent() -> None:
    with_block = build_turn_prompt(
        _event("=== Cross-thread references (observed facts) ===\nX"), Role.IMPLEMENTER, "C"
    )
    assert with_block.index("Cross-thread references") < with_block.index("=== Thread so far")
    without = build_turn_prompt(_event(None), Role.IMPLEMENTER, "C")
    assert "Cross-thread references" not in without


# --- per-tick read -------------------------------------------------------------------------------


class _FakeMcp:
    def __init__(self, threads: dict[str, dict[str, Any]], *, fail_list: bool = False) -> None:
        self.threads = threads
        self.fail_list = fail_list
        self.gets: list[str] = []

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        if name == "chatroom_list_threads":
            if self.fail_list:
                raise MagickitMcpError("down")
            items = [
                {
                    "thread_id": t,
                    "status": v["status"],
                    "last_msg_id": v["messages"][-1]["msg_id"],
                    "tags": v.get("tags", []),
                    "title": t,
                }
                for t, v in self.threads.items()
            ]
            return {"items": items[args["offset"] :], "total": len(items)}
        assert name == "chatroom_get_thread" and args["mode"] == "full"
        self.gets.append(args["thread_id"])
        v = self.threads[args["thread_id"]]
        return {
            "thread": {"thread_id": args["thread_id"], "status": v["status"]},
            "messages": v["messages"],
        }


def _threads() -> dict[str, dict[str, Any]]:
    return {
        "T-a": {"status": "resolved", "messages": [_msg("msg-1", refs=["T-b"])]},
        "T-b": {"status": "active", "messages": [_msg("msg-2")], "tags": ["T9"]},
    }


def test_refresh_uses_cache_and_bounds_fetches() -> None:
    mcp = _FakeMcp(_threads())
    cache: dict[str, Any] = {}
    first = asyncio.run(refresh_project(mcp, "p", cache, max_fetch=1))
    assert (first.fetched, first.pending) == (1, 1)
    second = asyncio.run(refresh_project(mcp, "p", cache, max_fetch=5))
    assert (second.fetched, second.pending) == (1, 0)
    assert mcp.gets == ["T-a", "T-b"]
    third = asyncio.run(refresh_project(mcp, "p", cache, max_fetch=5))
    assert third.fetched == 0 and len(third.snapshots) == 2
    assert {s.thread_id: s.tags for s in third.snapshots}["T-b"] == ("T9",)
    mcp.threads["T-b"]["messages"].append(_msg("msg-3", refs=["T-a"]))
    fourth = asyncio.run(refresh_project(mcp, "p", cache, max_fetch=5))
    assert fourth.fetched == 1 and mcp.gets[-1] == "T-b"


def test_refresh_listing_failure_keeps_cache() -> None:
    cache: dict[str, Any] = {"p": {"T-a": {"key": "k", "snapshot": {}}}}
    read = asyncio.run(refresh_project(_FakeMcp({}, fail_list=True), "p", cache))
    assert read.error is not None and cache["p"] == {"T-a": {"key": "k", "snapshot": {}}}


def test_run_tick_writes_index_and_digest_lines(tmp_path: Path) -> None:
    cli = _load_script()
    threads = _threads()
    threads["T-a"]["messages"].append(
        {
            **_msg("msg-5", "PR o/r#7 が merge された（abc）。", author=PR_EVENT_RELAY_AUTHOR),
            "timestamp": "2026-11-19T00:30:00+00:00",
        }
    )
    out = asyncio.run(
        cli.run_tick(
            _FakeMcp(threads),
            ["p"],
            tmp_path,
            max_fetch=10,
            digest_hours=24,
            now=datetime(2026, 11, 19, 2, tzinfo=UTC),
        )
    )
    assert out["digest_lines"] == ["p: T-a の PR o/r#7 merged → 参照のある open スレッド: T-b"]
    assert out["any_error"] is False
    index = json.loads((tmp_path / "thread-references-index.json").read_text(encoding="utf-8"))
    notice = notice_from_index(index, "p", "T-b")
    assert notice is not None and "  merged: o/r#7（msg-5" in notice
    # A failed listing keeps the previous index entry for that project.
    out2 = asyncio.run(
        cli.run_tick(
            _FakeMcp({}, fail_list=True),
            ["p"],
            tmp_path,
            max_fetch=10,
            digest_hours=24,
            now=datetime(2026, 11, 19, 3, tzinfo=UTC),
        )
    )
    assert out2["any_error"] is True
    index2 = json.loads((tmp_path / "thread-references-index.json").read_text(encoding="utf-8"))
    assert notice_from_index(index2, "p", "T-b") is not None


def test_run_candidates_lists_open_threads(tmp_path: Path) -> None:
    cli = _load_script()
    out = asyncio.run(cli.run_candidates(_FakeMcp(_threads()), "p", "T-b", tmp_path, max_fetch=10))
    assert [t["thread_id"] for t in out["open_threads"]] == ["T-b"]
    assert out["candidates"] == [{"thread_id": "T-b", "related_none_msg": None, "candidates": []}]


def test_cli_allowlist_is_read_only() -> None:
    from spirrow_mindwire.thread_references_tick import READ_ONLY_TOOLS

    assert frozenset({"chatroom_list_threads", "chatroom_get_thread"}) == READ_ONLY_TOOLS


# --- D-1 proposer step ---------------------------------------------------------------------------


def test_proposer_prompt_carries_thread_open_step() -> None:
    assert THREAD_OPEN_REFERENCES_BLOCK in _DEFAULT_SYSTEM_PROMPT
    assert "Write `Related: none` only when you checked" in THREAD_OPEN_REFERENCES_BLOCK
    assert "references_threads" in THREAD_OPEN_REFERENCES_BLOCK
