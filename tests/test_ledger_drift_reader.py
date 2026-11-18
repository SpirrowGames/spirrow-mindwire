"""ledger_drift read layer, config and CLI: paging, partial reads, the read-only allowlist."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from spirrow_mindwire.ledger_drift.__main__ import main
from spirrow_mindwire.ledger_drift.config import LedgerDriftConfig, parse_ledger_drift_config
from spirrow_mindwire.ledger_drift.detect import Section
from spirrow_mindwire.ledger_drift.reader import (
    PAGE_SIZE,
    READ_TOOLS,
    list_merged_prs,
    read_inputs,
)
from spirrow_mindwire.magickit.client import MagickitMcpError
from spirrow_mindwire.repo_projects import RepoProjectMap, RepoTarget

P = "proj"
NOW = datetime(2026, 11, 17, 4, 0, tzinfo=UTC)
SINCE = NOW - timedelta(days=30)
TARGET = RepoTarget(P, "o", "r")
REPO_MAP = RepoProjectMap(candidates={"o/r": frozenset({P})}, targets=(TARGET,))


def _pr(number: int, merged: datetime | None, updated: datetime, body: str = "") -> dict[str, Any]:
    return {
        "number": number,
        "merged_at": merged.isoformat().replace("+00:00", "Z") if merged else None,
        "updated_at": updated.isoformat().replace("+00:00", "Z"),
        "body": body,
    }


def _http(pages: dict[int, Any], status: dict[int, int] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        assert request.url.params["base"] == "develop"
        assert request.url.params["sort"] == "updated"
        return httpx.Response((status or {}).get(page, 200), json=pages.get(page, []))

    return httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler))


class FakeMcp:
    def __init__(
        self,
        threads: dict[str, dict[str, Any]],
        tasks: list[dict[str, Any]] | None,
        heads: dict[str, dict[str, Any]] | None = None,
        fail_listing: bool = False,
    ) -> None:
        self.threads = threads
        self.tasks = tasks
        self.heads = heads or {}
        self.fail_listing = fail_listing
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        assert name in READ_TOOLS, name
        self.calls.append(name)
        if name == "list_tasks":
            if self.tasks is None:
                raise MagickitMcpError("down")
            return {"success": True, "tasks": self.tasks}
        if name == "chatroom_list_threads":
            if self.fail_listing:
                raise MagickitMcpError("listing down")
            items = [
                {"thread_id": t}
                for t, v in self.threads.items()
                if v.get("status") in ("active", "awaiting_reply")
            ]
            return {"items": items[arguments["offset"] :], "total": len(items)}
        if name == "chatroom_get_thread":
            tid = arguments["thread_id"]
            if tid not in self.threads:
                return {"thread": None}
            out: dict[str, Any] = {"thread": self.threads[tid]}
            if arguments["mode"] == "full":
                out["messages"] = [self.heads.get(tid, {"role": "implementer"})]
            return out
        raise AssertionError(name)


def test_listing_reads_every_page_and_stops_past_the_window() -> None:
    old = SINCE - timedelta(days=1)
    page1 = [_pr(i, NOW - timedelta(days=1), NOW) for i in range(1, PAGE_SIZE + 1)]
    page2 = [_pr(500, NOW - timedelta(days=2), NOW), _pr(501, old, old)]
    page2 += [_pr(600 + i, None, old) for i in range(PAGE_SIZE - 2)]
    http = _http({1: page1, 2: page2, 3: [_pr(999, NOW, NOW)]})
    prs, why = asyncio.run(
        list_merged_prs(http, TARGET, base="develop", since=SINCE, until=NOW, repo_map=REPO_MAP)
    )
    assert why is None
    numbers = {p.number for p in prs}
    assert 500 in numbers and 501 not in numbers and 999 not in numbers  # page 3 never read
    assert len(numbers) == PAGE_SIZE + 1
    assert all(p.project == P for p in prs)


def test_a_failed_page_keeps_what_was_read_and_says_which_page() -> None:
    page1 = [_pr(i, NOW, NOW) for i in range(PAGE_SIZE)]
    http = _http({1: page1}, status={2: 502})
    prs, why = asyncio.run(
        list_merged_prs(http, TARGET, base="develop", since=SINCE, until=NOW, repo_map=REPO_MAP)
    )
    assert len(prs) == PAGE_SIZE
    assert why is not None and "page 2" in why and "502" in why


def _threads() -> dict[str, dict[str, Any]]:
    return {
        "T-a": {"status": "active", "owner": "takahito", "tags": ["T243"]},
        "T-done": {"status": "awaiting_reply", "owner": "Bohr", "tags": ["T224"]},
        "T-closed": {"status": "resolved", "tags": ["T224"]},
    }


def _tasks() -> list[dict[str, Any]]:
    return [
        {"id": "T243", "status": "not_started", "description": ""},
        {"id": "T224", "status": "completed"},
        {"id": "T191", "status": "not_started", "blocked_by": ["T224"]},
    ]


def _read(mcp: FakeMcp, http: httpx.AsyncClient | None) -> dict[Section, Any]:
    inputs = asyncio.run(
        read_inputs(
            mcp,
            http,
            project=P,
            targets=[TARGET],
            repo_map=REPO_MAP,
            bases=["develop"],
            since=SINCE,
            until=NOW,
        )
    )
    return {s: r.as_dict() for s, r in inputs.sections().items()}


def test_end_to_end_sections() -> None:
    http = _http({1: [_pr(517, NOW, NOW, "Thread: T-a"), _pr(518, NOW, NOW, "no line")]})
    heads = {"T-done": {"role": "proposer", "disposition": {"kind": "done"}}}
    out = _read(FakeMcp(_threads(), _tasks(), heads), http)
    assert [r["task_id"] for r in out[Section.NOT_STARTED]["rows"]] == ["T243"]
    assert out[Section.PR_RECORD]["count"] == 1
    assert [r["reason"] for r in out[Section.UNLINKED_PRS]["rows"]] == ["no-thread"]
    (cand,) = out[Section.CLOSE_CANDIDATES]["rows"]
    assert cand["thread_id"] == "T-done"
    assert cand["owner_kind"] == "agent" and cand["settled_close_target"] is True
    assert out[Section.STALE_BLOCKED_BY]["rows"][0]["task_id"] == "T191"


def test_no_token_blanks_only_the_pr_sections() -> None:
    out = _read(FakeMcp(_threads(), _tasks()), None)
    for s in (Section.NOT_STARTED, Section.PR_RECORD, Section.UNLINKED_PRS):
        assert out[s] == {"complete": False, "incomplete_reasons": ["prs: no GitHub token"]}
    assert out[Section.CLOSE_CANDIDATES]["complete"] is True
    assert out[Section.STALE_BLOCKED_BY]["complete"] is True


def test_task_listing_failure_blanks_every_section_that_reads_tasks() -> None:
    out = _read(FakeMcp(_threads(), None), _http({}))
    assert out[Section.STALE_BLOCKED_BY]["complete"] is False
    assert out[Section.CLOSE_CANDIDATES]["complete"] is False
    # unlinked_prs reads the task map for task-not-in-ledger (PR #585 gate objection 2).
    assert out[Section.UNLINKED_PRS]["complete"] is False


def test_task_listing_failure_never_reports_a_linked_pr_as_task_not_in_ledger() -> None:
    """PR #585 gate objection 2: a PR whose thread names an existing task, read while
    list_tasks is down, must not come out as a complete ``task-not-in-ledger`` row."""
    http = _http({1: [_pr(517, NOW, NOW, "Thread: T-a")]})
    out = _read(FakeMcp(_threads(), None), http)
    for s in (Section.NOT_STARTED, Section.PR_RECORD, Section.UNLINKED_PRS):
        assert out[s]["complete"] is False, s
        assert "count" not in out[s] and "rows" not in out[s]
        assert any(r.startswith("tasks: ") for r in out[s]["incomplete_reasons"])


class TwoProjectMcp(FakeMcp):
    """``list_tasks`` / ``chatroom_get_thread`` answer per project (PR #585 gate objection 3)."""

    def __init__(
        self,
        threads: dict[str, dict[str, Any]],
        tasks: dict[str, list[dict[str, Any]] | None],
        foreign_threads: dict[str, dict[str, Any]],
    ) -> None:
        super().__init__(threads, [])
        self.by_project = tasks
        self.foreign_threads = foreign_threads

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "list_tasks":
            self.calls.append(name)
            rows = self.by_project.get(arguments["project"], [])
            if rows is None:
                raise MagickitMcpError("foreign down")
            return {"success": True, "tasks": rows}
        if name == "chatroom_get_thread" and arguments["project"] != P:
            self.calls.append(name)
            return {"thread": self.foreign_threads.get(arguments["thread_id"])}
        return await super().call_tool(name, arguments)


def test_foreign_ledger_does_not_leak_into_the_projects_blocker_report() -> None:
    foreign: list[dict[str, Any]] = [
        {"id": "T5", "status": "not_started"},
        {"id": "T8", "status": "completed"},
        {"id": "T9", "status": "not_started", "blocked_by": ["T8"]},
    ]
    mcp = TwoProjectMcp(
        _threads(), {P: _tasks(), "other": foreign}, {"T-x": {"status": "active", "tags": ["T5"]}}
    )
    out = _read(mcp, _http({1: [_pr(517, NOW, NOW, "Thread: other/T-x")]}))
    # The cross-project PR still resolves through the foreign ledger ...
    assert [(r["project"], r["task_id"]) for r in out[Section.NOT_STARTED]["rows"]] == [
        ("other", "T5")
    ]
    # ... but (d) reports the report project's ledger only: T191 here, never other/T9.
    assert [(r["project"], r["task_id"]) for r in out[Section.STALE_BLOCKED_BY]["rows"]] == [
        (P, "T191")
    ]


def test_foreign_ledger_failure_blanks_only_the_pr_sections() -> None:
    mcp = TwoProjectMcp(
        _threads(), {P: _tasks(), "other": None}, {"T-x": {"status": "active", "tags": ["T5"]}}
    )
    out = _read(mcp, _http({1: [_pr(517, NOW, NOW, "Thread: other/T-x")]}))
    assert out[Section.UNLINKED_PRS]["complete"] is False
    assert out[Section.UNLINKED_PRS]["incomplete_reasons"][0].startswith("linked_tasks: other")
    assert out[Section.STALE_BLOCKED_BY]["complete"] is True
    assert out[Section.CLOSE_CANDIDATES]["complete"] is True


def test_thread_listing_failure_blanks_close_candidates_only() -> None:
    out = _read(FakeMcp(_threads(), _tasks(), fail_listing=True), _http({}))
    assert out[Section.CLOSE_CANDIDATES]["complete"] is False
    assert out[Section.STALE_BLOCKED_BY]["complete"] is True


# --- config --------------------------------------------------------------------------------


def test_config_defaults_and_validation() -> None:
    assert parse_ledger_drift_config({}) == LedgerDriftConfig()
    cfg = parse_ledger_drift_config(
        {"ledger_drift": {"enabled": True, "window_days": 7, "bases": ["main"]}}
    )
    assert cfg.enabled and cfg.window_days == 7 and cfg.bases == ("main",)
    bads: list[dict[str, Any]] = [
        {"window_days": 0},
        {"enabled": "yes"},
        {"bases": []},
        {"stale_after_minutes": True},
    ]
    for bad in bads:
        with pytest.raises(ValueError):
            parse_ledger_drift_config({"ledger_drift": bad})


# --- CLI -----------------------------------------------------------------------------------


def test_disabled_tick_calls_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sweep = tmp_path / "sweep.json"
    sweep.write_text(json.dumps({"clones": {P: str(tmp_path)}}), encoding="utf-8")
    assert main(["--tick", "--sweep-config", str(sweep), "--logs-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"phase": "ledger_drift", "enabled": False}
    assert not (tmp_path / "ledger_drift").exists()


def test_show_prints_the_digest_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--show", "--projects", P, "--logs-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert f"[{P}]" in out and "未生成" in out


def test_bad_config_exits_1(tmp_path: Path) -> None:
    sweep = tmp_path / "sweep.json"
    sweep.write_text(json.dumps({"ledger_drift": {"window_days": -1}}), encoding="utf-8")
    assert main(["--dry-run", "--sweep-config", str(sweep), "--projects", P]) == 1
