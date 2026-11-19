"""Tests for :mod:`spirrow_mindwire.thread_relations` (msg-9729 §3 required cases + msg-9731)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from spirrow_mindwire.pr_event_advance.decide import PR_EVENT_RELAY_AUTHOR
from spirrow_mindwire.thread_relations import (
    MERGED_DISPLAY_LIMIT,
    Edge,
    ThreadSnapshot,
    collect_edges,
    digest_merged_lines,
    merged_events,
    render_references_notice,
    snapshot_of,
    thread_references,
)


def _msg(
    msg_id: str,
    *,
    author: str = "Bohr",
    content: str = "x",
    refs: list[str] | None = None,
    at: str = "2026-11-19T00:00:00+00:00",
) -> dict[str, Any]:
    m: dict[str, Any] = {"msg_id": msg_id, "author": author, "content": content, "timestamp": at}
    if refs is not None:
        m["references_threads"] = refs
    return m


def _merged(msg_id: str, number: int, at: str) -> dict[str, Any]:
    body = (
        f"PR event (1b, {PR_EVENT_RELAY_AUTHOR}) — o/r#{number}\n\n"
        f"PR o/r#{number} が merge された（abc123）。"  # noqa: RUF001
    )
    return _msg(msg_id, author=PR_EVENT_RELAY_AUTHOR, content=body, at=at)


def _snap(
    tid: str, status: str = "open", msgs: list[dict[str, Any]] | None = None, **kw: Any
) -> ThreadSnapshot:
    return ThreadSnapshot(thread_id=tid, status=status, messages=tuple(msgs or []), **kw)


# --- required: msg-9729 §3 ------------------------------------------------------------------


def test_closed_referrer_stays_visible_from_referenced_thread() -> None:
    """A references B, then A is closed (affects_threads empty): B still sees A."""
    a = _snap("T-a", status="resolved", msgs=[_msg("msg-1", refs=["T-b"])])
    b = _snap("T-b", msgs=[_msg("msg-2")])
    edges = collect_edges([a, b])
    refs_b = thread_references(edges, "T-b")
    assert [r.other for r in refs_b.incoming] == ["T-a"]
    assert refs_b.outgoing == ()
    notice = render_references_notice(refs_b, {"T-a": a, "T-b": b})
    assert notice is not None
    assert "参照: T-a → このスレッド（msg-1） status=resolved" in notice  # noqa: RUF001


def test_many_messages_to_same_target_render_one_line() -> None:
    a = _snap(
        "T-a",
        msgs=[
            _msg("msg-1", refs=["T-b"]),
            _msg("msg-5", refs=["T-b", "T-b"]),
            _msg("msg-9", refs=["T-b"]),
        ],
    )
    b = _snap("T-b")
    refs = thread_references(collect_edges([a, b]), "T-a")
    assert len(refs.outgoing) == 1
    assert refs.outgoing[0].msg_ids == ("msg-1", "msg-5", "msg-9")
    notice = render_references_notice(refs, {"T-b": b})
    assert notice is not None
    assert notice.count("参照: このスレッド → T-b") == 1


def test_merge_survives_later_posts_in_dispatch_notice() -> None:
    b = _snap(
        "T-b",
        msgs=[
            _merged("msg-10", 7, "2026-11-01T00:00:00+00:00"),
            _msg("msg-11", author="Bohr", content="next round"),
            _msg("msg-12", author="Einstein", content="go"),
        ],
    )
    a = _snap("T-a", msgs=[_msg("msg-1", refs=["T-b"])])
    notice = render_references_notice(thread_references(collect_edges([a, b]), "T-a"), {"T-b": b})
    assert notice is not None
    assert "  merged: o/r#7（msg-10, 2026-11-01T00:00:00+00:00）" in notice  # noqa: RUF001
    assert "last_msg=2026-11-19T00:00:00+00:00 by Einstein" in notice


def test_four_merges_show_only_newest_three() -> None:
    """msg-9731: no time window, but at most 3 per counterpart; the oldest of 4 is not shown."""
    b = _snap(
        "T-b",
        msgs=[_merged(f"msg-{n}", n, f"2026-11-0{n}T00:00:00+00:00") for n in (1, 2, 3, 4)],
    )
    a = _snap("T-a", msgs=[_msg("msg-20", refs=["T-b"])])
    notice = render_references_notice(thread_references(collect_edges([a, b]), "T-a"), {"T-b": b})
    assert notice is not None
    assert MERGED_DISPLAY_LIMIT == 3
    shown = [line for line in notice.splitlines() if line.startswith("  merged:")]
    assert [s.split("（")[0] for s in shown] == [  # noqa: RUF001
        "  merged: o/r#4",
        "  merged: o/r#3",
        "  merged: o/r#2",
    ]
    assert "o/r#1" not in notice


# --- edge set ----------------------------------------------------------------------------------


def test_late_reference_counts_and_self_reference_is_dropped() -> None:
    a = _snap("T-a", msgs=[_msg("msg-1"), _msg("msg-2", refs=["T-a"]), _msg("msg-3", refs=["T-c"])])
    assert collect_edges([a]) == frozenset({Edge("T-a", "T-c", "msg-3")})


def test_affects_threads_read_only_on_closed_thread() -> None:
    open_a = _snap("T-a", affects_threads=("T-b",))
    closed_c = _snap("T-c", status="superseded", affects_threads=("T-b",))
    edges = collect_edges([open_a, closed_c])
    assert edges == frozenset({Edge("T-c", "T-b", None)})
    notice = render_references_notice(thread_references(edges, "T-b"), {})
    assert notice is not None
    assert "参照: T-c → このスレッド（affects_threads） status=unknown" in notice  # noqa: RUF001


def test_no_reference_means_no_notice() -> None:
    a = _snap("T-a", msgs=[_msg("msg-1")])
    assert render_references_notice(thread_references(collect_edges([a]), "T-a"), {}) is None


def test_malformed_references_are_ignored() -> None:
    a = _snap(
        "T-a",
        msgs=[
            {"msg_id": "msg-1", "references_threads": "T-b"},
            {"msg_id": "msg-2", "references_threads": [1, None, "T-c"]},
        ],
    )
    assert collect_edges([a]) == frozenset({Edge("T-a", "T-c", "msg-2")})


def test_snapshot_of_reads_nested_thread_shape() -> None:
    result = {
        "thread": {"thread_id": "T-a", "status": "resolved", "affects_threads": ["T-b"]},
        "messages": [_msg("msg-1", refs=["T-c"]), "junk"],
    }
    snap = snapshot_of(result)
    assert snap is not None
    assert snap.thread_id == "T-a" and not snap.is_open
    assert snap.affects_threads == ("T-b",)
    assert len(snap.messages) == 1
    assert snapshot_of({"messages": []}) is None
    assert snapshot_of(None) is None


# --- merged_events ---------------------------------------------------------------------------


def test_merged_events_ignore_other_authors_and_quotes() -> None:
    msgs = [
        _msg("msg-1", author="Bohr", content="PR o/r#1 が merge された（x）。"),  # noqa: RUF001
        _msg("msg-2", author=PR_EVENT_RELAY_AUTHOR, content="> PR o/r#2 が merge された（x）。"),  # noqa: RUF001
        _merged("msg-3", 3, "2026-11-03T00:00:00+00:00"),
    ]
    assert [e.slug for e in merged_events(msgs)] == ["o/r#3"]


def test_merged_events_dedupe_same_pr_keep_newest() -> None:
    msgs = [
        _merged("msg-1", 5, "2026-11-01T00:00:00+00:00"),
        _merged("msg-2", 5, "2026-11-02T00:00:00+00:00"),
    ]
    events = merged_events(msgs)
    assert len(events) == 1 and events[0].msg_id == "msg-2"


def test_merged_events_ignore_closed_unmerged_line() -> None:
    body = "PR o/r#9 が merge されずに close された（t、最終 head h）。"  # noqa: RUF001
    assert merged_events([_msg("msg-1", author=PR_EVENT_RELAY_AUTHOR, content=body)]) == ()


# --- digest (D-3) ------------------------------------------------------------------------------


def test_digest_lists_recent_merge_with_open_counterparts_only() -> None:
    a = _snap(
        "T-a",
        status="resolved",
        msgs=[
            _msg("msg-1", refs=["T-b", "T-c"]),
            _merged("msg-2", 11, "2026-11-19T01:00:00+00:00"),
            _merged("msg-3", 10, "2026-11-10T00:00:00+00:00"),
        ],
    )
    b = _snap("T-b")
    c = _snap("T-c", status="resolved")
    d = _snap("T-d", msgs=[_msg("msg-4", refs=["T-a"])])
    lines = digest_merged_lines(
        [a, b, c, d], collect_edges([a, b, c, d]), since=datetime(2026, 11, 18, 2, tzinfo=UTC)
    )
    assert lines == ["T-a の PR o/r#11 merged → 参照のある open スレッド: T-b, T-d"]


def test_digest_skips_merge_without_open_counterpart_or_time() -> None:
    a = _snap("T-a", msgs=[_msg("msg-1", refs=["T-b"]), _merged("msg-2", 1, "not-a-time")])
    b = _snap("T-b")
    since = datetime(2026, 1, 1, tzinfo=UTC)
    assert digest_merged_lines([a, b], collect_edges([a, b]), since=since) == []
    lone = _snap("T-x", msgs=[_merged("msg-3", 2, "2026-11-19T00:00:00+00:00")])
    assert digest_merged_lines([lone], collect_edges([lone]), since=since) == []
