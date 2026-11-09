"""PR-1b of T-dirty-clone-wip-retreat-and-silent-stall-alerts (Bohr msg-8043 v1.6 + msg-8045 v1.7).

The record of each non-no-op clone retreat goes to ``state/clone-retreats.jsonl`` (option (b):
nothing is posted to the thread). The run's first implementer dispatch of that thread carries it
into the real prompt, once. Routing is the same with or without the record.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire import cli
from spirrow_mindwire.adapters.implementer import _build_prompt
from spirrow_mindwire.clone_guard import CloneGuard, GitResult
from spirrow_mindwire.clone_retreat import (
    CloneRetreater,
    RetreatOutcome,
    RetreatResult,
    _subprocess_git,
    git_env,
)
from spirrow_mindwire.clone_retreat_log import (
    CLONE_RETREAT_LOG_FILENAME,
    DELIVERED_KIND,
    NOTICE_TAG,
    RETREAT_KIND,
    build_preserved_record,
    build_record,
    load_pending_notice,
    pending_record,
    record_preserved,
    record_result,
    render_notice,
)
from spirrow_mindwire.conductor.core import Conductor, ConductorOutcome
from spirrow_mindwire.value_objects import ChatroomEvent, Role, SessionHandle, ThreadRef

THREAD = "T-sample"
AT = datetime(2026, 11, 6, 1, 2, 3, tzinfo=UTC)
_TR = ThreadRef(project_id="p", thread_id=THREAD, chatroom_uri="mc://t")
_ROSTER = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER, "Heisenberg": Role.IMPLEMENTER}


# --- real git fixtures (as tests/test_clone_retreat.py) ----------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True, env=git_env()
    ).stdout


def _cfg(repo: Path) -> None:
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _cfg(seed)
    (seed / ".gitignore").write_text(".mindwire/\n", encoding="utf-8")
    (seed / "a.txt").write_text("base\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    c = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(c)], check=True, capture_output=True)
    _cfg(c)
    return c


def _run_cli(clone: Path, state: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(
            [
                "clone-retreat",
                "--repo-dir",
                str(clone),
                "--thread-id",
                THREAD,
                "--head-msg",
                "msg-9",
                "--stop-reason",
                "adapter.turn_timeout",
                "--state-dir",
                str(state),
            ]
        )


def _rows(state: Path) -> list[dict[str, Any]]:
    path = state / CLONE_RETREAT_LOG_FILENAME
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# --- requirement 1: the record's contents, per outcome -----------------------------------------


def _result(outcome: RetreatOutcome, **kw: Any) -> RetreatResult:
    return RetreatResult(outcome, THREAD, **kw)


@pytest.mark.parametrize("outcome", [RetreatOutcome.CLEAN, RetreatOutcome.NOT_ELIGIBLE])
def test_no_op_outcomes_write_no_record(outcome: RetreatOutcome, tmp_path: Path) -> None:
    path = tmp_path / CLONE_RETREAT_LOG_FILENAME
    row = record_result(path, _result(outcome), repo_dir=tmp_path, head_msg="m", stop_reason="r")
    assert row is None
    assert not path.exists()


def test_clean_clone_via_cli_writes_no_record(clone: Path, tmp_path: Path) -> None:
    _run_cli(clone, tmp_path / "state")
    assert _rows(tmp_path / "state") == []


def _common(row: dict[str, Any], outcome: str) -> None:
    assert row["kind"] == RETREAT_KIND
    assert row["thread_id"] == THREAD
    assert row["head_msg"] == "msg-9"
    assert row["stop_reason"] == "adapter.turn_timeout"
    assert row["outcome"] == outcome
    assert row["record_id"].startswith(f"{THREAD}@")


def test_retreated_pushed_record_from_the_real_cli(clone: Path, tmp_path: Path) -> None:
    (clone / "a.txt").write_text("base\nmore\n", encoding="utf-8")
    (clone / "new.txt").write_text("n1\nn2\n", encoding="utf-8")
    _run_cli(clone, tmp_path / "state")
    [row] = _rows(tmp_path / "state")
    _common(row, "retreated")
    assert row["pushed"] is True
    assert row["branch"].startswith(f"wip/{THREAD}-")
    assert (row["files"], row["insertions"], row["deletions"]) == (2, 3, 0)
    assert row["patch"] is None and row["untracked"] is None
    assert row["repo_dir"] == str(clone)


def test_retreated_unpushed_record_from_the_real_cli(clone: Path, tmp_path: Path) -> None:
    _git(clone, "remote", "set-url", "--push", "origin", str(tmp_path / "nowhere.git"))
    (clone / "a.txt").write_text("base\nlocal\n", encoding="utf-8")
    _run_cli(clone, tmp_path / "state")
    [row] = _rows(tmp_path / "state")
    _common(row, "retreated")
    assert row["pushed"] is False
    assert row["branch"].startswith("wip/")
    assert "NOT pushed" in render_notice(row)


def test_discarded_record_from_the_real_cli(clone: Path, tmp_path: Path) -> None:
    # Unmerged paths without an op marker (a conflicted `stash pop`): retreat cannot run.
    (clone / "a.txt").write_text("stashed\n", encoding="utf-8")
    _git(clone, "stash", "-q")
    (clone / "a.txt").write_text("moved\n", encoding="utf-8")
    _git(clone, "commit", "-q", "-am", "moved")
    _git(clone, "stash", "pop", check=False)
    _run_cli(clone, tmp_path / "state")
    [row] = _rows(tmp_path / "state")
    _common(row, "discarded")
    assert row["branch"] is None
    assert row["patch"] and Path(row["patch"]).exists()
    assert row["untracked"] and Path(row["untracked"]).exists()


def test_discard_incomplete_record_from_the_real_cli(clone: Path, tmp_path: Path) -> None:
    (clone / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(clone, "commit", "-q", "-am", "v1")
    _git(clone, "bisect", "start")
    (clone / "a.txt").write_text("dirty\n", encoding="utf-8")
    _run_cli(clone, tmp_path / "state")
    [row] = _rows(tmp_path / "state")
    _common(row, "discard_incomplete")
    assert row["marker"] == "BISECT_START"
    assert row["patch"] and Path(row["patch"]).exists()


def test_failed_record_keeps_branch_and_detail(tmp_path: Path) -> None:
    res = _result(
        RetreatOutcome.FAILED,
        branch="wip/T-sample-x",
        pushed=True,
        files=1,
        insertions=1,
        deletions=0,
        detail="committed to wip/T-sample-x but could not switch back to main",
    )
    row = build_record(res, repo_dir=tmp_path, head_msg="msg-9", stop_reason="adapter.turn_timeout")
    assert row is not None
    _common(row, "failed")
    assert row["branch"] == "wip/T-sample-x"
    assert "could not switch back" in render_notice(row)


_CLEANED_CLAIM = "not in the working tree"
_INSPECT_CLAIM = "Inspect the current clone state"


@pytest.mark.parametrize(
    ("outcome", "kw"),
    [
        (
            RetreatOutcome.FAILED,
            {
                "branch": "wip/T-sample-x",
                "pushed": True,
                "files": 1,
                "insertions": 1,
                "deletions": 0,
                "detail": "committed to wip/T-sample-x but could not switch back to main",
            },
        ),
        (RetreatOutcome.FAILED, {"detail": "CalledProcessError: git add failed"}),
        (
            RetreatOutcome.DISCARD_INCOMPLETE,
            {
                "patch": "state/x.patch",
                "untracked": "state/x.untracked.txt",
                "marker": "MERGE_HEAD",
            },
        ),
    ],
)
def test_incomplete_cleanup_notice_does_not_claim_the_clone_is_clean(
    outcome: RetreatOutcome, kw: dict[str, Any], tmp_path: Path
) -> None:
    """PR-gate #526 objection 1: a failed / incomplete cleanup must not say the work is gone."""
    row = build_record(_result(outcome, **kw), repo_dir=tmp_path, head_msg="msg-9", stop_reason="x")
    assert row is not None
    text = render_notice(row)
    assert _CLEANED_CLAIM not in text
    assert "cleaned it up" not in text
    assert "attempted to clean it up" in text
    assert _INSPECT_CLAIM in text
    # The inspection instruction comes before the recovery locations.
    first_location = min(
        i
        for i in (text.find("- branch:"), text.find("- patch:"), text.find("- outcome:"))
        if i >= 0
    )
    assert text.find(_INSPECT_CLAIM) < first_location


@pytest.mark.parametrize(
    ("outcome", "kw"),
    [
        (
            RetreatOutcome.RETREATED,
            {
                "branch": "wip/T-sample-x",
                "pushed": True,
                "files": 1,
                "insertions": 1,
                "deletions": 0,
            },
        ),
        (
            RetreatOutcome.DISCARDED,
            {"patch": "state/x.patch", "untracked": "state/x.untracked.txt"},
        ),
    ],
)
def test_completed_cleanup_notice_says_the_work_is_saved_elsewhere(
    outcome: RetreatOutcome, kw: dict[str, Any], tmp_path: Path
) -> None:
    row = build_record(_result(outcome, **kw), repo_dir=tmp_path, head_msg="msg-9", stop_reason="x")
    assert row is not None
    text = render_notice(row)
    assert _CLEANED_CLAIM in text
    assert _INSPECT_CLAIM not in text


def test_record_write_failure_does_not_change_the_exit_code(
    clone: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (clone / "a.txt").write_text("base\nx\n", encoding="utf-8")
    state = tmp_path / "state"
    (state / CLONE_RETREAT_LOG_FILENAME).mkdir(parents=True)  # a directory: the append fails
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "clone-retreat",
                "--repo-dir",
                str(clone),
                "--thread-id",
                THREAD,
                "--state-dir",
                str(state),
            ]
        )
    assert exc.value.code == 0
    assert "writing the retreat record failed" in capsys.readouterr().err


# --- requirement 3: a failed commit keeps the staged new file in the patch ----------------------


def test_failed_commit_keeps_staged_new_file_in_patch(clone: Path, tmp_path: Path) -> None:
    def git(repo_dir: Path, args: Sequence[str]) -> GitResult:
        if "commit" in args:
            return GitResult(1, "", "simulated commit failure")
        return _subprocess_git(repo_dir, args)

    (clone / "brand-new.txt").write_text("only in the index\n", encoding="utf-8")
    res = CloneRetreater(
        clone,
        thread_id=THREAD,
        head_msg="msg-9",
        stop_reason="adapter.turn_timeout",
        state_dir=tmp_path / "state",
        git=git,
        now=lambda: AT,
    ).retreat()
    assert res.outcome is RetreatOutcome.DISCARDED, res.row()
    assert any(s.startswith("git add -A -> 0") for s in res.steps), "add -A must have run"
    assert res.patch is not None and res.untracked is not None
    patch = Path(res.patch).read_text(encoding="utf-8")
    assert "brand-new.txt" in patch and "+only in the index" in patch
    listing = Path(res.untracked).read_text(encoding="utf-8")
    assert "brand-new.txt" not in listing  # staged, so no longer untracked: the patch holds it
    CloneGuard(clone).check()
    assert not (clone / "brand-new.txt").exists()


# --- requirement 2 (v1.7): the record reaches the next implementer's real context ---------------


class _Mcp:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.writes: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "chatroom_get_thread":
            return {"messages": self.messages}
        self.writes.append((name, arguments))
        return {"msg": {"msg_id": "msg-relay"}}


class _Dispatcher:
    def __init__(self) -> None:
        self.dispatched: list[tuple[Role, ChatroomEvent]] = []

    async def spawn_instance(
        self, thread_ref: ThreadRef, role: Role, instance_id: str
    ) -> SessionHandle:
        return SessionHandle(
            session_id="s",
            instance_id=instance_id,
            adapter_id="a",
            thread_ref=thread_ref,
            role=role,
            started_at=datetime.now(UTC),
        )

    async def dispatch(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        self.dispatched.append((handle.role, event))


def _thread(head_author: str, head_next: str) -> list[dict[str, Any]]:
    return [
        {"msg_id": "msg-1", "author": "Bohr", "content": "the design", "timestamp": ""},
        {"msg_id": "msg-2", "author": head_author, "content": f"go\n\nNEXT: {head_next}"},
    ]


async def _run(
    log: Path | None, head_author: str = "human", head_next: str = "Heisenberg"
) -> tuple[ConductorOutcome, _Dispatcher, _Mcp]:
    dispatcher = _Dispatcher()
    mcp = _Mcp(_thread(head_author, head_next))
    c = Conductor(
        mcp=mcp,
        dispatcher=dispatcher,
        thread_ref=_TR,
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=1,
        clone_retreat_log_path=log,
    )
    outcome = await c.run()
    return outcome, dispatcher, mcp


def _write(log: Path, res: RetreatResult) -> dict[str, Any]:
    row = record_result(
        log, res, repo_dir=Path("C:/clone"), head_msg="msg-9", stop_reason="adapter.turn_timeout"
    )
    assert row is not None
    return row


def _retreated(pushed: bool = True) -> RetreatResult:
    return _result(
        RetreatOutcome.RETREATED,
        branch="wip/T-sample-20261106T010203Z",
        pushed=pushed,
        files=24,
        insertions=4501,
        deletions=0,
    )


@pytest.mark.anyio
async def test_retreated_record_is_in_the_next_implementer_prompt(tmp_path: Path) -> None:
    log = tmp_path / CLONE_RETREAT_LOG_FILENAME
    _write(log, _retreated())
    _, dispatcher, _ = await _run(log)
    [(role, event)] = dispatcher.dispatched
    assert role is Role.IMPLEMENTER
    prompt = _build_prompt(event, role)  # the implementer adapter's real prompt assembly
    assert "wip/T-sample-20261106T010203Z" in prompt
    assert "24 files, +4501/-0" in prompt
    # Its own block after the trigger, never inside the author's message.
    assert prompt.index("New message from") < prompt.index(NOTICE_TAG)
    assert NOTICE_TAG not in event.payload.body


@pytest.mark.anyio
@pytest.mark.parametrize(
    "res",
    [
        _result(
            RetreatOutcome.DISCARDED,
            patch="C:/data/state/clone-discarded-T-sample-x.patch",
            untracked="C:/data/state/clone-discarded-T-sample-x.untracked.txt",
        ),
        _result(
            RetreatOutcome.DISCARD_INCOMPLETE,
            patch="C:/data/state/clone-discarded-T-sample-x.patch",
            untracked="C:/data/state/clone-discarded-T-sample-x.untracked.txt",
            marker="BISECT_START",
        ),
    ],
)
async def test_discarded_record_puts_the_patch_path_in_the_prompt(
    tmp_path: Path, res: RetreatResult
) -> None:
    log = tmp_path / CLONE_RETREAT_LOG_FILENAME
    _write(log, res)
    _, dispatcher, _ = await _run(log)
    [(role, event)] = dispatcher.dispatched
    prompt = _build_prompt(event, role)
    assert "C:/data/state/clone-discarded-T-sample-x.patch" in prompt
    assert "C:/data/state/clone-discarded-T-sample-x.untracked.txt" in prompt


@pytest.mark.anyio
async def test_record_from_the_real_cli_reaches_the_prompt(clone: Path, tmp_path: Path) -> None:
    (clone / "a.txt").write_text("base\nwork\n", encoding="utf-8")
    state = tmp_path / "state"
    _run_cli(clone, state)
    [row] = _rows(state)
    _, dispatcher, _ = await _run(state / CLONE_RETREAT_LOG_FILENAME)
    [(role, event)] = dispatcher.dispatched
    assert row["branch"] in _build_prompt(event, role)


@pytest.mark.anyio
async def test_record_is_delivered_once(tmp_path: Path) -> None:
    log = tmp_path / CLONE_RETREAT_LOG_FILENAME
    row = _write(log, _retreated())
    await _run(log)
    assert pending_record(log, THREAD) is None
    delivered = list(map(json.loads, log.read_text("utf-8").splitlines()))
    assert delivered[-1]["kind"] == DELIVERED_KIND
    assert delivered[-1]["record_id"] == row["record_id"]
    _, dispatcher, _ = await _run(log)
    [(_, event)] = dispatcher.dispatched
    assert event.clone_retreat_notice is None


@pytest.mark.anyio
async def test_a_newer_record_is_delivered_after_an_older_one(tmp_path: Path) -> None:
    # Both records are written with no clock tick between them, as on Windows' coarse clock:
    # the newer one must still be pending after the older one is delivered.
    log = tmp_path / CLONE_RETREAT_LOG_FILENAME
    _write(log, _retreated())
    await _run(log)
    _write(log, _result(RetreatOutcome.RETREATED, branch="wip/T-sample-later", pushed=True))
    _, dispatcher, _ = await _run(log)
    [(_, event)] = dispatcher.dispatched
    assert event.clone_retreat_notice is not None
    assert "wip/T-sample-later" in event.clone_retreat_notice


@pytest.mark.anyio
async def test_other_threads_record_is_not_shown(tmp_path: Path) -> None:
    log = tmp_path / CLONE_RETREAT_LOG_FILENAME
    record_result(
        log,
        RetreatResult(RetreatOutcome.RETREATED, "T-other", branch="wip/T-other-x", pushed=True),
        repo_dir=tmp_path,
        head_msg="m",
        stop_reason="r",
    )
    _, dispatcher, _ = await _run(log)
    [(_, event)] = dispatcher.dispatched
    assert event.clone_retreat_notice is None


@pytest.mark.anyio
async def test_non_implementer_dispatch_gets_no_notice_and_keeps_it_pending(
    tmp_path: Path,
) -> None:
    log = tmp_path / CLONE_RETREAT_LOG_FILENAME
    _write(log, _retreated())
    _, dispatcher, _ = await _run(log, head_author="Bohr", head_next="Einstein")
    [(role, event)] = dispatcher.dispatched
    assert role is Role.NAYSAYER
    assert event.clone_retreat_notice is None
    assert pending_record(log, THREAD) is not None  # still waiting for the implementer


@pytest.mark.anyio
async def test_failed_dispatch_leaves_the_record_pending(tmp_path: Path) -> None:
    log = tmp_path / CLONE_RETREAT_LOG_FILENAME
    _write(log, _retreated())

    class _Boom(_Dispatcher):
        async def dispatch(self, handle: SessionHandle, event: ChatroomEvent) -> None:
            raise RuntimeError("turn_timeout")

    c = Conductor(
        mcp=_Mcp(_thread("human", "Heisenberg")),
        dispatcher=_Boom(),
        thread_ref=_TR,
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=1,
        clone_retreat_log_path=log,
    )
    with pytest.raises(RuntimeError):
        await c.run()
    assert pending_record(log, THREAD) is not None


def test_unreadable_log_is_no_notice(tmp_path: Path) -> None:
    log = tmp_path / CLONE_RETREAT_LOG_FILENAME
    log.mkdir()  # opening a directory raises
    assert load_pending_notice(log, THREAD) is None


def test_torn_line_is_skipped_and_the_next_row_still_reads(tmp_path: Path) -> None:
    log = tmp_path / CLONE_RETREAT_LOG_FILENAME
    log.write_text('{"kind": "retreat", "record_id"', encoding="utf-8")  # no newline: torn
    _write(log, _retreated())
    assert pending_record(log, THREAD) is not None


# --- acceptance: routing is the same before and after the record --------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("head_author", "head_next"),
    [
        ("human", "Heisenberg"),
        ("Einstein", "Heisenberg"),
        ("Bohr", "Einstein"),
        ("Einstein", "Bohr"),
        ("Bohr", "none"),
    ],
)
async def test_routing_is_unchanged_by_the_record(
    tmp_path: Path, head_author: str, head_next: str
) -> None:
    without, d0, m0 = await _run(None, head_author, head_next)
    log = tmp_path / CLONE_RETREAT_LOG_FILENAME
    _write(log, _retreated())
    with_record, d1, m1 = await _run(log, head_author, head_next)
    assert [r for r, _ in d0.dispatched] == [r for r, _ in d1.dispatched]
    assert [e.payload.msg_id for _, e in d0.dispatched] == [
        e.payload.msg_id for _, e in d1.dispatched
    ]
    assert without == with_record
    # Nothing is posted to the thread because of the record (option (b)).
    assert m0.writes == m1.writes


def test_records_with_the_same_timestamp_get_distinct_ids(tmp_path: Path) -> None:
    at = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    rows = [
        build_record(_retreated(), repo_dir=tmp_path, head_msg="msg-9", stop_reason="x", at=at)
        for _ in range(2)
    ]
    assert rows[0] is not None and rows[1] is not None
    assert rows[0]["at"] == rows[1]["at"]
    assert rows[0]["record_id"] != rows[1]["record_id"]


# --- T-role-exit-leaves-clone-off-default-branch PR-A: clone-recover --preserve records ----------


def _preserved(thread: str = THREAD, *, recovered: bool = True) -> dict[str, Any]:
    return build_preserved_record(
        thread_id=thread,
        repo_dir=Path("C:/clones/repo"),
        branch="feature/x",
        preserved_ref="wip/feature/x-20261110T000000Z",
        preserved_sha="0123456789abcdef0123456789abcdef01234567",
        recovered=recovered,
        detail="1 unpushed commit(s) kept on local ref wip/feature/x-20261110T000000Z",
        at=AT,
    )


def test_preserved_notice_names_the_local_ref_and_sha() -> None:
    text = render_notice(_preserved())
    assert text.startswith(NOTICE_TAG)
    assert "`wip/feature/x-20261110T000000Z`" in text
    assert "0123456789abcdef0123456789abcdef01234567" in text
    assert "NOT pushed" in text
    assert "switched the clone back to the default branch" in text
    assert "left the shared clone dirty" not in text


def test_preserved_notice_when_the_switch_failed_says_to_inspect_first() -> None:
    rec = _preserved(recovered=False)
    assert rec["outcome"] == "failed"
    text = render_notice(rec)
    assert "did not complete" in text
    assert "git status" in text
    assert "wip/feature/x-20261110T000000Z" in text


@pytest.mark.anyio
async def test_preserved_record_reaches_this_threads_implementer_once(tmp_path: Path) -> None:
    log = tmp_path / CLONE_RETREAT_LOG_FILENAME
    record_preserved(
        log,
        thread_id=THREAD,
        repo_dir=tmp_path,
        branch="feature/x",
        preserved_ref="wip/feature/x-20261110T000000Z",
        preserved_sha="abc123",
        recovered=True,
    )
    _, dispatcher, _ = await _run(log)
    [(role, event)] = dispatcher.dispatched
    assert event.clone_retreat_notice is not None
    prompt = _build_prompt(event, role)
    assert "wip/feature/x-20261110T000000Z" in prompt
    assert "abc123" in prompt
    assert pending_record(log, THREAD) is None


@pytest.mark.anyio
async def test_preserved_record_of_another_thread_is_not_shown(tmp_path: Path) -> None:
    log = tmp_path / CLONE_RETREAT_LOG_FILENAME
    record_preserved(
        log,
        thread_id="T-other",
        repo_dir=tmp_path,
        branch="feature/x",
        preserved_ref="wip/feature/x-20261110T000000Z",
        preserved_sha="abc123",
        recovered=True,
    )
    _, dispatcher, _ = await _run(log)
    [(_, event)] = dispatcher.dispatched
    assert event.clone_retreat_notice is None
    assert pending_record(log, "T-other") is not None
