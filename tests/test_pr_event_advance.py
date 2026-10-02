"""1b — ``T-pr-event-advances-thread`` (design v0.5, Bohr msg-5912; Einstein msg-5911 go).

Requirement ids in the test names / comments are the msg-5912 table's (R1..R13).
"""

from __future__ import annotations

import ast
import itertools
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.conductor.gate_records import (
    RELAY_AUTHOR,
    ci_hold_head,
    ci_route_heads,
    prior_advisory_approvals,
    render_admission_heading,
    render_ci_hold_marker,
    render_relay_heading,
    verdict_heads,
)
from spirrow_mindwire.conductor.handoff import HandoffKind, HumanAsk, resolve_handoff
from spirrow_mindwire.github.client import CiState, CiStatus, PrRef, PrResolution, PrState
from spirrow_mindwire.identity.classification import (
    default_classification_path,
    load_legitimate_roles,
)
from spirrow_mindwire.magickit.client import MagickitMcpError, ThreadResolvedError
from spirrow_mindwire.pr_event_advance import (
    PR_EVENT_RELAY_AUTHOR,
    Noop,
    NoopReason,
    Post,
    PrFacts,
    closes_thread,
    decide,
    heading_ref,
    operator_handoff,
    read_relay_tail,
)
from spirrow_mindwire.pr_event_advance.runner import (
    TOOLS_USED,
    advance_thread,
    pr_facts_of,
    run_tick,
)
from spirrow_mindwire.value_objects import Role

_THREAD = "T-some-work-thread"
_REF = "acme/widgets#7"
_HEAD = "703b836737f29fe0f4139d86d2d02077c662ce5f"
_OTHER = "1111111111111111111111111111111111111111"
_MERGE = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"


def _relay(*, token: str = "human", hold: str | None = None, head: str | None = _HEAD) -> str:
    body = (
        f"{render_relay_heading(_REF, head)}\n\nVERDICT: comment (ci=pending)\n\n"
        f"critique\n\nNEXT: {token}"
    )
    return f"{body}\n\n{render_ci_hold_marker(head=hold)}" if hold else body


def _tail(**kw: Any) -> Any:
    return read_relay_tail(RELAY_AUTHOR, _relay(**kw))


def _pr(state: str, *, head: str | None = _HEAD, body: str = "") -> PrFacts:
    return PrFacts(
        state=state,
        head_sha=head,
        merge_commit_sha=_MERGE if state == "merged" else None,
        closed_at=datetime(2026, 10, 2, 8, 0, tzinfo=UTC) if state != "open" else None,
        body=body,
    )


def _decide(
    tail: Any, pr: PrFacts, ci: CiState | None = None, proposer: str | None = "Bohr"
) -> Noop | Post:
    return decide(thread_id=_THREAD, tail=tail, pr=pr, ci=ci, proposer=proposer)


# --------------------------------------------------------------------------- R1: the 9 rows


def test_row1_a_non_relay_tail_is_ignored() -> None:
    assert read_relay_tail("Heisenberg", _relay()) is None
    assert _decide(None, _pr("merged")) == Noop(NoopReason.NOT_RELAY_TAIL)


def test_row1_an_unreadable_heading_is_not_a_relay_tail() -> None:
    assert read_relay_tail(RELAY_AUTHOR, "") is None
    other = "PR-gate somethingelse — acme/widgets#7\n\nNEXT: human"
    assert read_relay_tail(RELAY_AUTHOR, other) is None


# --------------------------------------------------------------------------- R14 (v0.6 / v0.6.1)


def _admission(heading: str, next_line: str = "NEXT: human") -> str:
    return f"{heading}\n\nADMISSION: route_human (rule=R3)\n\nreason\n\n{next_line}"


@pytest.mark.parametrize(
    "heading",
    [
        f"PR-gate admission (pre-gate CI wait) — {_REF}",  # the current writer's form
        f"PR-gate admission — {_REF}",  # no parenthetical (tests/test_conductor_gate_records.py)
        f"PR-gate admission (pre-gate CI wait) - {_REF}",  # ASCII hyphen separator
    ],
)
def test_r14a_admission_heading_forms_yield_the_pr(heading: str) -> None:
    tail = read_relay_tail(RELAY_AUTHOR, _admission(heading))
    assert tail is not None and tail.pr_ref == _REF and tail.hold_head is None


@pytest.mark.parametrize("head", [_HEAD, None])
def test_r14b_verdict_heading_with_and_without_sha(head: str | None) -> None:
    tail = read_relay_tail(RELAY_AUTHOR, _relay(head=head))
    assert tail is not None and tail.pr_ref == _REF


@pytest.mark.parametrize(
    "heading",
    [
        "PR-gate (Tier B independent naysayer) — not-a-ref",
        "PR-gate admission (pre-gate CI wait) — not-a-ref",
        "PR-gate admission — acme/widgets",
    ],
)
def test_r14c_a_ref_that_does_not_parse_is_row1(heading: str) -> None:
    assert read_relay_tail(RELAY_AUTHOR, _admission(heading)) is None
    assert heading_ref(heading) is None


def test_r14d_render_admission_heading_is_byte_identical_to_the_old_writer() -> None:
    assert render_admission_heading(_REF) == f"PR-gate admission (pre-gate CI wait) — {_REF}"
    assert heading_ref(render_admission_heading(_REF)) == _REF
    assert heading_ref(render_relay_heading(_REF, _HEAD)) == _REF
    assert heading_ref(render_relay_heading(_REF, None)) == _REF


def test_r14_admission_tail_rows() -> None:
    escalation = read_relay_tail(
        RELAY_AUTHOR, _admission(render_admission_heading(_REF), "NEXT: human")
    )
    ci_route = read_relay_tail(
        RELAY_AUTHOR, _admission(render_admission_heading(_REF), "NEXT: Heisenberg")
    )
    for tail in (escalation, ci_route):
        closed = _decide(tail, _pr("closed"))
        assert isinstance(closed, Post) and closed.next_line == "NEXT: Bohr"
        settled = _decide(tail, _pr("merged", body=f"Closes-thread: {_THREAD}"))
        assert isinstance(settled, Post) and settled.next_line == "NEXT: none"
        assert _decide(tail, _pr("open"), ci=CiState.SUCCESS) == Noop(NoopReason.OPEN_NO_HOLD)


def test_r14_admission_heading_is_not_read_by_verdict_heads() -> None:
    # R6 is untouched: an admission heading names no head.
    assert verdict_heads([_admission(render_admission_heading(_REF))]) == frozenset()


def test_row1_a_quoted_heading_below_line_one_does_not_count() -> None:
    quoted = f"see below\n\n> {render_relay_heading(_REF, _HEAD)}\n\nNEXT: human"
    assert read_relay_tail(RELAY_AUTHOR, quoted) is None


def test_row2_a_settled_thread_is_never_reopened() -> None:
    assert _decide(_tail(token="none"), _pr("merged")) == Noop(NoopReason.SETTLED)
    assert _decide(_tail(token="none"), _pr("closed")) == Noop(NoopReason.SETTLED)


def test_row3_merged_with_closes_thread_for_this_thread_is_none() -> None:
    d = _decide(_tail(), _pr("merged", body=f"Fix.\r\n\r\nCloses-thread: {_THREAD}\r\n"))
    assert isinstance(d, Post)
    assert d.next_line == "NEXT: none"
    assert f"PR {_REF} が merge された（{_MERGE}）" in d.body  # noqa: RUF001


def test_row3_merged_without_closes_thread_goes_to_the_proposer() -> None:
    d = _decide(_tail(), _pr("merged"))
    assert isinstance(d, Post) and d.next_line == "NEXT: Bohr"


def test_row3_closes_thread_naming_another_thread_counts_as_absent() -> None:
    d = _decide(_tail(), _pr("merged", body="Closes-thread: T-somewhere-else"))
    assert isinstance(d, Post) and d.next_line == "NEXT: Bohr"


def test_row3_closes_thread_merge_never_reads_the_roster() -> None:
    # B2: NEXT: none needs no persona, so a broken roster must not turn it into an operator task.
    d = _decide(_tail(), _pr("merged", body=f"Closes-thread: {_THREAD}"), proposer=None)
    assert isinstance(d, Post) and d.next_line == "NEXT: none"


def test_row3_merged_with_unresolved_roster_writes_the_fact_and_hands_to_operator() -> None:
    d = _decide(_tail(), _pr("merged"), proposer=None)
    assert isinstance(d, Post)
    assert "merge された" in d.body
    assert d.next_line == operator_handoff(_THREAD)


def test_row4_closed_unmerged_always_goes_to_the_proposer_even_with_closes_thread() -> None:
    d = _decide(_tail(), _pr("closed", body=f"Closes-thread: {_THREAD}"))
    assert isinstance(d, Post)
    assert d.next_line == "NEXT: Bohr"
    assert "merge されずに close された" in d.body
    assert "本スレッドの作業はこの PR では landing していない" in d.body
    assert _HEAD in d.body


def test_row4_the_token_holder_does_not_matter() -> None:
    # Einstein msg-5903 B1: a REQUEST_CHANGES relay parked on the implementer still advances.
    d = _decide(_tail(token="Heisenberg"), _pr("closed"))
    assert isinstance(d, Post) and d.next_line == "NEXT: Bohr"


def test_row4_closed_wins_over_a_ci_hold() -> None:
    # §F.1.1 優先順位と排他: a dead PR is never re-gated.
    d = _decide(_tail(hold=_HEAD), _pr("closed"), ci=CiState.SUCCESS)
    assert isinstance(d, Post) and d.next_line == "NEXT: Bohr"


def test_row4_closed_with_unresolved_roster_hands_to_operator() -> None:
    d = _decide(_tail(), _pr("closed"), proposer=None)
    assert isinstance(d, Post) and d.next_line == operator_handoff(_THREAD)


def test_row5_open_without_a_hold_does_nothing() -> None:
    # Einstein msg-5909: the common "REQUEST_CHANGES, waiting on the implementer" state.
    assert _decide(_tail(token="Heisenberg"), _pr("open")) == Noop(NoopReason.OPEN_NO_HOLD)


def test_row6_head_moved_is_checked_before_ci() -> None:
    d = _decide(_tail(hold=_HEAD), _pr("open", head=_OTHER), ci=CiState.SUCCESS)
    assert d == Noop(NoopReason.HEAD_MOVED)


@pytest.mark.parametrize("ci", [None, CiState.PENDING, CiState.UNKNOWN])
def test_row7_ci_not_terminal_waits(ci: CiState | None) -> None:
    assert _decide(_tail(hold=_HEAD), _pr("open"), ci=ci) == Noop(NoopReason.CI_PENDING)


@pytest.mark.parametrize("ci", [CiState.SUCCESS, CiState.FAILURE])
def test_row8_ci_terminal_on_the_held_head_re_fires_the_gate_once(ci: CiState) -> None:
    d = _decide(_tail(hold=_HEAD), _pr("open"), ci=ci, proposer=None)  # roster not consulted
    assert isinstance(d, Post)
    assert d.next_line == f"NEXT: pr-review {_REF}"
    assert ci.value in d.body


def test_row8_head_comparison_is_case_insensitive() -> None:
    d = _decide(_tail(hold=_HEAD), _pr("open", head=_HEAD.upper()), ci=CiState.SUCCESS)
    assert isinstance(d, Post)


@pytest.mark.parametrize("state", ["not-found", "draft", ""])
def test_row9_unexpected_state_does_nothing(state: str) -> None:
    assert _decide(_tail(), _pr(state)) == Noop(NoopReason.UNKNOWN_STATE)


def test_closes_thread_is_line_anchored() -> None:
    assert closes_thread(f"Closes-thread: {_THREAD}", _THREAD)
    assert closes_thread(f"a\nCloses-thread: T-x\nCloses-thread: {_THREAD}\n", _THREAD)
    assert not closes_thread(f"see Closes-thread: {_THREAD}", _THREAD)
    assert not closes_thread(f"Closes-thread: {_THREAD}-v2", _THREAD)


# --------------------------------------------------------------------------- R13: totality


def _tail_body(kind: str, token: str, hold: bool) -> str:
    marker = f"\n\n{render_ci_hold_marker(head=_HEAD)}" if hold else ""
    headings = {
        "verdict": render_relay_heading(_REF, _HEAD),
        "verdict-no-sha": render_relay_heading(_REF, None),
        "admission": render_admission_heading(_REF),
        "admission-bare": f"PR-gate admission — {_REF}",
        "other": "status update",
    }
    return f"{headings[kind]}\n\nbody\n\nNEXT: {token}{marker}"


def test_r13_decide_returns_a_value_for_every_input_combination() -> None:
    authors = [RELAY_AUTHOR, "Heisenberg"]
    kinds = ["verdict", "verdict-no-sha", "admission", "admission-bare", "other"]
    tokens = ["none", "human", "Heisenberg"]
    states = ["open", "merged", "closed", "weird"]
    holds = [True, False]
    heads = [_HEAD, _OTHER]
    cis: list[CiState | None] = [None, *CiState]
    bodies = ["", f"Closes-thread: {_THREAD}"]
    proposers = ["Bohr", None]
    n = 0
    for author, kind, token, state, hold, head, ci, body, proposer in itertools.product(
        authors, kinds, tokens, states, holds, heads, cis, bodies, proposers
    ):
        tail = read_relay_tail(author, _tail_body(kind, token, hold))
        d = _decide(tail, _pr(state, head=head, body=body), ci=ci, proposer=proposer)
        assert isinstance(d, Noop | Post), (author, kind, token, state, hold, head, ci)
        if isinstance(d, Post):
            assert d.next_line.splitlines()[-1].startswith("NEXT: ")
        n += 1
    assert n == 2 * 5 * 3 * 4 * 2 * 2 * 5 * 2 * 2


# --------------------------------------------------------------------------- R9: operator form


def test_r9_the_operator_block_passes_the_conductor_operator_check() -> None:
    d = _decide(_tail(), _pr("closed"), proposer=None)
    assert isinstance(d, Post)
    handoff = resolve_handoff(d.render(), {"Bohr": Role.PROPOSER})
    assert handoff.kind is HandoffKind.HUMAN
    assert handoff.human_ask is HumanAsk.OPERATOR_WORK
    assert handoff.operator_fault is None
    assert handoff.operator_task is not None and _THREAD in handoff.operator_task


def test_r9_the_proposer_and_pr_review_lines_resolve_as_intended() -> None:
    roster = {"Bohr": Role.PROPOSER}
    to_proposer = _decide(_tail(), _pr("closed"))
    assert isinstance(to_proposer, Post)
    assert resolve_handoff(to_proposer.render(), roster).kind is HandoffKind.ROLE
    refire = _decide(_tail(hold=_HEAD), _pr("open"), ci=CiState.SUCCESS)
    assert isinstance(refire, Post)
    assert resolve_handoff(refire.render(), roster).kind is HandoffKind.PR_REVIEW
    settled = _decide(_tail(), _pr("merged", body=f"Closes-thread: {_THREAD}"))
    assert isinstance(settled, Post)
    assert resolve_handoff(settled.render(), roster).kind is HandoffKind.NONE


# --------------------------------------------------------------------------- R11: verdict_heads


def test_r11_a_hold_then_a_real_verdict_on_the_same_head_counts_the_head() -> None:
    hold = _relay(hold=_HEAD)
    verdict = f"{render_relay_heading(_REF, _HEAD)}\n\nVERDICT: approve (ci=success)\n\nNEXT: human"
    assert verdict_heads([hold, verdict]) == frozenset({_HEAD})


def test_r11_a_hold_alone_does_not_count_the_head() -> None:
    assert verdict_heads([_relay(hold=_HEAD)]) == frozenset()


def test_r2_ci_hold_marker_round_trip_and_tolerant_read() -> None:
    assert ci_hold_head(render_ci_hold_marker(head=_HEAD.upper())) == _HEAD
    assert ci_hold_head("<!-- mindwire:ci-hold v1 {not json} -->") is None
    assert ci_hold_head("<!-- mindwire:ci-hold v1 [1] -->") is None
    assert ci_hold_head("no marker") is None


# --------------------------------------------------------------------------- R3: identity


def test_r3_pr_event_relay_is_a_registered_machine_with_no_roles() -> None:
    loaded = load_legitimate_roles(default_classification_path())
    entry = loaded.by_key(PR_EVENT_RELAY_AUTHOR)
    assert entry is not None
    assert entry.kind == "machine"
    assert entry.legitimate == frozenset()


def test_r3_pr_event_relay_is_not_read_by_the_gate_record_readers() -> None:
    # The conductor feeds verdict_heads / ci_route_heads only RELAY_AUTHOR bodies, and
    # prior_advisory_approvals filters on RELAY_AUTHOR itself; a 1b post must never count.
    assert PR_EVENT_RELAY_AUTHOR != RELAY_AUTHOR
    post = _decide(_tail(hold=_HEAD), _pr("open"), ci=CiState.SUCCESS)
    assert isinstance(post, Post)
    body = f"{render_relay_heading(_REF, _HEAD)}\n\n{post.render()}"  # worst case: a heading
    assert prior_advisory_approvals([(PR_EVENT_RELAY_AUTHOR, body)], _REF) == 0
    assert read_relay_tail(PR_EVENT_RELAY_AUTHOR, body) is None
    # And what 1b writes carries no gate-record marker of either kind.
    assert ci_route_heads([post.render()]) == frozenset()
    assert ci_hold_head(post.render()) is None


# --------------------------------------------------------------------------- R4/R5/R10: I/O


class _FakeChatroom:
    """A chatroom with a few threads; records every tool call (R10 asserts on the names)."""

    def __init__(self, threads: dict[str, list[dict[str, Any]]]) -> None:
        self.threads = threads
        self.calls: list[str] = []
        self.unreadable: set[str] = set()
        self.resolved: set[str] = set()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(name)
        if name == "chatroom_list_threads":
            return {"items": [{"thread_id": t} for t in self.threads]}
        tid = arguments["thread_id"]
        if name == "chatroom_get_thread":
            if tid in self.unreadable:
                raise MagickitMcpError("refused")
            return {"messages": list(self.threads[tid])}
        if name == "chatroom_post_message":
            if tid in self.resolved:
                raise ThreadResolvedError("resolved")
            msg = {
                "msg_id": f"msg-{len(self.threads[tid]) + 1}",
                "author": arguments["author"],
                "content": arguments["content"],
            }
            self.threads[tid].append(msg)
            return {"msg": msg}
        raise AssertionError(f"unexpected tool {name}")


class _FakeGh:
    def __init__(self, state: PrState, ci: CiStatus | None = None) -> None:
        self.state = state
        self.ci = ci or CiStatus(state=CiState.SUCCESS, head_sha=_HEAD, failing=[])
        self.ci_reads = 0

    async def fetch_pr_state(self, pr: PrRef) -> PrState:
        return self.state

    async def fetch_ci_status(self, pr: PrRef) -> CiStatus:
        self.ci_reads += 1
        return self.ci


_PR = PrRef("acme", "widgets", 7)


def _closed(merged: bool, body: str = "") -> PrState:
    return PrState(
        ref=_PR,
        resolution=PrResolution.CLOSED,
        closed_at=datetime(2026, 10, 2, tzinfo=UTC),
        merged=merged,
        head_sha=_HEAD,
        merge_commit_sha=_MERGE if merged else None,
        body=body,
    )


def _open(head: str = _HEAD) -> PrState:
    return PrState(ref=_PR, resolution=PrResolution.OPEN, head_sha=head)


def _chat(tail_author: str = RELAY_AUTHOR, tail_body: str | None = None) -> _FakeChatroom:
    return _FakeChatroom(
        {
            _THREAD: [
                {"msg_id": "msg-1", "author": "Heisenberg", "content": "pushed\n\nNEXT: pr-review"},
                {"msg_id": "msg-2", "author": tail_author, "content": tail_body or _relay()},
            ]
        }
    )


@pytest.mark.anyio
async def test_r4_merged_pr_posts_once_and_r5_the_second_tick_does_nothing() -> None:
    chat = _chat()
    gh = _FakeGh(_closed(True))
    first = await run_tick(mcp=chat, gh=gh, project="p", proposer="Bohr")
    assert first.as_dict()["posted"] == 1
    posted = chat.threads[_THREAD][-1]
    assert posted["author"] == PR_EVENT_RELAY_AUTHOR
    assert posted["content"].endswith("NEXT: Bohr")
    second = await run_tick(mcp=chat, gh=gh, project="p", proposer="Bohr")
    assert second.as_dict()["posted"] == 0
    assert second.outcomes[0].reason == NoopReason.NOT_RELAY_TAIL.value
    assert len(chat.threads[_THREAD]) == 3


@pytest.mark.anyio
async def test_r4_gh_unresolvable_writes_nothing() -> None:
    chat = _chat()
    gh = _FakeGh(PrState(ref=_PR, resolution=PrResolution.UNRESOLVABLE))
    out = await advance_thread(mcp=chat, gh=gh, project="p", thread_id=_THREAD, proposer="Bohr")
    assert (out.action, out.reason) == ("skipped", "gh-unresolvable")
    assert "chatroom_post_message" not in chat.calls


@pytest.mark.anyio
async def test_r4_an_unreadable_thread_writes_nothing() -> None:
    chat = _chat()
    chat.unreadable.add(_THREAD)
    out = await advance_thread(
        mcp=chat, gh=_FakeGh(_closed(True)), project="p", thread_id=_THREAD, proposer="Bohr"
    )
    assert out.action == "skipped" and out.reason.startswith("thread-unreadable")
    assert "chatroom_post_message" not in chat.calls


@pytest.mark.anyio
async def test_r4_unresolved_roster_writes_the_fact_with_the_operator_block() -> None:
    chat = _chat()
    out = await advance_thread(
        mcp=chat, gh=_FakeGh(_closed(False)), project="p", thread_id=_THREAD, proposer=None
    )
    assert out.action == "posted" and out.next_line == "NEXT: operator"
    assert chat.threads[_THREAD][-1]["content"].endswith(operator_handoff(_THREAD))


@pytest.mark.anyio
async def test_r4_a_resolved_thread_is_reported_not_raised() -> None:
    chat = _chat()
    chat.resolved.add(_THREAD)
    out = await advance_thread(
        mcp=chat, gh=_FakeGh(_closed(True)), project="p", thread_id=_THREAD, proposer="Bohr"
    )
    assert (out.action, out.reason) == ("skipped", "thread-resolved")


@pytest.mark.anyio
async def test_r4_ci_is_read_only_for_a_held_open_pr_on_the_same_head() -> None:
    gh = _FakeGh(_open())
    await advance_thread(mcp=_chat(), gh=gh, project="p", thread_id=_THREAD, proposer="Bohr")
    assert gh.ci_reads == 0  # no hold → row 5, no CI read
    held = _chat(tail_body=_relay(hold=_HEAD))
    out = await advance_thread(mcp=held, gh=gh, project="p", thread_id=_THREAD, proposer="Bohr")
    assert gh.ci_reads == 1
    assert out.next_line == f"NEXT: pr-review {_REF}"


@pytest.mark.anyio
async def test_r4_a_ci_read_for_a_different_head_waits() -> None:
    gh = _FakeGh(_open(), CiStatus(state=CiState.SUCCESS, head_sha=_OTHER, failing=[]))
    held = _chat(tail_body=_relay(hold=_HEAD))
    out = await advance_thread(mcp=held, gh=gh, project="p", thread_id=_THREAD, proposer="Bohr")
    assert (out.action, out.reason) == ("noop", NoopReason.CI_PENDING.value)


@pytest.mark.anyio
async def test_r4_unregistered_threads_are_flagged() -> None:
    chat = _chat()
    report = await run_tick(
        mcp=chat, gh=_FakeGh(_closed(True)), project="p", proposer="Bohr", registered=frozenset()
    )
    assert report.outcomes[0].unregistered is True


def test_pr_facts_of_maps_every_resolution() -> None:
    assert pr_facts_of(PrState(ref=_PR, resolution=PrResolution.UNRESOLVABLE)) is None
    assert pr_facts_of(_open()) == PrFacts(state="open", head_sha=_HEAD)
    merged = pr_facts_of(_closed(True, body="x"))
    assert merged is not None and merged.state == "merged" and merged.body == "x"
    closed = pr_facts_of(_closed(False))
    assert closed is not None and closed.state == "closed"
    gone = pr_facts_of(PrState(ref=_PR, resolution=PrResolution.NOT_FOUND))
    assert gone is not None and gone.state == "not-found"


@pytest.mark.anyio
async def test_r10_the_close_api_is_never_called() -> None:
    chat = _chat()
    await run_tick(
        mcp=chat,
        gh=_FakeGh(_closed(True, f"Closes-thread: {_THREAD}")),
        project="p",
        proposer="Bohr",
    )
    assert chat.threads[_THREAD][-1]["content"].endswith("NEXT: none")
    assert set(chat.calls) <= TOOLS_USED
    assert not any("close" in name for name in chat.calls)


def test_r10_the_package_does_not_name_the_close_api() -> None:
    # Static half of R10: outside docstrings (prose may name the API it does not call), no string
    # literal in the package names the close tool, and nothing that closes is imported.
    package = Path(__file__).resolve().parents[1] / "src" / "spirrow_mindwire" / "pr_event_advance"
    files = list(package.glob("*.py"))
    assert {p.name for p in files} >= {"__init__.py", "decide.py", "runner.py", "__main__.py"}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and ast.get_docstring(n) is not None
            and isinstance(n.body[0], ast.Expr)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and id(node) not in docstrings:
                assert "close_thread" not in str(node.value), path.name
            if isinstance(node, ast.ImportFrom):
                names = {a.name for a in node.names}
                assert not any("close_thread" in n or n == "close_alert" for n in names), names
                assert "gate_bootstrap" not in (node.module or ""), path.name
