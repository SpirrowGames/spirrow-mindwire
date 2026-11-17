"""ledger_drift core: linking, the five sections, and the result-file freshness contract.

T-ledger-writeback-on-merge-and-thread-close-on-task-done, design v0.3 (msg-9524 / msg-9526).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spirrow_mindwire.ledger_drift.detect import (
    MergedPr,
    Section,
    SectionResult,
    TaskInfo,
    ThreadInfo,
    UnlinkedReason,
    detect,
    link_pr,
    pr_mentioned,
    thread_task_ids,
)
from spirrow_mindwire.ledger_drift.result import (
    Freshness,
    build,
    digest_lines,
    judge,
    result_path,
    write_atomic,
)

P = "proj"
NOW = datetime(2026, 11, 17, 4, 0, tzinfo=UTC)


def _pr(number: int, body: str, project: str | None = P) -> MergedPr:
    return MergedPr(
        repo="o/r",
        number=number,
        merged_at="2026-11-10T00:00:00Z",
        base="develop",
        body=body,
        project=project,
    )


def _task(task_id: str, status: str, notes: str = "", blocked_by: tuple[str, ...] = ()) -> TaskInfo:
    return TaskInfo(project=P, task_id=task_id, status=status, notes=notes, blocked_by=blocked_by)


def _tasks(*items: TaskInfo) -> dict[tuple[str, str], TaskInfo]:
    return {(t.project, t.task_id): t for t in items}


def _thread(
    thread_id: str, *, tags: tuple[str, ...] = (), related: tuple[str, ...] = ()
) -> ThreadInfo:
    return ThreadInfo(
        project=P, thread_id=thread_id, status="active", tags=tags, related_tasks=related
    )


# --- linking -------------------------------------------------------------------------------


def test_related_tasks_win_over_tags() -> None:
    t = _thread("T-x", tags=("T1",), related=("T9",))
    assert thread_task_ids(t) == ("T9",)


def test_tags_must_match_exactly_and_thread_id_is_never_parsed() -> None:
    t = _thread("T-t232-phase-b", tags=("T243", "t5", "T12a", "phase-b", "T243"))
    assert thread_task_ids(t) == ("T243",)


def test_pr_mention_is_not_a_prefix_match() -> None:
    assert pr_mentioned("PR #52 merged", 52)
    assert not pr_mentioned("PR #520 merged", 52)
    assert not pr_mentioned("PR 52 merged", 52)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("no line here", UnlinkedReason.NO_THREAD),
        ("Thread: T-a\nCloses-thread: other/T-b", UnlinkedReason.AMBIGUOUS),
    ],
)
def test_link_reasons_come_from_resolve_thread_link(body: str, reason: UnlinkedReason) -> None:
    assert link_pr(_pr(1, body)).reason is reason


def test_unqualified_thread_without_a_project_is_project_unknown() -> None:
    assert link_pr(_pr(1, "Thread: T-a", project=None)).reason is UnlinkedReason.PROJECT_UNKNOWN


def test_qualified_thread_resolves_in_its_own_project() -> None:
    assert link_pr(_pr(1, "Thread: other/T-a")).thread_key == ("other", "T-a")


# --- sections ------------------------------------------------------------------------------


def _run(
    prs: list[MergedPr],
    threads: Mapping[tuple[str, str], ThreadInfo | None],
    open_threads: Iterable[ThreadInfo] = (),
    tasks: Mapping[tuple[str, str], TaskInfo] | None = None,
    incomplete: Mapping[str, str] | None = None,
) -> dict[Section, SectionResult]:
    return detect(
        prs=[link_pr(p) for p in prs],
        threads=threads,
        open_threads=open_threads,
        tasks=tasks or {},
        incomplete=incomplete,
    )


def test_a_and_a_prime() -> None:
    th = _thread("T-a", tags=("T243",))
    tasks = _tasks(_task("T243", "not_started", notes="PR #517 merged"))
    out = _run([_pr(517, "Thread: T-a"), _pr(518, "Thread: T-a")], {(P, "T-a"): th}, tasks=tasks)
    a = out[Section.NOT_STARTED].rows
    assert [r["task_id"] for r in a] == ["T243"]
    assert a[0]["prs"] == ["o/r#517", "o/r#518"]
    assert out[Section.PR_RECORD].rows[0]["prs_not_in_notes"] == ["o/r#518"]


def test_a_prime_ignores_status() -> None:
    th = _thread("T-a", tags=("T1",))
    out = _run([_pr(5, "Thread: T-a")], {(P, "T-a"): th}, tasks=_tasks(_task("T1", "in_progress")))
    assert out[Section.NOT_STARTED].rows == []
    assert out[Section.PR_RECORD].rows[0]["status"] == "in_progress"


def test_multiple_candidates_are_all_listed_and_flagged() -> None:
    th = _thread("T-a", tags=("T1", "T2"))
    tasks = _tasks(_task("T1", "not_started"), _task("T2", "not_started"))
    rows = _run([_pr(5, "Thread: T-a")], {(P, "T-a"): th}, tasks=tasks)[Section.NOT_STARTED].rows
    assert [(r["task_id"], r["multiple_candidates"]) for r in rows] == [("T1", True), ("T2", True)]


def test_unlinked_reasons() -> None:
    threads = {
        (P, "T-gone"): None,
        (P, "T-empty"): _thread("T-empty"),
        (P, "T-x"): _thread("T-x", tags=("T7",)),
    }
    out = _run(
        [
            _pr(1, "nothing"),
            _pr(2, "Thread: T-gone"),
            _pr(3, "Thread: T-empty"),
            _pr(4, "Thread: T-x"),
        ],
        threads,
    )
    reasons = {r["pr"]: r["reason"] for r in out[Section.UNLINKED_PRS].rows}
    assert reasons == {
        "o/r#1": "no-thread",
        "o/r#2": "thread-not-found",
        "o/r#3": "thread-has-no-task",
        "o/r#4": "task-not-in-ledger",
    }


def test_close_candidates_need_at_least_one_task_all_completed() -> None:
    open_threads = [
        _thread("T-done", tags=("T1",)),
        _thread("T-half", tags=("T1", "T2")),
        _thread("T-none"),
        _thread("T-missing", tags=("T99",)),
    ]
    tasks = _tasks(_task("T1", "completed"), _task("T2", "in_progress"))
    rows = _run([], {}, open_threads, tasks)[Section.CLOSE_CANDIDATES].rows
    assert [r["thread_id"] for r in rows] == ["T-done"]


def test_close_candidates_are_not_bounded_by_the_pr_window() -> None:
    # No PR at all in the window: the long-idle thread is still found (msg-9526 Obj-2-1).
    rows = _run([], {}, [_thread("T-old", tags=("T224",))], _tasks(_task("T224", "completed")))
    assert rows[Section.CLOSE_CANDIDATES].rows[0]["thread_id"] == "T-old"


def test_stale_blocked_by_over_the_whole_ledger() -> None:
    tasks = _tasks(
        _task("T190", "completed"),
        _task("T191", "not_started", blocked_by=("T190", "T300")),
        _task("T300", "in_progress"),
    )
    rows = _run([], {}, [], tasks)[Section.STALE_BLOCKED_BY].rows
    assert rows == [
        {"project": P, "task_id": "T191", "status": "not_started", "completed_blockers": ["T190"]}
    ]


def test_incomplete_input_blanks_only_the_sections_that_need_it() -> None:
    tasks = _tasks(_task("T1", "completed"), _task("T2", "x", blocked_by=("T1",)))
    out = _run([], {}, [], tasks, incomplete={"open_threads": "page 3 failed"})
    assert out[Section.CLOSE_CANDIDATES].as_dict() == {
        "complete": False,
        "incomplete_reasons": ["open_threads: page 3 failed"],
    }
    assert "count" not in out[Section.CLOSE_CANDIDATES].as_dict()
    assert out[Section.STALE_BLOCKED_BY].as_dict()["count"] == 1


# --- result file ---------------------------------------------------------------------------


def _write(tmp_path: Path, generated_at: datetime, **incomplete: str) -> Path:
    path = result_path(tmp_path, P)
    sections = _run([], {}, [], {}, incomplete=incomplete)
    write_atomic(
        path,
        build(
            project=P,
            generated_at=generated_at,
            since=generated_at - timedelta(days=30),
            until=generated_at,
            sections=sections,
        ),
    )
    return path


def test_result_path_rejects_a_path_like_project(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        result_path(tmp_path, "../x")


def test_missing_file_is_missing(tmp_path: Path) -> None:
    judged = judge(result_path(tmp_path, P), now=NOW, stale_after=timedelta(minutes=30))
    assert judged.freshness is Freshness.MISSING
    assert digest_lines(judged) == ["台帳のずれ: 未生成"]


def test_fresh_then_stale(tmp_path: Path) -> None:
    path = _write(tmp_path, NOW - timedelta(minutes=10))
    fresh = judge(path, now=NOW, stale_after=timedelta(minutes=30))
    assert fresh.freshness is Freshness.FRESH
    stale = judge(path, now=NOW + timedelta(hours=3), stale_after=timedelta(minutes=30))
    assert stale.freshness is Freshness.STALE
    assert stale.payload["window"]["applies_to"] == ["not_started", "pr_record", "unlinked_prs"]
    heading = digest_lines(stale)[0]
    assert heading.startswith("台帳のずれ: 古い結果 (") and "参考値" in heading


def test_incomplete_sections_are_reported(tmp_path: Path) -> None:
    path = _write(tmp_path, NOW, prs="page 2 failed")
    judged = judge(path, now=NOW, stale_after=timedelta(minutes=30))
    assert set(judged.incomplete_sections()) == {"not_started", "pr_record", "unlinked_prs"}
    assert any("不完全: prs: page 2 failed" in line for line in digest_lines(judged))


def test_garbage_file_is_unreadable_not_fresh(tmp_path: Path) -> None:
    path = result_path(tmp_path, P)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"generated_at": NOW.isoformat()}), encoding="utf-8")
    assert judge(path, now=NOW, stale_after=timedelta(minutes=30)).freshness is Freshness.UNREADABLE


def test_write_leaves_no_temp_file(tmp_path: Path) -> None:
    path = _write(tmp_path, NOW)
    assert [p.name for p in path.parent.iterdir()] == [f"{P}.json"]
