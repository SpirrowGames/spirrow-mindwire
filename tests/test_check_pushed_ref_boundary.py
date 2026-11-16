"""``check_pushed`` judges only what the CONTINUE turn changed, and accepts pushed tips a narrow
fetch refspec cannot see.

Thread T-check-pushed-misreads-narrow-fetch-refspec-and-stale-branches. Design v2 (Bohr
msg-9350) tests 1-9, v2.1 (msg-9352) tests 10-13, v2.2 (msg-9354) tests 11 (replaced), 14, 15.
Test numbers below are the design's.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_conductor_continuation import _ENTRY, _ROSTER, _report
from test_conductor_core import _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire import loop_runner
from spirrow_mindwire.clone_guard import (
    CloneGuard,
    DirtyCloneError,
    DirtyCloneReason,
    GitResult,
    _subprocess_git,
)
from spirrow_mindwire.conductor.continuation import render_relay
from spirrow_mindwire.conductor.core import CONDUCTOR_RELAY_AUTHOR, Conductor
from spirrow_mindwire.dispatcher.core import Dispatcher
from spirrow_mindwire.dispatcher.registry import InMemoryAdapterRegistry
from spirrow_mindwire.ports import SpawnContext
from spirrow_mindwire.turn_ref_snapshot import (
    RETAIN_PER_THREAD,
    ContinuationScope,
    TurnRefSnapshotStore,
)
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

_NARROW = "+refs/heads/main:refs/remotes/origin/main"
_TS = datetime(2026, 11, 16, tzinfo=UTC)
_PROJECT = _thread_ref().project_id
_THREAD = _thread_ref().thread_id


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **env} if env else None,
    ).stdout


def _configure(r: Path) -> None:
    for k, v in (
        ("user.email", "t@example.invalid"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
    ):
        _git(r, "config", k, v)


def _commit(r: Path, name: str, *, env: dict[str, str] | None = None) -> str:
    (r / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    _git(r, "add", f"{name}.txt")
    _git(r, "commit", "-q", "-m", name, env=env)
    return _git(r, "rev-parse", "HEAD").strip()


@pytest.fixture
def narrow(tmp_path: Path) -> Path:
    """A clone whose ``remote.origin.fetch`` maps main only (voxelworld-impl, Fermi msg-9285)."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    r = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(r))
    _configure(r)
    (r / ".gitignore").write_text(".mindwire/\n", encoding="utf-8")
    _git(r, "add", ".gitignore")
    _commit(r, "init")
    _git(r, "push", "-q", "-u", "origin", "main")
    _git(r, "remote", "set-head", "origin", "main")
    _git(r, "config", "--replace-all", "remote.origin.fetch", _NARROW)
    return r


def _feature(r: Path, branch: str, name: str, *, push: bool) -> str:
    _git(r, "switch", "-q", "-c", branch)
    sha = _commit(r, name)
    if push:
        _git(r, "push", "-q", "-u", "origin", branch)
    _git(r, "switch", "-q", "main")
    return sha


class _Recording:
    """A git runner that records argv and can fail ``ls-remote``."""

    def __init__(self, *, fail_ls_remote: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._fail = fail_ls_remote

    def __call__(self, repo_dir: Path, args: Sequence[str]) -> GitResult:
        self.calls.append(tuple(args))
        if self._fail and args and args[0] == "ls-remote":
            return GitResult(128, "", "fatal: unable to access origin")
        return _subprocess_git(repo_dir, args)

    def ls_remote_calls(self) -> int:
        return sum(1 for c in self.calls if c and c[0] == "ls-remote")


# ------------------------------------------------------------------------- D-1' (remote tips)


def test_1_narrow_refspec_pushed_tip_passes(narrow: Path) -> None:
    _feature(narrow, "feature/n", "n", push=True)
    assert not (narrow / ".git" / "refs" / "remotes" / "origin" / "feature").exists()
    git = _Recording()
    CloneGuard(narrow, git=git).check_pushed()
    assert git.ls_remote_calls() == 1  # the candidate existed: rev-list alone would refuse it


def test_2_remote_advanced_past_local_tip_is_still_refused(narrow: Path, tmp_path: Path) -> None:
    """Out of D-1' scope by design: only exact tip equality is recognised (msg-9350)."""
    _feature(narrow, "feature/n", "n", push=True)
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", "-b", "feature/n", str(tmp_path / "origin.git"), str(other))
    _configure(other)
    _commit(other, "later")
    _git(other, "push", "-q", "origin", "feature/n")
    with pytest.raises(DirtyCloneError) as ei:
        CloneGuard(narrow).check_pushed()
    assert ei.value.reason is DirtyCloneReason.UNPUSHED
    assert "feature/n" in ei.value.detail


def test_3_ls_remote_failure_keeps_refusal(narrow: Path) -> None:
    _feature(narrow, "feature/n", "n", push=True)
    with pytest.raises(DirtyCloneError) as ei:
        CloneGuard(narrow, git=_Recording(fail_ls_remote=True)).check_pushed()
    assert ei.value.reason is DirtyCloneReason.UNPUSHED
    assert "ls-remote failed" in ei.value.detail


def test_4_no_candidate_never_calls_ls_remote(narrow: Path) -> None:
    git = _Recording(fail_ls_remote=True)
    CloneGuard(narrow, git=git).check_pushed()
    assert git.ls_remote_calls() == 0


def test_unpushed_new_branch_is_refused(narrow: Path) -> None:
    _feature(narrow, "feature/local", "l", push=False)
    with pytest.raises(DirtyCloneError) as ei:
        CloneGuard(narrow).check_pushed()
    assert "feature/local" in ei.value.detail


# ------------------------------------------------------------------------- D-2' (baseline)


def test_5_branch_unpushed_before_the_snapshot_is_ignored_with_warning(
    narrow: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _feature(narrow, "stale/old", "old", push=False)
    guard = CloneGuard(narrow)
    baseline = guard.ref_snapshot()
    with caplog.at_level(logging.WARNING):
        guard.check_pushed(baseline=baseline)
    assert "predate this turn" in caplog.text
    assert "stale/old" in caplog.text


def test_6_new_branch_on_an_old_unpushed_commit_is_refused(narrow: Path) -> None:
    old = _feature(narrow, "stale/old", "old", push=False)
    guard = CloneGuard(narrow)
    baseline = guard.ref_snapshot()
    _git(narrow, "branch", "fresh", old)
    with pytest.raises(DirtyCloneError) as ei:
        guard.check_pushed(baseline=baseline)
    assert "fresh" in ei.value.detail
    assert "stale/old" not in ei.value.detail


def test_6_backdated_commit_is_refused(narrow: Path) -> None:
    guard = CloneGuard(narrow)
    baseline = guard.ref_snapshot()
    _git(narrow, "switch", "-q", "-c", "feature/backdated")
    old_date = "2020-01-01T00:00:00+0000"
    _commit(narrow, "bd", env={"GIT_COMMITTER_DATE": old_date, "GIT_AUTHOR_DATE": old_date})
    _git(narrow, "switch", "-q", "main")
    with pytest.raises(DirtyCloneError) as ei:
        guard.check_pushed(baseline=baseline)
    assert "feature/backdated" in ei.value.detail


def test_8_same_key_is_never_overwritten(tmp_path: Path) -> None:
    store = TurnRefSnapshotStore(tmp_path / "s")
    kw: dict[str, Any] = {"project": "p", "thread_id": "t", "trigger_msg_id": "m1"}
    assert store.record(repo_dir=tmp_path, refs={"refs/heads/x": "a" * 40}, **kw) is True
    assert store.record(repo_dir=tmp_path, refs={"refs/heads/x": "b" * 40}, **kw) is False
    scope = ContinuationScope(project="p", thread_id="t", trigger_msg_id="m1")
    assert store.baseline_for(scope, tmp_path) == {"refs/heads/x": "a" * 40}


def test_9_no_snapshot_means_every_branch_is_judged(narrow: Path, tmp_path: Path) -> None:
    _feature(narrow, "stale/old", "old", push=False)
    store = TurnRefSnapshotStore(tmp_path / "s")
    scope = ContinuationScope(project="p", thread_id="t", trigger_msg_id="m1")
    assert store.baseline_for(scope, narrow) is None
    with pytest.raises(DirtyCloneError):
        CloneGuard(narrow).check_pushed(baseline=store.baseline_for(scope, narrow))


def test_11_old_snapshot_never_stands_in_for_the_missing_one(narrow: Path, tmp_path: Path) -> None:
    """Einstein msg-9353 Obj-1: S0 feature=A (unpushed), S1 feature=B, the turn resets to A."""
    b = _feature(narrow, "feature/x", "b", push=True)
    _git(narrow, "switch", "-q", "feature/x")
    a = _commit(narrow, "a")  # unpushed
    _git(narrow, "switch", "-q", "main")
    store = TurnRefSnapshotStore(tmp_path / "s")
    common: dict[str, Any] = {"project": "p", "thread_id": "t", "repo_dir": narrow}
    store.record(trigger_msg_id="m0", refs={"refs/heads/feature/x": a}, **common)
    guard = CloneGuard(narrow)
    scope = ContinuationScope(project="p", thread_id="t", trigger_msg_id="m1")

    # S1 missing: S0 would make A->A look unchanged; the lookup must not use it.
    assert store.baseline_for(scope, narrow) is None
    with pytest.raises(DirtyCloneError):
        guard.check_pushed(baseline=store.baseline_for(scope, narrow))

    # S1 present: B->A is a change, refused.
    store.record(trigger_msg_id="m1", refs={"refs/heads/feature/x": b}, **common)
    with pytest.raises(DirtyCloneError):
        guard.check_pushed(baseline=store.baseline_for(scope, narrow))


def test_11_retention_drops_only_the_oldest_launches(tmp_path: Path) -> None:
    store = TurnRefSnapshotStore(tmp_path / "s")
    for i in range(RETAIN_PER_THREAD + 1):
        store.record(project="p", thread_id="t", trigger_msg_id=f"m{i}", repo_dir=tmp_path, refs={})

    def scope(i: int) -> ContinuationScope:
        return ContinuationScope(project="p", thread_id="t", trigger_msg_id=f"m{i}")

    assert store.baseline_for(scope(0), tmp_path) is None  # dropped: every branch is judged
    assert store.baseline_for(scope(1), tmp_path) == {}
    assert store.baseline_for(scope(RETAIN_PER_THREAD), tmp_path) == {}


def test_snapshot_is_per_repo_dir(tmp_path: Path) -> None:
    store = TurnRefSnapshotStore(tmp_path / "s")
    store.record(project="p", thread_id="t", trigger_msg_id="m1", repo_dir=tmp_path / "a", refs={})
    scope = ContinuationScope(project="p", thread_id="t", trigger_msg_id="m1")
    assert store.baseline_for(scope, tmp_path / "b") is None


def test_unreadable_store_refuses_record_and_judges_all(tmp_path: Path) -> None:
    store = TurnRefSnapshotStore(tmp_path / "s")
    path = store.path_for("p", "t")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    from spirrow_mindwire.turn_ref_snapshot import SnapshotStoreError

    with pytest.raises(SnapshotStoreError):
        store.record(project="p", thread_id="t", trigger_msg_id="m1", repo_dir=tmp_path, refs={})
    scope = ContinuationScope(project="p", thread_id="t", trigger_msg_id="m1")
    assert store.baseline_for(scope, tmp_path) is None


# ------------------------------------------------------------------------- 10. dispatcher


class _Adapter:
    adapter_id = "fake"
    capabilities = frozenset(
        {Capability.READ_THREAD, Capability.POST_REPLY, Capability.EXECUTE_CODE}
    )

    def __init__(self) -> None:
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


def _event(msg_id: str) -> ChatroomEvent:
    return ChatroomEvent(
        event_id=f"T:{msg_id}",
        event_type=EventType.NEW_MESSAGE,
        thread_ref=_thread_ref(),
        occurred_at=_TS,
        payload=NewMessagePayload(msg_id=msg_id, author="Einstein", body="go", parent_msg_id=None),
    )


def _dispatcher(repo: Path, adapter: _Adapter, store: TurnRefSnapshotStore) -> Dispatcher:
    reg = InMemoryAdapterRegistry()
    reg.register(adapter)
    return Dispatcher(
        registry=reg,
        gateway=_Gateway(),
        clone_guard=CloneGuard(repo),
        ref_snapshot_store=store,
    )


@pytest.mark.anyio
async def test_10_snapshot_write_failure_refuses_before_the_adapter(
    narrow: Path, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the store directory should be", encoding="utf-8")
    adapter = _Adapter()
    disp = _dispatcher(narrow, adapter, TurnRefSnapshotStore(blocker))
    handle = await disp.spawn_instance(_thread_ref(), Role.IMPLEMENTER, "implementer-1")
    with pytest.raises(DirtyCloneError) as ei:
        await disp.dispatch(handle, _event("m1"))
    assert ei.value.reason is DirtyCloneReason.GIT_UNAVAILABLE
    assert "ref snapshot not recorded" in ei.value.detail
    assert adapter.delivered == 0

    # Relaunch on the same trigger once the store works: that launch's state is the boundary
    # (the failed launch never ran, so it left no work to absorb).
    store = TurnRefSnapshotStore(tmp_path / "ok")
    disp = _dispatcher(narrow, adapter, store)
    handle = await disp.spawn_instance(_thread_ref(), Role.IMPLEMENTER, "implementer-1")
    await disp.dispatch(handle, _event("m1"))
    assert adapter.delivered == 1
    scope = ContinuationScope(project=_PROJECT, thread_id=_THREAD, trigger_msg_id="m1")
    assert store.baseline_for(scope, narrow) == CloneGuard(narrow).ref_snapshot()


@pytest.mark.anyio
async def test_10_only_implementer_dispatches_record(narrow: Path, tmp_path: Path) -> None:
    store = TurnRefSnapshotStore(tmp_path / "s")
    adapter = _Adapter()
    disp = _dispatcher(narrow, adapter, store)
    handle = await disp.spawn_instance(_thread_ref(), Role.PROPOSER, "proposer-1")
    await disp.dispatch(handle, _event("m1"))
    assert adapter.delivered == 1
    assert not store.path_for(_PROJECT, _THREAD).exists()


@pytest.mark.anyio
async def test_8_relaunch_keeps_the_first_launch_state(narrow: Path, tmp_path: Path) -> None:
    store = TurnRefSnapshotStore(tmp_path / "s")
    adapter = _Adapter()
    disp = _dispatcher(narrow, adapter, store)
    handle = await disp.spawn_instance(_thread_ref(), Role.IMPLEMENTER, "implementer-1")
    await disp.dispatch(handle, _event("m1"))
    first = CloneGuard(narrow).ref_snapshot()
    _feature(narrow, "feature/attempt1", "a1", push=False)  # the first attempt's leftover
    disp2 = _dispatcher(narrow, adapter, store)
    handle2 = await disp2.spawn_instance(_thread_ref(), Role.IMPLEMENTER, "implementer-1")
    await disp2.dispatch(handle2, _event("m1"))  # same trigger, relaunched
    scope = ContinuationScope(project=_PROJECT, thread_id=_THREAD, trigger_msg_id="m1")
    assert store.baseline_for(scope, narrow) == first
    with pytest.raises(DirtyCloneError):
        CloneGuard(narrow).check_pushed(baseline=store.baseline_for(scope, narrow))


# ------------------------------------------------------------------------- conductor wiring


class _Wired:
    """What ``loop_runner._continuation_clone_check`` reads off the dispatcher."""

    def __init__(self, repo: Path, store: TurnRefSnapshotStore) -> None:
        self.clone_guard = CloneGuard(repo)
        self.ref_snapshot_store = store


def _conductor(mcp: _FakeChatroomMcp, disp: _ScriptedDispatcher, wired: _Wired | None) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=10,
        continue_cap=3,
        continuation_clone_check=(
            loop_runner._continuation_clone_check(wired) if wired is not None else None
        ),
    )


def _record(store: TurnRefSnapshotStore, repo: Path, msg_id: str) -> None:
    store.record(
        project=_PROJECT,
        thread_id=_THREAD,
        trigger_msg_id=msg_id,
        repo_dir=repo,
        refs=CloneGuard(repo).ref_snapshot(),
    )


def _relay() -> str:
    return render_relay(n=1, cap=3, continue_body="step 2 of 3", implementer="Heisenberg")


@pytest.mark.anyio
async def test_stale_branch_no_longer_blocks_a_clean_continuation(
    narrow: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Defect 2 (Fermi msg-9285): another thread's months-old branch does not stop this one."""
    _feature(narrow, "stale/august", "aug", push=False)
    store = TurnRefSnapshotStore(tmp_path / "s")
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)  # m1
    _record(store, narrow, "m1")
    _feature(narrow, "feature/mine", "mine", push=True)  # the turn's work, pushed (narrow clone)
    mcp.seed(author="Heisenberg", content=_report())  # m2
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})
    with caplog.at_level(logging.WARNING):
        await _conductor(mcp, disp, _Wired(narrow, store)).run()
    assert disp.dispatches[0][0] is Role.IMPLEMENTER  # the relay was posted and dispatched
    assert "stale/august" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("recovery", [False, True])
async def test_13_unpushed_work_stays_refused_on_report_and_recovery(
    narrow: Path, tmp_path: Path, recovery: bool
) -> None:
    """Einstein msg-9349 Obj-2: launch 10:00, commit 10:01, CONTINUE 10:10, relay left at head.

    The relay-launched turn's snapshot (taken after the commit) must not become the boundary.
    """
    store = TurnRefSnapshotStore(tmp_path / "s")
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)  # m1: the launch
    _record(store, narrow, "m1")
    _feature(narrow, "feature/wip", "wip", push=False)  # the turn's unpushed commit
    mcp.seed(author="Heisenberg", content=_report())  # m2: CONTINUE
    if recovery:
        mcp.seed(author=CONDUCTOR_RELAY_AUTHOR, content=_relay())  # m3, left at the head
        _record(store, narrow, "m3")  # a later launch on the relay saw the commit already
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})
    with pytest.raises(DirtyCloneError) as ei:
        await _conductor(mcp, disp, _Wired(narrow, store)).run()
    assert ei.value.reason is DirtyCloneReason.UNPUSHED
    assert "feature/wip" in ei.value.detail
    assert disp.dispatches == []


def _msgs(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {"msg_id": f"m{i}", "author": a, "content": c, "timestamp": "2026-11-16T00:00:00Z"}
        for i, (a, c) in enumerate(pairs, start=1)
    ]


_NOTICE = ("Fermi", "FYI: sweep notice\n\nNEXT: Bohr")


@pytest.mark.parametrize("recovery", [False, True])
def test_12_14_interleaved_notice_is_skipped_when_identifying_the_trigger(recovery: bool) -> None:
    """The walk back skips messages that hand to no implementer.

    (Today ``chain_count`` refuses such a chain before the clone check runs; the scope still has
    to be right if that rule is ever relaxed.)
    """
    pairs = [("Einstein", _ENTRY), _NOTICE, ("Heisenberg", _report())]
    if recovery:
        pairs.append((CONDUCTOR_RELAY_AUTHOR, _relay()))
    messages = _msgs(*pairs)
    conductor = _conductor(_FakeChatroomMcp(), _ScriptedDispatcher(_FakeChatroomMcp(), {}), None)
    scope = conductor._continuation_scope(messages, 2)
    assert scope.trigger_msg_id == "m1"
    assert scope.between_msg_ids == ("m2",)
    assert (scope.project, scope.thread_id) == (_PROJECT, _THREAD)


def test_trigger_is_the_latest_relay_in_a_chain() -> None:
    messages = _msgs(
        ("Einstein", _ENTRY),
        ("Heisenberg", _report()),
        (CONDUCTOR_RELAY_AUTHOR, _relay()),
        ("Heisenberg", _report("step 3 of 3")),
    )
    conductor = _conductor(_FakeChatroomMcp(), _ScriptedDispatcher(_FakeChatroomMcp(), {}), None)
    scope = conductor._continuation_scope(messages, 3)
    assert scope.trigger_msg_id == "m3"
    assert scope.between_msg_ids == ()


def test_no_implementer_handoff_before_continue_means_no_trigger() -> None:
    messages = _msgs(_NOTICE, ("Heisenberg", _report()))
    conductor = _conductor(_FakeChatroomMcp(), _ScriptedDispatcher(_FakeChatroomMcp(), {}), None)
    scope = conductor._continuation_scope(messages, 1)
    assert scope.trigger_msg_id is None


def test_12_14_interleaved_notice_keeps_the_launch_snapshot(narrow: Path, tmp_path: Path) -> None:
    _feature(narrow, "stale/old", "old", push=False)
    store = TurnRefSnapshotStore(tmp_path / "s")
    _record(store, narrow, "m1")
    scope = ContinuationScope(
        project=_PROJECT, thread_id=_THREAD, trigger_msg_id="m1", between_msg_ids=("m2",)
    )
    check = loop_runner._continuation_clone_check(_Wired(narrow, store))
    assert check is not None
    check(scope)  # stale/old predates the launch: passes


def test_15_snapshot_between_trigger_and_continue_judges_all(
    narrow: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _feature(narrow, "stale/old", "old", push=False)
    store = TurnRefSnapshotStore(tmp_path / "s")
    _record(store, narrow, "m1")
    _record(store, narrow, "m2")  # the dispatcher saw a launch here: identification disagrees
    scope = ContinuationScope(
        project=_PROJECT, thread_id=_THREAD, trigger_msg_id="m1", between_msg_ids=("m2",)
    )
    check = loop_runner._continuation_clone_check(_Wired(narrow, store))
    assert check is not None
    with caplog.at_level(logging.WARNING), pytest.raises(DirtyCloneError) as ei:
        check(scope)
    assert "stale/old" in ei.value.detail
    assert "identification disagrees" in caplog.text


@pytest.mark.anyio
async def test_relay_launched_turn_uses_the_relay_snapshot(narrow: Path, tmp_path: Path) -> None:
    """A second CONTINUE in a chain: its trigger is the relay, not the original entry."""
    store = TurnRefSnapshotStore(tmp_path / "s")
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)  # m1
    _record(store, narrow, "m1")
    _feature(narrow, "feature/step1", "s1", push=False)  # left unpushed by step 1 (hypothetical)
    mcp.seed(author="Heisenberg", content=_report())  # m2
    mcp.seed(author=CONDUCTOR_RELAY_AUTHOR, content=_relay())  # m3
    _record(store, narrow, "m3")
    mcp.seed(author="Heisenberg", content=_report("step 3 of 3"))  # m4
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})
    # feature/step1 predates the m3 launch at the same sha: not this turn's work.
    await _conductor(mcp, disp, _Wired(narrow, store)).run()
    assert disp.dispatches and disp.dispatches[0][0] is Role.IMPLEMENTER
