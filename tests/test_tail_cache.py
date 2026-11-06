"""Tail cache for 1b and park-wake (T-sweep-preloop-overhead-exceeds-launch-budget D-1).

Pins:
* the cache: hit when the head equals the key, miss otherwise; no heads means every thread misses;
  missing / broken / wrong-schema files are read as empty (fail-open); threads the listing no
  longer returns are pruned; round trip through the file
* 1b and park-wake: a hit costs no ``chatroom_get_thread``, a moved head is re-read, the decision
  is still made from the cached tail, and a post drops the thread from the cache
* ``posted_heads``: the msg id of a post, ``""`` when it could not be read or the post failed
* the CLIs accept ``--heads-json`` / ``--tail-cache``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.conductor.gate_records import RELAY_AUTHOR, render_relay_heading
from spirrow_mindwire.github.client import PrRef, PrResolution, PrState
from spirrow_mindwire.magickit.client import MagickitMcpError
from spirrow_mindwire.park_wake import runner as park_runner
from spirrow_mindwire.pr_event_advance import runner as pr_runner
from spirrow_mindwire.repo_projects import PrProject, RepoProjectMap
from spirrow_mindwire.tail_cache import SCHEMA_VERSION, CachedTail, TailCache, load_heads
from spirrow_mindwire.value_objects import Role

#: T-gate-fires-on-pr-open (msg-8030): acme/widgets belongs to "p" only.
_REPO_MAP = RepoProjectMap(candidates={"acme/widgets": frozenset({"p"})})
_PR_PROJECT = PrProject("p")

_ROSTER = {"Bohr": Role.PROPOSER, "Heisenberg": Role.IMPLEMENTER, "Einstein": Role.NAYSAYER}
_HEAD_SHA = "703b836737f29fe0f4139d86d2d02077c662ce5f"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _tail(msg_id: str, content: str = "x\n\nNEXT: Bohr", author: str = "Bohr") -> CachedTail:
    return CachedTail(msg_id=msg_id, author=author, content=content, next_participant=None)


def _write_cache(path: Path, entries: dict[str, CachedTail]) -> None:
    cache = TailCache(path, {}, status="absent")
    for tid, t in entries.items():
        cache.put(tid, t)
    cache.save()


# --------------------------------------------------------------------------- the cache


def test_hit_only_when_the_head_equals_the_key(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    _write_cache(path, {"T-a": _tail("msg-5"), "T-b": _tail("msg-7")})
    cache = TailCache.open(path, {"T-a": "msg-5", "T-b": "msg-8"})
    assert cache.status == "loaded"
    assert cache.get("T-a") == _tail("msg-5")
    assert cache.get("T-b") is None  # head moved
    assert cache.get("T-c") is None  # never cached
    assert (cache.hits, cache.misses) == (1, 2)


def test_a_thread_absent_from_the_heads_is_a_miss(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    _write_cache(path, {"T-a": _tail("msg-5")})
    assert TailCache.open(path, {}).get("T-a") is None


@pytest.mark.parametrize(
    ("content", "status"),
    [
        (None, "absent"),
        ("{not json", "unreadable"),
        (json.dumps({"schema": SCHEMA_VERSION + 1, "threads": {}}), "schema-mismatch"),
        (json.dumps([1, 2]), "schema-mismatch"),
        (json.dumps({"schema": SCHEMA_VERSION, "threads": []}), "unreadable"),
    ],
)
def test_a_missing_or_broken_file_reads_as_empty(
    tmp_path: Path, content: str | None, status: str
) -> None:
    path = tmp_path / "p.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    cache = TailCache.open(path, {"T-a": "msg-5"})
    assert cache.status == status
    assert cache.get("T-a") is None


def test_prune_drops_threads_the_listing_no_longer_returns(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    _write_cache(path, {"T-a": _tail("msg-5"), "T-gone": _tail("msg-9")})
    cache = TailCache.open(path, {"T-a": "msg-5", "T-gone": "msg-9"})
    cache.prune(["T-a"])
    cache.save()
    again = TailCache.open(path, {"T-a": "msg-5", "T-gone": "msg-9"})
    assert again.get("T-a") is not None
    assert again.get("T-gone") is None


def test_round_trip_keeps_next_participant(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    t = CachedTail(msg_id="msg-3", author="A", content="c", next_participant="Bohr")
    _write_cache(path, {"T-a": t})
    assert TailCache.open(path, {"T-a": "msg-3"}).get("T-a") == t


def test_load_heads_fails_open(tmp_path: Path) -> None:
    good = tmp_path / "h.json"
    good.write_text(json.dumps({"heads": {"T-a": "msg-1", "T-b": ""}, "count": 2}), "utf-8")
    assert load_heads(good) == {"T-a": "msg-1"}
    bad = tmp_path / "bad.json"
    bad.write_text("{}", "utf-8")
    assert load_heads(bad) == {}
    assert load_heads(tmp_path / "missing.json") == {}
    assert load_heads(None) == {}


# --------------------------------------------------------------------------- fakes


class _Chat:
    """Threads keyed by id; counts reads per thread and records posts."""

    def __init__(self, threads: dict[str, list[dict[str, Any]]]) -> None:
        self.threads = threads
        self.reads: list[str] = []
        self.post_fails: set[str] = set()
        self.blank_msg_id: set[str] = set()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_list_threads":
            items = [{"thread_id": t} for t in self.threads]
            return {"items": items, "total": len(items)}
        if name == "get_identity":
            return {"status": "found", "identity": {"allowed_roles": ["proposer"]}}
        if name == "chatroom_get_thread" and arguments.get("mode") == "summary":
            return {"thread": {"status": "resolved"}}
        tid = arguments["thread_id"]
        if name == "chatroom_get_thread":
            self.reads.append(tid)
            return {"messages": list(self.threads[tid])}
        if name == "chatroom_post_message":
            if tid in self.post_fails:
                raise MagickitMcpError("timeout")
            msg = {
                "msg_id": f"msg-{len(self.threads[tid]) + 100}",
                "author": arguments["author"],
                "content": arguments["content"],
            }
            self.threads[tid].append(msg)
            if tid in self.blank_msg_id:
                return {"msg": {}}
            return {"msg": msg}
        raise AssertionError(name)


class _Gh:
    def __init__(self, state: PrState) -> None:
        self.state = state

    async def fetch_pr_state(self, pr: PrRef) -> PrState:
        return self.state

    async def fetch_ci_status(self, pr: PrRef) -> Any:
        raise AssertionError("no CI read expected")


def _merged() -> PrState:
    return PrState(
        ref=PrRef("acme", "widgets", 7),
        resolution=PrResolution.CLOSED,
        merged=True,
        head_sha=_HEAD_SHA,
        merge_commit_sha="ab" * 20,
    )


def _relay_body() -> str:
    return (
        f"{render_relay_heading('acme/widgets#7', _HEAD_SHA)}\n\n"
        "VERDICT: approve (ci=success)\n\ncritique\n\nNEXT: human"
    )


def _heads(chat: _Chat) -> dict[str, str]:
    return {tid: msgs[-1]["msg_id"] for tid, msgs in chat.threads.items()}


# --------------------------------------------------------------------------- 1b


@pytest.mark.anyio
async def test_1b_a_hit_costs_no_read_and_still_decides(tmp_path: Path) -> None:
    chat = _Chat(
        {
            "T-quiet": [{"msg_id": "msg-1", "author": "Bohr", "content": "x\n\nNEXT: Einstein"}],
            "T-relay": [{"msg_id": "msg-2", "author": RELAY_AUTHOR, "content": _relay_body()}],
        }
    )
    path = tmp_path / "p.json"
    gh = _Gh(_merged())
    first = await pr_runner.run_tick(
        repo_map=_REPO_MAP,
        mcp=chat,
        gh=gh,
        project="p",
        proposer="Bohr",
        cache=TailCache.open(path, _heads(chat)),
    )
    assert sorted(chat.reads) == ["T-quiet", "T-relay"]
    assert first.as_dict()["posted"] == 1
    posted_id = chat.threads["T-relay"][-1]["msg_id"]
    assert first.as_dict()["posted_heads"] == {"T-relay": posted_id}

    chat.reads.clear()
    second = await pr_runner.run_tick(
        repo_map=_REPO_MAP,
        mcp=chat,
        gh=gh,
        project="p",
        proposer="Bohr",
        cache=TailCache.open(path, _heads(chat)),
    )
    # T-quiet did not move: served from the cache. T-relay moved (our post): read again.
    assert chat.reads == ["T-relay"]
    assert second.as_dict()["tail_cache"] == {"status": "loaded", "hits": 1, "misses": 1}
    assert second.as_dict()["posted"] == 0

    chat.reads.clear()
    await pr_runner.run_tick(
        repo_map=_REPO_MAP,
        mcp=chat,
        gh=gh,
        project="p",
        proposer="Bohr",
        cache=TailCache.open(path, _heads(chat)),
    )
    assert chat.reads == []


@pytest.mark.anyio
async def test_1b_a_decision_from_a_cached_tail_still_posts(tmp_path: Path) -> None:
    # The PR state changes without the thread moving: the cached tail must still be decided.
    chat = _Chat(
        {"T-relay": [{"msg_id": "msg-2", "author": RELAY_AUTHOR, "content": _relay_body()}]}
    )
    path = tmp_path / "p.json"
    open_pr = PrState(
        ref=PrRef("acme", "widgets", 7), resolution=PrResolution.OPEN, head_sha=_HEAD_SHA
    )
    await pr_runner.run_tick(
        repo_map=_REPO_MAP,
        mcp=chat,
        gh=_Gh(open_pr),
        project="p",
        proposer="Bohr",
        cache=TailCache.open(path, _heads(chat)),
    )
    chat.reads.clear()
    report = await pr_runner.run_tick(
        repo_map=_REPO_MAP,
        mcp=chat,
        gh=_Gh(_merged()),
        project="p",
        proposer="Bohr",
        cache=TailCache.open(path, _heads(chat)),
    )
    assert chat.reads == []
    assert report.as_dict()["posted"] == 1


@pytest.mark.anyio
async def test_1b_posted_heads_is_blank_when_the_id_is_unknown_or_the_post_failed() -> None:
    chat = _Chat(
        {
            "T-blank": [{"msg_id": "msg-1", "author": RELAY_AUTHOR, "content": _relay_body()}],
            "T-fail": [{"msg_id": "msg-2", "author": RELAY_AUTHOR, "content": _relay_body()}],
        }
    )
    chat.blank_msg_id.add("T-blank")
    chat.post_fails.add("T-fail")
    report = await pr_runner.run_tick(
        repo_map=_REPO_MAP, mcp=chat, gh=_Gh(_merged()), project="p", proposer="Bohr"
    )
    assert report.as_dict()["posted_heads"] == {"T-blank": "", "T-fail": ""}
    assert "tail_cache" not in report.as_dict()  # no cache asked for: unchanged output


# --------------------------------------------------------------------------- park-wake


def _park(content_trigger: str = "thread:T-target") -> str:
    return f"waiting\n\nSTOP: blocked-on {content_trigger} wake:Bohr\nNEXT: none"


@pytest.mark.anyio
async def test_park_wake_a_hit_costs_no_read(tmp_path: Path) -> None:
    chat = _Chat(
        {
            "T-done": [
                {"msg_id": "msg-1", "author": "Bohr", "content": "x\n\nSTOP: done\nNEXT: none"}
            ],
            "T-live": [{"msg_id": "msg-2", "author": "Bohr", "content": "x\n\nNEXT: Einstein"}],
        }
    )
    path = tmp_path / "p.json"
    await park_runner.run_tick(
        mcp=chat,
        gh=_Gh(_merged()),
        project="p",
        roster=_ROSTER,
        cache=TailCache.open(path, _heads(chat)),
    )
    assert sorted(chat.reads) == ["T-done", "T-live"]
    chat.reads.clear()
    chat.threads["T-live"].append(
        {"msg_id": "msg-3", "author": "Einstein", "content": "y\n\nNEXT: Bohr"}
    )
    report = await park_runner.run_tick(
        mcp=chat,
        gh=_Gh(_merged()),
        project="p",
        roster=_ROSTER,
        cache=TailCache.open(path, _heads(chat)),
    )
    assert chat.reads == ["T-live"]
    out = report.as_dict()
    assert out["counts"]["done"] == 1  # classified from the cached tail
    assert out["tail_cache"] == {"status": "loaded", "hits": 1, "misses": 1}


@pytest.mark.anyio
async def test_park_wake_wake_drops_the_thread_and_reports_its_head(tmp_path: Path) -> None:
    chat = _Chat({"T-park": [{"msg_id": "msg-1", "author": "Bohr", "content": _park()}]})
    path = tmp_path / "p.json"
    report = await park_runner.run_tick(
        mcp=chat,
        gh=_Gh(_merged()),
        project="p",
        roster=_ROSTER,
        cache=TailCache.open(path, _heads(chat)),
    )
    out = report.as_dict()
    assert [w["action"] for w in out["woken"]] == ["posted"]
    new_id = chat.threads["T-park"][-1]["msg_id"]
    assert out["posted_heads"] == {"T-park": new_id}
    # The woken thread is not served from the cache with its pre-wake tail.
    assert TailCache.open(path, {"T-park": "msg-1"}).get("T-park") is None


@pytest.mark.anyio
async def test_park_wake_blank_msg_id_is_reported_blank() -> None:
    chat = _Chat({"T-park": [{"msg_id": "msg-1", "author": "Bohr", "content": _park()}]})
    chat.blank_msg_id.add("T-park")
    report = await park_runner.run_tick(mcp=chat, gh=_Gh(_merged()), project="p", roster=_ROSTER)
    assert report.as_dict()["posted_heads"] == {"T-park": ""}


# ------------------------------------------------------------- the 1b -> park-wake hand-off


@pytest.mark.anyio
async def test_a_1b_post_reaches_park_wake_as_a_moved_head(tmp_path: Path) -> None:
    """The wrapper writes 1b's posted_heads over the heads before park-wake (msg-7845 Obj-2-1)."""
    chat = _Chat(
        {"T-relay": [{"msg_id": "msg-2", "author": RELAY_AUTHOR, "content": _relay_body()}]}
    )
    path = tmp_path / "p.json"
    heads = _heads(chat)
    one_b = await pr_runner.run_tick(
        repo_map=_REPO_MAP,
        mcp=chat,
        gh=_Gh(_merged()),
        project="p",
        proposer="Bohr",
        cache=TailCache.open(path, heads),
    )
    heads.update(one_b.as_dict()["posted_heads"])  # what Update-HeadsFromPosted does
    assert heads["T-relay"] == chat.threads["T-relay"][-1]["msg_id"]
    chat.reads.clear()
    await park_runner.run_tick(
        mcp=chat,
        gh=_Gh(_merged()),
        project="p",
        roster=_ROSTER,
        cache=TailCache.open(path, heads),
    )
    assert chat.reads == ["T-relay"]  # re-read at the new head, not the stale cached tail


# --------------------------------------------------------------------------- CLIs


@pytest.mark.parametrize("module", ["pr_event_advance", "park_wake"])
def test_cli_accepts_the_cache_flags(module: str) -> None:
    import importlib

    main = importlib.import_module(f"spirrow_mindwire.{module}.__main__").main
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_open_cache_is_none_without_the_flag(tmp_path: Path) -> None:
    import argparse

    from spirrow_mindwire.pr_event_advance.__main__ import open_cache

    assert open_cache(argparse.Namespace(tail_cache=None, heads_json=None)) is None
    cache = open_cache(argparse.Namespace(tail_cache=tmp_path / "c.json", heads_json=None))
    assert cache is not None and cache.status == "absent" and cache.heads == {}
