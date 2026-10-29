# ruff: noqa: RUF001 - Japanese report text uses fullwidth punctuation on purpose
"""T-operator-task-lane PR2: runner + operator pass end to end (fake chatroom / spawner / session).

Each test names the design version whose behaviour it pins (msg-7408 - msg-7438). The state files
are real files under ``tmp_path``; only the chatroom, the process starts and the guard are fakes.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.magickit.client import ThreadResolvedError
from spirrow_mindwire.operator_lane import lane_pass as lp
from spirrow_mindwire.operator_lane import report as rpt
from spirrow_mindwire.operator_lane import runner as rn
from spirrow_mindwire.operator_lane import state as st
from spirrow_mindwire.operator_lane.config import LaneConfig, load_config
from spirrow_mindwire.operator_lane.guard import OK, OPEN, UNDETERMINED, GuardVerdict
from spirrow_mindwire.operator_lane.session import SessionOutcome

PROJECT = "spirrow-mindwire"
THREAD = "T-x"
T0 = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
WRAPPER_PID = 100
RUNNER_PID = 4242
HEAD_BODY = (
    "probe を流して結果を返してください。\n\n"
    "OPERATOR-TASK: scripts/probe.py を流して結果を報告する\n"
    "TIER-C-CHECK: none\n"
    "NEXT: operator"
)
SESSION_BODY = (
    "### 測った事実\n- probe: ok\n### 実行したこと\n- probe を 1 回流した\n"
    "### 残っていること\n- なし\nNEXT: Bohr"
)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Probe:
    def __init__(self) -> None:
        self.procs: dict[int, datetime] = {WRAPPER_PID: T0, RUNNER_PID: T0}

    def started_at(self, pid: int) -> datetime | None:
        return self.procs.get(pid)


class FakeChat:
    def __init__(self) -> None:
        self.msgs: list[dict[str, Any]] = [
            {"msg_id": "msg-0", "author": "Einstein", "content": "earlier"},
            {"msg_id": "msg-1", "author": "Bohr", "content": HEAD_BODY},
        ]
        self.n = 1
        self.post_mode: str | None = None
        self.posts: list[str] = []

    def messages(self, project: str, thread_id: str) -> list[dict[str, Any]]:
        return [dict(m) for m in self.msgs]

    def add(self, author: str, content: str) -> str:
        self.n += 1
        msg_id = f"msg-{self.n}"
        self.msgs.append({"msg_id": msg_id, "author": author, "content": content})
        return msg_id

    def post(self, project: str, thread_id: str, content: str) -> str:
        if self.post_mode == "resolved":
            raise ThreadResolvedError("thread is resolved")
        if self.post_mode == "down":
            raise RuntimeError("magickit unreachable")
        self.posts.append(content)
        msg_id = self.add("operator", content)
        if self.post_mode == "landed_then_timeout":
            raise RuntimeError("read timeout")
        return msg_id


class FakeLauncher:
    def __init__(self, stdout: str | None = None, outcome: SessionOutcome | None = None) -> None:
        self.stdout = stdout if stdout is not None else stream(SESSION_BODY)
        self.outcome = outcome or SessionOutcome(0, False)
        self.calls: list[list[str]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        cwd: str | None,
        timeout_sec: int,
    ) -> SessionOutcome:
        self.calls.append(list(argv))
        assert "OPERATOR-TASK: scripts/probe.py" in stdin_path.read_text(encoding="utf-8")
        stdout_path.write_text(self.stdout, encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return self.outcome


def stream(result: str, commands: Sequence[str] = ()) -> str:
    events: list[dict[str, Any]] = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": c}} for c in commands
                ]
            },
        },
        {"type": "result", "is_error": False, "result": result},
    ]
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"


class Harness:
    def __init__(self, tmp_path: Path, **config: Any) -> None:
        self.paths = st.LanePaths(tmp_path)
        self.paths.config.parent.mkdir(parents=True, exist_ok=True)
        cfg = {"enabled": True, "projects": [PROJECT], **config}
        self.paths.config.write_text(json.dumps(cfg), encoding="utf-8")
        self.head_skip = tmp_path / "head_skip.json"
        self.set_skip_record("msg-1")
        self.chat = FakeChat()
        self.clock = Clock()
        self.probe = Probe()
        self.launcher = FakeLauncher()
        self.guard_verdict = GuardVerdict(OK)
        self.spawned: list[str] = []
        self.on_spawn: Callable[[str], None] = self.run_runner
        self.run_ids = iter(f"run{i:04d}" for i in range(1, 100))
        self.mono = 0.0

    def tick(self, seconds: float) -> None:
        self.mono += seconds

    def set_skip_record(self, head: str, nomination: str = "operator") -> None:
        self.head_skip.write_text(
            json.dumps(
                {
                    THREAD: {
                        "last_observed_head_msg_id": head,
                        "last_observed_nomination": nomination,
                    }
                }
            ),
            encoding="utf-8",
        )

    def guard(self, config: LaneConfig) -> GuardVerdict:
        return self.guard_verdict

    def runner_deps(self) -> rn.RunnerDeps:
        return rn.RunnerDeps(
            paths=self.paths,
            config=load_config(self.paths.config),
            chat=self.chat,
            launcher=self.launcher,
            guard=self.guard,
            probe=self.probe,
            clock=self.clock,
            pid=RUNNER_PID,
        )

    def run_runner(self, run_id: str) -> None:
        rn.run_session(run_id, self.runner_deps())

    def spawn_runner(self, *, run_id: str, data_dir: Path, log_path: Path) -> int:
        self.spawned.append(run_id)
        self.on_spawn(run_id)
        return RUNNER_PID

    def run_pass(self) -> lp.PassResult:
        deps = lp.PassDeps(
            paths=self.paths,
            chat=self.chat,
            spawner=self,
            probe=self.probe,
            guard=self.guard,
            clock=self.clock,
            wrapper_pid=WRAPPER_PID,
            head_skip_path=self.head_skip,
            sleep=self.tick,
            monotonic=lambda: self.mono,
            new_run_id=lambda: next(self.run_ids),
        )
        return lp.run_pass(
            [
                {
                    "project": PROJECT,
                    "thread_id": THREAD,
                    "head_msg_id": self.chat.msgs[-1]["msg_id"],
                }
            ],
            deps,
        )

    def lock(self) -> dict[str, Any] | None:
        with st.hold_mutex(self.paths.mutex):
            return st.read_lock(self.paths)

    def heads(self) -> dict[str, Any]:
        return st.load_heads(self.paths)[st.head_key(PROJECT, THREAD)]


# --- happy path and the completion table -------------------------------------------------------


def test_one_task_one_session_posted_then_done(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    res = h.run_pass()
    assert res.action == "dispatched"
    assert h.spawned == ["run0001"]
    assert len(h.launcher.calls) == 1
    report = h.chat.posts[0]
    assert rpt.validate_report(report, run_id="run0001", requester="Bohr") == []
    assert "対象: msg-1 の OPERATOR-TASK: scripts/probe.py を流して結果を報告する" in report
    lock = h.lock()
    assert lock is not None and lock["result"]["kind"] == "posted"
    assert h.heads() == {"head_msg_id": "msg-1", "spawn_attempts": 0, "dispatched": True}
    assert h.run_pass().action == "done"
    assert h.lock() is None
    assert (h.paths.reports_done / "run0001.md").exists()


def test_session_argv_has_no_mcp_route_to_magickit() -> None:
    """D2'' (b-1): the session runs with --strict-mcp-config and an empty server list."""
    argv = rn.session_argv(LaneConfig())
    assert "--strict-mcp-config" in argv
    mcp = json.loads(rn.SESSION_MCP_CONFIG.read_text(encoding="utf-8"))
    assert mcp == {"mcpServers": {}}
    assert "magickit" not in rn.SESSION_MCP_CONFIG.read_text(encoding="utf-8").casefold()


def test_result_posted_is_done_even_when_posted_reads_false(tmp_path: Path) -> None:
    """D2⁶: the lock's ``result=posted`` decides; a lagging read never causes a quarantine."""
    h = Harness(tmp_path)
    h.run_pass()
    h.chat.msgs = h.chat.msgs[:2]  # the read side has not caught up with the post yet
    h.chat.posts.clear()
    assert h.run_pass().action == "done"
    assert h.chat.posts == []


def test_post_failed_is_reposted_with_late_report_after_a_third_party_post(tmp_path: Path) -> None:
    """D2⁵: a third party moving the head does not swallow the report."""
    h = Harness(tmp_path)
    h.chat.post_mode = "down"
    h.run_pass()
    lock = h.lock()
    assert lock is not None and lock["result"]["kind"] == "post_failed"
    h.chat.post_mode = None
    h.chat.add("Fermi", "進捗どうですか？")
    assert h.run_pass().action == "reposted"
    posted = h.chat.posts[-1]
    assert posted.startswith("OPERATOR-LANE: late-report（msg-1 の後に 1 件の投稿あり）")
    assert posted.rstrip().endswith("NEXT: Bohr")
    assert h.lock() is None


def test_failed_repost_keeps_the_lock_for_the_next_tick(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.chat.post_mode = "down"
    h.run_pass()
    assert h.run_pass().action == "repost_failed"
    assert h.lock() is not None
    h.chat.post_mode = None
    assert h.run_pass().action == "reposted"


def test_post_that_timed_out_but_landed_is_not_posted_twice(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.chat.post_mode = "landed_then_timeout"
    h.run_pass()
    assert len(h.chat.posts) == 1
    h.chat.post_mode = None
    assert h.run_pass().action == "done"
    assert len(h.chat.posts) == 1


def test_runner_died_before_result_with_report_file_reposts(tmp_path: Path) -> None:
    """D2⁶ row 3: no ``result``, a valid ``.md`` -> re-post, never quarantine."""
    h = Harness(tmp_path)
    h.chat.post_mode = "down"
    h.run_pass()
    lock = h.lock()
    assert lock is not None
    lock.pop("result")
    with st.hold_mutex(h.paths.mutex):
        st.replace_lock(h.paths, lock)
    h.probe.procs.pop(RUNNER_PID)  # the runner is gone
    h.chat.post_mode = None
    assert h.run_pass().action == "reposted"
    assert "kind=report" in h.chat.posts[-1]


def test_runner_died_before_result_after_a_landed_post_posts_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.chat.post_mode = "landed_then_timeout"
    h.run_pass()
    lock = h.lock()
    assert lock is not None
    lock.pop("result")
    with st.hold_mutex(h.paths.mutex):
        st.replace_lock(h.paths, lock)
    h.probe.procs.pop(RUNNER_PID)
    h.chat.post_mode = None
    assert h.run_pass().action == "done"
    assert len(h.chat.posts) == 1


def test_only_tmp_left_after_a_kill_is_quarantined(tmp_path: Path) -> None:
    """D2⁷: ``.md.tmp`` alone is not a report."""
    h = Harness(tmp_path)

    def die_mid_write(run_id: str) -> None:
        lock = h.lock()
        assert lock is not None
        with st.hold_mutex(h.paths.mutex):
            st.runner_cas_to_running(
                h.paths, run_id=run_id, pid=RUNNER_PID, pid_started_at=T0, now=h.clock()
            )
        h.paths.reports.mkdir(parents=True, exist_ok=True)
        h.paths.report_tmp(run_id).write_text("## operator report（lane run", encoding="utf-8")

    h.on_spawn = die_mid_write
    h.run_pass()
    h.probe.procs.pop(RUNNER_PID)
    assert h.run_pass().action == "quarantined"
    assert "kind=quarantine" in h.chat.posts[-1]
    assert "書きかけの report" in h.chat.posts[-1]
    assert not h.paths.report_tmp("run0001").exists()


def test_report_with_truncated_next_is_quarantined_not_reposted(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.chat.post_mode = "down"
    h.run_pass()
    path = h.paths.report("run0001")
    path.write_text(path.read_text(encoding="utf-8").rsplit("NEXT:", 1)[0], encoding="utf-8")
    h.chat.post_mode = None
    assert h.run_pass().action == "quarantined"
    assert "report ファイルの検証に失敗" in h.chat.posts[-1]
    assert str(path) in h.chat.posts[-1]
    assert all("kind=report" not in p for p in h.chat.posts)


def test_session_failure_with_a_third_party_post_is_quarantined(tmp_path: Path) -> None:
    """D2⁵: someone else moving the head is not mistaken for completion."""
    h = Harness(tmp_path)
    h.launcher.outcome = SessionOutcome(1, False)
    h.run_pass()
    h.chat.add("Fermi", "まだですか")
    assert h.run_pass().action == "quarantined"
    text = h.chat.posts[-1]
    assert "kind=quarantine" in text
    assert "OPERATOR-LANE: needs-hands quarantined" in text
    assert "late-report" in text
    assert rpt.validate_report(text, run_id="run0001", requester="Bohr") == []


def test_quarantine_is_not_posted_when_the_report_already_landed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.run_pass()
    lock = h.lock()
    assert lock is not None
    lock["result"] = {"kind": "session_failed", "exit_code": 1}
    with st.hold_mutex(h.paths.mutex):
        st.replace_lock(h.paths, lock)
    count = len(h.chat.posts)
    assert h.run_pass().action == "quarantined"
    assert len(h.chat.posts) == count


def test_timeout_is_quarantined_with_the_fact(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.launcher.outcome = SessionOutcome(None, True)
    h.run_pass()
    h.run_pass()
    assert "3600 秒で打ち切られました" in h.chat.posts[-1]


def test_invalid_session_output_is_quarantined(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.launcher.stdout = stream("done, all good")
    h.run_pass()
    lock = h.lock()
    assert lock is not None and lock["result"]["kind"] == "invalid_report"
    assert not h.paths.report("run0001").exists()
    assert h.run_pass().action == "quarantined"


def test_runner_and_conductor_share_validate_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2⁷: one function at all three gates — before save, before post, before re-post."""
    calls: list[str] = []
    real = rpt.validate_report

    def spy(text: str, *, run_id: str, requester: str) -> list[str]:
        import inspect

        calls.append(inspect.stack()[1].function)
        return real(text, run_id=run_id, requester=requester)

    monkeypatch.setattr(rpt, "validate_report", spy)
    h = Harness(tmp_path)
    h.chat.post_mode = "down"
    h.run_pass()
    h.chat.post_mode = None
    h.run_pass()
    assert calls[:2] == ["run_session", "post_report"]
    assert "_settle_finished" in calls and "_repost" in calls


def test_thread_resolved_on_repost_notifies_and_finishes(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.chat.post_mode = "down"
    h.run_pass()
    h.chat.post_mode = "resolved"
    res = h.run_pass()
    assert res.action == "thread_resolved"
    assert res.events[-1]["level"] == "notify"
    assert h.lock() is None
    assert (h.paths.reports_done / "run0001.md").exists()


# --- before the session: guard, head, CAS ------------------------------------------------------


def test_guard_open_disables_the_lane_without_dispatch(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.guard_verdict = GuardVerdict(OPEN, ("rule missing",))
    res = h.run_pass()
    assert res.action == "guard_open"
    assert h.spawned == []
    assert load_config(h.paths.config).enabled is False
    assert h.run_pass().action == "disabled"


def test_guard_open_seen_by_the_runner_disables_the_lane(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def runner_sees_open(run_id: str) -> None:
        h.guard_verdict = GuardVerdict(OPEN, ("rule disabled",))
        h.run_runner(run_id)

    h.on_spawn = runner_sees_open
    h.guard_verdict = GuardVerdict(OK)
    h.probe.procs.pop(RUNNER_PID)  # the runner exits after aborting
    res = h.run_pass()
    assert res.action == "guard_open"
    assert h.launcher.calls == []
    assert load_config(h.paths.config).enabled is False
    assert h.lock() is None
    assert h.heads()["dispatched"] is False


def test_guard_undetermined_skips_the_tick_and_keeps_the_lane_on(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.guard_verdict = GuardVerdict(UNDETERMINED, ("HTTP 502",))
    assert h.run_pass().action == "guard_undetermined"
    assert load_config(h.paths.config).enabled is True
    assert h.spawned == []


def test_head_moved_before_the_session_starts_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def head_moves(run_id: str) -> None:
        h.chat.add("Bohr", "やっぱり不要です\n\nNEXT: Einstein")
        h.run_runner(run_id)

    h.on_spawn = head_moves
    h.probe.procs.pop(RUNNER_PID)
    assert h.run_pass().action == "head_moved"
    assert h.launcher.calls == []
    assert h.chat.posts == []


def test_audit_hit_stops_on_the_operator_form(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.launcher.stdout = stream(SESSION_BODY, ["gh pr merge 12 --squash"])
    h.run_pass()
    text = h.chat.posts[-1]
    assert "OPERATOR-LANE: audit-hit gh pr merge" in text
    assert text.rstrip().endswith("NEXT: operator")


# --- spawn failures (D3⁵ / D3⁶, D5) ----------------------------------------------------------


def test_runner_that_dies_at_once_is_settled_in_the_same_tick(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def dies_at_once(run_id: str) -> None:  # an import error, say
        h.probe.procs.pop(RUNNER_PID, None)

    h.on_spawn = dies_at_once
    res = h.run_pass()
    assert res.action == "spawn_failed"
    assert h.lock() is None
    assert h.heads() == {"head_msg_id": "msg-1", "spawn_attempts": 1, "dispatched": False}
    h.probe.procs[RUNNER_PID] = T0
    h.on_spawn = h.run_runner  # the retry starts properly
    assert h.run_pass().action == "dispatched"
    assert h.run_pass().action == "done"


def test_second_spawn_failure_quarantines(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def die(run_id: str) -> None:
        h.probe.procs.pop(RUNNER_PID, None)

    h.on_spawn = die
    assert h.run_pass().action == "spawn_failed"
    h.probe.procs[RUNNER_PID] = T0
    res = h.run_pass()
    assert res.action == "quarantined"
    assert "2 回続けて起動できませんでした" in h.chat.posts[-1]
    assert h.heads()["dispatched"] is True
    assert h.lock() is None


def test_quarantine_post_failure_does_not_recount_attempts(tmp_path: Path) -> None:
    h = Harness(tmp_path)

    def die(run_id: str) -> None:
        h.probe.procs.pop(RUNNER_PID, None)

    h.on_spawn = die
    h.run_pass()
    h.probe.procs[RUNNER_PID] = T0
    h.chat.post_mode = "down"
    assert h.run_pass().action == "quarantine_post_failed"
    h.chat.post_mode = None
    assert h.run_pass().action == "quarantined"
    assert h.heads()["spawn_attempts"] == 2


def test_spawning_past_deadline_with_the_wrapper_alive_is_a_spawn_failure(tmp_path: Path) -> None:
    """D3⁶: a hung runner is caught by ``spawn_deadline`` although the wrapper pid lives."""
    h = Harness(tmp_path)
    h.on_spawn = lambda run_id: None  # hangs before its CAS
    assert h.run_pass().action == "dispatched"
    assert h.run_pass().action == "busy"
    h.clock.now = T0 + timedelta(seconds=121)
    assert h.run_pass().action == "spawn_failed"
    assert h.heads()["spawn_attempts"] == 1


def test_late_runner_cannot_start_a_session(tmp_path: Path) -> None:
    """D3⁷: past the deadline the runner's CAS fails although nobody reclaimed the lock."""
    h = Harness(tmp_path)
    h.on_spawn = lambda run_id: None
    h.run_pass()
    h.clock.now = T0 + timedelta(seconds=130)
    assert h.lock() is not None
    assert rn.run_session("run0001", h.runner_deps()) == rn.EXIT_STOPPED
    assert h.launcher.calls == []


def test_wrapper_killed_after_spawn_still_completes(tmp_path: Path) -> None:
    """D3⁵ #2: the runner moves its own lock to running; the wrapper's death changes nothing."""
    h = Harness(tmp_path)

    def wrapper_dies_then_runner_runs(run_id: str) -> None:
        h.probe.procs.pop(WRAPPER_PID)
        h.run_runner(run_id)

    h.on_spawn = wrapper_dies_then_runner_runs
    assert h.run_pass().action == "dispatched"
    assert h.run_pass().action == "done"
    assert all("kind=quarantine" not in p for p in h.chat.posts)


# --- selection, pre-screen, state fail-closed ---------------------------------------------------


def test_disabled_lane_touches_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path, enabled=False)
    assert h.run_pass().action == "disabled"
    assert not h.paths.state_dir.exists()


def test_heads_file_lost_stops_dispatch(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.run_pass()
    h.run_pass()
    h.paths.heads.unlink()
    res = h.run_pass()
    assert res.action == "heads_unavailable"
    assert res.events[0]["level"] == "notify"


def test_head_skip_rebuilt_does_not_redispatch(tmp_path: Path) -> None:
    """D3'': deleting head_skip.json and rebuilding it does not re-dispatch a dispatched head."""
    h = Harness(tmp_path)
    h.chat.post_mode = "resolved"  # the report can never land, so the head stays put
    h.run_pass()
    h.run_pass()
    h.head_skip.unlink()
    h.set_skip_record("msg-1")
    h.chat.post_mode = None
    assert h.run_pass().action == "idle"
    assert h.spawned == ["run0001"]


def test_new_head_replaces_the_threads_entry(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.run_pass()
    h.run_pass()
    new = h.chat.add("Bohr", HEAD_BODY)
    h.set_skip_record(new)
    h.run_pass()
    assert h.heads()["head_msg_id"] == new
    assert list(st.load_heads(h.paths)) == [st.head_key(PROJECT, THREAD)]


def test_live_manual_lease_blocks_dispatch_and_expired_one_is_reclaimed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    st.initialise(h.paths)
    with st.hold_mutex(h.paths.mutex):
        st.manual_acquire(h.paths, lease_id="L", ttl=timedelta(minutes=60), now=T0, target={})
    assert h.run_pass().action == "busy"
    assert h.spawned == []
    h.clock.now = T0 + timedelta(minutes=61)
    res = h.run_pass()
    assert res.action == "manual_lease_reclaimed"
    assert res.events[0]["level"] == "notify"
    assert h.run_pass().action == "dispatched"


def test_mutex_held_elsewhere_skips_the_pass_without_touching_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    st.initialise(h.paths)
    monkeypatch.setattr(st, "MUTEX_TIMEOUT_SEC", 0.2)
    with st.hold_mutex(h.paths.mutex):
        real = st.hold_mutex

        def short(path: Path, **kw: Any) -> Any:
            return real(path, timeout=0.2)

        monkeypatch.setattr(lp, "hold_mutex", short)
        assert h.run_pass().action == "mutex_timeout"
    assert h.lock() is None
    assert h.spawned == []


def test_eligibility_filters(tmp_path: Path) -> None:
    cfg = LaneConfig(enabled=True, projects=(PROJECT,))
    skip = {THREAD: {"last_observed_head_msg_id": "msg-1", "last_observed_nomination": "operator"}}
    cand = {"project": PROJECT, "thread_id": THREAD, "head_msg_id": "msg-1"}
    assert lp.eligible_candidates(cfg, [cand], {}, skip) == [cand]
    assert lp.eligible_candidates(cfg, [{**cand, "project": "other"}], {}, skip) == []
    assert lp.eligible_candidates(cfg, [{**cand, "head_msg_id": "msg-9"}], {}, skip) == []
    human = {THREAD: {**skip[THREAD], "last_observed_nomination": "human"}}
    assert lp.eligible_candidates(cfg, [cand], {}, human) == []
    done = {st.head_key(PROJECT, THREAD): {"head_msg_id": "msg-1", "dispatched": True}}
    assert lp.eligible_candidates(cfg, [cand], done, skip) == []


def test_marked_or_malformed_operator_heads_are_not_taken() -> None:
    assert rn.is_operator_task_head(HEAD_BODY) == "scripts/probe.py を流して結果を報告する"
    marked = "OPERATOR-LANE: needs-hands quarantined\n" + HEAD_BODY
    assert rn.is_operator_task_head(marked) is None
    assert rn.is_operator_task_head("do it\nNEXT: operator") is None


def test_prescreen_posts_needs_hands_without_a_session(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.chat.msgs[-1]["content"] = HEAD_BODY.replace(
        "scripts/probe.py を流して", "magickit を deploy して"
    )
    res = h.run_pass()
    assert res.action == "prescreened"
    assert h.spawned == []
    text = h.chat.posts[-1]
    assert "OPERATOR-LANE: needs-hands prescreen「deploy」" in text
    assert text.rstrip().endswith("NEXT: operator")
    assert h.heads()["dispatched"] is True
