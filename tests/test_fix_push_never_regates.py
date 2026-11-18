"""T-fix-push-never-regates (Bohr msg-9571 v0.1 / msg-9573 v0.2, Einstein go).

D1  — 1b park rows 1a-1f: a ``BLOCKED-ON: pr <P>`` tail on an open PR whose current head this
      thread never gated is handed to ``pr-review`` once CI is terminal.
D2' — an L1 CI-gate wait (``VERDICT: comment (ci=pending|unknown)`` + an L1 opening, or the
      ci-hold marker) is not a review: ``verdict_heads`` skips it and 1b re-fires on it, so the
      old marker-less UNKNOWN COMMENTs are rescued too.
D3  — the R6 note names who may re-nominate, and on a merge wait says to retry the merge.

The relay bodies here are built from the real writers (``render_relay_heading`` and the L1
``_ci_gate_response``), so a reworded hold breaks these tests rather than the reader.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from spirrow_mindwire.conductor.gate_records import (
    RELAY_AUTHOR,
    ci_wait,
    ci_wait_head,
    render_ci_hold_marker,
    render_relay_heading,
    verdict_heads,
)
from spirrow_mindwire.gate_admission import (
    COMPLETED,
    CheckRow,
    GateAdmission,
    gate_admission,
)
from spirrow_mindwire.github.client import CiState, CiStatus, PrRef, PrResolution, PrState
from spirrow_mindwire.naysayer.pr_review import _ci_gate_response
from spirrow_mindwire.pr_event_advance import PR_EVENT_RELAY_AUTHOR, NoopReason
from spirrow_mindwire.pr_event_advance.decide import (
    ParkTail,
    PrFacts,
    decide_park,
    read_park_tail,
)
from spirrow_mindwire.pr_event_advance.runner import run_tick
from spirrow_mindwire.repo_projects import RepoProjectMap

_THREAD = "T-some-work-thread"
_REF = "acme/widgets#7"
_PR = PrRef("acme", "widgets", 7)
_HEAD = "703b836737f29fe0f4139d86d2d02077c662ce5f"
_OLD = "1111111111111111111111111111111111111111"
_REPO_MAP = RepoProjectMap(candidates={"acme/widgets": frozenset({"p"})})
_PR_BODY = f"Fix it.\n\nThread: {_THREAD}\n"


# --------------------------------------------------------------------------- relay builders


def _l1_relay(state: CiState, head: str = _HEAD, *, marker: bool = False) -> str:
    """A verdict relay exactly as ``orchestrator._post_design_relay`` lays out an L1 hold."""
    verdict, l1_body = _ci_gate_response(CiStatus(state=state, head_sha=head, failing=[]), _REF)
    body = (
        f"{render_relay_heading(_REF, head)}\n\nVERDICT: {verdict.value} (ci={state.value})\n\n"
        f"{l1_body}\n\nNEXT: human"
    )
    return f"{body}\n\n{render_ci_hold_marker(head=head)}" if marker else body


def _model_relay(event: str, ci: str, head: str = _HEAD) -> str:
    """A relay of a review the model produced (or a chatroom replay of one)."""
    return (
        f"{render_relay_heading(_REF, head)}\n\nVERDICT: {event} (ci={ci})\n\n"
        "The diff changes X without a test.\n\nNEXT: Heisenberg"
    )


# --------------------------------------------------------------------------- D2' reader


@pytest.mark.parametrize("state", [CiState.PENDING, CiState.UNKNOWN])
def test_d2_legacy_marker_less_l1_hold_is_a_ci_wait(state: CiState) -> None:
    assert ci_wait(_l1_relay(state)) == (_HEAD, state)
    assert verdict_heads([_l1_relay(state)]) == frozenset()


def test_d2_marker_hold_still_reads_as_pending_wait() -> None:
    assert ci_wait(_l1_relay(CiState.PENDING, marker=True)) == (_HEAD, CiState.PENDING)


def test_d2_crlf_relay_body_still_reads() -> None:
    body = _l1_relay(CiState.UNKNOWN).replace("\n", "\r\n")
    assert ci_wait_head(body) == _HEAD


@pytest.mark.parametrize(
    ("event", "ci"),
    [
        ("COMMENT", "success"),
        ("REQUEST_CHANGES", "success"),
        ("APPROVE", "success"),
        # A chatroom replay re-posts a model verdict with the CURRENT ci value (the replay pass
        # runs before the L1 gate). It is still a review and must still count.
        ("REQUEST_CHANGES", "pending"),
        ("COMMENT", "unknown"),
        ("APPROVE", "unknown"),
    ],
)
def test_d2_model_reviews_are_still_counted(event: str, ci: str) -> None:
    body = _model_relay(event, ci)
    assert ci_wait(body) is None
    assert verdict_heads([body]) == frozenset({_HEAD})


def test_d2_l1_failure_is_a_send_back_not_a_wait() -> None:
    body = _l1_relay(CiState.FAILURE)
    assert ci_wait(body) is None
    assert verdict_heads([body]) == frozenset({_HEAD})


def test_d2_a_quoted_hold_below_line_three_is_not_a_wait() -> None:
    quoted = _l1_relay(CiState.UNKNOWN).replace("\n", "\n> ")
    body = (
        f"{render_relay_heading(_REF, _HEAD)}\n\nVERDICT: COMMENT (ci=success)\n\n"
        f"Earlier the gate said:\n> {quoted}\n\nNEXT: human"
    )
    assert ci_wait(body) is None
    assert verdict_heads([body]) == frozenset({_HEAD})


def test_d2_a_heading_without_head_is_not_a_wait() -> None:
    body = _l1_relay(CiState.UNKNOWN).replace(f" @ {_HEAD}", "", 1)
    assert ci_wait(body) is None


def test_d2_second_unknown_wait_on_one_head_is_counted() -> None:
    """Bound: a gate token that cannot read Actions answers UNKNOWN on every re-fire. The
    second UNKNOWN wait on the same head counts, so the next re-fire stops at R6."""
    one = _l1_relay(CiState.UNKNOWN)
    assert verdict_heads([one]) == frozenset()
    assert verdict_heads([one, one]) == frozenset({_HEAD})
    assert verdict_heads([one, _l1_relay(CiState.UNKNOWN, _OLD)]) == frozenset()
    assert verdict_heads([_l1_relay(CiState.PENDING)] * 2) == frozenset()


# --------------------------------------------------------------------------- I/O fakes


class _Chat:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(name)
        if name == "chatroom_list_threads":
            return {"items": [{"thread_id": _THREAD}], "total": 1}
        if name == "chatroom_get_thread":
            return {"messages": list(self.messages)}
        if name == "chatroom_post_message":
            msg = {
                "msg_id": f"msg-{len(self.messages) + 1}",
                "author": arguments["author"],
                "content": arguments["content"],
            }
            self.messages.append(msg)
            return {"msg": msg}
        raise AssertionError(name)


class _Gh:
    def __init__(self, state: PrState, ci: CiState) -> None:
        self.state = state
        self.ci = ci
        self.ci_reads = 0

    async def fetch_pr_state(self, pr: PrRef) -> PrState:
        return self.state

    async def fetch_ci_status(self, pr: PrRef) -> CiStatus:
        self.ci_reads += 1
        return CiStatus(state=self.ci, head_sha=self.state.head_sha, failing=[])


def _open(head: str = _HEAD, body: str = _PR_BODY) -> PrState:
    return PrState(ref=_PR, resolution=PrResolution.OPEN, head_sha=head, body=body)


async def _tick(chat: _Chat, gh: _Gh) -> Any:
    return await run_tick(repo_map=_REPO_MAP, mcp=chat, gh=gh, project="p", proposer="Bohr")


# --------------------------------------------------------------------------- admission builders

_NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _admit(messages: list[dict[str, Any]], conclusion: str) -> Any:
    relay = [str(m["content"]) for m in messages if m["author"] == RELAY_AUTHOR]
    return gate_admission(
        rollup=[
            CheckRow(
                name="gate",
                status=COMPLETED,
                conclusion=conclusion,
                started_at=_NOW - timedelta(minutes=10),
                created_at=_NOW - timedelta(minutes=11),
            )
        ],
        head_sha=_HEAD,
        head_committed_date=_NOW - timedelta(hours=1),
        head_pushed_at=_NOW - timedelta(hours=1),
        now=_NOW,
        nomination_is_self=True,  # 1b's pr-review is a machine hand-off, never R0
        verdict_heads=verdict_heads(relay),
        implementer_routed_heads=frozenset(),
        head_conflicting=False,
        mergeable_unknown_since=None,
    )


# --------------------------------------------------------------------------- D2' end to end


@pytest.mark.anyio
async def test_d2_chain_old_unknown_comment_then_ci_success_re_fires_and_admits_r7() -> None:
    """check 0 件 → marker-less ``ci=unknown`` COMMENT (old form) → same head CI success → 1b
    posts ``pr-review`` → admission answers R7 (fire), not R6."""
    messages = [
        {"msg_id": "msg-1", "author": "Heisenberg", "content": "pushed\n\nNEXT: pr-review"},
        {"msg_id": "msg-2", "author": RELAY_AUTHOR, "content": _l1_relay(CiState.UNKNOWN)},
    ]
    chat, gh = _Chat(messages), _Gh(_open(), CiState.SUCCESS)
    first = await _tick(chat, gh)
    assert first.as_dict()["posted"] == 1
    posted = messages[-1]
    assert posted["author"] == PR_EVENT_RELAY_AUTHOR
    assert posted["content"].endswith(f"NEXT: pr-review {_REF}")
    result = _admit(messages, "success")
    assert (result.admission, result.rule) == (GateAdmission.INVOKE, "R7")
    second = await _tick(chat, gh)
    assert second.as_dict()["posted"] == 0


@pytest.mark.anyio
async def test_d2_chain_ci_failure_routes_to_the_implementer_r4() -> None:
    messages = [{"msg_id": "msg-1", "author": RELAY_AUTHOR, "content": _l1_relay(CiState.UNKNOWN)}]
    chat, gh = _Chat(messages), _Gh(_open(), CiState.FAILURE)
    assert (await _tick(chat, gh)).as_dict()["posted"] == 1
    result = _admit(messages, "failure")
    assert (result.admission, result.rule) == (GateAdmission.ROUTE_IMPLEMENTER, "R4")


@pytest.mark.anyio
async def test_d2_without_ci_the_unknown_wait_stays_a_noop() -> None:
    """A repo with no CI: the rollup stays empty, ``fetch_ci_status`` answers UNKNOWN, row 7."""
    messages = [{"msg_id": "msg-1", "author": RELAY_AUTHOR, "content": _l1_relay(CiState.UNKNOWN)}]
    chat, gh = _Chat(messages), _Gh(_open(), CiState.UNKNOWN)
    report = await _tick(chat, gh)
    assert report.as_dict()["posted"] == 0
    assert report.outcomes[0].reason == NoopReason.CI_PENDING.value


@pytest.mark.anyio
async def test_d2_a_model_review_tail_is_not_re_fired() -> None:
    messages = [
        {"msg_id": "msg-1", "author": RELAY_AUTHOR, "content": _model_relay("COMMENT", "unknown")}
    ]
    report = await _tick(_Chat(messages), _Gh(_open(), CiState.SUCCESS))
    assert report.outcomes[0].reason == NoopReason.OPEN_NO_HOLD.value


# --------------------------------------------------------------------------- D1 park rows


def _park(ref: str = _REF, wake: str = "Bohr") -> str:
    return f"Pushed the fix.\n\nBLOCKED-ON: pr {ref}\nNEXT: {wake}"


def test_d1_park_tail_reader() -> None:
    assert read_park_tail("Bohr", _park()) == ParkTail(_REF)
    assert read_park_tail("Bohr", "BLOCKED-ON: thread T-x\nNEXT: Bohr") is None
    assert read_park_tail("Bohr", "no park\n\nNEXT: Bohr") is None
    assert read_park_tail(RELAY_AUTHOR, _park()) is None
    assert read_park_tail(PR_EVENT_RELAY_AUTHOR, _park()) is None
    # A malformed operand names nothing to observe.
    assert read_park_tail("Bohr", "BLOCKED-ON: pr not-a-ref\nNEXT: Bohr") is None


def _facts(state: str = "open", head: str | None = _HEAD, body: str = _PR_BODY) -> PrFacts:
    return PrFacts(state=state, head_sha=head, body=body)


def _decide_park(
    pr: PrFacts, ci: CiState | None = CiState.SUCCESS, gated: frozenset[str] | None = frozenset()
) -> Any:
    return decide_park(
        thread_project="p",
        thread_id=_THREAD,
        pr_project="p",
        park=ParkTail(_REF),
        pr=pr,
        ci=ci,
        gated_heads=gated,
    )


def test_d1_row_1a_a_pr_of_another_thread_is_left_alone() -> None:
    other = _facts(body="Thread: T-another-thread\n")
    assert _decide_park(other).reason is NoopReason.FOREIGN_PR
    assert _decide_park(_facts(body="no thread line")).reason is NoopReason.FOREIGN_PR
    # A quoted Thread: line does not count.
    assert _decide_park(_facts(body=f"> Thread: {_THREAD}\n")).reason is NoopReason.FOREIGN_PR
    # A project-qualified line naming another project does not count.
    assert _decide_park(_facts(body=f"Thread: q/{_THREAD}\n")).reason is NoopReason.FOREIGN_PR


@pytest.mark.parametrize("state", ["merged", "closed"])
def test_d1_row_1b_a_settled_pr_is_the_park_wake_arms(state: str) -> None:
    assert _decide_park(_facts(state)).reason is NoopReason.PARK_PR_SETTLED


def test_d1_row_1c_a_head_this_thread_already_gated_is_left_alone() -> None:
    # The APPROVE-then-merge-wait park: head unchanged, verdict on it → unchanged behaviour.
    assert _decide_park(_facts(), gated=frozenset({_HEAD})).reason is NoopReason.ALREADY_GATED


@pytest.mark.parametrize("ci", [CiState.PENDING, None])
def test_d1_row_1d_ci_not_terminal_waits(ci: CiState | None) -> None:
    assert _decide_park(_facts(), ci=ci).reason is NoopReason.PARK_CI_PENDING


def test_d1_row_1e_no_ci_waits() -> None:
    assert _decide_park(_facts(), ci=CiState.UNKNOWN).reason is NoopReason.PARK_NO_CI


@pytest.mark.parametrize("ci", [CiState.SUCCESS, CiState.FAILURE])
def test_d1_row_1f_terminal_ci_on_an_ungated_head_hands_to_the_gate(ci: CiState) -> None:
    post = _decide_park(_facts(), ci=ci, gated=frozenset({_OLD}))
    assert post.next_line == f"NEXT: pr-review {_REF}"
    assert _HEAD in post.body


def test_d1_unread_history_is_a_noop_never_a_guess() -> None:
    assert _decide_park(_facts(), gated=None).reason is NoopReason.UNKNOWN_STATE


@pytest.mark.anyio
async def test_d1_chain_park_after_fix_push_re_fires_once_and_admits_r7() -> None:
    """The four stalls: REQUEST_CHANGES on the old head, fix pushed, ``BLOCKED-ON: pr`` park."""
    messages = [
        {
            "msg_id": "msg-1",
            "author": RELAY_AUTHOR,
            "content": _model_relay("REQUEST_CHANGES", "success", _OLD),
        },
        {"msg_id": "msg-2", "author": "Bohr", "content": _park()},
    ]
    chat, gh = _Chat(messages), _Gh(_open(), CiState.SUCCESS)
    first = await _tick(chat, gh)
    assert first.as_dict()["posted"] == 1
    assert messages[-1]["author"] == PR_EVENT_RELAY_AUTHOR
    assert messages[-1]["content"].endswith(f"NEXT: pr-review {_REF}")
    result = _admit(messages, "success")
    assert (result.admission, result.rule) == (GateAdmission.INVOKE, "R7")
    second = await _tick(chat, gh)
    assert second.as_dict()["posted"] == 0
    assert second.outcomes[0].reason == NoopReason.NOT_RELAY_TAIL.value


@pytest.mark.anyio
async def test_d1_park_on_a_ci_wait_head_is_re_fired() -> None:
    """A park on a head whose only relay is an L1 wait: D2' makes the head ungated, so D1 fires."""
    messages = [
        {"msg_id": "msg-1", "author": RELAY_AUTHOR, "content": _l1_relay(CiState.UNKNOWN)},
        {"msg_id": "msg-2", "author": "Bohr", "content": _park()},
    ]
    assert (await _tick(_Chat(messages), _Gh(_open(), CiState.SUCCESS))).as_dict()["posted"] == 1


@pytest.mark.anyio
async def test_d1_history_and_ci_are_read_only_when_needed() -> None:
    messages = [{"msg_id": "msg-1", "author": "Bohr", "content": _park()}]
    foreign = _Gh(_open(body="Thread: T-another\n"), CiState.SUCCESS)
    chat = _Chat(messages)
    report = await _tick(chat, foreign)
    assert report.outcomes[0].reason == NoopReason.FOREIGN_PR.value
    assert foreign.ci_reads == 0
    pending = _Gh(_open(), CiState.PENDING)
    chat = _Chat(list(messages))
    await _tick(chat, pending)
    assert pending.ci_reads == 1
    assert chat.calls.count("chatroom_get_thread") == 1  # the tail read only, no history read
