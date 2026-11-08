"""settled_close (T-done-disposition-auto-close boundary 6, Bohr msg-8455 §2 6-1 .. 6-3)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.magickit.client import MagickitMcpError, raise_if_envelope
from spirrow_mindwire.magickit.read_only import ReadOnlyViolationError
from spirrow_mindwire.settled_close import __main__ as cli
from spirrow_mindwire.settled_close.classify import (
    LISTED_CLASSES,
    REFUSAL_REASON_CLASS,
    OwnerKind,
    SettledClass,
    ThreadHead,
    classify,
    refusal_class,
)
from spirrow_mindwire.settled_close.config import (
    SettledCloseConfig,
    SettledCloseConfigError,
    parse_settled_close_config,
    sweep_projects,
)
from spirrow_mindwire.settled_close.runner import (
    CLOSE_TOOLS,
    READ_TOOLS,
    render_table,
    run_pass,
)

_P = "spirrow-mindwire"
_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
_OLD = (_NOW - timedelta(days=2)).isoformat()
_GRACE = timedelta(hours=24)
_ROSTER: Mapping[str, Any] = {
    "Bohr": "proposer",
    "Heisenberg": "implementer",
    "Einstein": "naysayer",
}
_DONE_BODY = "Settled.\n\nSTOP: done"


def _msg(
    *,
    msg_id: str = "msg-10",
    content: str = _DONE_BODY,
    disposition: Any = None,
    role: str | None = "proposer",
    next_participant: str | None = None,
    timestamp: str = _OLD,
) -> dict[str, Any]:
    return {
        "msg_id": msg_id,
        "content": content,
        "disposition": {"kind": "done"} if disposition is None else disposition,
        "role": role,
        "next_participant": next_participant,
        "timestamp": timestamp,
        "author": "Bohr",
    }


def _head(
    msg: dict[str, Any] | None = None, *, owner: str = "Fermi", tags: tuple[str, ...] = ()
) -> ThreadHead:
    return ThreadHead(_P, "T-x", owner, tags, msg if msg is not None else _msg())


def _cls(head: ThreadHead, owner_kind: OwnerKind = OwnerKind.AGENT, **kw: Any) -> Any:
    return classify(
        head,
        closer=kw.pop("closer", "orchestrator"),
        now=_NOW,
        grace=_GRACE,
        roster=_ROSTER,
        owner_kind=owner_kind,
        **kw,
    )


# --- classify: one cell per check, in magickit's order --------------------------------------


def test_a_proposer_done_past_grace_with_an_agent_owner_would_close() -> None:
    assert _cls(_head()).cls is SettledClass.WOULD_CLOSE


def test_a_pr_gate_ledger_belongs_to_phase_2() -> None:
    v = _cls(_head(owner="orchestrator", tags=("pr-review",)))
    assert (v.cls, v.reason) == (SettledClass.PR_GATE_LEDGER, "pr_gate_ledger")


def test_a_head_without_a_msg_id_is_unreadable() -> None:
    assert _cls(_head(_msg(msg_id=""))).cls is SettledClass.UNREADABLE


@pytest.mark.parametrize(
    "disposition",
    [{"kind": "blocked_on", "trigger": {"arm": "pr", "ref": "o/r#1"}, "wake": "Bohr"}, {}],
)
def test_a_head_that_is_not_done_is_not_settled(disposition: Any) -> None:
    assert _cls(_head(_msg(disposition=disposition))).cls is SettledClass.NOT_SETTLED


def test_a_stored_done_whose_body_does_not_resolve_to_done_is_not_eligible() -> None:
    """msg-8177 §B: the resolver is the necessary condition, not the stored field alone."""
    v = _cls(_head(_msg(content="More to do.\n\nNEXT: Heisenberg")))
    assert (v.cls, v.reason) == (SettledClass.NOT_ELIGIBLE, "body-disagrees")


def test_a_done_that_names_a_successor_is_not_eligible() -> None:
    v = _cls(_head(_msg(next_participant="Bohr")))
    assert (v.cls, v.reason) == (SettledClass.NOT_ELIGIBLE, "names_successor")


def test_a_gated_thread_is_gate_pending_whatever_its_role() -> None:
    """msg-8328 §1 / msg-8270 §3: the gate runs first and answers NaysayerReviewStaleError."""
    for role in ("proposer", "naysayer", None):
        v = _cls(_head(_msg(role=role), tags=("gate:naysayer",)))
        assert v.cls is SettledClass.GATE_PENDING


def test_a_closer_that_owns_the_thread_needs_no_carve_out() -> None:
    v = _cls(_head(owner="orchestrator"))
    assert (v.cls, v.reason) == (SettledClass.OWNER_CAN_CLOSE, "owner_is_author")


@pytest.mark.parametrize(
    ("role", "kind"),
    [
        ("human", "human-done"),
        ("naysayer", "naysayer-done"),
        ("implementer", "implementer-done"),
        (None, "role-unknown"),
    ],
)
def test_only_a_proposer_done_is_eligible(role: str | None, kind: str) -> None:
    """msg-8185 Obj-5-1: the eligible role set is {proposer}; the rest are listed by kind."""
    v = _cls(_head(_msg(role=role)))
    assert (v.cls, v.reason) == (SettledClass.NOT_ELIGIBLE, kind)


@pytest.mark.parametrize("timestamp", ["", "yesterday", "2026-11-01T00:00:00"])
def test_an_unreadable_or_naive_timestamp_is_not_eligible(timestamp: str) -> None:
    v = _cls(_head(_msg(timestamp=timestamp)))
    assert (v.cls, v.reason) == (SettledClass.NOT_ELIGIBLE, "timestamp_unreadable")


def test_a_done_inside_the_grace_is_deferred_with_its_due_time() -> None:
    posted = _NOW - timedelta(hours=3)
    v = _cls(_head(_msg(timestamp=posted.isoformat())))
    assert (v.cls, v.reason, v.due_at) == (SettledClass.DEFERRED, "grace_pending", posted + _GRACE)


def test_the_grace_boundary_is_inclusive_like_magickit() -> None:
    """magickit refuses when ``age < grace``: exactly the grace is old enough."""
    v = _cls(_head(_msg(timestamp=(_NOW - _GRACE).isoformat().replace("+00:00", "Z"))))
    assert v.cls is SettledClass.WOULD_CLOSE


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        (OwnerKind.HUMAN, "owner_human"),
        (OwnerKind.UNRESOLVED, "owner_unresolved"),
        (OwnerKind.UNAVAILABLE, "owner_lookup_unavailable"),
        (OwnerKind.MISSING, "owner_missing"),
    ],
)
def test_an_owner_not_shown_to_be_an_agent_is_owner_pending(kind: OwnerKind, reason: str) -> None:
    v = _cls(_head(), owner_kind=kind)
    assert (v.cls, v.reason) == (SettledClass.OWNER_PENDING, reason)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("Done.\n\nSTOP: done\nNEXT: none", SettledClass.LEGACY_DONE),
        ("Wait.\n\nSTOP: blocked-on pr:o/r#1 wake:Bohr\nNEXT: none", SettledClass.LEGACY_BLOCKED),
        ("Bare.\n\nNEXT: none", SettledClass.UNCLASSIFIED),
    ],
)
def test_legacy_heads_are_sorted_only_with_include_legacy(
    content: str, expected: SettledClass
) -> None:
    """msg-8172 §4: a null disposition is sorted by the conductor's resolver; the server would
    refuse every one of them (``not_done``), so they are listed, never closed."""
    head = _head({**_msg(content=content), "disposition": None})
    with_legacy = _cls(head, include_legacy=True)
    assert (with_legacy.cls, with_legacy.reason) == (expected, "not_done")
    assert _cls(head).cls is SettledClass.NOT_SETTLED


def test_a_legacy_handoff_is_not_settled_even_with_include_legacy() -> None:
    head = _head({**_msg(content="Go.\n\nNEXT: Heisenberg"), "disposition": None})
    assert _cls(head, include_legacy=True).cls is SettledClass.NOT_SETTLED


# --- refusal mapping ---------------------------------------------------------------------------


def test_refusals_map_by_details_reason_then_error_type() -> None:
    assert refusal_class("SettledDispositionNotClosableError", "head_moved") is (
        SettledClass.HEAD_MOVED
    )
    assert refusal_class("SettledDispositionNotClosableError", "grace_pending") is (
        SettledClass.DEFERRED
    )
    assert refusal_class("NaysayerReviewStaleError", None) is SettledClass.GATE_PENDING
    # closer_not_configured is today's answer to every close; it is not placed, it is refused.
    assert refusal_class("SettledDispositionNotClosableError", "closer_not_configured") is (
        SettledClass.REFUSED
    )
    assert refusal_class(None, None) is SettledClass.REFUSED


def test_every_mapped_refusal_lands_in_a_listed_class() -> None:
    listed = set(LISTED_CLASSES) | {SettledClass.PR_GATE_LEDGER}
    assert set(REFUSAL_REASON_CLASS.values()) <= listed


def test_the_client_carries_details_reason_from_the_envelope() -> None:
    envelope = {
        "error_type": "SettledDispositionNotClosableError",
        "error": "no",
        "details": {"reason": "head_moved", "head_msg_id": "msg-1"},
    }
    with pytest.raises(MagickitMcpError) as info:
        raise_if_envelope(envelope)
    assert info.value.error_type == "SettledDispositionNotClosableError"
    assert info.value.details_reason == "head_moved"
    with pytest.raises(MagickitMcpError) as info:
        raise_if_envelope({"error_type": "X", "details": {"reason": 3}})
    assert info.value.details_reason is None


# --- config ------------------------------------------------------------------------------------


def test_absent_config_is_disabled() -> None:
    assert parse_settled_close_config({}) == SettledCloseConfig()
    assert SettledCloseConfig().enabled is False


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"enabled": "yes"},
        {"enabled": True, "max_attempts": 0},
        {"breaker": True},
        {"author": " "},
        {"grace_seconds": -1},
    ],
)
def test_bad_config_is_refused(raw: Any) -> None:
    with pytest.raises(SettledCloseConfigError):
        parse_settled_close_config({"settled_close": raw})


def test_sweep_projects_reads_clones_projects_and_candidates() -> None:
    sweep = {
        "clones": {"a": "C:/a"},
        "projects": ["b", " "],
        "candidates": [{"project": "c", "thread_id": "T"}, "junk"],
    }
    assert sweep_projects(sweep) == ["a", "b", "c"]


# --- run_pass ----------------------------------------------------------------------------------


class _Chatroom:
    """Threads with one head each, identities, and a close that magickit would refuse or allow."""

    def __init__(
        self,
        threads: dict[str, dict[str, Any]],
        identities: dict[str, list[str]] | None = None,
        close: dict[str, dict[str, Any] | None] | None = None,
    ) -> None:
        self.threads = threads
        self.identities = identities or {}
        self.close = close or {}
        self.status: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name == "chatroom_list_threads":
            items = [{"thread_id": t} for t in self.threads]
            return {"items": items if arguments.get("offset", 0) == 0 else [], "total": len(items)}
        if name == "chatroom_get_thread":
            tid = arguments["thread_id"]
            t = self.threads[tid]
            thread = {"owner": t["owner"], "tags": t.get("tags", [])}
            thread["status"] = self.status.get(tid, "active")
            return {"thread": thread, "messages": [t["head"]]}
        if name == "get_identity":
            roles = self.identities.get(arguments["identity_name"])
            if roles is None:
                return {"status": "not_found"}
            return {"status": "found", "identity": {"allowed_roles": roles}}
        if name == "chatroom_close_thread":
            tid = arguments["thread_id"]
            envelope = self.close.get(tid)
            if envelope is None:
                self.status[tid] = "resolved"
                return {"thread": {"status": "resolved"}}
            raise_if_envelope(envelope)
        raise AssertionError(name)


def _threads() -> dict[str, dict[str, Any]]:
    return {
        "T-close": {"owner": "operator", "head": _msg(msg_id="msg-1")},
        "T-fresh": {
            "owner": "operator",
            "head": _msg(msg_id="msg-2", timestamp=(_NOW - timedelta(hours=1)).isoformat()),
        },
        "T-human": {"owner": "Fermi", "head": _msg(msg_id="msg-3")},
        "T-gated": {"owner": "operator", "tags": ["gate:naysayer"], "head": _msg(msg_id="msg-4")},
        "T-busy": {"owner": "operator", "head": _msg(msg_id="msg-5", content="Go.\nNEXT: Bohr")},
    }


_IDS = {"operator": ["integrator"], "Fermi": ["proposer", "reviewer", "human"]}


def _run(room: _Chatroom, *, enabled: bool, config: SettledCloseConfig | None = None) -> Any:
    return asyncio.run(
        run_pass(
            room,
            projects=[_P],
            config=config or SettledCloseConfig(),
            enabled=enabled,
            include_legacy=False,
            roster=_ROSTER,
            now=_NOW,
        )
    )


def test_a_dry_run_reads_only_and_sorts_every_thread() -> None:
    room = _Chatroom(_threads(), _IDS)
    report = _run(room, enabled=False)
    assert set(room.names()) <= READ_TOOLS
    assert report.close_calls == 0
    got = {r.thread_id: r.verdict.cls for r in report.rows}
    assert got == {
        "T-close": SettledClass.WOULD_CLOSE,
        "T-fresh": SettledClass.DEFERRED,
        "T-human": SettledClass.OWNER_PENDING,
        "T-gated": SettledClass.GATE_PENDING,
        "T-busy": SettledClass.NOT_ELIGIBLE,
    }
    # one registry read per distinct owner, not per thread
    assert sorted(a["identity_name"] for n, a in room.calls if n == "get_identity") == [
        "Fermi",
        "operator",
    ]
    out = report.as_dict()
    assert out["enabled"] is False and out["close_calls"] == 0
    assert out["authority"].startswith("magickit decides")
    json.dumps(out)


def test_an_enabled_pass_closes_only_would_close_and_sends_the_judged_head() -> None:
    room = _Chatroom(_threads(), _IDS)
    report = _run(room, enabled=True)
    closes = [a for n, a in room.calls if n == "chatroom_close_thread"]
    assert [(a["thread_id"], a["settled_head_msg_id"]) for a in closes] == [("T-close", "msg-1")]
    assert closes[0]["role"] == "integrator" and closes[0]["embodiment"] == "unknown"
    assert [r.thread_id for r in report.by_class(SettledClass.CLOSED)] == ["T-close"]
    assert report.close_calls == 1


def test_an_enabled_refusal_is_placed_by_the_servers_typed_reason() -> None:
    def refuse(reason: str) -> dict[str, Any]:
        return {
            "error_type": "SettledDispositionNotClosableError",
            "error": "refused",
            "details": {"reason": reason},
        }

    room = _Chatroom(_threads(), _IDS, close={"T-close": refuse("head_moved")})
    row = _run(room, enabled=True).by_class(SettledClass.HEAD_MOVED)[0]
    assert (row.thread_id, row.details_reason, row.observed_status) == (
        "T-close",
        "head_moved",
        "active",
    )
    room = _Chatroom(_threads(), _IDS, close={"T-close": refuse("closer_not_configured")})
    row = _run(room, enabled=True).by_class(SettledClass.REFUSED)[0]
    assert row.as_dict()["details_reason"] == "closer_not_configured"


def test_the_read_back_decides_even_when_the_close_raised() -> None:
    room = _Chatroom(_threads(), _IDS)

    async def flaky(name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_close_thread":
            room.status[arguments["thread_id"]] = "resolved"
            raise MagickitMcpError("transport hiccup after commit")
        return await _Chatroom.call_tool(room, name, arguments)

    room.call_tool = flaky  # type: ignore[method-assign]
    assert [r.thread_id for r in _run(room, enabled=True).by_class(SettledClass.CLOSED)] == [
        "T-close"
    ]


def test_the_cap_and_the_breaker_bound_an_enabled_pass() -> None:
    threads = {f"T-{i}": {"owner": "operator", "head": _msg(msg_id=f"msg-{i}")} for i in range(6)}
    refuse = {
        "error_type": "SettledDispositionNotClosableError",
        "details": {"reason": "closer_not_configured"},
    }
    room = _Chatroom(threads, _IDS, close=dict.fromkeys(threads, refuse))
    report = _run(room, enabled=True, config=SettledCloseConfig(breaker=2))
    assert report.close_calls == 2 and report.breaker_tripped
    assert {why for _, why in report.not_reached} == {"breaker"}
    room = _Chatroom(threads, _IDS)
    report = _run(room, enabled=True, config=SettledCloseConfig(max_attempts=4))
    assert report.close_calls == 4
    assert [why for _, why in report.not_reached] == ["attempt-cap", "attempt-cap"]


def test_a_listing_failure_costs_only_that_project() -> None:
    room = _Chatroom(_threads(), _IDS)

    async def broken(name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_list_threads" and arguments["project"] == "bad":
            raise MagickitMcpError("down")
        return await _Chatroom.call_tool(room, name, arguments)

    room.call_tool = broken  # type: ignore[method-assign]
    report = asyncio.run(
        run_pass(
            room,
            projects=["bad", _P],
            config=SettledCloseConfig(),
            enabled=False,
            include_legacy=False,
            roster=_ROSTER,
            now=_NOW,
        )
    )
    assert report.errors and report.errors[0]["project"] == "bad"
    assert len(report.rows) == 5


def test_the_table_leads_with_the_authority_note_and_lists_threads() -> None:
    table = render_table(_run(_Chatroom(_threads(), _IDS), enabled=False))
    assert table.splitlines()[2].startswith("**magickit decides")
    assert "| spirrow-mindwire/T-close | operator | msg-1 | - |" in table
    assert "### deferred (1)" in table


# --- CLI: a disabled tick never builds an MCP client ---------------------------------------------


def _forbid_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("an MCP client was built")

    monkeypatch.setattr(cli, "StreamableHttpChatroomMcp", boom)


@pytest.mark.parametrize(
    "sweep", [None, {}, {"settled_close": {}}, {"settled_close": {"enabled": False}}]
)
def test_a_disabled_tick_makes_no_mcp_call(
    sweep: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _forbid_mcp(monkeypatch)
    argv = ["--tick", "--projects", _P]
    if sweep is not None:
        path = tmp_path / "sweep.json"
        path.write_text(json.dumps(sweep), encoding="utf-8")
        argv += ["--sweep-config", str(path)]
    assert cli.main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"phase": "settled_close", "enabled": False, "close_calls": 0}


def test_a_bad_config_fails_before_any_mcp_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _forbid_mcp(monkeypatch)
    path = tmp_path / "sweep.json"
    path.write_text(json.dumps({"settled_close": {"enabled": "true"}}), encoding="utf-8")
    assert cli.main(["--tick", "--sweep-config", str(path)]) == 1
    assert "settled_close.enabled" in json.loads(capsys.readouterr().out)["error"]


def test_a_dry_run_cannot_send_a_close_even_if_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dry-run's client is wrapped in READ_TOOLS: a close raises before it leaves."""
    from spirrow_mindwire.magickit.read_only import ReadOnlyMcp

    guard = ReadOnlyMcp(_Chatroom({}), READ_TOOLS, label="settled_close")
    with pytest.raises(ReadOnlyViolationError):
        asyncio.run(guard.call_tool("chatroom_close_thread", {}))
    assert "chatroom_close_thread" not in READ_TOOLS
    assert {"chatroom_close_thread"} == CLOSE_TOOLS - READ_TOOLS


def test_the_operator_cli_exposes_settled_close() -> None:
    from spirrow_mindwire.cli import _build_parser

    args = _build_parser().parse_args(["settled-close", "--dry-run", "--include-legacy"])
    assert args.command == "settled-close" and args.dry_run and args.include_legacy
