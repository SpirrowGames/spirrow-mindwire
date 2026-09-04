"""CLI wiring for the D-2 log-only unregistered-threads enumerator.

Loaded by path the same way :mod:`tests.test_pr_review_sweep_phase0_cli`
loads its script, because ``scripts/`` is not an importable package.

The tests exercise the CLI's failure envelope end-to-end: a per-project
MCP failure lands in ``projects[].error`` with ``unregistered_count =
null``, and a setup failure (missing / unparseable ``sweep.json`` or an
attempted write) exits non-zero. That is the machine check behind
msg-2531 §2's three invariants (do-not-drop / do-not-hide / bounded).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from spirrow_mindwire.magickit.client import MagickitMcpError
from spirrow_mindwire.unregistered_threads import LIVE_STATUSES, RegisteredIndex

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "unregistered_threads.py"


def _load_cli_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("unregistered_threads_cli", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_cli_module()


class _RecordingMcp:
    """Returns a fixed listing per project and records every call the CLI makes."""

    def __init__(self, listings: dict[str, list[dict[str, Any]]]) -> None:
        self._listings = listings
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name == "chatroom_list_threads":
            project = str(arguments.get("project") or "")
            offset = int(arguments.get("offset") or 0)
            items = self._listings.get(project, [])
            if offset > 0:
                return {"items": [], "total": len(items)}
            return {"items": items, "total": len(items)}
        raise AssertionError(f"unexpected tool {name}")


class _FailingMcp:
    """Raises :class:`MagickitMcpError` for the named project, otherwise delegates."""

    def __init__(self, inner: _RecordingMcp, fail_project: str, reason: str = "boom") -> None:
        self._inner = inner
        self._fail_project = fail_project
        self._reason = reason

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_list_threads" and arguments.get("project") == self._fail_project:
            raise MagickitMcpError(self._reason)
        return await self._inner.call_tool(name, arguments)


def _write_sweep(tmp_path: Path, entries: list[dict[str, str]]) -> Path:
    path = tmp_path / "sweep.json"
    path.write_text(json.dumps({"candidates": entries}), encoding="utf-8")
    return path


# --------------------------------------------------------------------- ReadOnlyMcp allowlist


def test_read_only_wrapper_refuses_writes() -> None:
    """The write-zero property is enforced, not promised (mirrors pr_review_sweep_phase0)."""

    class _Bad:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            return {"items": [], "total": 0}

    wrapper = _MODULE.ReadOnlyMcp(_Bad())
    with pytest.raises(_MODULE.Phase0WriteAttemptedError):
        asyncio.run(wrapper.call_tool("chatroom_post_message", {}))


def test_read_only_wrapper_allows_the_one_read_tool() -> None:
    inner = _RecordingMcp({"p": []})
    wrapper = _MODULE.ReadOnlyMcp(inner)
    result = asyncio.run(
        wrapper.call_tool(
            "chatroom_list_threads",
            {"project": "p", "status_filter": sorted(LIVE_STATUSES), "limit": 200, "offset": 0},
        )
    )
    assert result == {"items": [], "total": 0}


# --------------------------------------------------------------------- happy paths


def test_list_live_threads_forwards_the_status_filter() -> None:
    """The tool call must scope to LIVE_STATUSES server-side, not filter client-side."""
    inner = _RecordingMcp({"p": [{"thread_id": "T-a", "status": "active"}]})
    threads = asyncio.run(_MODULE._list_live_threads(inner, "p"))
    assert len(inner.calls) == 1
    _, args = inner.calls[0]
    assert args["project"] == "p"
    assert args["status_filter"] == sorted(LIVE_STATUSES)
    assert args["limit"] == 200
    assert threads == [{"thread_id": "T-a", "status": "active"}]


def test_run_produces_a_report_per_project_in_config_order() -> None:
    registered = RegisteredIndex(pairs=frozenset({("p1", "T-x")}), projects=("p1", "p2"))
    inner = _RecordingMcp(
        {
            "p1": [
                {"thread_id": "T-x", "status": "active"},
                {"thread_id": "T-y", "status": "active"},
            ],
            "p2": [
                {"thread_id": "T-z", "status": "awaiting_reply"},
                {"thread_id": "T-pr-review-spirrow-mindwire-1", "status": "active"},
            ],
        }
    )

    async def _fake_run() -> Any:
        mcp = _MODULE.ReadOnlyMcp(inner)
        reports: list[Any] = []
        for project in registered.projects:
            reports.append(await _MODULE._enumerate_project_with_recovery(mcp, project, registered))
        return _MODULE.EnumerateReport(projects=tuple(reports))

    report = asyncio.run(_fake_run())
    payload = report.as_json()

    assert [p["project"] for p in payload["projects"]] == ["p1", "p2"]
    assert payload["projects"][0]["unregistered"] == ["T-y"]
    assert payload["projects"][0]["unregistered_count"] == 1
    assert payload["projects"][0]["error"] is None
    assert payload["projects"][1]["unregistered"] == ["T-z"]
    assert payload["projects"][1]["unregistered_count"] == 1
    assert payload["unregistered_count_total"] == 2
    assert payload["any_unmeasured"] is False


# --------------------------------------------------------------------- per-project failure envelope


def test_a_per_project_mcp_failure_is_recorded_not_raised() -> None:
    """Invariant 2 from msg-2531 §2: 0 件 と 測れなかった を同じ表示にしない."""
    registered = RegisteredIndex(pairs=frozenset(), projects=("healthy", "broken"))
    inner = _RecordingMcp({"healthy": [{"thread_id": "T-a", "status": "active"}]})
    failing = _FailingMcp(inner, fail_project="broken", reason="transport boom")

    async def _fake_run() -> Any:
        reports: list[Any] = []
        for project in registered.projects:
            reports.append(
                await _MODULE._enumerate_project_with_recovery(failing, project, registered)
            )
        return _MODULE.EnumerateReport(projects=tuple(reports))

    report = asyncio.run(_fake_run())
    payload = report.as_json()

    healthy = payload["projects"][0]
    broken = payload["projects"][1]
    assert healthy["unregistered_count"] == 1
    assert healthy["error"] is None
    assert broken["unregistered_count"] is None
    assert "transport boom" in (broken["error"] or "")
    # And critically: a broken project must NOT be summed as 0.
    assert payload["unregistered_count_total"] == 1
    assert payload["any_unmeasured"] is True
    assert payload["unmeasured_projects"] == ["broken"]


def test_a_per_project_unexpected_exception_is_recorded_not_raised() -> None:
    """A programming error in the listing path must still surface as an error field, not a crash."""

    class _ExplodingMcp:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            raise RuntimeError("bug")

    async def _fake_run() -> Any:
        return await _MODULE._enumerate_project_with_recovery(
            _ExplodingMcp(), "p", RegisteredIndex(pairs=frozenset(), projects=("p",))
        )

    report = asyncio.run(_fake_run())
    assert report.unregistered_count is None
    assert report.error is not None
    assert "RuntimeError" in report.error


# --------------------------------------------------------------------- main() exit codes and stdout


def test_main_exits_two_on_missing_sweep_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Setup failures exit non-zero so the wrapper can render `?` uniformly."""
    argv = ["prog", "--sweep-config", str(tmp_path / "does-not-exist.json")]
    old = sys.argv
    try:
        sys.argv = argv
        rc = _MODULE.main()
    finally:
        sys.argv = old
    assert rc == 2
    err = capsys.readouterr().err
    assert "cannot read" in err


def test_main_exits_two_on_unparseable_sweep_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "sweep.json"
    path.write_text("not-json", encoding="utf-8")
    argv = ["prog", "--sweep-config", str(path)]
    old = sys.argv
    try:
        sys.argv = argv
        rc = _MODULE.main()
    finally:
        sys.argv = old
    assert rc == 2


def test_main_reports_empty_run_on_empty_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty sweep.json is a valid file; the CLI reports empty and exits 0."""
    path = _write_sweep(tmp_path, [])
    argv = ["prog", "--sweep-config", str(path)]
    old = sys.argv
    try:
        sys.argv = argv
        rc = _MODULE.main()
    finally:
        sys.argv = old
    assert rc == 0
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert payload["projects"] == []
    assert payload["unregistered_count_total"] == 0
    assert payload["any_unmeasured"] is False


def test_main_end_to_end_with_stubbed_mcp(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: config -> ReadOnlyMcp -> enumeration -> ASCII-only JSON on stdout.

    The MCP transport class is swapped for a recorder so the test is
    hermetic; the substitution mirrors the pattern
    :mod:`tests.test_pr_review_sweep_phase0_cli` uses.
    """
    path = _write_sweep(
        tmp_path,
        [
            {"project": "p1", "thread_id": "T-x", "repo_dir": "/x"},
            {"project": "p2", "thread_id": "T-w", "repo_dir": "/w"},
        ],
    )

    class _Stub:
        def __init__(self, url: str | None = None) -> None:
            pass

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            project = str(arguments.get("project") or "")
            offset = int(arguments.get("offset") or 0)
            if offset > 0:
                return {"items": [], "total": 0}
            if project == "p1":
                return {
                    "items": [
                        {"thread_id": "T-x", "status": "active"},
                        {"thread_id": "T-live-unregistered", "status": "awaiting_reply"},
                    ],
                    "total": 2,
                }
            return {"items": [], "total": 0}

    monkeypatch.setattr(_MODULE, "StreamableHttpChatroomMcp", _Stub)

    argv = ["prog", "--sweep-config", str(path)]
    old = sys.argv
    try:
        sys.argv = argv
        rc = _MODULE.main()
    finally:
        sys.argv = old

    assert rc == 0
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert [p["project"] for p in payload["projects"]] == ["p1", "p2"]
    assert payload["projects"][0]["unregistered"] == ["T-live-unregistered"]
    assert payload["projects"][1]["unregistered"] == []
    assert payload["unregistered_count_total"] == 1
    assert payload["any_unmeasured"] is False
    # D-33: stdout is ASCII-only, so ``json.dumps(..., ensure_ascii=True)``
    # is what the CLI must use. A future edit that regresses to
    # ``ensure_ascii=False`` would fail this assertion the moment the
    # test payload gains any non-ASCII character; we approximate that
    # here by checking the current payload round-trips as ASCII.
    assert stdout.encode("ascii", errors="strict")
