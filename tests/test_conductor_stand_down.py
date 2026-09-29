"""T44 — fail-closed launch resolution and the loud stand-down.

Thread T-silent-stops-need-a-generic-watchdog-and-a-loud-stand-down (Fermi msg-3098 §4 DoD, Bohr
msg-4569 DoD table). One case per point that can fail to resolve, plus the exit-code rule
"chatroom で言えたら exit 0、言えなかったら非 0":

| point      | thread resolved? | expected                                                     |
|------------|------------------|--------------------------------------------------------------|
| project    | no               | event_log ``conductor.stand_down``, exit 3, nothing posted   |
| thread     | no (08-28 case)  | same                                                         |
| repo_dir   | yes              | notice in target thread, ``NEXT: human``, HUMAN stop, exit 0 |
| identity   | yes              | notice in target thread, ``NEXT: human``, exit 0             |
| any post fails | —            | exit 3 (wrapper quarantine + Discord)                        |
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import _ROSTER, _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire import loop_runner
from spirrow_mindwire.conductor.core import Conductor, ConductorOutcome, StopReason
from spirrow_mindwire.conductor.stand_down import (
    EVENT_KIND_STAND_DOWN,
    STAND_DOWN_EXIT_CODE,
    StandDownError,
    StandDownReason,
    UnresolvedItem,
)
from spirrow_mindwire.config import ConductorConfig, MindwireSettings, Stage3LoopConfig
from spirrow_mindwire.magickit.client import MagickitMcpError, ThreadResolvedError

_STAND_DOWN_LOGGER = "spirrow_mindwire.conductor.stand_down"
_DONE = ConductorOutcome(
    rounds=0, stop_reason=StopReason.SETTLED, last_msg_id=None, forced_naysayer_turns=0
)


def _stand_down_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _STAND_DOWN_LOGGER and r.getMessage().startswith(EVENT_KIND_STAND_DOWN)
    ]


class _ResolutionMcp:
    """Chatroom fake for the launch phase: a thread that exists, or one magickit refuses.

    ``refuse`` reproduces the production envelope observed for a project / thread mismatch
    (``ChatroomNotFoundError: Thread 'T-cond' not found in project '...'``) as the client raises it.
    """

    def __init__(
        self,
        *,
        refuse: bool = False,
        post_error: BaseException | None = None,
        post_msg_id: str = "m-notice",
    ) -> None:
        self._refuse = refuse
        self._post_error = post_error
        self._post_msg_id = post_msg_id
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name == "chatroom_get_thread":
            if self._refuse:
                raise MagickitMcpError(
                    "magickit tool returned an error envelope: error_type='ChatroomNotFoundError' "
                    f"error=\"Thread '{arguments['thread_id']}' not found in project "
                    f"'{arguments['project']}'\""
                )
            return {
                "messages": [{"msg_id": "m1", "author": "Bohr", "content": "x\n\nNEXT: Heisenberg"}]
            }
        if name == "chatroom_post_message":
            if self._post_error is not None:
                raise self._post_error
            return {"msg": {"msg_id": self._post_msg_id}}
        raise AssertionError(f"unexpected tool {name!r}")

    def posts(self) -> list[dict[str, Any]]:
        return [a for n, a in self.calls if n == "chatroom_post_message"]


def _settings(
    *, project: str = "spirrow-mindwire", thread: str = "T-cond", repo_dir: Path | None
) -> MindwireSettings:
    return MindwireSettings(
        loop=Stage3LoopConfig(project=project, repo_dir=repo_dir),
        conductor=ConductorConfig(task_thread_id=thread),
    )


@pytest.fixture
def _nothing_built(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record any attempt to get past resolution — preflight or building/spawning anything."""
    reached: list[str] = []

    def _build(*_a: Any, **_kw: Any) -> Any:
        reached.append("build_conductor")
        raise AssertionError("build_conductor must not run after a stand-down")

    def _pre(_cfg: Any) -> None:
        reached.append("preflight")

    monkeypatch.setattr(loop_runner, "build_conductor", _build)
    monkeypatch.setattr(loop_runner, "_preflight", _pre)
    return reached


# --------------------------------------------------------------------------- #
# thread did not resolve → no chatroom to speak in → exit 3
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kwargs", "refuse", "item", "reason"),
    [
        ({"project": ""}, False, UnresolvedItem.PROJECT, StandDownReason.PROJECT_UNSET),
        ({"thread": " "}, False, UnresolvedItem.THREAD, StandDownReason.THREAD_UNSET),
        # 2026-08-28: task_thread_id and [loop].project disagree → magickit: thread not found.
        (
            {"project": "spirrow-magickit"},
            True,
            UnresolvedItem.THREAD,
            StandDownReason.THREAD_UNREADABLE,
        ),
    ],
    ids=["project unset", "thread unset", "project/thread mismatch"],
)
async def test_unresolved_project_or_thread_stands_down_non_zero_and_posts_nothing(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _nothing_built: list[str],
    kwargs: dict[str, str],
    refuse: bool,
    item: UnresolvedItem,
    reason: StandDownReason,
) -> None:
    caplog.set_level(logging.INFO)
    mcp = _ResolutionMcp(refuse=refuse)
    with pytest.raises(StandDownError) as excinfo:
        await loop_runner.run_conductor(_settings(repo_dir=tmp_path, **kwargs), mcp=mcp)

    assert excinfo.value.code == STAND_DOWN_EXIT_CODE != 0
    assert excinfo.value.event.kind == EVENT_KIND_STAND_DOWN
    assert excinfo.value.event.fields["unresolved"] == item.value
    assert excinfo.value.event.fields["reason"] == reason.value
    # event_log: the reason is on the log line the wrapper's quarantine record captures.
    lines = _stand_down_lines(caplog)
    assert len(lines) == 1
    assert f"unresolved={item.value}" in lines[0] and f"reason={reason.value}" in lines[0]
    if refuse:
        assert "not found in project" in lines[0]  # the magickit refusal text survives
    assert mcp.posts() == []  # no chatroom was resolved, so none is written to
    assert _nothing_built == []  # neither preflight nor any adapter was reached
    # No `conductor stopped:` line: the wrapper takes the non-zero branch, not the verdict parse.
    assert not any("conductor stopped:" in r.getMessage() for r in caplog.records)


def test_stand_down_exits_3_through_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The process exit code is what the wrapper branches on: 3 is neither 0 nor 2, so it lands in
    # the "unknown non-zero → quarantine" branch with no wrapper change (Bohr msg-4569).
    monkeypatch.setattr(
        loop_runner, "load_settings", lambda: _settings(repo_dir=tmp_path, project="")
    )
    monkeypatch.setattr("sys.argv", ["mindwire-loop", "--mode", "conductor"])
    # Hermetic: ``main()`` takes no injected client, so ``run_conductor`` builds the real
    # ``StreamableHttpChatroomMcp`` — which fails fast when MINDWIRE_MAGICKIT_MCP_URL is unset
    # (CI) and would otherwise reach the LIVE magickit on a host where it is set. Unset the
    # variable so local runs match CI, and substitute the fake so no transport is built at all.
    monkeypatch.delenv("MINDWIRE_MAGICKIT_MCP_URL", raising=False)
    mcp = _ResolutionMcp()
    monkeypatch.setattr(loop_runner, "StreamableHttpChatroomMcp", lambda: mcp)
    with pytest.raises(SystemExit) as excinfo:
        loop_runner.main()
    assert excinfo.value.code == STAND_DOWN_EXIT_CODE
    assert mcp.posts() == []  # thread unresolved → nothing posted (Bohr msg-4569)


# --------------------------------------------------------------------------- #
# thread resolved, repo_dir did not → report in the target thread, exit 0
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("repo_dir_kind", "reason"),
    [("unset", StandDownReason.REPO_DIR_UNSET), ("missing", StandDownReason.REPO_DIR_MISSING)],
)
async def test_unresolved_repo_dir_reports_in_target_thread_and_stops_at_human(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    _nothing_built: list[str],
    repo_dir_kind: str,
    reason: StandDownReason,
) -> None:
    caplog.set_level(logging.INFO)
    repo_dir = None if repo_dir_kind == "unset" else tmp_path / "does-not-exist"
    mcp = _ResolutionMcp()
    outcome = await loop_runner.run_conductor(_settings(repo_dir=repo_dir), mcp=mcp)

    assert outcome.stop_reason is StopReason.HUMAN
    assert outcome.rounds == 0
    assert outcome.last_msg_id == "m-notice"  # the sweep records the stop against the notice
    (post,) = mcp.posts()
    assert (post["project"], post["thread_id"]) == ("spirrow-mindwire", "T-cond")  # target thread
    assert "repo_dir" in post["content"] and reason.value in post["content"]
    assert post["content"].rstrip().splitlines()[-1] == "NEXT: human"
    assert _nothing_built == []  # nothing spawned, preflight not run on an unusable repo_dir
    lines = _stand_down_lines(caplog)
    assert len(lines) == 1 and f"reason={reason.value}" in lines[0]
    # Parseable by the wrapper's Get-ConductorVerdict: exit 0 needs a `conductor stopped:` line.
    stops = [r.getMessage() for r in caplog.records if "conductor stopped:" in r.getMessage()]
    assert stops == [
        "conductor stopped: reason=human rounds=0 forced_naysayer=0 "
        "forced_naysayer_saveable=0 last_msg=m-notice"
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mcp",
    [
        _ResolutionMcp(post_error=ThreadResolvedError("thread is resolved")),
        _ResolutionMcp(post_msg_id=""),
    ],
    ids=["thread resolved under us", "post returned no msg_id"],
)
async def test_repo_dir_notice_that_does_not_land_exits_non_zero(
    tmp_path: Path, _nothing_built: list[str], mcp: _ResolutionMcp
) -> None:
    with pytest.raises(StandDownError) as excinfo:
        await loop_runner.run_conductor(_settings(repo_dir=None), mcp=mcp)
    assert excinfo.value.code == STAND_DOWN_EXIT_CODE
    assert excinfo.value.event.fields["unresolved"] == UnresolvedItem.REPO_DIR.value


@pytest.mark.anyio
async def test_all_points_resolved_proceeds_to_preflight_and_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reached: list[str] = []

    class _C:
        async def run(self) -> Any:
            reached.append("run")
            return _DONE

        async def aclose(self) -> None:
            return None

    def _build(_s: Any, **kw: Any) -> Any:
        reached.append("build")
        assert kw["mcp"] is mcp  # one transport for resolution and the run
        return _C()

    monkeypatch.setattr(loop_runner, "_preflight", lambda _cfg: reached.append("preflight"))
    monkeypatch.setattr(loop_runner, "build_conductor", _build)
    mcp = _ResolutionMcp()
    assert await loop_runner.run_conductor(_settings(repo_dir=tmp_path), mcp=mcp) is _DONE
    assert reached == ["preflight", "build", "run"]
    assert mcp.posts() == []


@pytest.mark.anyio
async def test_transport_failure_is_not_a_thread_stand_down(tmp_path: Path) -> None:
    # Magickit being unreachable says nothing about this thread; it keeps the existing exit-1 path.
    class _Down:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            raise ConnectionError("magickit down")

    with pytest.raises(ConnectionError):
        await loop_runner.run_conductor(_settings(repo_dir=tmp_path), mcp=_Down())


# --------------------------------------------------------------------------- #
# identity did not resolve (decided in _route) → report in the target thread
# --------------------------------------------------------------------------- #


def _conductor(mcp: _FakeChatroomMcp, dispatcher: _ScriptedDispatcher) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=dispatcher,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("next_line", "reason"),
    [
        ("NEXT: Bohrr", StandDownReason.IDENTITY_UNRESOLVED),  # Einstein msg-4548's typo case
        ("NEXT: Fermi", StandDownReason.IDENTITY_NOT_SPAWNABLE),  # ADR-2026-09-14-21 web identity
    ],
    ids=["typo", "not spawnable"],
)
async def test_unresolved_identity_reports_in_target_thread_not_elsewhere(
    caplog: pytest.LogCaptureFixture, next_line: str, reason: StandDownReason
) -> None:
    caplog.set_level(logging.INFO)
    mcp = _FakeChatroomMcp()
    # Authored by the naysayer so the ABSENT turn is not first sent to a forced consult (that
    # routing is unchanged by T44); the stop is the identity failure itself.
    mcp.seed(author="Einstein", content=f"critique\n\n{next_line}")
    dispatcher = _ScriptedDispatcher(mcp, {})
    outcome = await _conductor(mcp, dispatcher).run()

    assert dispatcher.spawns == []
    (post,) = mcp.posts
    assert (post["project"], post["thread_id"]) == ("spirrow-mindwire", "T-cond")
    assert post["content"].rstrip().splitlines()[-1] == "NEXT: human"
    assert outcome.last_msg_id == "m2"  # the stop is recorded against the notice
    lines = _stand_down_lines(caplog)
    assert len(lines) == 1
    assert "unresolved=identity" in lines[0] and f"reason={reason.value}" in lines[0]
    assert "thread=T-cond" in lines[0]


@pytest.mark.anyio
async def test_head_with_no_next_line_is_not_an_identity_stand_down(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Nothing was named, so no identity failed to resolve: NO_HANDOFF keeps its old behaviour.
    caplog.set_level(logging.INFO)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content="critique with no handoff line")
    outcome = await _conductor(mcp, _ScriptedDispatcher(mcp, {})).run()
    assert outcome.stop_reason is StopReason.NO_HANDOFF
    assert mcp.posts == []
    assert _stand_down_lines(caplog) == []


@pytest.mark.anyio
async def test_identity_notice_that_does_not_land_exits_non_zero() -> None:
    class _ResolvedOnPost(_FakeChatroomMcp):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            if name == "chatroom_post_message":
                raise ThreadResolvedError("thread is resolved")
            return await super().call_tool(name, arguments)

    mcp = _ResolvedOnPost()
    mcp.seed(author="Einstein", content="critique\n\nNEXT: Bohrr")
    with pytest.raises(StandDownError) as excinfo:
        await _conductor(mcp, _ScriptedDispatcher(mcp, {})).run()
    assert excinfo.value.code == STAND_DOWN_EXIT_CODE
    assert excinfo.value.event.fields["reason"] == StandDownReason.IDENTITY_UNRESOLVED.value
