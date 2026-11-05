"""Implementer self-continuation — ``CONTINUE:`` (T-implementer-self-continuation).

Design: Bohr msg-7879 / msg-7881 / msg-7883 / msg-7885 / msg-7887. Numbered tests follow the
"テストに追加する項目" lists: 1-4 (msg-7881), 5 (msg-7883, dirty fallback), 6 (msg-7883 / msg-7885 /
msg-7887, format variance).
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire.clone_guard import CloneGuard, DirtyCloneError, DirtyCloneReason
from spirrow_mindwire.conductor.continuation import (
    EMPTY_BODY_PLACEHOLDER,
    Link,
    chain_count,
    find_continue_declaration,
    relay_chain_count,
    render_relay,
)
from spirrow_mindwire.conductor.core import CONDUCTOR_RELAY_AUTHOR, Conductor, StopReason
from spirrow_mindwire.conductor.handoff import parse_next_token
from spirrow_mindwire.routing import ContinueVerdict, continuation_admissible
from spirrow_mindwire.value_objects import Role

_ROSTER: Mapping[str, Role] = {
    "Bohr": Role.PROPOSER,
    "Heisenberg": Role.IMPLEMENTER,
    "Einstein": Role.NAYSAYER,
}

_ENTRY = "設計 OK\n\nTIER-C-CHECK: none\nNEXT: Heisenberg"


def _report(body: str = "step 2 of 3") -> str:
    return f"step 1 done, pushed\n\nCONTINUE: {body}\nNEXT: Heisenberg"


def _ok() -> None:
    return None


def _conductor(
    mcp: _FakeChatroomMcp,
    disp: _ScriptedDispatcher,
    *,
    clone_check: Callable[[], None] | None = _ok,
    cap: int = 3,
) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        max_rounds=10,
        continue_cap=cap,
        continuation_clone_check=clone_check,
    )


def _relays(mcp: _FakeChatroomMcp) -> list[dict[str, Any]]:
    return [p for p in mcp.posts if p.get("author") == CONDUCTOR_RELAY_AUTHOR]


# ------------------------------------------------------------------ 1. relay routes to implementer


@pytest.mark.anyio
async def test_1_relay_ends_on_next_implementer_and_dispatches_it() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report("write tests"))
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["all done\n\nNEXT: Bohr"]})

    await _conductor(mcp, disp).run()

    relays = _relays(mcp)
    assert len(relays) == 1
    body = relays[0]["content"]
    assert body.splitlines()[0] == "[continue 1/3] write tests"
    assert body.splitlines()[-1] == "NEXT: Heisenberg"
    assert parse_next_token(body) == "Heisenberg"
    # The implementer is dispatched on the relay (the adapter's self-filter never sees its own
    # report as the trigger).
    assert disp.dispatches[0] == (Role.IMPLEMENTER, "m3")


@pytest.mark.anyio
async def test_1b_relay_head_passes_guard_i_by_carve_out_iv() -> None:
    """A relay left at the head (the run ended after posting it) relaunches the implementer."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    mcp.seed(
        author=CONDUCTOR_RELAY_AUTHOR,
        content=render_relay(n=1, cap=3, continue_body="step 2 of 3", implementer="Heisenberg"),
    )
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})

    await _conductor(mcp, disp).run()

    assert disp.dispatches[0] == (Role.IMPLEMENTER, "m3")


@pytest.mark.anyio
async def test_1c_forged_relay_without_chain_is_still_guarded() -> None:
    """A relay-looking post with no entry above it is not carve-out ④: guard (i) applies."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="go\n\nNEXT: Heisenberg")
    mcp.seed(
        author=CONDUCTOR_RELAY_AUTHOR,
        content=render_relay(n=1, cap=3, continue_body="x", implementer="Heisenberg"),
    )
    disp = _ScriptedDispatcher(mcp, {})

    await _conductor(mcp, disp).run()

    assert (Role.IMPLEMENTER, "m2") not in disp.dispatches


# ------------------------------------------------------------------ 2. interrupt breaks the chain


@pytest.mark.anyio
@pytest.mark.parametrize("interrupter", ["Bohr", "Einstein", "human"])
async def test_2_interrupt_in_chain_falls_back_to_self_handoff(interrupter: str) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    mcp.seed(
        author=CONDUCTOR_RELAY_AUTHOR,
        content=render_relay(n=1, cap=3, continue_body="step 2 of 3", implementer="Heisenberg"),
    )
    mcp.seed(author=interrupter, content="ちょっと待って")
    mcp.seed(author="Heisenberg", content=_report("step 3"))
    disp = _ScriptedDispatcher(mcp, {})

    outcome = await _conductor(mcp, disp).run()

    assert outcome.stop_reason is StopReason.SELF_HANDOFF
    assert _relays(mcp) == []
    assert disp.dispatches == []


# ------------------------------------------------------------------ 3. cap


@pytest.mark.anyio
async def test_3_cap_plus_one_posts_continue_cap_to_proposer() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    for n in (1, 2, 3):
        mcp.seed(author="Heisenberg", content=_report(f"part {n + 1}"))
        mcp.seed(
            author=CONDUCTOR_RELAY_AUTHOR,
            content=render_relay(
                n=n, cap=3, continue_body=f"part {n + 1}", implementer="Heisenberg"
            ),
        )
    mcp.seed(author="Heisenberg", content=_report("part 5"))
    disp = _ScriptedDispatcher(mcp, {})

    await _conductor(mcp, disp).run()

    relays = _relays(mcp)
    assert len(relays) == 1
    assert "continue_cap (4/3)" in relays[0]["content"]
    assert relays[0]["content"].splitlines()[-1] == "NEXT: Bohr"
    assert (Role.IMPLEMENTER, "m9") not in disp.dispatches
    assert all(role is not Role.IMPLEMENTER for role, _ in disp.dispatches)


def test_3b_n_is_counted_from_the_chain_not_the_report() -> None:
    r, c, e = Link.CONTINUE_REPORT, Link.CONTINUE_RELAY, Link.ENTRY
    assert chain_count([e, r]) == 1
    assert chain_count([e, r, c, r]) == 2
    assert chain_count([e, r, c, r, c, r, c, r]) == 4
    assert chain_count([r]) is None  # no entry
    assert chain_count([e, r, Link.OTHER, r]) is None
    assert chain_count([e, r, r]) is None  # a second report with no relay between
    assert relay_chain_count([e, r, c]) == 1
    assert relay_chain_count([c]) is None
    assert (
        continuation_admissible(
            head_author_is_implementer=True, chain_n=4, cap=3, clone_is_clean=lambda: True
        )
        is ContinueVerdict.CAP
    )
    assert (
        continuation_admissible(
            head_author_is_implementer=True, chain_n=4, cap=3, clone_is_clean=lambda: False
        )
        is ContinueVerdict.DIRTY
    )


# ------------------------------------------------------------------ 4. no CONTINUE line


@pytest.mark.anyio
async def test_4_implementer_self_handoff_without_continue_stops() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content="途中です\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(mcp, {})

    outcome = await _conductor(mcp, disp).run()

    assert outcome.stop_reason is StopReason.SELF_HANDOFF
    assert disp.spawns == []


@pytest.mark.anyio
async def test_4b_non_implementer_continue_is_still_self_handoff() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="CONTINUE: more design\nNEXT: Bohr")
    disp = _ScriptedDispatcher(mcp, {})

    outcome = await _conductor(mcp, disp).run()

    assert outcome.stop_reason is StopReason.SELF_HANDOFF
    assert _relays(mcp) == []


# ------------------------------------------------------------------ 5. dirty fallback


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def pushed_clone(tmp_path: Path) -> Path:
    """A clone of a bare origin, on main, with a feature branch pushed and tracking."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    r = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(r))
    for k, v in (
        ("user.email", "t@example.invalid"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
    ):
        _git(r, "config", k, v)
    (r / ".gitignore").write_text(".mindwire/\n", encoding="utf-8")
    (r / "a.txt").write_text("a\n", encoding="utf-8")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    _git(r, "push", "-q", "-u", "origin", "main")
    _git(r, "remote", "set-head", "origin", "main")
    _git(r, "switch", "-q", "-c", "feature/x")
    _git(r, "push", "-q", "-u", "origin", "feature/x")
    _git(r, "switch", "-q", "main")
    return r


def _check(repo: Path) -> Callable[[], None]:
    guard = CloneGuard(repo)

    def check() -> None:
        guard.check()
        guard.check_pushed()

    return check


def test_5_clean_pushed_clone_passes(pushed_clone: Path) -> None:
    _check(pushed_clone)()


def test_5_local_branch_without_upstream_but_fully_pushed_passes(pushed_clone: Path) -> None:
    """A branch with no upstream whose commits a remote already reaches is not unpushed."""
    _git(pushed_clone, "branch", "--no-track", "scratch-view", "origin/feature/x")
    _check(pushed_clone)()


@pytest.mark.anyio
async def test_5c_dirty_clone_wins_over_cap(pushed_clone: Path) -> None:
    """PR #519 gate Obj-1: over the cap with unpushed work -> DIRTY, never a hand-off to the
    proposer that cannot fetch the work."""
    _make_dirty(pushed_clone, "unpushed")
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    for n in (1, 2, 3):
        mcp.seed(author="Heisenberg", content=_report(f"part {n + 1}"))
        mcp.seed(
            author=CONDUCTOR_RELAY_AUTHOR,
            content=render_relay(
                n=n, cap=3, continue_body=f"part {n + 1}", implementer="Heisenberg"
            ),
        )
    mcp.seed(author="Heisenberg", content=_report("part 5"))
    disp = _ScriptedDispatcher(mcp, {})

    with pytest.raises(DirtyCloneError) as ei:
        await _conductor(mcp, disp, clone_check=_check(pushed_clone)).run()

    assert ei.value.reason is DirtyCloneReason.UNPUSHED
    assert _relays(mcp) == []  # no continue_cap notice posted
    assert disp.dispatches == []


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["unpushed", "unpushed_no_upstream", "uncommitted"])
async def test_5d_recovered_relay_head_rechecks_clone(pushed_clone: Path, state: str) -> None:
    """PR #519 gate round 3 Obj-1: a relay left at the head after a restart is re-admitted only
    on a clean, pushed clone -- condition 4 does not depend on which message the run resumes from.
    """
    expected = _make_dirty(pushed_clone, state)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    mcp.seed(
        author=CONDUCTOR_RELAY_AUTHOR,
        content=render_relay(n=1, cap=3, continue_body="step 2 of 3", implementer="Heisenberg"),
    )
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})

    with pytest.raises(DirtyCloneError) as ei:
        await _conductor(mcp, disp, clone_check=_check(pushed_clone)).run()

    assert ei.value.reason is expected
    assert disp.dispatches == []
    assert mcp.posts == []


@pytest.mark.anyio
async def test_5e_recovered_relay_head_on_clean_clone_dispatches(pushed_clone: Path) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    mcp.seed(
        author=CONDUCTOR_RELAY_AUTHOR,
        content=render_relay(n=1, cap=3, continue_body="step 2 of 3", implementer="Heisenberg"),
    )
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})

    await _conductor(mcp, disp, clone_check=_check(pushed_clone)).run()

    assert disp.dispatches[0] == (Role.IMPLEMENTER, "m3")


@pytest.mark.anyio
async def test_5f_recovered_relay_head_unwired_check_is_not_carve_out_iv() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    mcp.seed(
        author=CONDUCTOR_RELAY_AUTHOR,
        content=render_relay(n=1, cap=3, continue_body="step 2 of 3", implementer="Heisenberg"),
    )
    disp = _ScriptedDispatcher(mcp, {})

    await _conductor(mcp, disp, clone_check=None).run()

    assert (Role.IMPLEMENTER, "m3") not in disp.dispatches


def _make_dirty(repo: Path, state: str) -> DirtyCloneReason:
    if state == "uncommitted":
        (repo / "a.txt").write_text("changed\n", encoding="utf-8")
        return DirtyCloneReason.DIRTY_TREE
    if state == "untracked":
        (repo / "new.txt").write_text("n\n", encoding="utf-8")
        return DirtyCloneReason.DIRTY_TREE
    if state == "unpushed_no_upstream":
        # PR #519 gate round 2: a brand-new local branch, never pushed, so it has no
        # upstream and its `%(upstream:track)` is empty -- the rev-list fallback must catch it.
        _git(repo, "switch", "-q", "-c", "feature/y")
        (repo / "c.txt").write_text("c\n", encoding="utf-8")
        _git(repo, "add", "c.txt")
        _git(repo, "commit", "-q", "-m", "local only, no upstream")
        _git(repo, "switch", "-q", "main")
        upstream = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "feature/y@{upstream}"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert upstream.returncode != 0, "feature/y must have no upstream for this case"
        return DirtyCloneReason.UNPUSHED
    if state != "unpushed":
        raise AssertionError(f"unknown dirty state {state!r}")
    _git(repo, "switch", "-q", "feature/x")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "local only")
    _git(repo, "switch", "-q", "main")
    return DirtyCloneReason.UNPUSHED


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["uncommitted", "untracked", "unpushed", "unpushed_no_upstream"])
async def test_5_dirty_clone_refuses_continuation(
    pushed_clone: Path, state: str, caplog: pytest.LogCaptureFixture
) -> None:
    expected = _make_dirty(pushed_clone, state)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["x\n\nNEXT: Bohr"]})

    with caplog.at_level(logging.INFO), pytest.raises(DirtyCloneError) as ei:
        await _conductor(mcp, disp, clone_check=_check(pushed_clone)).run()

    assert ei.value.reason is expected
    if state == "unpushed_no_upstream":
        # Proves the no-upstream rev-list fallback fired, not the tracked `ahead` branch.
        assert "feature/y (no upstream)" in ei.value.detail
    assert _relays(mcp) == []
    assert disp.dispatches == []
    assert "continue invalidated: reason=dirty" in caplog.text
    assert "verdict=dirty" in caplog.text


@pytest.mark.anyio
async def test_5b_unwired_clone_check_is_fail_closed() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    disp = _ScriptedDispatcher(mcp, {})

    outcome = await _conductor(mcp, disp, clone_check=None).run()

    assert outcome.stop_reason is StopReason.SELF_HANDOFF
    assert _relays(mcp) == []


# ------------------------------------------------------------------ 6. format variance


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # simplest: CONTINUE directly above NEXT (walk starts at i_next - 1, msg-7887)
        ("work\nCONTINUE: next step\nNEXT: Heisenberg", "next step"),
        # NEXT followed by trailing blank lines
        ("work\nCONTINUE: next step\nNEXT: Heisenberg\n\n\n", "next step"),
        # blank line(s) between
        ("work\nCONTINUE: a\n\n\nNEXT: Heisenberg", "a"),
        # TIER-C-CHECK between
        ("work\nCONTINUE: a\nTIER-C-CHECK: none\nNEXT: Heisenberg", "a"),
        # CONTINUE -> blank -> TIER-C-CHECK -> NEXT (msg-7885)
        ("work\nCONTINUE: a\n\nTIER-C-CHECK: none\nNEXT: Heisenberg", "a"),
        # leading whitespace
        ("work\n   CONTINUE:   a  \nNEXT: Heisenberg", "a"),
        # empty right-hand side still continues
        ("work\nCONTINUE:\nNEXT: Heisenberg", ""),
        # two CONTINUE lines: the one nearest NEXT wins
        ("work\nCONTINUE: far\nCONTINUE: near\nNEXT: Heisenberg", "near"),
        # CONTINUE in prose, outside the trailing block
        ("CONTINUE: in prose\nmore prose\nNEXT: Heisenberg", None),
        # CONTINUE inside a code fence just above the block
        ("example:\n```\nCONTINUE: example\n```\nNEXT: Heisenberg", None),
        # unclosed fence: CONTINUE and NEXT are example text inside it (PR #519 gate round 3)
        ("Example:\n```\nCONTINUE: example only\nNEXT: Heisenberg", None),
        # same with a ~~~ fence and a blank line
        ("Example:\n~~~text\nCONTINUE: example only\n\nNEXT: Heisenberg", None),
        # a closed fence earlier in the report does not hide a real declaration
        ("ex:\n```\nCONTINUE: x\n```\ndone\nCONTINUE: real\nNEXT: Heisenberg", "real"),
        # PR #519 gate round 4: a ~~~ line inside an open ``` fence is content, not a close
        ("Example:\n```text\n~~~\nCONTINUE: example only\nNEXT: Heisenberg", None),
        # ... and a ``` line inside an open ~~~ fence likewise
        ("Example:\n~~~\n```\nCONTINUE: example only\nNEXT: Heisenberg", None),
        # a shorter ``` run inside an open ```` fence does not close it
        ("Example:\n````md\n```\nCONTINUE: example only\nNEXT: Heisenberg", None),
        # a ```` fence holding a lone ``` line, closed by ````: the real declaration after counts
        ("ex:\n````md\n```\nCONTINUE: x\n````\ndone\nCONTINUE: real\nNEXT: Heisenberg", "real"),
        # a longer closing run closes a shorter opening fence
        ("ex:\n```\nCONTINUE: x\n`````\ndone\nCONTINUE: real\nNEXT: Heisenberg", "real"),
        # a ``` line with an info string cannot close a fence
        ("Example:\n```\n```text\nCONTINUE: example only\nNEXT: Heisenberg", None),
        # NEXT is the first line: empty walk
        ("NEXT: Heisenberg", None),
        # no NEXT at the end
        ("CONTINUE: a\nNEXT: Heisenberg\nafterword", None),
    ],
)
def test_6_find_continue_declaration(body: str, expected: str | None) -> None:
    assert find_continue_declaration(body) == expected


@pytest.mark.anyio
async def test_6b_empty_body_and_nearest_line_reach_the_relay() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(
        author="Heisenberg",
        content="pushed\n\nCONTINUE: far\nCONTINUE:\n\nTIER-C-CHECK: none\nNEXT: Heisenberg",
    )
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})

    await _conductor(mcp, disp).run()

    relays = _relays(mcp)
    assert relays[0]["content"].splitlines()[0] == f"[continue 1/3] {EMPTY_BODY_PLACEHOLDER}"


@pytest.mark.anyio
async def test_6c_fenced_continue_is_self_handoff() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(
        author="Heisenberg",
        content="例:\n```\nCONTINUE: example\nNEXT: Heisenberg\n```\nNEXT: Heisenberg",
    )
    disp = _ScriptedDispatcher(mcp, {})

    outcome = await _conductor(mcp, disp).run()

    assert outcome.stop_reason is StopReason.SELF_HANDOFF
    assert _relays(mcp) == []


@pytest.mark.anyio
async def test_6d_unclosed_fence_continue_is_self_handoff() -> None:
    """PR #519 gate round 3 Obj-2: the fence is never closed, so the trailing CONTINUE / NEXT are
    example text; no relay, ordinary SELF_HANDOFF."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(
        author="Heisenberg",
        content="Example:\n```\nCONTINUE: example only\nNEXT: Heisenberg",
    )
    disp = _ScriptedDispatcher(mcp, {})

    outcome = await _conductor(mcp, disp).run()

    assert outcome.stop_reason is StopReason.SELF_HANDOFF
    assert _relays(mcp) == []
