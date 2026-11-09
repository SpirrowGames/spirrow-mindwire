"""Exclusion by tag in the unregistered-threads enumerator (T-sweep-exclude-by-tag).

Spec: Bohr msg-8731 (確定設計 v9) as amended by msg-8749 (v10: the fallback is
always built). Pinned here:

* how each shape of a listing ``tags`` / a get_thread ``thread.tags`` is read
  (list / ``[]`` / ``null`` / string / list holding a non-string / no key);
* the two ``null`` constants are independent (one endpoint's value never
  changes the other's reading);
* the per-project P / null / M counters are taken before the registered filter;
* the fallback's targets (the wrapper's rule order up to the tag step), its
  order (``idle_days`` ascending), the cap, and a failed call -> ``unreadable``;
* a partial rollout (``0 < P < M``): only the items without tags are looked up,
  and none of them ends ``field_missing``;
* the parity fixture: the keys sent to ``chatroom_get_thread`` equal the
  fixture's ``expected_tag_step`` — the same set ``tests/Test-UnregisteredIntake.ps1``
  checks the PowerShell rule order carries to the tag step.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from spirrow_mindwire import unregistered_threads as ut
from spirrow_mindwire.magickit.client import MagickitMcpError
from spirrow_mindwire.unregistered_threads import (
    EXCLUDE_TAGS,
    TAG_LOOKUP_CAP,
    EnumerateReport,
    ListedThread,
    classify_activity_at,
    classify_get_thread_tags,
    classify_listing_tags,
    enumerate_project,
    match_exclude_tag,
    parse_registered,
    tag_lookup_targets,
)

_ROOT = Path(__file__).resolve().parents[1]
_PARITY = json.loads(
    (_ROOT / "tests" / "data" / "sweep_exclude_by_tag_parity.json").read_text(encoding="utf-8")
)


def _load_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "unregistered_threads_cli_tags", _ROOT / "scripts" / "unregistered_threads.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CLI = _load_cli()


class _FakeMcp:
    """A listing per project and a get_thread answer per key; records every get_thread key."""

    def __init__(
        self,
        listings: dict[str, list[dict[str, Any]]],
        threads: dict[str, Any] | None = None,
        fail: frozenset[str] = frozenset(),
    ) -> None:
        self._listings = listings
        self._threads = threads or {}
        self._fail = fail
        self.looked_up: list[str] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_list_threads":
            items = self._listings.get(str(arguments["project"]), [])
            if int(arguments.get("offset") or 0) > 0:
                return {"items": [], "total": len(items)}
            return {"items": items, "total": len(items)}
        if name == "chatroom_get_thread":
            assert arguments.get("mode") == "summary"
            key = f"{arguments['project']}/{arguments['thread_id']}"
            self.looked_up.append(key)
            if key in self._fail:
                raise MagickitMcpError("boom")
            return self._threads.get(key, {"thread": {"tags": []}})
        raise AssertionError(f"unexpected tool {name}")


def _item(
    tid: str, *, tags: Any = "drop", at: str | None = "2026-11-08T00:00:00+00:00"
) -> dict[str, Any]:
    it: dict[str, Any] = {"thread_id": tid, "status": "active", "last_activity_at": at}
    if tags != "drop":
        it["tags"] = tags
    return it


def _sweep(**extra: Any) -> dict[str, Any]:
    return {"candidates": [], "clones": {"p": "/c/p"}, **extra}


# ------------------------------------------------------------------ value shapes


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"tags": ["a", "probe"]}, ("a", "probe")),
        ({"tags": []}, ()),
        ({}, None),
        ({"tags": None}, None),
        ({"tags": "probe"}, None),
        ({"tags": ["probe", 1]}, None),
    ],
)
def test_listing_tag_shapes(item: dict[str, Any], expected: Any) -> None:
    assert classify_listing_tags(item) == expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"thread": {"tags": ["x"]}}, ("ok", ("x",))),
        ({"thread": {"tags": []}}, ("ok", ())),
        ({"thread": {}}, ("field_missing", None)),
        ({"thread": {"tags": None}}, ("unreadable", None)),
        ({"thread": {"tags": "probe"}}, ("unreadable", None)),
        ({"thread": {"tags": ["probe", None]}}, ("unreadable", None)),
        ({"messages": []}, ("unreadable", None)),
        ("not a dict", ("unreadable", None)),
    ],
)
def test_get_thread_tag_shapes(answer: Any, expected: Any) -> None:
    assert classify_get_thread_tags(answer) == expected


def test_match_is_trimmed_and_case_folded() -> None:
    assert EXCLUDE_TAGS == ("do-not-sweep", "not-swept", "scratch", "probe")
    assert match_exclude_tag(["design", "  Not-Swept "]) == "not-swept"
    assert match_exclude_tag(["PROBE"]) == "probe"
    assert match_exclude_tag(["operator-probe", "probes"]) is None
    assert match_exclude_tag([]) is None


# ------------------------------------------------------------------ the two null constants


def test_measured_constants() -> None:
    """D-0 (2026-11-09): null observed on neither endpoint, so neither reads null as empty."""
    assert ut.LISTING_TAGS_NULL_MEANS_EMPTY is None
    assert ut.GET_THREAD_TAGS_NULL_MEANS_EMPTY is False


def test_get_thread_constant_does_not_change_the_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ut, "GET_THREAD_TAGS_NULL_MEANS_EMPTY", True)
    assert classify_get_thread_tags({"thread": {"tags": None}}) == ("ok", ())
    assert classify_listing_tags({"tags": None}) is None  # still a fallback target


def test_listing_constant_does_not_change_get_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ut, "LISTING_TAGS_NULL_MEANS_EMPTY", True)
    assert classify_listing_tags({"tags": None}) == ()
    assert classify_get_thread_tags({"thread": {"tags": None}}) == ("unreadable", None)


def test_listing_null_true_is_ok_and_not_looked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ut, "LISTING_TAGS_NULL_MEANS_EMPTY", True)
    reg = parse_registered(_sweep())
    mcp = _FakeMcp({"p": [_item("T-n", tags=None)]})
    report = asyncio.run(_CLI._enumerate_all(mcp, reg))
    (t,) = report.projects[0].threads
    assert (t.tag_state, t.exclude_tag) == ("ok", None)
    assert report.projects[0].tags_null_count == 1
    assert mcp.looked_up == []
    assert report.as_json()["listing_tags_null_measured"] is True


def test_report_says_listing_null_is_unmeasured() -> None:
    payload = EnumerateReport().as_json()
    assert payload["listing_tags_null_measured"] is False
    assert payload["exclude_tags"] == list(EXCLUDE_TAGS)
    assert payload["tag_lookup_cap"] == TAG_LOOKUP_CAP == 20


# ------------------------------------------------------------------ counters


def test_counters_are_taken_before_the_registered_filter() -> None:
    reg = parse_registered(
        {"candidates": [{"project": "p", "thread_id": "T-reg"}], "clones": {"p": "/c/p"}}
    )
    items = [
        _item("T-reg", tags=["a"]),
        {**_item("T-done", tags=[]), "status": "resolved"},
        _item("T-null", tags=None),
        _item("T-absent"),
    ]
    listed = [ListedThread(i, classify_activity_at(i["last_activity_at"])) for i in items]
    r = enumerate_project("p", listed, reg)
    assert (r.tags_field_present_count, r.tags_null_count, r.listed_count) == (2, 1, 4)
    assert r.as_json()["tags_field_present_count"] == 2


def test_listing_tags_set_the_tag_state_without_a_lookup() -> None:
    reg = parse_registered(_sweep())
    mcp = _FakeMcp({"p": [_item("T-probe", tags=["Probe"]), _item("T-plain", tags=[])]})
    report = asyncio.run(_CLI._enumerate_all(mcp, reg))
    rows = {t.thread_id: (t.tag_state, t.exclude_tag) for t in report.projects[0].threads}
    assert rows == {"T-probe": ("ok", "probe"), "T-plain": ("ok", None)}
    assert mcp.looked_up == []
    assert (report.tag_lookup_needed, report.tag_lookup_done) == (0, 0)


# ------------------------------------------------------------------ fallback


def test_fallback_reads_get_thread_and_matches() -> None:
    reg = parse_registered(_sweep())
    mcp = _FakeMcp(
        {"p": [_item("T-a"), _item("T-b"), _item("T-c"), _item("T-d")]},
        threads={
            "p/T-a": {"thread": {"tags": ["scratch"]}},
            "p/T-b": {"thread": {"tags": []}},
            "p/T-c": {"thread": {"status": "active"}},
        },
        fail=frozenset({"p/T-d"}),
    )
    report = asyncio.run(_CLI._enumerate_all(mcp, reg))
    rows = {t.thread_id: (t.tag_state, t.exclude_tag) for t in report.projects[0].threads}
    assert rows == {
        "T-a": ("ok", "scratch"),
        "T-b": ("ok", None),
        "T-c": ("field_missing", None),
        "T-d": ("unreadable", None),
    }


def test_fallback_targets_follow_the_wrapper_rule_order() -> None:
    reg = parse_registered(_sweep(projects=["q"], exclude=["p/T-ex"], intake={"max_idle_days": 5}))
    mcp = _FakeMcp(
        {
            "p": [
                _item("T-ok"),
                _item("T-ex"),
                _item("T-pr-review-p-1"),
                _item("T-old", at="2026-01-01T00:00:00+00:00"),
                _item("T-notime", at=None),
            ],
            "q": [_item("T-noclone")],
        }
    )
    asyncio.run(_CLI._enumerate_all(mcp, reg))
    assert mcp.looked_up == ["p/T-ok"]


def test_fallback_order_and_cap() -> None:
    n = TAG_LOOKUP_CAP + 5
    items = [_item(f"T-{i:02d}", at=f"2026-11-{(i % 9) + 1:02d}T00:00:00+00:00") for i in range(n)]
    reg = parse_registered(_sweep())
    listed = [ListedThread(i, classify_activity_at(i["last_activity_at"])) for i in items]
    now = datetime.fromisoformat("2026-11-10T00:00:00+00:00")
    pre = EnumerateReport(projects=(enumerate_project("p", listed, reg, now=now),))
    targets = tag_lookup_targets(pre.projects, reg)
    idles = [
        next(t.idle_days or 0.0 for t in pre.projects[0].threads if t.thread_id == tid)
        for _, tid in targets
    ]
    assert idles == sorted(idles)  # freshest first

    mcp = _FakeMcp({"p": items})
    report = asyncio.run(_CLI._enumerate_all(mcp, reg))
    assert len(mcp.looked_up) == TAG_LOOKUP_CAP
    states = [t.tag_state for t in report.projects[0].threads]
    assert states.count("unread") == 5 and states.count("ok") == TAG_LOOKUP_CAP
    assert (report.tag_lookup_needed, report.tag_lookup_done) == (n, TAG_LOOKUP_CAP)
    payload = report.as_json()
    assert (payload["tag_lookup_needed"], payload["tag_lookup_done"]) == (n, TAG_LOOKUP_CAP)


def test_partial_rollout_looks_up_only_the_items_without_tags() -> None:
    """msg-8749: 0 < P < M — the tagless items are looked up, never left field_missing."""
    reg = parse_registered(_sweep())
    mcp = _FakeMcp({"p": [_item("T-has", tags=["design"]), _item("T-lacks")]})
    report = asyncio.run(_CLI._enumerate_all(mcp, reg))
    p = report.projects[0]
    assert (p.tags_field_present_count, p.listed_count) == (1, 2)
    assert mcp.looked_up == ["p/T-lacks"]
    assert {t.tag_state for t in p.threads} <= {"ok", "unread"}


# ------------------------------------------------------------------ parity fixture


def test_parity_fixture_report_is_the_enumerators_output() -> None:
    reg = parse_registered(_PARITY["sweep"])
    now = datetime.fromisoformat(_PARITY["now"])
    reports = []
    for project in reg.projects:
        items = _PARITY["listings"].get(project, [])
        listed = [ListedThread(i, classify_activity_at(i.get("last_activity_at"))) for i in items]
        reports.append(enumerate_project(project, listed, reg, now=now))
    assert EnumerateReport(projects=tuple(reports)).as_json() == _PARITY["report"]


def test_parity_get_thread_keys_equal_the_tag_step_keys() -> None:
    reg = parse_registered(_PARITY["sweep"])
    names = {p for i in _PARITY["listings"].values() for p in (i[0]["thread_id"],)}
    assert names  # the fixture is not empty
    mcp = _FakeMcp(_PARITY["listings"])
    asyncio.run(_CLI._enumerate_all(mcp, reg))
    assert mcp.looked_up == _PARITY["expected_tag_step"]
    tids = {i["thread_id"] for items in _PARITY["listings"].values() for i in items}
    assert {"T-listed", "T-pr-review-mw-1"} <= tids  # explicit and PR-review are in the fixture
