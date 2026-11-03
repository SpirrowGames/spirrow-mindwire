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

#: msg-7441 F-2: the real shape of a magickit ``chatroom_list_threads`` page. Every listing item
#: in this file is built from it, so a test can no longer pass on a field the real listing does
#: not carry (the fake items used to have ``updated_at``; the real ones never did).
_FIXTURE = Path(__file__).resolve().parent / "data" / "magickit_chatroom_list_threads_page.json"
_REAL_PAGE: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))["response"]
_REAL_ITEM: dict[str, Any] = _REAL_PAGE["items"][0]
_KEEP: Any = object()


def _real_item(
    thread_id: str, status: str, *, last_activity_at: Any = _KEEP, drop: tuple[str, ...] = ()
) -> dict[str, Any]:
    """One listing item of the real shape, with ``thread_id`` / ``status`` overridden.

    ``last_activity_at`` keeps the captured value unless given (``None`` = present but null);
    ``drop`` removes keys outright (the schema-drift case: the key is absent, not null).
    """
    item: dict[str, Any] = json.loads(json.dumps(_REAL_ITEM))
    item["thread_id"] = thread_id
    item["status"] = status
    if last_activity_at is not _KEEP:
        item["last_activity_at"] = last_activity_at
    for key in drop:
        item.pop(key, None)
    return item


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
    inner = _RecordingMcp({"p": [_real_item("T-a", "active")]})
    threads, malformed, _ = asyncio.run(_MODULE._list_live_threads(inner, "p"))
    assert len(inner.calls) == 1
    _, args = inner.calls[0]
    assert args["project"] == "p"
    assert args["status_filter"] == sorted(LIVE_STATUSES)
    assert args["limit"] == 200
    assert [t.thread for t in threads] == [_real_item("T-a", "active")]
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
                    _real_item("T-a", "active"),
                    "not-a-dict",
                    ["also", "not"],
                ],
                "total": 3,
            }

    inner = _Mixed()
    threads, malformed, _ = asyncio.run(_MODULE._list_live_threads(inner, "p"))
    assert [t.thread for t in threads] == [_real_item("T-a", "active")]
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
                        _real_item("T-a", "active"),
                        "garbage",
                        _real_item("T-b", "active"),
                    ],
                    "total": 5,
                }
            if offset == 3:
                return {
                    "items": [
                        _real_item("T-c", "active"),
                        _real_item("T-d", "active"),
                    ],
                    "total": 5,
                }
            return {"items": [], "total": 5}

    inner = _TwoPage()
    threads, malformed, _ = asyncio.run(_MODULE._list_live_threads(inner, "p"))
    # Offset advanced by RAW length (3) after page 1, not by dict-only (2).
    assert [c["offset"] for c in inner.calls] == [0, 3]
    assert [t.thread["thread_id"] for t in threads] == ["T-a", "T-b", "T-c", "T-d"]
    assert malformed == 1


def test_run_produces_a_report_per_project_in_config_order() -> None:
    registered = RegisteredIndex(pairs=frozenset({("p1", "T-x")}), projects=("p1", "p2"))
    inner = _RecordingMcp(
        {
            "p1": [
                _real_item("T-x", "active"),
                _real_item("T-y", "active"),
            ],
            "p2": [
                _real_item("T-z", "awaiting_reply"),
                _real_item("T-pr-review-spirrow-mindwire-1", "active"),
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
                    _real_item("T-live", "active"),
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
    inner = _RecordingMcp({"p": [_real_item("T-a", "active")]})

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
    inner = _RecordingMcp({"healthy": [_real_item("T-a", "active")]})
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
                        _real_item("T-x", "active"),
                        _real_item("T-live-unregistered", "awaiting_reply"),
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


# ----------------------- T-sweep-intake-discovery-by-default (msg-6770 §1 / msg-6778)


def test_list_live_threads_classifies_time_without_dropping() -> None:
    """The boundary labels each item's last_activity_at; it never drops an item for its time."""
    inner = _RecordingMcp(
        {
            "p": [
                _real_item("T-ok", "active", last_activity_at="2026-10-01T00:00:00Z"),
                _real_item("T-none", "active", last_activity_at=None),
                _real_item("T-bad", "active", last_activity_at="not-a-time"),
            ]
        }
    )
    threads, malformed, _ = asyncio.run(_MODULE._list_live_threads(inner, "p"))
    assert [(t.thread["thread_id"], t.time.state) for t in threads] == [
        ("T-ok", "ok"),
        ("T-none", "missing"),
        ("T-bad", "unparseable"),
    ]
    assert malformed == 0


def test_no_per_thread_read_is_possible() -> None:
    """Einstein msg-6769 (N+1): the listing is the only tool this script may call.

    The time comes from the listing's ``last_activity_at``; a per-thread read
    (chatroom_get_thread) would be refused by ReadOnlyMcp before it left
    the process.
    """
    assert frozenset({"chatroom_list_threads"}) == _MODULE.READ_ONLY_TOOLS


def test_the_timestamp_is_parsed_in_exactly_one_place() -> None:
    """msg-6770 §1: classify_activity_at is called only from _list_live_threads."""
    import ast

    root = Path(__file__).resolve().parents[1]
    callers: list[str] = []
    for base in ("src", "scripts", "deploy"):
        for path in (root / base).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for node in ast.walk(fn):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "classify_activity_at"
                    ):
                        callers.append(f"{path.relative_to(root).as_posix()}::{fn.name}")
    assert callers == ["scripts/unregistered_threads.py::_list_live_threads"]


def test_main_scans_a_project_named_only_in_clones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """穴 1 / 穴 3: a project with no candidate is scanned once its clone is declared."""
    path = tmp_path / "sweep.json"
    path.write_text(json.dumps({"clones": {"pnew": "/c/new"}}), encoding="utf-8")

    class _Stub:
        def __init__(self, url: str | None = None) -> None:
            pass

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            if int(arguments.get("offset") or 0) > 0 or arguments.get("project") != "pnew":
                return {"items": [], "total": 1}
            return {
                "items": [_real_item("T-n", "active", last_activity_at="2026-10-01T00:00:00Z")],
                "total": 1,
            }

    monkeypatch.setattr(_MODULE, "StreamableHttpChatroomMcp", _Stub)
    old = sys.argv
    try:
        sys.argv = ["prog", "--sweep-config", str(path)]
        rc = _MODULE.main()
    finally:
        sys.argv = old
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert [p["project"] for p in payload["projects"]] == ["pnew"]
    (row,) = payload["projects"][0]["threads"]
    assert row["thread_id"] == "T-n"
    assert row["time_state"] == "ok"
    assert row["last_activity_at"] == "2026-10-01T00:00:00+00:00"
    assert "updated_at" not in row
    assert isinstance(row["idle_days"], float)


# ----------------------- T-sweep-intake-discovery-by-default (msg-7441 F-1/F-2, msg-7445 F-3)


def test_the_fixture_is_the_real_listing_shape() -> None:
    """F-2: the pinned page is magickit's 13-field item, with last_activity_at and no updated_at."""
    items = _REAL_PAGE["items"]
    assert items, "fixture has no items"
    for item in items:
        assert len(item) == 13, sorted(item)
        assert "last_activity_at" in item
        assert "updated_at" not in item
    assert _MODULE.ACTIVITY_FIELD == "last_activity_at"


def test_no_listing_item_in_this_file_is_built_with_updated_at() -> None:
    """F-2: the fake ``updated_at``-only shape is gone from these tests and cannot come back."""
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    keys = [
        k.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for k in node.keys
        if isinstance(k, ast.Constant)
    ]
    assert "updated_at" not in keys


def _report_for(items: list[Any], registered: RegisteredIndex | None = None) -> Any:
    reg = registered or RegisteredIndex(pairs=frozenset(), projects=("p",))
    inner = _RecordingMcp({"p": items})
    return asyncio.run(
        _MODULE._enumerate_project_with_recovery(_MODULE.ReadOnlyMcp(inner), "p", reg)
    )


def test_real_shape_page_reports_no_absent_field() -> None:
    """F-3d: the real listing triggers no schema WARN (field_absent_count == 0)."""
    report = _report_for([dict(i) for i in _REAL_PAGE["items"]])
    assert report.field_absent_count == 0
    assert report.listed_count == len(_REAL_PAGE["items"])
    assert all(t.time_state == "ok" for t in report.threads)


def test_all_items_lacking_the_key_are_counted_even_when_none_is_unregistered() -> None:
    """F-3c/F-3d: counted before the registered filter — M/M with 0 unregistered threads."""
    items = [_real_item(f"T-{n}", "active", drop=("last_activity_at",)) for n in range(23)]
    registered = RegisteredIndex(
        pairs=frozenset(("p", f"T-{n}") for n in range(23)), projects=("p",)
    )
    report = _report_for(items, registered)
    assert report.unregistered_count == 0
    assert (report.field_absent_count, report.listed_count) == (23, 23)
    assert report.as_json()["field_absent_count"] == 23
    assert report.as_json()["listed_count"] == 23


def test_one_item_of_a_hundred_lacking_the_key_is_counted_as_one() -> None:
    """Einstein msg-7444: 1 of 100 is reported as 1/100, a count — nothing more is inferred."""
    items = [_real_item(f"T-{n}", "active") for n in range(99)]
    items.append(_real_item("T-odd", "active", drop=("last_activity_at",)))
    report = _report_for(items)
    assert (report.field_absent_count, report.listed_count) == (1, 100)
    odd = [t for t in report.threads if t.thread_id == "T-odd"]
    assert [t.time_state for t in odd] == ["missing"]


def test_a_present_null_key_is_not_counted_as_absent() -> None:
    """Einstein msg-7442: a legitimately null last_activity_at is 時刻不明, not schema drift."""
    report = _report_for([_real_item(f"T-{n}", "active", last_activity_at=None) for n in range(3)])
    assert report.field_absent_count == 0
    assert [t.time_state for t in report.threads] == ["missing"] * 3


def test_an_unparseable_value_is_not_counted_as_absent() -> None:
    report = _report_for([_real_item("T-bad", "active", last_activity_at="not-a-time")])
    assert report.field_absent_count == 0
    assert [t.time_state for t in report.threads] == ["unparseable"]


def test_a_failed_listing_has_unknown_absent_and_listed_counts() -> None:
    inner = _FailingMcp(_RecordingMcp({}), fail_project="p")
    report = asyncio.run(
        _MODULE._enumerate_project_with_recovery(
            inner, "p", RegisteredIndex(pairs=frozenset(), projects=("p",))
        )
    )
    assert (report.field_absent_count, report.listed_count) == (None, None)
