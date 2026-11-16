"""Tail cache for the sweep's 1b and park-wake ticks.

T-sweep-preloop-overhead-exceeds-launch-budget D-1.

Why this exists. 1b (:mod:`~spirrow_mindwire.pr_event_advance`) and park-wake
(:mod:`~spirrow_mindwire.park_wake`) each read the tail of EVERY open thread of a project on every
tick, so their cost grows with the thread count. On 2026-11-04 that was a large part of a
pre-loop phase that no longer fit in the launch budget (operator msg-7823). Most threads have not
moved since the previous tick, and the only input both ticks take from a thread is its tail
message.

What is cached. For each thread: the tail message the two ticks decide from (``msg_id``,
``author``, ``content``, ``next_participant``, ``timestamp``), keyed by the thread's head id
(``head_msg_id``).
The decision itself is NOT cached: 1b and park-wake still decide every thread on every tick,
because their other inputs (PR state, CI, trigger facts) change without the thread moving.

When it is used (Bohr msg-7853 D-1):

* the sweep runs the head probe first and passes its heads to both ticks (``--heads-json``);
* a thread whose head equals the cached key is served from the cache, and every other thread is
  read from the chatroom as before. There is no TTL: the key is the head, and a thread whose head
  has not moved has the same tail.
* a thread the listing no longer returns is dropped from the cache;
* a missing, unreadable or wrong-schema cache file, and a missing or unreadable heads file, all
  give an empty view, so every thread is read in full, as before this cache existed (fail-open).
  A thread absent from the heads is a miss, never a hit.

A tick that posts into a thread removes that thread from the cache: its tail is now the post.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

#: Bumped when the file's shape changes; a file with another value is ignored (full read).
#: 2: the tail's ``timestamp`` (boundary 5 reads it; a v1 entry has none, and a missing time is
#: "after the cutoff", so v1 files are dropped rather than read).
SCHEMA_VERSION = 2


@dataclass(frozen=True)
class CachedTail:
    """The tail message fields 1b and park-wake decide from."""

    msg_id: str
    author: str
    content: str
    next_participant: str | None
    timestamp: str | None = None

    @classmethod
    def from_message(cls, msg: Mapping[str, Any]) -> CachedTail:
        np = msg.get("next_participant")
        ts = msg.get("timestamp")
        if isinstance(ts, datetime):
            # Every live caller hands a JSON-decoded chatroom row, whose ``timestamp`` is a string;
            # a datetime is kept (as ISO text) rather than dropped, because a dropped time reads as
            # "after the cutoff" (PR-gate on #548).
            ts = ts.isoformat()
        return cls(
            msg_id=str(msg.get("msg_id") or ""),
            author=str(msg.get("author", "")),
            content=str(msg.get("content", "")),
            next_participant=np if isinstance(np, str) else None,
            timestamp=ts if isinstance(ts, str) and ts else None,
        )

    def as_message(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "msg_id": self.msg_id,
            "author": self.author,
            "content": self.content,
        }
        if self.next_participant is not None:
            out["next_participant"] = self.next_participant
        if self.timestamp is not None:
            out["timestamp"] = self.timestamp
        return out


def load_heads(path: Path | None) -> dict[str, str]:
    """``{thread_id: head_msg_id}`` from the sweep's heads file, or ``{}`` when unknown.

    Accepts ``scripts/thread_heads.py``'s shape (``{"heads": {...}}``). Anything else — no file,
    unreadable JSON, ``heads`` missing or not a mapping — is "no heads known", which makes every
    thread a cache miss.
    """
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    heads = data.get("heads") if isinstance(data, dict) else None
    if not isinstance(heads, dict):
        return {}
    return {k: v for k, v in heads.items() if isinstance(k, str) and isinstance(v, str) and v}


class TailCache:
    """One project's cache file plus this tick's heads. Not thread-safe; one tick at a time.

    ``status`` says how the file was found (``loaded`` / ``absent`` / ``unreadable`` /
    ``schema-mismatch`` / ``disabled``), for the tick's JSON. ``hits`` / ``misses`` count lookups.
    """

    def __init__(
        self,
        path: Path | None,
        heads: Mapping[str, str],
        entries: dict[str, tuple[str, CachedTail]] | None = None,
        status: str = "disabled",
    ) -> None:
        self.path = path
        self.heads = dict(heads)
        self._entries: dict[str, tuple[str, CachedTail]] = entries or {}
        self.status = status
        self.hits = 0
        self.misses = 0

    @classmethod
    def open(cls, path: Path | None, heads: Mapping[str, str]) -> TailCache:
        if path is None:
            return cls(None, heads)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(path, heads, status="absent")
        except OSError:
            return cls(path, heads, status="unreadable")
        try:
            data = json.loads(raw)
        except ValueError:
            return cls(path, heads, status="unreadable")
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
            return cls(path, heads, status="schema-mismatch")
        threads = data.get("threads")
        if not isinstance(threads, dict):
            return cls(path, heads, status="unreadable")
        entries: dict[str, tuple[str, CachedTail]] = {}
        for tid, row in threads.items():
            if not isinstance(tid, str) or not isinstance(row, dict):
                continue
            key = row.get("head_msg_id")
            tail = row.get("tail")
            if not isinstance(key, str) or not key or not isinstance(tail, dict):
                continue
            entries[tid] = (key, CachedTail.from_message(tail))
        return cls(path, heads, entries, status="loaded")

    def get(self, thread_id: str) -> CachedTail | None:
        """The cached tail when this tick's head for ``thread_id`` equals the cached key."""
        head = self.heads.get(thread_id)
        entry = self._entries.get(thread_id)
        if head and entry is not None and entry[0] == head and entry[1].msg_id == head:
            self.hits += 1
            return entry[1]
        self.misses += 1
        return None

    def put(self, thread_id: str, tail: CachedTail) -> None:
        """Record a tail read from the chatroom, keyed by its own msg id (its head when read)."""
        if tail.msg_id:
            self._entries[thread_id] = (tail.msg_id, tail)
        else:
            self._entries.pop(thread_id, None)

    def forget(self, thread_id: str) -> None:
        self._entries.pop(thread_id, None)

    def prune(self, live: Iterable[str]) -> None:
        """Drop every thread the listing did not return this tick."""
        keep = set(live)
        for tid in [t for t in self._entries if t not in keep]:
            del self._entries[tid]

    def save(self) -> None:
        """Write the file (temp + replace). A failed write is swallowed: next tick reads in full."""
        if self.path is None:
            return
        data = {
            "schema": SCHEMA_VERSION,
            "threads": {
                tid: {"head_msg_id": key, "tail": tail.as_message()}
                for tid, (key, tail) in sorted(self._entries.items())
            },
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            return

    def report(self) -> dict[str, Any]:
        return {"status": self.status, "hits": self.hits, "misses": self.misses}


__all__ = ["SCHEMA_VERSION", "CachedTail", "TailCache", "load_heads"]
