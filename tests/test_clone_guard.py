"""Pre-dispatch dirty-clone guard (T-timed-out-implementer-turn-leaves-dirty-shared-clone, v4).

Design: Bohr msg-5781 (consolidated v4), endorsed Einstein msg-5782. Real git repositories in
``tmp_path`` for every state git itself can produce; an injected runner for the failure modes
(timeout, missing git, non-zero exit) that a healthy git cannot be made to produce on demand.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire import clone_guard as cg
from spirrow_mindwire import loop_runner
from spirrow_mindwire.clone_guard import (
    DIRTY_CLONE_EXIT_CODE,
    DIRTY_CLONE_PAYLOAD_PREFIX,
    CloneGuard,
    DirtyCloneError,
    DirtyCloneReason,
    GitResult,
)
from spirrow_mindwire.conductor.core import ConductorStopSlot, ConductorStopSnapshot
from spirrow_mindwire.config import MindwireSettings
from spirrow_mindwire.dispatcher.core import Dispatcher
from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.spec_pin import SpecPinWriter
from spirrow_mindwire.ulid_util import new_ulid
from spirrow_mindwire.value_objects import (
    Capability,
    ChatroomEvent,
    EventType,
    HealthStatus,
    NewMessagePayload,
    Role,
    SessionHandle,
    SessionState,
    ThreadRef,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TS = datetime(2026, 10, 2, tzinfo=UTC)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clean clone on ``main`` with ``.mindwire/`` ignored, as the real repo has it (A-13)."""
    r = tmp_path / "clone"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / ".gitignore").write_text(".mindwire/\n", encoding="utf-8")
    (r / "a.txt").write_text("a\n", encoding="utf-8")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _reason(repo: Path, **kw: Any) -> DirtyCloneReason | None:
    try:
        CloneGuard(repo, **kw).check()
    except DirtyCloneError as exc:
        return exc.reason
    return None


# --------------------------------------------------------------------------- accept


def test_clean_clone_on_main_passes(repo: Path) -> None:
    assert _reason(repo) is None


def test_ignored_pin_alone_passes(repo: Path) -> None:
    (repo / ".mindwire").mkdir()
    (repo / ".mindwire" / "pin").write_text("schema_version: 1\n", encoding="utf-8")
    assert _reason(repo) is None


def test_no_origin_head_falls_back_to_main(repo: Path) -> None:
    # No remote at all: refs/remotes/origin/HEAD does not exist → `main`, and HEAD is main.
    assert _reason(repo) is None


def test_origin_head_names_the_default_branch(repo: Path) -> None:
    sha = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-ref", "refs/remotes/origin/develop", sha)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
    assert _reason(repo) == DirtyCloneReason.WRONG_HEAD  # main is not origin/HEAD's develop
    _git(repo, "switch", "-q", "-c", "develop")
    assert _reason(repo) is None


# --------------------------------------------------------------------------- refuse


def test_tracked_change_is_dirty_tree(repo: Path) -> None:
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(DirtyCloneError) as ei:
        CloneGuard(repo).check()
    assert ei.value.reason == DirtyCloneReason.DIRTY_TREE
    assert ei.value.head == "main"
    assert any("a.txt" in e for e in ei.value.porcelain)


def test_staged_change_is_dirty_tree(repo: Path) -> None:
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    assert _reason(repo) == DirtyCloneReason.DIRTY_TREE


def test_untracked_file_is_dirty_tree(repo: Path) -> None:
    (repo / "new_test.py").write_text("x\n", encoding="utf-8")
    assert _reason(repo) == DirtyCloneReason.DIRTY_TREE


@pytest.mark.parametrize("marker", cg.OP_IN_PROGRESS_MARKERS)
def test_operation_in_progress_is_refused(repo: Path, marker: str) -> None:
    git_dir = repo / ".git"
    if marker.startswith("rebase-"):
        (git_dir / marker).mkdir()
    else:
        (git_dir / marker).write_text(_git(repo, "rev-parse", "HEAD"), encoding="utf-8")
    with pytest.raises(DirtyCloneError) as ei:
        CloneGuard(repo).check()
    assert ei.value.reason == DirtyCloneReason.OP_IN_PROGRESS
    assert marker in ei.value.detail


def test_detached_head_is_wrong_head(repo: Path) -> None:
    _git(repo, "switch", "-q", "--detach")
    assert _reason(repo) == DirtyCloneReason.WRONG_HEAD


def test_feature_branch_is_wrong_head(repo: Path) -> None:
    # Einstein msg-5774 #1: a turn that committed its WIP and stopped leaves a CLEAN tree on its
    # own feature branch; the next thread must not branch off it.
    _git(repo, "switch", "-q", "-c", "feature/other-thread")
    with pytest.raises(DirtyCloneError) as ei:
        CloneGuard(repo).check()
    assert ei.value.reason == DirtyCloneReason.WRONG_HEAD
    assert ei.value.head == "feature/other-thread"


# --------------------------------------------------------------------------- index.lock


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.on_sleep: Any = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s
        if self.on_sleep is not None:
            self.on_sleep(self.now)


def test_index_lock_that_goes_away_within_the_wait_passes(repo: Path) -> None:
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    clock = _Clock()
    clock.on_sleep = lambda now: lock.unlink() if now >= 1.0 and lock.exists() else None
    assert _reason(repo, sleep=clock.sleep, monotonic=clock.monotonic) is None
    assert clock.sleeps and all(s == cg.LOCK_POLL_S for s in clock.sleeps)
    assert clock.now == pytest.approx(1.0)


def test_index_lock_held_past_the_wait_is_clone_busy(repo: Path) -> None:
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")
    clock = _Clock()
    assert (
        _reason(repo, sleep=clock.sleep, monotonic=clock.monotonic) == DirtyCloneReason.CLONE_BUSY
    )
    assert clock.now == pytest.approx(cg.LOCK_WAIT_S)


def test_every_git_call_disables_optional_locks(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    real_run = subprocess.run

    def _spy(argv: list[str], **kw: Any) -> Any:
        calls.append((list(argv), dict(kw.get("env") or {})))
        return real_run(argv, **kw)

    monkeypatch.setattr(subprocess, "run", _spy)
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")  # also exercise the error path
    with pytest.raises(DirtyCloneError):
        CloneGuard(repo).check()
    assert calls
    for argv, env in calls:
        assert argv[:2] == ["git", "--no-optional-locks"], argv
        assert env.get("GIT_OPTIONAL_LOCKS") == "0"


def test_guard_leaves_the_clone_untouched(repo: Path) -> None:
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    (repo / "u.txt").write_text("u\n", encoding="utf-8")
    before = _git(repo, "status", "--porcelain=v1")
    with pytest.raises(DirtyCloneError):
        CloneGuard(repo).check()
    assert _git(repo, "status", "--porcelain=v1") == before
    assert not (repo / ".git" / "index.lock").exists()
    assert _git(repo, "stash", "list") == ""


# --------------------------------------------------------------------------- git_unavailable


def _scripted(repo: Path, *, fail_on: str, result: GitResult | BaseException) -> cg.GitRunner:
    def run(r: Path, args: Sequence[str]) -> GitResult:
        if fail_on in " ".join(args):
            if isinstance(result, BaseException):
                raise result
            return result
        return cg._subprocess_git(r, args)

    return run


def test_status_nonzero_exit_is_git_unavailable_not_clean(repo: Path) -> None:
    # Einstein msg-5776 #1: a failing `git status` prints nothing on stdout.
    runner = _scripted(repo, fail_on="status", result=GitResult(128, "", "fatal: index locked"))
    with pytest.raises(DirtyCloneError) as ei:
        CloneGuard(repo, git=runner).check()
    assert ei.value.reason == DirtyCloneReason.GIT_UNAVAILABLE
    assert "exit 128" in ei.value.detail


def test_git_timeout_is_git_unavailable(repo: Path) -> None:
    runner = _scripted(
        repo, fail_on="status", result=subprocess.TimeoutExpired(["git"], cg.GIT_TIMEOUT_S)
    )
    assert _reason(repo, git=runner) == DirtyCloneReason.GIT_UNAVAILABLE


def test_git_missing_is_git_unavailable(tmp_path: Path) -> None:
    def runner(_r: Path, _a: Sequence[str]) -> GitResult:
        raise FileNotFoundError("git")

    assert _reason(tmp_path, git=runner) == DirtyCloneReason.GIT_UNAVAILABLE


def test_not_a_repo_is_git_unavailable(tmp_path: Path) -> None:
    assert _reason(tmp_path) == DirtyCloneReason.GIT_UNAVAILABLE


def test_origin_head_lookup_failure_is_not_the_main_fallback(repo: Path) -> None:
    # Exit 1 (ref absent) falls back to main; any other failure is a git fault, not "main".
    runner = _scripted(
        repo, fail_on="refs/remotes/origin/HEAD", result=GitResult(128, "", "fatal: bad")
    )
    assert _reason(repo, git=runner) == DirtyCloneReason.GIT_UNAVAILABLE


# --------------------------------------------------------------------------- dispatcher


class _Adapter:
    adapter_id = "fake"
    capabilities = frozenset(
        {
            Capability.READ_THREAD,
            Capability.POST_REPLY,
            Capability.EXECUTE_CODE,
            Capability.NAYSAYER_QUALIFIED,
        }
    )

    def __init__(self, adapter_id: str = "fake") -> None:
        self.adapter_id = adapter_id
        self.delivered = 0

    async def spawn(self, thread_ref: ThreadRef, role: Role, ctx: SpawnContext) -> SessionHandle:
        return SessionHandle(
            session_id=new_ulid(),
            instance_id=ctx.own_instance_id,
            adapter_id=self.adapter_id,
            thread_ref=thread_ref,
            role=role,
            started_at=_TS,
        )

    async def deliver_event(self, handle: SessionHandle, event: ChatroomEvent) -> None:
        self.delivered += 1

    async def halt(self, handle: SessionHandle, *, grace: timedelta = timedelta(seconds=5)) -> None:
        return None

    async def health(self, handle: SessionHandle) -> HealthStatus:
        return HealthStatus(state=SessionState.IDLE, last_active_at=_TS, error=None, details={})


class _Gateway:
    async def post_reply(self, *a: Any, **kw: Any) -> str:
        return "m"


def _thread() -> ThreadRef:
    return ThreadRef(project_id="p", thread_id="T-x", chatroom_uri="mc://t")


def _event(event_id: str) -> ChatroomEvent:
    return ChatroomEvent(
        event_id=event_id,
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_thread(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id="m1", author="human", body="hi", parent_msg_id=None),
    )


def _dispatcher(repo: Path, adapter: _Adapter) -> Dispatcher:
    reg = InMemoryAdapterRegistry()
    reg.register(adapter)
    return Dispatcher(
        registry=reg,
        gateway=_Gateway(),
        spec_pin_writer=SpecPinWriter(pin_target_dir=repo, spec_source_root=repo),
        clone_guard=CloneGuard(repo),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("role", [Role.PROPOSER, Role.IMPLEMENTER, Role.NAYSAYER])
async def test_dirty_clone_refuses_every_role_before_pin_and_delivery(
    repo: Path, role: Role
) -> None:
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    adapter = _Adapter()
    disp = _dispatcher(repo, adapter)
    handle = await disp.spawn_instance(_thread(), role, f"{role.value}-1")
    with pytest.raises(DirtyCloneError) as ei:
        await disp.dispatch(handle, _event(f"e-{role.value}"))
    assert ei.value.reason == DirtyCloneReason.DIRTY_TREE
    assert adapter.delivered == 0
    assert not (repo / ".mindwire" / "pin").exists()  # refused BEFORE the pin write


@pytest.mark.anyio
async def test_clean_clone_dispatches_and_writes_pin(repo: Path) -> None:
    adapter = _Adapter()
    disp = _dispatcher(repo, adapter)
    handle = await disp.spawn_instance(_thread(), Role.IMPLEMENTER, "implementer-1")
    await disp.dispatch(handle, _event("e1"))
    assert adapter.delivered == 1
    assert (repo / ".mindwire" / "pin").is_file()
    # ...and the pin it just wrote does not make the NEXT dispatch's guard refuse.
    await disp.dispatch(handle, _event("e2"))
    assert adapter.delivered == 2


def test_composition_root_injects_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # build_registry's adapter-type checks are not under test here; the guard wiring is.
    monkeypatch.setattr(loop_runner, "build_registry", lambda **_kw: InMemoryAdapterRegistry())
    settings = MindwireSettings()
    settings.loop.repo_dir = tmp_path
    _mcp, _reg, disp = loop_runner._build_dispatcher(
        settings,
        mcp=object(),  # type: ignore[arg-type]
        proposer=_Adapter("p"),
        implementer=_Adapter("i"),
        naysayer=_Adapter("n"),
    )
    guard = disp._clone_guard
    assert isinstance(guard, CloneGuard)
    assert guard.repo_dir == tmp_path


# --------------------------------------------------------------------------- loop_runner.main


def test_main_maps_dirty_clone_to_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    async def _fake_run_conductor(
        _settings: MindwireSettings, *, stop_slot: ConductorStopSlot, **_kw: object
    ) -> None:
        # The conductor records an adapter-error snapshot for ANY dispatch exception; the dirty
        # clone must still not produce the exit-1 `reason=adapter_error` stop line.
        stop_slot.snapshot = ConductorStopSnapshot(
            rounds=0,
            last_msg_id="m1",
            forced=0,
            forced_saveable=0,
            error_code="dispatch.dirty_clone",
        )
        raise DirtyCloneError(
            repo_dir=tmp_path,
            reason=DirtyCloneReason.WRONG_HEAD,
            head="feature/x",
            detail="HEAD is on 'feature/x'",
        )

    monkeypatch.setattr(loop_runner, "run_conductor", _fake_run_conductor)
    monkeypatch.setattr(loop_runner, "load_settings", lambda: MindwireSettings())
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])
    with pytest.raises(SystemExit) as ei:
        loop_runner.main()
    assert ei.value.code == DIRTY_CLONE_EXIT_CODE == 8
    out = capsys.readouterr().out
    assert "reason=adapter_error" not in out
    rows = [ln for ln in out.splitlines() if ln.startswith(DIRTY_CLONE_PAYLOAD_PREFIX)]
    assert len(rows) == 1
    payload = json.loads(rows[0][len(DIRTY_CLONE_PAYLOAD_PREFIX) :])
    assert payload["reason"] == "wrong_head"
    assert payload["head"] == "feature/x"
    assert payload["repo_dir"] == str(tmp_path)


def test_dirty_clone_exit_code_is_distinct_and_pinned_to_the_wrapper() -> None:
    from spirrow_mindwire.conductor import run_budget, stall, stand_down

    taken = {
        1,
        2,
        stand_down.STAND_DOWN_EXIT_CODE,
        stall.STALLED_EXIT_CODE,
        run_budget.RUN_TIMEOUT_EXIT_CODE,
        run_budget.RUN_KILLED_EXIT_CODE,
        run_budget.RUN_KILL_UNCONFIRMED_EXIT_CODE,
    }
    assert DIRTY_CLONE_EXIT_CODE not in taken
    sweep = (_REPO_ROOT / "deploy" / "run-conductor-scheduled.ps1").read_text(encoding="utf-8")
    m = re.search(r"^\$DirtyCloneExitCode\s*=\s*(\d+)\s*$", sweep, re.MULTILINE)
    assert m is not None and int(m.group(1)) == DIRTY_CLONE_EXIT_CODE
    assert f"^{DIRTY_CLONE_PAYLOAD_PREFIX.strip()}" in sweep


def test_turn_end_obligation_is_filed_for_the_implementer() -> None:
    from spirrow_mindwire.obligations import load_manifest

    (entry,) = [o for o in load_manifest().obligations if o.id == "OBL-TURN-END-CLEAN-CLONE"]
    assert entry.role is Role.IMPLEMENTER
    assert "default branch" in entry.body and "feature branch" in entry.body
