"""T-merged-pr-fact-never-reaches-design-thread — design v0.4 (Bohr msg-7560, Einstein endorse).

Three pieces, one test group each:

(a) :func:`lifecycle_post` — 1b's rows 3 / 4 as one pure function, shared by 1b and the conductor.
(b) the conductor's settled-PR check — a ``NEXT: pr-review`` on a merged / closed PR posts the fact
    under ``pr-event-relay`` BEFORE admission (so before R0-OVERRIDE) and never fires the gate.
(c) the R6 note — an already-reviewed head no longer stops in silence: one admission-headed note
    moves the head, so the next tick does not spin on the same R6 (magickit#111), and 1b can still
    report a later merge from it.

Plus the property that keeps (b) and 1b from double-managing one tail: the two callers of
:func:`lifecycle_post` take disjoint tails, because no ``pr-gate-relay`` post ends ``pr-review``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from test_conductor_core import (
    _GREEN,
    _HEAD,
    _conductor,
    _FakeChatroomMcp,
    _rollup,
    _ScriptedDispatcher,
    _ScriptedPrGate,
    _ScriptedRollupSource,
    _thread_ref,
)

from spirrow_mindwire.conductor.core import Conductor, StopReason
from spirrow_mindwire.conductor.gate_records import (
    MERGE_WAIT_LINE,
    ci_route_heads,
    render_relay_heading,
    verdict_heads,
)
from spirrow_mindwire.conductor.handoff import parse_next_token
from spirrow_mindwire.conductor.head_skip import stage1_skips
from spirrow_mindwire.conductor.parked_lane import ParkedLane, classify_parked
from spirrow_mindwire.gate_admission import AdmissionResult, GateAdmission
from spirrow_mindwire.github.client import (
    CheckRollup,
    CiStatus,
    PrRef,
    PrResolution,
    PrState,
    ReviewEvent,
    parse_pr_ref,
)
from spirrow_mindwire.pr_event_advance.decide import (
    PR_EVENT_RELAY_AUTHOR,
    PrFacts,
    PrLifecycle,
    decide,
    heading_ref,
    lifecycle_post,
    pr_facts_of,
    read_relay_tail,
)
from spirrow_mindwire.pr_event_advance.runner import advance_thread

_REF = "acme/widgets#7"
_THREAD = _thread_ref().thread_id  # "T-cond"
_CLOSED_AT = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
_MERGE_SHA = "9f1c2d3e4b5a69788796a5b4c3d2e1f0a9b8c7d6"


def _ref() -> PrRef:
    ref = parse_pr_ref(_REF)
    assert ref is not None
    return ref


def _state(resolution: PrResolution, *, merged: bool = False, body: str = "") -> PrState:
    return PrState(
        ref=_ref(),
        resolution=resolution,
        closed_at=_CLOSED_AT if resolution is PrResolution.CLOSED else None,
        merged=merged,
        head_sha=_HEAD,
        merge_commit_sha=_MERGE_SHA if merged else None,
        body=body,
    )


_MERGED = _state(PrResolution.CLOSED, merged=True)
_CLOSED = _state(PrResolution.CLOSED, merged=False)
_OPEN = _state(PrResolution.OPEN)


class _StateSource:
    """A fake ``PrStateSource`` (and 1b ``PrSource``): one PR state, or a raise."""

    def __init__(self, state: PrState | Exception) -> None:
        self._state = state
        self.asked: list[str] = []

    async def fetch_pr_state(self, pr: PrRef) -> PrState:
        self.asked.append(pr.slug)
        if isinstance(self._state, Exception):
            raise self._state
        return self._state

    async def fetch_ci_status(self, pr: PrRef) -> CiStatus:  # 1b only; never reached here
        raise AssertionError("CI is not read on rows 3 / 4 / 5")


def _settled_conductor(
    mcp: _FakeChatroomMcp,
    *,
    state: _StateSource | None,
    gate: _ScriptedPrGate,
    rollup: _ScriptedRollupSource | None = None,
) -> Conductor:
    disp = _ScriptedDispatcher(mcp, {})
    base = _conductor(mcp, disp, orchestrator=gate, rollup_source=rollup)
    base._pr_state_source = state  # the one ctor arg _conductor() does not take
    return base


def _facts(state: PrState) -> PrFacts:
    facts = pr_facts_of(state)
    assert facts is not None
    return facts


# --------------------------------------------------------------------------- #
# (a) lifecycle_post — msg-7560 v0.4 (a), test list from v0.1 §4.
# --------------------------------------------------------------------------- #


def test_merged_with_closes_thread_naming_this_thread_settles() -> None:
    pr = _facts(_state(PrResolution.CLOSED, merged=True, body=f"x\nCloses-thread: {_THREAD}\n"))
    post = lifecycle_post(thread_id=_THREAD, pr_ref=_REF, pr=pr, proposer="Bohr")
    assert post is not None
    assert post.next_line == "NEXT: none"
    assert _MERGE_SHA in post.body


@pytest.mark.parametrize("body", ["", "Closes-thread: T-some-other-thread\n"])
def test_merged_without_closes_thread_for_this_thread_goes_to_the_proposer(body: str) -> None:
    pr = _facts(_state(PrResolution.CLOSED, merged=True, body=body))
    post = lifecycle_post(thread_id=_THREAD, pr_ref=_REF, pr=pr, proposer="Bohr")
    assert post is not None
    assert post.next_line == "NEXT: Bohr"


def test_closed_unmerged_always_goes_to_the_proposer_even_with_closes_thread() -> None:
    pr = _facts(_state(PrResolution.CLOSED, body=f"Closes-thread: {_THREAD}\n"))
    post = lifecycle_post(thread_id=_THREAD, pr_ref=_REF, pr=pr, proposer="Bohr")
    assert post is not None
    assert post.next_line == "NEXT: Bohr"
    assert "close された" in post.body


def test_unresolved_proposer_becomes_the_three_line_operator_form() -> None:
    post = lifecycle_post(thread_id=_THREAD, pr_ref=_REF, pr=_facts(_MERGED), proposer=None)
    assert post is not None
    lines = post.next_line.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("OPERATOR-TASK: ")
    assert lines[1] == "TIER-C-CHECK: none"
    assert lines[2] == "NEXT: operator"


@pytest.mark.parametrize(
    "pr",
    [_facts(_OPEN), _facts(_state(PrResolution.NOT_FOUND)), PrFacts(state="weird")],
    ids=["open", "not-found", "unknown"],
)
def test_open_or_unknown_pr_has_no_post(pr: PrFacts) -> None:
    assert lifecycle_post(thread_id=_THREAD, pr_ref=_REF, pr=pr, proposer="Bohr") is None


@pytest.mark.parametrize("state", [_MERGED, _CLOSED], ids=["merged", "closed"])
@pytest.mark.parametrize("proposer", ["Bohr", None])
def test_decide_rows_3_and_4_are_exactly_lifecycle_post(
    state: PrState, proposer: str | None
) -> None:
    # The extraction changed no byte of 1b's output: decide() IS lifecycle_post on rows 3 / 4.
    tail = read_relay_tail(
        "pr-gate-relay", f"{render_relay_heading(_REF, _HEAD)}\n\nVERDICT: approve\n\nNEXT: human"
    )
    pr = _facts(state)
    got = decide(thread_id=_THREAD, tail=tail, pr=pr, ci=None, proposer=proposer)
    assert got == lifecycle_post(thread_id=_THREAD, pr_ref=_REF, pr=pr, proposer=proposer)


def test_pr_facts_of_is_still_reexported_by_the_runner() -> None:
    from spirrow_mindwire.pr_event_advance import runner

    assert runner.pr_facts_of is pr_facts_of
    assert _facts(_MERGED).state == PrLifecycle.MERGED.value


# --------------------------------------------------------------------------- #
# (b) the conductor's settled-PR check — msg-7560 v0.4 (b).
# --------------------------------------------------------------------------- #


def _seed_reviewed_then_renominated(mcp: _FakeChatroomMcp, *, author: str) -> None:
    """A verdict already on _HEAD, then a fresh ``NEXT: pr-review`` — the magickit#111 shape."""
    mcp.seed(author="Heisenberg", content=f"opened\n\nNEXT: pr-review {_REF}")
    mcp.seed(
        author="pr-gate-relay",
        content=(
            f"{render_relay_heading(_REF, _HEAD)}\n\n"
            "VERDICT: comment (ci=success)\n\ncritique\n\nNEXT: human"
        ),
    )
    mcp.seed(author=author, content=f"advisory declined\n\nNEXT: pr-review {_REF}")


@pytest.mark.anyio
@pytest.mark.parametrize("author", ["Heisenberg", "human"], ids=["role", "human-R0"])
@pytest.mark.parametrize("reviewed", [True, False], ids=["R6-head", "new-head"])
@pytest.mark.parametrize("state", [_MERGED, _CLOSED], ids=["merged", "closed"])
async def test_settled_pr_posts_the_fact_once_and_never_reaches_admission(
    author: str, reviewed: bool, state: PrState
) -> None:
    mcp = _FakeChatroomMcp()
    if reviewed:
        _seed_reviewed_then_renominated(mcp, author=author)
    else:
        mcp.seed(author=author, content=f"pushed\n\nNEXT: pr-review {_REF}")
    gate = _ScriptedPrGate(mcp, ReviewEvent.APPROVE)
    rollup = _ScriptedRollupSource(_rollup(*_GREEN))
    source = _StateSource(state)
    outcome = await _settled_conductor(mcp, state=source, gate=gate, rollup=rollup).run()

    assert gate.fired == []  # the naysayer was not called
    assert rollup.asked == []  # admission (R0-R7) was never reached
    assert source.asked == [_REF]
    assert outcome.stop_reason is StopReason.PR_SETTLED
    assert len(mcp.posts) == 1
    posted = mcp.posts[0]
    assert posted["author"] == PR_EVENT_RELAY_AUTHOR
    expected = lifecycle_post(thread_id=_THREAD, pr_ref=_REF, pr=_facts(state), proposer="Bohr")
    assert expected is not None
    assert posted["content"] == expected.render()
    assert outcome.last_msg_id == mcp._messages[-1]["msg_id"]


@pytest.mark.anyio
async def test_the_tick_after_a_settled_post_does_not_come_back_to_the_gate() -> None:
    # Closes-thread names this thread → NEXT: none. The second run reads that head and stops
    # SETTLED: no second post, no state read, no gate.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content=f"pushed\n\nNEXT: pr-review {_REF}")
    gate = _ScriptedPrGate(mcp, ReviewEvent.APPROVE)
    merged = _state(PrResolution.CLOSED, merged=True, body=f"Closes-thread: {_THREAD}\n")
    first = await _settled_conductor(mcp, state=_StateSource(merged), gate=gate).run()
    assert first.stop_reason is StopReason.PR_SETTLED
    again = _StateSource(merged)
    second = await _settled_conductor(mcp, state=again, gate=gate).run()
    assert second.stop_reason is StopReason.SETTLED
    assert again.asked == []
    assert len(mcp.posts) == 1
    assert gate.fired == []


@pytest.mark.anyio
async def test_a_settled_post_handing_to_the_proposer_is_not_read_by_1b() -> None:
    # The proposer-bound post is a pr-event-relay tail: its NEXT is the proposer, not pr-review,
    # so the conductor routes it as a role hand-off, and 1b's row 1 rejects it (no second post).
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content=f"pushed\n\nNEXT: pr-review {_REF}")
    gate = _ScriptedPrGate(mcp, ReviewEvent.APPROVE)
    await _settled_conductor(mcp, state=_StateSource(_MERGED), gate=gate).run()
    tail = mcp._messages[-1]
    assert parse_next_token(tail["content"]) == "Bohr"
    outcome = await advance_thread(
        mcp=mcp, gh=_StateSource(_MERGED), project="p", thread_id=_THREAD, proposer="Bohr"
    )
    assert (outcome.action, outcome.reason) == ("noop", "not-relay-tail")
    assert len(mcp.posts) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state",
    [
        RuntimeError("network down"),
        _state(PrResolution.UNRESOLVABLE),
        _OPEN,
        _state(PrResolution.NOT_FOUND),
    ],
    ids=["raises", "unresolvable", "open", "not-found"],
)
async def test_unread_or_open_pr_keeps_the_pre_change_path(state: PrState | Exception) -> None:
    # Fail-closed: nothing is guessed. On green CI the path is R7 → the gate fires, as before.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content=f"pushed\n\nNEXT: pr-review {_REF}")
    gate = _ScriptedPrGate(mcp, ReviewEvent.APPROVE)
    rollup = _ScriptedRollupSource(_rollup(*_GREEN))
    outcome = await _settled_conductor(
        mcp, state=_StateSource(state), gate=gate, rollup=rollup
    ).run()
    assert gate.fired == [_REF]
    assert outcome.stop_reason is StopReason.MERGE_WAIT
    assert all(p["author"] != PR_EVENT_RELAY_AUTHOR for p in mcp.posts)


@pytest.mark.anyio
async def test_no_state_source_wired_keeps_the_pre_change_path() -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content=f"pushed\n\nNEXT: pr-review {_REF}")
    gate = _ScriptedPrGate(mcp, ReviewEvent.APPROVE)
    outcome = await _settled_conductor(mcp, state=None, gate=gate).run()
    assert gate.fired == [_REF]
    assert outcome.stop_reason is StopReason.MERGE_WAIT


def test_pr_settled_is_its_own_stop_reason() -> None:
    assert StopReason.PR_SETTLED.value == "pr_settled"


# --------------------------------------------------------------------------- #
# (c) the R6 note — msg-7556 (b), kept in msg-7560 v0.4 (c).
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_r6_posts_one_note_that_moves_the_head_and_pollutes_no_gate_record() -> None:
    mcp = _FakeChatroomMcp()
    _seed_reviewed_then_renominated(mcp, author="Heisenberg")
    gate = _ScriptedPrGate(mcp, ReviewEvent.APPROVE)
    rollup = _ScriptedRollupSource(_rollup(*_GREEN))
    relay_before = [m["content"] for m in mcp._messages if m["author"] == "pr-gate-relay"]
    outcome = await _settled_conductor(
        mcp, state=_StateSource(_OPEN), gate=gate, rollup=rollup
    ).run()

    assert gate.fired == []
    assert outcome.stop_reason is StopReason.HUMAN
    assert len(mcp.posts) == 1
    note = mcp.posts[0]
    assert note["author"] == "pr-gate-relay"
    body = note["content"]
    assert heading_ref(body.splitlines()[0]) == _REF  # 1b reads it as an admission tail
    assert "ADMISSION: already_reviewed (rule=R6)" in body
    assert _HEAD[:12] in body
    assert body.rstrip().endswith("NEXT: human")
    assert parse_next_token(body) == "human"
    assert MERGE_WAIT_LINE not in body  # the verdict on this head was a COMMENT, not a merge wait
    # No verdict heading and no ci-route marker: R6 / R5 inputs are unchanged by the note.
    relay_after = [m["content"] for m in mcp._messages if m["author"] == "pr-gate-relay"]
    assert verdict_heads(relay_after) == verdict_heads(relay_before)
    assert ci_route_heads(relay_after) == ci_route_heads(relay_before)
    assert outcome.last_msg_id == mcp._messages[-1]["msg_id"]

    # The next tick does not spin on R6 (magickit#111's 91 relaunches): the head is now the
    # note's NEXT: human, which the sweep's Stage 1 skips, where the old pr-review head was
    # relaunched every tick. Even run by hand, the conductor never reaches admission again.
    assert stage1_skips(body)
    assert not stage1_skips(mcp._messages[-2]["content"])  # the pr-review head it replaced
    rollup_again = _ScriptedRollupSource(_rollup(*_GREEN))
    await _settled_conductor(mcp, state=_StateSource(_OPEN), gate=gate, rollup=rollup_again).run()
    assert rollup_again.asked == []
    assert gate.fired == []
    assert [p["author"] for p in mcp.posts if p["author"] == "pr-gate-relay"] == ["pr-gate-relay"]


@pytest.mark.anyio
async def test_r6_on_a_merge_wait_head_keeps_the_merge_wait_lane() -> None:
    # The head's verdict was an APPROVE merge wait. The note repeats MERGE-WAIT:, so the board
    # keeps listing it as a merge wait (not as a decision) and the stop is MERGE_WAIT, the same
    # terminal the verdict reached.
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content=f"opened\n\nNEXT: pr-review {_REF}")
    mcp.seed(
        author="pr-gate-relay",
        content=(
            f"{render_relay_heading(_REF, _HEAD)}\n\nVERDICT: approve (ci=success)\n\nok\n\n"
            f"{MERGE_WAIT_LINE}\nNEXT: human"
        ),
    )
    mcp.seed(author="Heisenberg", content=f"again\n\nNEXT: pr-review {_REF}")
    gate = _ScriptedPrGate(mcp, ReviewEvent.APPROVE)
    rollup = _ScriptedRollupSource(_rollup(*_GREEN))
    outcome = await _settled_conductor(
        mcp, state=_StateSource(_OPEN), gate=gate, rollup=rollup
    ).run()
    assert outcome.stop_reason is StopReason.MERGE_WAIT
    body = mcp.posts[-1]["content"]
    assert MERGE_WAIT_LINE in body
    assert classify_parked(body).lane is ParkedLane.MERGE_WAIT


@pytest.mark.anyio
@pytest.mark.parametrize("latest_is_merge_wait", [True, False])
async def test_r6_merge_wait_follows_the_latest_verdict_on_the_head(
    latest_is_merge_wait: bool,
) -> None:
    # Two verdict relays on the same head (a re-run). The note follows the newest one only;
    # the older, superseded verdict decides nothing (PR #504 gate advisory, reverse scan).
    approve = (
        f"{render_relay_heading(_REF, _HEAD)}\n\nVERDICT: approve (ci=success)\n\nok\n\n"
        f"{MERGE_WAIT_LINE}\nNEXT: human"
    )
    comment = (
        f"{render_relay_heading(_REF, _HEAD)}\n\nVERDICT: comment (ci=success)\n\nnote\n\n"
        "NEXT: human"
    )
    older, newer = (comment, approve) if latest_is_merge_wait else (approve, comment)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content=f"opened\n\nNEXT: pr-review {_REF}")
    mcp.seed(author="pr-gate-relay", content=older)
    mcp.seed(author="pr-gate-relay", content=newer)
    mcp.seed(author="Heisenberg", content=f"again\n\nNEXT: pr-review {_REF}")
    gate = _ScriptedPrGate(mcp, ReviewEvent.APPROVE)
    rollup = _ScriptedRollupSource(_rollup(*_GREEN))
    outcome = await _settled_conductor(
        mcp, state=_StateSource(_OPEN), gate=gate, rollup=rollup
    ).run()
    assert gate.fired == []
    body = mcp.posts[-1]["content"]
    assert (MERGE_WAIT_LINE in body) is latest_is_merge_wait
    expected = StopReason.MERGE_WAIT if latest_is_merge_wait else StopReason.HUMAN
    assert outcome.stop_reason is expected


@pytest.mark.anyio
async def test_conductor_first_then_1b_reports_a_merge_exactly_once() -> None:
    # The degraded order v0.4 names: the conductor could not read the PR state (so the settled
    # check was skipped), hit R6 and posted its note. 1b then reads the note's admission heading,
    # sees the merge, and posts row 3 once; its next tick is a no-op.
    mcp = _FakeChatroomMcp()
    _seed_reviewed_then_renominated(mcp, author="Heisenberg")
    gate = _ScriptedPrGate(mcp, ReviewEvent.APPROVE)
    rollup = _ScriptedRollupSource(_rollup(*_GREEN))
    blind = _StateSource(_state(PrResolution.UNRESOLVABLE))
    await _settled_conductor(mcp, state=blind, gate=gate, rollup=rollup).run()
    assert [p["author"] for p in mcp.posts] == ["pr-gate-relay"]  # the R6 note

    first = await advance_thread(
        mcp=mcp, gh=_StateSource(_MERGED), project="p", thread_id=_THREAD, proposer="Bohr"
    )
    second = await advance_thread(
        mcp=mcp, gh=_StateSource(_MERGED), project="p", thread_id=_THREAD, proposer="Bohr"
    )
    assert first.action == "posted"
    assert (second.action, second.reason) == ("noop", "not-relay-tail")
    assert [p["author"] for p in mcp.posts] == ["pr-gate-relay", PR_EVENT_RELAY_AUTHOR]
    assert "が merge された" in mcp.posts[-1]["content"]
    assert gate.fired == []


# --------------------------------------------------------------------------- #
# Disjoint tails: every pr-gate-relay post the conductor writes is read by 1b's heading reader
# and never ends ``pr-review`` — so the conductor's pr-review intercept and 1b never share a tail.
# --------------------------------------------------------------------------- #


def _admission(admission: GateAdmission, rule: str) -> AdmissionResult:
    return AdmissionResult(admission=admission, rule=rule, reason="machine reason")


def _red_rollup() -> CheckRollup:
    from test_conductor_core import _RED

    return _rollup(*_RED)


async def _writer_bodies(kind: str) -> str:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Heisenberg", content=f"pushed\n\nNEXT: pr-review {_REF}")
    cond = _conductor(mcp, _ScriptedDispatcher(mcp, {}), orchestrator=None)
    msg: dict[str, Any]
    if kind == "R3":
        msg = await cond._post_admission_escalation(
            _REF, _admission(GateAdmission.ROUTE_HUMAN, "R3")
        )
    elif kind == "R5":
        msg = await cond._post_admission_escalation(
            _REF, _admission(GateAdmission.ROUTE_HUMAN, "R5")
        )
    elif kind == "R4":
        msg = await cond._post_ci_route(
            _REF, _admission(GateAdmission.ROUTE_IMPLEMENTER, "R4"), _red_rollup()
        )
    else:
        msg, _ = await cond._post_already_reviewed_note(
            _REF,
            _admission(GateAdmission.ALREADY_REVIEWED, "R6"),
            _rollup(*_GREEN),
            [],
        )
    assert mcp.posts[-1]["author"] == "pr-gate-relay"
    return str(msg["content"])


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["R3", "R4", "R5", "R6"])
async def test_every_conductor_relay_post_is_a_1b_tail_and_never_ends_pr_review(kind: str) -> None:
    body = await _writer_bodies(kind)
    assert heading_ref(body.strip().splitlines()[0]) == _REF
    token = parse_next_token(body)
    assert token is not None
    assert not token.lower().startswith("pr-review")
