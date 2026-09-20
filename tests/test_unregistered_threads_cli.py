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
    threads, malformed = asyncio.run(_MODULE._list_live_threads(inner, "p"))
    assert len(inner.calls) == 1
    _, args = inner.calls[0]
    assert args["project"] == "p"
    assert args["status_filter"] == sorted(LIVE_STATUSES)
    assert args["limit"] == 200
    assert threads == [{"thread_id": "T-a", "status": "active"}]
    assert malformed == 0


def test_list_live_threads_counts_non_dict_items() -> None:
    """msg-2648 §3: items dropped because they were not JSON objects are counted, not swallowed.

    The count is returned alongside the shape-clean list; the caller
    threads it into the ProjectReport. The offset arithmetic is
    deliberately NOT compensated for — see the CLI docstring for why
    (PR-gate #221 advisory disposition).
    """

    class _Mixed:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((name, arguments))
            offset = int(arguments.get("offset") or 0)
            if offset > 0:
                return {"items": [], "total": 3}
            return {
                "items": [
                    {"thread_id": "T-a", "status": "active"},
                    "not-a-dict",
                    ["also", "not"],
                ],
                "total": 3,
            }

    inner = _Mixed()
    threads, malformed = asyncio.run(_MODULE._list_live_threads(inner, "p"))
    assert threads == [{"thread_id": "T-a", "status": "active"}]
    assert malformed == 2


def test_list_live_threads_advances_offset_by_raw_page_length() -> None:
    """The offset increment MUST equal the server's own idea of the cursor position.

    PR-gate #221 suggested compensating the offset for dropped items;
    the disposition (msg-2648 §2(a)) declined — a compensated offset
    against a correctly-paging server produces duplicates. Pin the
    contract: even when the first page returns malformed items, the
    second-page call uses ``offset == len(raw items)``.
    """

    class _TwoPage:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            self.calls.append(dict(arguments))
            offset = int(arguments.get("offset") or 0)
            if offset == 0:
                return {
                    "items": [
                        {"thread_id": "T-a", "status": "active"},
                        "garbage",
                        {"thread_id": "T-b", "status": "active"},
                    ],
                    "total": 5,
                }
            if offset == 3:
                return {
                    "items": [
                        {"thread_id": "T-c", "status": "active"},
                        {"thread_id": "T-d", "status": "active"},
                    ],
                    "total": 5,
                }
            return {"items": [], "total": 5}

    inner = _TwoPage()
    threads, malformed = asyncio.run(_MODULE._list_live_threads(inner, "p"))
    # Offset advanced by RAW length (3) after page 1, not by dict-only (2).
    assert [c["offset"] for c in inner.calls] == [0, 3]
    assert [t["thread_id"] for t in threads] == ["T-a", "T-b", "T-c", "T-d"]
    assert malformed == 1


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
    assert payload["projects"][0]["malformed_count"] == 0
    assert payload["projects"][1]["unregistered"] == ["T-z"]
    assert payload["projects"][1]["unregistered_count"] == 1
    assert payload["projects"][1]["malformed_count"] == 0
    assert payload["unregistered_count_total"] == 2
    assert payload["any_unmeasured"] is False
    assert payload["malformed_count_total"] == 0
    assert payload["any_malformed"] is False


def test_run_surfaces_malformed_items_without_mixing_them_into_the_count() -> None:
    """End-to-end: garbled items appear in ``malformed_count`` and NOT in ``unregistered_count``.

    msg-2648 §3: two states, kept separate. A day where the MCP call
    returned two malformed items and one legitimate unregistered thread
    reports ``unregistered_count == 1`` and ``malformed_count == 2`` —
    not ``unregistered_count == 3``.
    """
    registered = RegisteredIndex(pairs=frozenset(), projects=("p",))

    class _GarbledMcp:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            offset = int(arguments.get("offset") or 0)
            if offset > 0:
                return {"items": [], "total": 3}
            return {
                "items": [
                    {"thread_id": "T-live", "status": "active"},
                    "stray-string",
                    None,
                ],
                "total": 3,
            }

    async def _fake_run() -> Any:
        mcp = _MODULE.ReadOnlyMcp(_GarbledMcp())
        report = await _MODULE._enumerate_project_with_recovery(mcp, "p", registered)
        return _MODULE.EnumerateReport(projects=(report,))

    payload = asyncio.run(_fake_run()).as_json()
    proj = payload["projects"][0]
    assert proj["unregistered_count"] == 1
    assert proj["unregistered"] == ["T-live"]
    assert proj["malformed_count"] == 2
    assert payload["unregistered_count_total"] == 1
    assert payload["malformed_count_total"] == 2
    assert payload["any_malformed"] is True


def test_a_project_that_measures_zero_and_drops_zero_reports_any_malformed_false() -> None:
    """A clean project must not trip the malformed alarm."""
    registered = RegisteredIndex(pairs=frozenset(), projects=("p",))
    inner = _RecordingMcp({"p": [{"thread_id": "T-a", "status": "active"}]})

    async def _fake_run() -> Any:
        mcp = _MODULE.ReadOnlyMcp(inner)
        report = await _MODULE._enumerate_project_with_recovery(mcp, "p", registered)
        return _MODULE.EnumerateReport(projects=(report,))

    payload = asyncio.run(_fake_run()).as_json()
    assert payload["projects"][0]["malformed_count"] == 0
    assert payload["any_malformed"] is False


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
    assert healthy["malformed_count"] == 0
    assert broken["unregistered_count"] is None
    assert broken["malformed_count"] is None
    assert "transport boom" in (broken["error"] or "")
    # And critically: a broken project must NOT be summed as 0.
    assert payload["unregistered_count_total"] == 1
    assert payload["any_unmeasured"] is True
    assert payload["unmeasured_projects"] == ["broken"]
    # A failed call has an unknown malformed_count, not zero.
    assert payload["malformed_count_total"] == 0
    assert payload["any_malformed"] is False


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
