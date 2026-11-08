"""D-16d-1: thread-head classification (A-D), the head cache, its budget and its eviction.

Spec: T-stalled-pr-has-no-detector msg-8359 §2-2, msg-8361 Obj-1-2, msg-8363, msg-8365,
msg-8368 ①, msg-8370 Obj-4-3, msg-8477 and msg-8480.
"""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.stall_ledger.adapters import ChatroomThreadAdapter
from spirrow_mindwire.stall_ledger.driver import TickOutcome, TickPaths, run_tick
from spirrow_mindwire.stall_ledger.heads import (
    N_UNREAD,
    WAIT_KIND_BY_ARM,
    HeadClass,
    HeadGroup,
    WaitKind,
    classify_head,
    threshold_for,
    wait_kind_for_arm,
)
from spirrow_mindwire.stall_ledger.predicates import N_THRESHOLDS
from spirrow_mindwire.stall_ledger.store import THREAD_HEADS_KEY

PROJECT = "p"
CREATED = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
LAST = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
FOOTER = "\n\n<!-- source: tools=3 · mcp=0 -->"


def _cls(body: str, field_value: str | None = None) -> HeadClass:
    return classify_head(body, next_participant=field_value)


# ─── Classification (msg-8359 §2-2, msg-8361 Obj-1-2, msg-8363, msg-8365) ──────────────


def test_a_next_persona_with_no_field_or_the_same_field() -> None:
    assert _cls("work\n\nNEXT: Heisenberg" + FOOTER).group is HeadGroup.NOMINATED
    assert _cls("work\n\nNEXT: Heisenberg", "Heisenberg").group is HeadGroup.NOMINATED
    assert _cls("x\n\nNEXT: pr-review o/r#5").group is HeadGroup.NOMINATED
    # The field alone (no body NEXT) is still a nomination: the conductor follows it.
    assert _cls("judgement page", "Bohr").group is HeadGroup.NOMINATED


def test_c_field_disagreeing_with_the_body_is_the_m1_shape() -> None:
    head = _cls("x\n\nNEXT: Heisenberg", "Bohr")
    assert (head.group, head.detail) == (HeadGroup.UNEXPLAINED, "field_body_mismatch")


def test_c_next_none_without_stop_line_msg_7374_shape() -> None:
    # msg-8361: the msg-7374 head (NEXT: none, no STOP line) landing in C is an acceptance case.
    head = _cls("棚卸しを書きました。\n\nNEXT: none" + FOOTER)
    assert head.group is HeadGroup.UNEXPLAINED
    assert head.detail == "none_stop_absent"


def test_c_no_next_line_at_all() -> None:
    assert _cls("just prose, no control line").group is HeadGroup.UNEXPLAINED


def test_c_refused_terminal_form() -> None:
    # STOP: something other than done, as the final control line.
    head = _cls("x\n\nSTOP: finished")
    assert head.group is HeadGroup.UNEXPLAINED
    # BLOCKED-ON with an arm the parser does not know.
    assert _cls("x\n\nBLOCKED-ON: weather sunny\nNEXT: Bohr").group is HeadGroup.UNEXPLAINED


@pytest.mark.parametrize(
    "body",
    [
        "",
        "NEXT:",
        "NEXT: ???",
        "```\nNEXT: Bohr\n```",
        "STOP: blocked-on pr:o/r#1 wake:Bohr\nNEXT: none",  # legacy blocked-on: no roster -> C
        "x\n\nNEXT: operator",  # operator without its 3-line form
    ],
)
def test_c_is_the_default_for_any_head_that_matches_nothing(body: str) -> None:
    assert _cls(body).group is HeadGroup.UNEXPLAINED


def test_b_stop_done_terminal_and_legacy() -> None:
    assert _cls("finished\n\nSTOP: done" + FOOTER).group is HeadGroup.DONE
    assert _cls("finished\n\nSTOP: done\nNEXT: none").group is HeadGroup.DONE


@pytest.mark.parametrize(
    ("arm", "ref", "wait"),
    [
        ("pr", "o/r#1", WaitKind.MANUAL),
        ("thread", "T-x", WaitKind.MANUAL),
        ("queue-empty", "p", WaitKind.MANUAL),
        ("deploy", "req-1", WaitKind.AUTO),
    ],
)
def test_d_blocked_on_any_trigger_with_its_wait_kind(arm: str, ref: str, wait: WaitKind) -> None:
    head = _cls(f"waiting\n\nBLOCKED-ON: {arm} {ref}\nNEXT: Bohr")
    assert (head.group, head.wait, head.trigger) == (HeadGroup.WAITING, wait, f"{arm} {ref}")


def test_wait_kind_table_and_unknown_prefix_is_auto() -> None:
    assert WAIT_KIND_BY_ARM == {
        "deploy": WaitKind.AUTO,
        "pr": WaitKind.MANUAL,
        "thread": WaitKind.MANUAL,
        "queue-empty": WaitKind.MANUAL,
    }
    assert wait_kind_for_arm("something-new") is WaitKind.AUTO
    assert wait_kind_for_arm(None) is WaitKind.AUTO


def test_d_next_human_and_well_formed_operator() -> None:
    assert _cls("ask\n\nNEXT: human").wait is WaitKind.HUMAN
    operator = _cls("x\n\nOPERATOR-TASK: re-register the task\nTIER-C-CHECK: none\nNEXT: operator")
    assert (operator.group, operator.wait) == (HeadGroup.WAITING, WaitKind.MANUAL)


def test_thresholds_per_group() -> None:
    six = N_THRESHOLDS["thread_with_nomination"]
    assert six == timedelta(hours=6)
    assert threshold_for(HeadClass(HeadGroup.NOMINATED, "x")) == six
    assert threshold_for(HeadClass(HeadGroup.UNEXPLAINED, "x")) == six
    assert threshold_for(HeadClass(HeadGroup.DONE, "x")) is None
    assert threshold_for(None) == N_UNREAD == timedelta(hours=72)


# ─── Tick level: cache, budget, eviction ───────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@dataclass
class FakeChatroom:
    """A chatroom with threads ``tid -> (row, body, field)``. Counts head reads per thread."""

    clock: FakeClock
    threads: dict[str, dict[str, Any]] = field(default_factory=dict)
    reads: dict[str, int] = field(default_factory=dict)
    fail_listing: bool = False
    extra_rows: list[Any] = field(default_factory=list)
    read_seconds: float = 0.0
    fail_reads: bool = False

    def put(
        self,
        tid: str,
        body: str,
        *,
        msg: str = "m-1",
        at: datetime = LAST,
        status: str = "active",
        field_value: str | None = None,
    ) -> None:
        self.threads[tid] = {
            "row": {
                "thread_id": tid,
                "status": status,
                "created_at": CREATED.isoformat(),
                "last_msg_id": msg,
                "last_activity_at": at.isoformat(),
            },
            "messages": [
                {"msg_id": "m-0", "content": "older"},
                {"msg_id": msg, "content": body, "next_participant": field_value},
            ],
        }

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_list_threads":
            if self.fail_listing:
                raise RuntimeError("listing down")
            rows = [t["row"] for t in self.threads.values()] + self.extra_rows
            off = arguments["offset"]
            return {"items": rows[off : off + 200], "total": len(rows)}
        if name == "chatroom_get_thread":
            tid = arguments["thread_id"]
            self.reads[tid] = self.reads.get(tid, 0) + 1
            self.clock.t += self.read_seconds
            if self.fail_reads:
                raise RuntimeError("get_thread down")
            return {"messages": self.threads[tid]["messages"]}
        raise AssertionError(name)


def _tick(state: Path, room: FakeChatroom, now: datetime, **kw: Any) -> TickOutcome:
    return asyncio.run(
        run_tick(
            paths=TickPaths(state_dir=state),
            adapters=[ChatroomThreadAdapter(room, PROJECT)],
            now=lambda: now,
            monotonic=room.clock,
            out=io.StringIO(),
            **kw,
        )
    )


def _store(state: Path) -> dict[str, Any]:
    data = json.loads((state / "stall-ledger.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _heads(state: Path) -> dict[str, Any]:
    heads = _store(state).get(THREAD_HEADS_KEY, {})
    assert isinstance(heads, dict)
    return heads


def _heartbeat(outcome: TickOutcome) -> dict[str, Any]:
    (line,) = [line for line in outcome.lines if line["kind"] == "heartbeat"]
    return line


def _key(tid: str) -> str:
    return f"thread:{PROJECT}/{tid}"


def test_unchanged_key_reads_nothing_changed_key_reads_once(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock())
    room.put("T-a", "x\n\nNEXT: Bohr")
    _tick(tmp_path, room, LAST + timedelta(hours=1))
    assert room.reads == {"T-a": 1}
    _tick(tmp_path, room, LAST + timedelta(hours=2))
    assert room.reads == {"T-a": 1}  # same (last_msg_id, last_activity_at): 0 fetches
    room.put("T-a", "y\n\nNEXT: Heisenberg", msg="m-2", at=LAST + timedelta(hours=2))
    _tick(tmp_path, room, LAST + timedelta(hours=3))
    assert room.reads == {"T-a": 2}  # changed: exactly 1 fetch
    assert _heads(tmp_path)[_key("T-a")]["last_msg_id"] == "m-2"


def test_last_activity_at_alone_changing_reads_again(tmp_path: Path) -> None:
    # msg-8370 Obj-4-3: the key is the pair, so an edit that moves only the activity time counts.
    room = FakeChatroom(FakeClock())
    room.put("T-a", "x\n\nNEXT: Bohr")
    _tick(tmp_path, room, LAST + timedelta(hours=1))
    room.put("T-a", "x\n\nNEXT: Bohr", at=LAST + timedelta(minutes=5))
    _tick(tmp_path, room, LAST + timedelta(hours=2))
    assert room.reads == {"T-a": 2}


def test_nominated_head_opens_after_6h_not_before(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock())
    room.put("T-a", "x\n\nNEXT: Heisenberg")
    out = _tick(tmp_path, room, LAST + timedelta(hours=5))
    assert out.counters.opened == 0
    out = _tick(tmp_path, room, LAST + timedelta(hours=7))
    assert out.counters.opened == 1
    assert _key("T-a") in _store(tmp_path)["records"]


def test_unexplained_head_opens_after_6h(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock())
    room.put("T-c", "x\n\nNEXT: none")
    out = _tick(tmp_path, room, LAST + timedelta(hours=7))
    assert out.counters.opened == 1


def test_done_head_never_opens_and_closes_an_open_record(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock())
    room.put("T-b", "x\n\nNEXT: none")  # C: opens at 7h
    _tick(tmp_path, room, LAST + timedelta(hours=7))
    assert _key("T-b") in _store(tmp_path)["records"]
    room.put("T-b", "finished\n\nSTOP: done", msg="m-2")
    out = _tick(tmp_path, room, LAST + timedelta(hours=200))
    assert out.counters.opened == 0
    assert _store(tmp_path)["records"] == {}


def test_waiting_head_keeps_the_provisional_72h(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock())
    room.put("T-d", "x\n\nBLOCKED-ON: pr o/r#1\nNEXT: Bohr")
    assert _tick(tmp_path, room, LAST + timedelta(hours=70)).counters.opened == 0
    assert _tick(tmp_path, room, LAST + timedelta(hours=73)).counters.opened == 1


def test_head_over_budget_is_pending_and_keeps_72h(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock(), read_seconds=30.0)
    room.put("T-1", "x\n\nNEXT: Bohr")
    room.put("T-2", "x\n\nNEXT: Bohr")
    # Fetch window = t_tick_max - FETCH_MARGIN (2 min) = 30s: one 30s read fits, then no more.
    kw = {"t_tick_max": timedelta(minutes=2, seconds=30)}
    out = _tick(tmp_path, room, LAST + timedelta(hours=7), **kw)
    assert sum(room.reads.values()) == 1
    hb = _heartbeat(out)
    assert hb["pending_heads"] == 1
    assert hb["head_fetches"] == 1
    # The read one is A and opens at 7h; the unread one is C / 72h and does not.
    assert out.counters.opened == 1
    (unread,) = [t for t in ("T-1", "T-2") if t not in room.reads]
    assert _key(unread) not in _store(tmp_path)["records"]
    assert _key(unread) not in _heads(tmp_path)
    # The next tick reads the owed head, and it opens.
    room.clock.t = 0.0
    out = _tick(tmp_path, room, LAST + timedelta(hours=8), **kw)
    assert room.reads[unread] == 1
    assert _heartbeat(out)["pending_heads"] == 0
    assert _key(unread) in _store(tmp_path)["records"]


def test_failed_head_read_is_pending_and_retried(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock(), fail_reads=True)
    room.put("T-a", "x\n\nNEXT: Bohr")
    out = _tick(tmp_path, room, LAST + timedelta(hours=7))
    assert _heartbeat(out)["pending_heads"] == 1
    assert out.counters.opened == 0
    assert _heads(tmp_path) == {}
    # A transient failure is retried on the next tick.
    room.fail_reads = False
    out = _tick(tmp_path, room, LAST + timedelta(hours=8))
    assert room.reads == {"T-a": 2}
    assert _heartbeat(out)["pending_heads"] == 0


@pytest.mark.parametrize(
    ("messages", "detail"),
    [
        ([{"msg_id": "other", "content": "?"}], "head_missing"),
        ([], "head_missing"),
        ([{"msg_id": "m-1", "content": None}], "head_malformed"),
    ],
)
def test_missing_or_malformed_head_is_cached_as_c_not_retried(
    tmp_path: Path, messages: list[Any], detail: str
) -> None:
    # PR #541 Obj-1-1: a read that succeeds without a usable head is a definite answer.
    room = FakeChatroom(FakeClock())
    room.put("T-a", "x\n\nNEXT: Bohr")
    room.threads["T-a"]["messages"] = messages
    out = _tick(tmp_path, room, LAST + timedelta(hours=7))
    hb = _heartbeat(out)
    assert hb["pending_heads"] == 0
    assert hb["head_fetches"] == 1
    assert out.counters.opened == 1  # C opens at 6h
    assert _heads(tmp_path)[_key("T-a")]["group"] == HeadGroup.UNEXPLAINED.value
    assert _heads(tmp_path)[_key("T-a")]["detail"] == detail
    # Same key: the head is not fetched again, so it cannot keep spending the budget.
    for hours in (8, 9, 10):
        out = _tick(tmp_path, room, LAST + timedelta(hours=hours))
        assert _heartbeat(out)["head_fetches"] == 0
        assert _heartbeat(out)["pending_heads"] == 0
    # A new post changes the key and the head is read once.
    room.put("T-a", "done\n\nSTOP: done", msg="m-2")
    out = _tick(tmp_path, room, LAST + timedelta(hours=11))
    assert _heartbeat(out)["head_fetches"] == 1
    assert _heads(tmp_path)[_key("T-a")]["group"] == HeadGroup.DONE.value


# ─── Eviction (msg-8480) ───────────────────────────────────────────────────────────────


def test_resolved_thread_is_evicted_that_tick(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock())
    room.put("T-a", "x\n\nNEXT: Bohr")
    _tick(tmp_path, room, LAST + timedelta(hours=1))
    assert _key("T-a") in _heads(tmp_path)
    room.threads["T-a"]["row"]["status"] = "resolved"
    _tick(tmp_path, room, LAST + timedelta(hours=2))
    assert _heads(tmp_path) == {}
    assert room.reads == {"T-a": 1}  # a resolved thread is not read


def test_thread_absent_from_a_complete_listing_is_evicted(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock())
    room.put("T-a", "x\n\nNEXT: Bohr")
    room.put("T-b", "x\n\nNEXT: Bohr")
    _tick(tmp_path, room, LAST + timedelta(hours=1))
    del room.threads["T-a"]
    _tick(tmp_path, room, LAST + timedelta(hours=2))
    assert set(_heads(tmp_path)) == {_key("T-b")}


def test_failed_listing_evicts_nothing(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock())
    room.put("T-a", "x\n\nNEXT: Bohr")
    _tick(tmp_path, room, LAST + timedelta(hours=1))
    room.fail_listing = True
    _tick(tmp_path, room, LAST + timedelta(hours=2))
    assert set(_heads(tmp_path)) == {_key("T-a")}


def test_incomplete_listing_evicts_nothing(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock())
    room.put("T-a", "x\n\nNEXT: Bohr")
    room.put("T-b", "x\n\nNEXT: Bohr")
    _tick(tmp_path, room, LAST + timedelta(hours=1))
    del room.threads["T-a"]
    room.extra_rows = [{"thread_id": 3}]  # unkeyed row -> listing not complete
    _tick(tmp_path, room, LAST + timedelta(hours=2))
    assert set(_heads(tmp_path)) == {_key("T-a"), _key("T-b")}


def test_evicted_then_reopened_thread_is_read_once(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock())
    room.put("T-a", "x\n\nNEXT: Bohr")
    _tick(tmp_path, room, LAST + timedelta(hours=1))
    room.threads["T-a"]["row"]["status"] = "resolved"
    _tick(tmp_path, room, LAST + timedelta(hours=2))
    room.threads["T-a"]["row"]["status"] = "active"
    _tick(tmp_path, room, LAST + timedelta(hours=3))
    _tick(tmp_path, room, LAST + timedelta(hours=4))
    assert room.reads == {"T-a": 2}


def test_broken_cache_value_is_reread_not_corrupt(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock())
    room.put("T-a", "x\n\nNEXT: Bohr")
    _tick(tmp_path, room, LAST + timedelta(hours=1))
    path = tmp_path / "stall-ledger.json"
    data = _store(tmp_path)
    data[THREAD_HEADS_KEY] = {_key("T-a"): {"group": "Z"}}
    path.write_text(json.dumps(data), encoding="utf-8")
    out = _tick(tmp_path, room, LAST + timedelta(hours=2))
    assert _heartbeat(out)["store"] == "valid"
    assert room.reads == {"T-a": 2}
    assert _heads(tmp_path)[_key("T-a")]["group"] == "A"
