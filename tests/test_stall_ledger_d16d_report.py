"""D-16d-1 part 2: ``--report`` reads the head cache and the ledger, and fetches nothing.

Spec: T-stalled-pr-has-no-detector msg-8477 ⑤, msg-8549 §1-§4 as amended by msg-8552
(Obj-7-1: ``NEXT: human`` stays D at 72h and is only listed; Obj-7-2: no PR-state form of B).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_stall_ledger_d16d_heads import (
    LAST,
    FakeChatroom,
    FakeClock,
    _heads,
    _key,
    _store,
    _tick,
)

from spirrow_mindwire.stall_ledger import driver as driver_mod
from spirrow_mindwire.stall_ledger.heads import CachedHead
from spirrow_mindwire.stall_ledger.report import NOT_THREAD, build_report, report_from_path
from spirrow_mindwire.stall_ledger.store import PENDING_HEADS_KEY, THREAD_HEADS_KEY

REPO = Path(__file__).resolve().parent.parent
STORE = "stall-ledger.json"


def _load_cli() -> Any:
    path = REPO / "scripts" / "stall_ledger_tick.py"
    spec = importlib.util.spec_from_file_location("_d16d_stall_ledger_tick_report", path)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cli
    spec.loader.exec_module(cli)
    return cli


def _room_with_all_groups() -> FakeChatroom:
    room = FakeChatroom(FakeClock())
    room.put("T-a", "x\n\nNEXT: Heisenberg")  # A
    room.put("T-b", "finished\n\nSTOP: done")  # B
    room.put("T-c", "x\n\nNEXT: none")  # C
    room.put("T-m1", "x\n\nNEXT: Heisenberg", field_value="Bohr")  # C (field != body)
    room.put("T-d", "x\n\nBLOCKED-ON: pr o/r#1\nNEXT: Bohr")  # D (blocked-on)
    room.put("T-h", "ask\n\nNEXT: human")  # D (next_human)
    return room


def test_report_counts_match_the_cache(tmp_path: Path) -> None:
    room = _room_with_all_groups()
    _tick(tmp_path, room, LAST + timedelta(hours=7))
    cache = _heads(tmp_path)
    expected = Counter(entry["group"] for entry in cache.values())
    report = report_from_path(tmp_path / STORE)
    assert report["heads_by_group"] == {g: expected.get(g, 0) for g in "ABCD"}
    assert report["heads_by_group"] == {"A": 1, "B": 1, "C": 2, "D": 2}
    assert report["pending_heads"]["count"] == 0
    assert report["next_human"] == {"count": 1, "threads": [_key("T-h")]}


def test_report_lists_open_records_under_their_group(tmp_path: Path) -> None:
    room = _room_with_all_groups()
    _tick(tmp_path, room, LAST + timedelta(hours=73))  # past 6h and 72h: every non-B opens
    report = report_from_path(tmp_path / STORE)
    by_group = {g: [e["key"] for e in rows] for g, rows in report["open_records_by_group"].items()}
    assert by_group == {
        "A": [_key("T-a")],
        "B": [],
        "C": [_key("T-c"), _key("T-m1")],
        "D": [_key("T-d"), _key("T-h")],
        NOT_THREAD: [],
    }
    assert set(_store(tmp_path)["records"]) == {k for keys in by_group.values() for k in keys}


def test_next_human_head_still_opens_at_72h_and_is_listed(tmp_path: Path) -> None:
    # msg-8552 Obj-7-1: NEXT: human stays in D at 72h (the M-1-class backstop); not count-only.
    room = FakeChatroom(FakeClock())
    room.put("T-h", "merge wait\n\nNEXT: human")
    assert _tick(tmp_path, room, LAST + timedelta(hours=70)).counters.opened == 0
    assert _tick(tmp_path, room, LAST + timedelta(hours=73)).counters.opened == 1
    report = report_from_path(tmp_path / STORE)
    assert report["next_human"]["threads"] == [_key("T-h")]
    (row,) = report["open_records_by_group"]["D"]
    assert (row["key"], row["detail"]) == (_key("T-h"), "next_human")


def test_pending_heads_are_kept_for_the_report_and_read_as_c(tmp_path: Path) -> None:
    room = FakeChatroom(FakeClock(), fail_reads=True)
    room.put("T-p", "x\n\nNEXT: Bohr")
    out = _tick(tmp_path, room, LAST + timedelta(hours=73))
    assert out.counters.pending_heads == 1
    report = report_from_path(tmp_path / STORE)
    assert report["pending_heads"]["count"] == 1
    assert report["pending_heads"]["keys"] == [_key("T-p")]
    (row,) = report["open_records_by_group"]["C"]
    assert (row["key"], row["head_read"]) == (_key("T-p"), False)
    # Once read, the pending list is replaced by the next tick's (empty) one.
    room.fail_reads = False
    _tick(tmp_path, room, LAST + timedelta(hours=74))
    assert report_from_path(tmp_path / STORE)["pending_heads"]["count"] == 0
    assert _store(tmp_path)[PENDING_HEADS_KEY]["keys"] == []


def test_report_fetches_nothing_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    room = _room_with_all_groups()
    _tick(tmp_path / "state", room, LAST + timedelta(hours=7))
    store_path = tmp_path / "state" / STORE
    before = store_path.read_bytes()
    reads_before = dict(room.reads)

    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("--report must not fetch or take the budget")

    cli = _load_cli()
    monkeypatch.setattr(driver_mod.FetchBudget, "try_start", forbidden)
    monkeypatch.setattr(driver_mod.FetchBudget, "__init__", forbidden)
    monkeypatch.setattr(cli, "run_tick", forbidden)
    import spirrow_mindwire.github.client as gh
    import spirrow_mindwire.magickit.client as mk

    monkeypatch.setattr(gh, "GitHubClient", forbidden)
    monkeypatch.setattr(mk, "StreamableHttpChatroomMcp", forbidden)

    code = cli.main(["--data-dir", str(tmp_path), "--report", "--project", "p", "--repo", "o/r"])
    assert code == 0
    assert room.reads == reads_before
    assert store_path.read_bytes() == before
    (line,) = capsys.readouterr().out.strip().splitlines()
    printed = json.loads(line)
    assert printed["kind"] == "stall_report"
    assert printed["heads_by_group"] == {"A": 1, "B": 1, "C": 2, "D": 2}


def test_report_on_absent_or_corrupt_store_repairs_nothing(tmp_path: Path) -> None:
    absent = report_from_path(tmp_path / STORE)
    assert absent["store"] == "absent"
    assert absent["heads_by_group"] == {"A": 0, "B": 0, "C": 0, "D": 0}
    (tmp_path / STORE).write_text("{not json", encoding="utf-8")
    corrupt = report_from_path(tmp_path / STORE)
    assert corrupt["store"] == "corrupt"
    assert (tmp_path / STORE).read_text(encoding="utf-8") == "{not json"  # not moved aside


def test_unparsable_cache_entry_is_listed_not_counted_and_other_units_go_to_not_thread() -> None:
    good = {
        "last_msg_id": "m",
        "last_activity_at": "t",
        "group": "A",
        "detail": "next_participant",
        "read_at": "t",
    }
    assert CachedHead.from_json(good) is not None
    envelope = {
        THREAD_HEADS_KEY: {"thread:p/T-a": good, "thread:p/T-x": {"group": "Z"}},
        "records": {
            "pr:o/r#1": {"stall_epoch_start": "s", "klass": "k"},
            "thread:p/T-a": {"stall_epoch_start": "s", "klass": "k"},
        },
    }
    report = build_report(envelope, store_state="schema_version='1.0'")
    assert report["heads_by_group"] == {"A": 1, "B": 0, "C": 0, "D": 0}
    assert report["heads_unparsed"] == ["thread:p/T-x"]
    assert [e["key"] for e in report["open_records_by_group"][NOT_THREAD]] == ["pr:o/r#1"]
    assert [e["key"] for e in report["open_records_by_group"]["A"]] == ["thread:p/T-a"]


def test_quarantined_records_are_listed_under_not_thread() -> None:
    """PR #547 gate Obj-1-1: quarantine entries live in ``quarantined_records``."""
    envelope = {
        "records": {"pr:o/r#1": {"stall_epoch_start": "s", "klass": "k"}},
        "quarantined_records": {
            "thread:p/T-q": {
                "reason": "missing_fields",
                "first_detected_at": "t1",
                "last_detected_at": "t2",
                "count": 2,
            },
            "pr:o/r#9": "not an object",
        },
    }
    report = build_report(envelope, store_state="schema_version='1.0'")
    not_thread = report["open_records_by_group"][NOT_THREAD]
    assert [e["key"] for e in not_thread] == ["pr:o/r#1", "pr:o/r#9", "thread:p/T-q"]
    by_key = {e["key"]: e for e in not_thread}
    assert "quarantined" not in by_key["pr:o/r#1"]
    assert by_key["thread:p/T-q"] == {
        "key": "thread:p/T-q",
        "quarantined": True,
        "reason": "missing_fields",
        "first_detected_at": "t1",
        "last_detected_at": "t2",
    }
    assert by_key["pr:o/r#9"]["quarantined"] is True
    assert by_key["pr:o/r#9"]["reason"] is None
    # a quarantined thread key is never bucketed into a head group
    grouped = report["open_records_by_group"]
    assert all(e["key"] != "thread:p/T-q" for g in "ABCD" for e in grouped[g])


def test_documented_report_command_needs_no_repo_or_project(tmp_path: Path) -> None:
    """Advisory #547 Obj-1-2: the ``docs/deploy.md`` command line parses and runs as written."""
    cli = _load_cli()
    assert cli.main(["--data-dir", str(tmp_path), "--report"]) == 0
