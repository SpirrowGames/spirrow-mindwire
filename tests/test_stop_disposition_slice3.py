"""T-next-line-carries-who-not-why Slice 3 (Bohr msg-5175 §3): send, classify, wake.

What this file pins:

1. :mod:`spirrow_mindwire.conductor.disposition` — the payload magickit #98 accepts, sent only for a
   valid ``STOP:`` line, and the four-way classification of magickit msg-1015 v11 §1;
2. the gateway sends it with the post, sends nothing for ``ABSENT`` / ``MALFORMED`` (msg-5179 §3),
   re-posts without it only on a registry outage, and lets every other refusal propagate;
3. the conductor's ``conductor stopped:`` line names the class on a ``NEXT: none`` settle, so an
   unexplained stop no longer reads as plain "settled" (msg-5179 §3);
4. the D-7 evaluator (:mod:`spirrow_mindwire.park_wake`): operands, the per-arm monotonic
   declaration, one-wake-per-key FIFO for the non-monotonic arm, fail-closed facts, and a wake
   message that routes like the deferred handoff and is not itself a park.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_conductor_core import _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref
from test_decider_step2 import _conductor_with

from spirrow_mindwire.conductor.core import StopReason
from spirrow_mindwire.conductor.disposition import (
    StopClass,
    body_disposition,
    classify_stop,
    disposition_payload,
)
from spirrow_mindwire.conductor.handoff import (
    STOP_TRIGGER_ARMS,
    HandoffKind,
    StopLine,
    StopStatus,
    resolve_handoff,
)
from spirrow_mindwire.github.client import PrRef, PrResolution, PrState
from spirrow_mindwire.magickit.client import MagickitMcpError, ThreadResolvedError
from spirrow_mindwire.magickit.gateway import (
    DISPOSITION_UNAVAILABLE_ERROR,
    DISPOSITION_VALUE_ERRORS,
    MagickitChatroomGateway,
)
from spirrow_mindwire.park_wake.decide import (
    DEPLOY_TERMINAL_STATUSES,
    MONOTONIC,
    PARK_WAKE_RELAY_AUTHOR,
    Fact,
    Park,
    QueueTarget,
    ThreadTarget,
    TriggerArm,
    parse_trigger,
    render_wake,
    select_wakes,
)
from spirrow_mindwire.park_wake.runner import TOOLS_USED, run_tick
from spirrow_mindwire.value_objects import Role

_ROSTER = {"Bohr": Role.PROPOSER, "Heisenberg": Role.IMPLEMENTER, "Einstein": Role.NAYSAYER}


def _line(body: str) -> StopLine:
    stop = resolve_handoff(body, _ROSTER).stop_line
    assert stop is not None
    return stop


# --------------------------------------------------------------------------- 1: disposition


def test_payload_for_done_and_blocked_on() -> None:
    assert disposition_payload(_line("x\n\nSTOP: done\nNEXT: none")) == {"kind": "done"}
    stop = _line("x\n\nSTOP: blocked-on pr:acme/widgets#7 wake:heisenberg\nNEXT: none")
    assert disposition_payload(stop) == {
        "kind": "blocked_on",
        "trigger": {"arm": "pr", "ref": "acme/widgets#7"},
        "wake": "Heisenberg",  # the roster's canonical spelling, not the author's
    }


@pytest.mark.parametrize(
    "body",
    [
        "x\n\nNEXT: none",
        "x\n\nstop: done\nNEXT: none",
        "x\n\nSTOP: blocked-on human wake:human\nNEXT: none",
        "x\n\nSTOP: blocked-on thread:T-a wake:Nobody\nNEXT: none",
        "x\n\nSTOP: done\nNEXT: Bohr",
        "x\n\nSTOP: done\nNEXT: human",
        "no next line at all",
    ],
)
def test_nothing_is_sent_without_a_valid_line_on_next_none(body: str) -> None:
    assert body_disposition(body, _ROSTER) is None


@pytest.mark.parametrize(
    ("status", "human", "expected"),
    [
        (StopStatus.DONE, False, StopClass.DONE),
        (StopStatus.DONE, True, StopClass.DONE),
        (StopStatus.BLOCKED_ON, False, StopClass.BLOCKED_ON),
        (StopStatus.ABSENT, False, StopClass.UNCLASSIFIED),
        (StopStatus.MALFORMED, False, StopClass.UNCLASSIFIED),
        (StopStatus.ABSENT, True, StopClass.HUMAN_CLOSE),
        (StopStatus.MALFORMED, True, StopClass.HUMAN_CLOSE),
    ],
)
def test_classification(status: StopStatus, human: bool, expected: StopClass) -> None:
    assert classify_stop(StopLine(status), author_is_human=human) is expected


# --------------------------------------------------------------------------- 2: gateway


class _PostMcp:
    def __init__(self, *, fail_with: str | None = None, fail_times: int = 1) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_with = fail_with
        self._fail_times = fail_times

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        assert name == "chatroom_post_message"
        self.calls.append(dict(arguments))
        if self._fail_with is not None and self._fail_times > 0 and "disposition" in arguments:
            self._fail_times -= 1
            raise MagickitMcpError("refused", error_type=self._fail_with or None)
        return {"msg": {"msg_id": f"msg-{len(self.calls)}"}}


async def _post(gw: MagickitChatroomGateway, body: str) -> str:
    return await gw.post_reply(
        _thread_ref(),
        author="Bohr",
        body=body,
        reply_to_msg_id=None,
        idempotency_key="k",
        role=Role.PROPOSER,
    )


@pytest.mark.anyio
async def test_gateway_sends_the_disposition() -> None:
    mcp = _PostMcp()
    await _post(MagickitChatroomGateway(mcp, roster=_ROSTER), "x\n\nSTOP: done\nNEXT: none")
    (call,) = mcp.calls
    assert call["disposition"] == {"kind": "done"}
    assert call["role"] == "proposer"


@pytest.mark.anyio
@pytest.mark.parametrize("body", ["x\n\nNEXT: none", "x\n\nSTOP: dun\nNEXT: none", "x\nNEXT: Bohr"])
async def test_gateway_sends_no_field_without_a_valid_line(body: str) -> None:
    mcp = _PostMcp()
    await _post(MagickitChatroomGateway(mcp, roster=_ROSTER), body)
    (call,) = mcp.calls
    assert "disposition" not in call


@pytest.mark.anyio
async def test_gateway_without_roster_never_sends() -> None:
    mcp = _PostMcp()
    await _post(MagickitChatroomGateway(mcp), "x\n\nSTOP: done\nNEXT: none")
    (call,) = mcp.calls
    assert "disposition" not in call


@pytest.mark.anyio
async def test_gateway_reposts_without_the_field_only_on_a_registry_outage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mcp = _PostMcp(fail_with=DISPOSITION_UNAVAILABLE_ERROR)
    with caplog.at_level(logging.WARNING, logger="spirrow_mindwire.magickit.gateway"):
        msg_id = await _post(
            MagickitChatroomGateway(mcp, roster=_ROSTER), "x\n\nSTOP: done\nNEXT: none"
        )
    assert msg_id == "msg-2"
    assert "disposition" in mcp.calls[0] and "disposition" not in mcp.calls[1]
    assert mcp.calls[1]["content"] == mcp.calls[0]["content"]  # the STOP line stays in the body
    assert any("disposition not sent" in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
@pytest.mark.parametrize("error_type", sorted(DISPOSITION_VALUE_ERRORS))
async def test_a_refused_disposition_never_stalls_the_post(
    error_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    """PR #455 REQUEST_CHANGES: the value is agent text, so a refusal must not drop the post.

    Dropping it would leave the head unmoved and replay the same mistake on every tick.
    """
    mcp = _PostMcp(fail_with=error_type)
    body = "x\n\nSTOP: blocked-on thread:T-1 wake:Heisenberg\nNEXT: none"
    with caplog.at_level(logging.ERROR, logger="spirrow_mindwire.magickit.gateway"):
        msg_id = await _post(MagickitChatroomGateway(mcp, roster=_ROSTER), body)
    assert msg_id == "msg-2"
    assert "disposition" in mcp.calls[0] and "disposition" not in mcp.calls[1]
    assert mcp.calls[1]["content"] == body
    assert any(r.levelno == logging.ERROR and error_type in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
@pytest.mark.parametrize("error_type", ["RoleNotAllowed", "ThreadResolvedError", None])
async def test_gateway_propagates_refusals_that_are_not_about_the_disposition(
    error_type: str | None,
) -> None:
    mcp = _PostMcp(fail_with=error_type)
    if error_type is None:  # a transport failure with no classification at all
        mcp._fail_with = ""
    with pytest.raises(MagickitMcpError):
        await _post(MagickitChatroomGateway(mcp, roster=_ROSTER), "x\n\nSTOP: done\nNEXT: none")
    assert len(mcp.calls) == 1


# --------------------------------------------------------------------------- 3: stop line


def _stopped_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("conductor stopped:")
    ]


@pytest.mark.parametrize(
    ("author", "body", "expected"),
    [
        ("Bohr", "x\n\nNEXT: none", "stop_class=unclassified"),
        ("Bohr", "x\n\nSTOP: Done\nNEXT: none", "stop_class=unclassified"),
        ("Bohr", "x\n\nSTOP: done\nNEXT: none", "stop_class=done"),
        (
            "Bohr",
            "x\n\nSTOP: blocked-on thread:T-a wake:Heisenberg\nNEXT: none",
            "stop_class=blocked_on",
        ),
        ("human", "close it\n\nNEXT: none", "stop_class=human_close"),
    ],
)
@pytest.mark.anyio
async def test_stop_line_names_the_class_on_a_none_settle(
    caplog: pytest.LogCaptureFixture, author: str, body: str, expected: str
) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author=author, content=body)
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.conductor.core"):
        outcome = await _conductor_with(mcp, _ScriptedDispatcher(mcp, {}), None).run()
    assert outcome.stop_reason is StopReason.SETTLED
    (line,) = _stopped_lines(caplog)
    assert line.endswith(expected)
    assert f"reason={StopReason.SETTLED.value} " in line


@pytest.mark.anyio
async def test_stop_line_has_no_class_on_other_stops(caplog: pytest.LogCaptureFixture) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="x\n\nSTOP: done\nNEXT: human")
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.conductor.core"):
        await _conductor_with(mcp, _ScriptedDispatcher(mcp, {}), None).run()
    lines = _stopped_lines(caplog)
    assert lines and all("stop_class=" not in line for line in lines)


# --------------------------------------------------------------------------- 4: D-7 decide


def test_every_parser_arm_declares_monotonicity() -> None:
    assert tuple(arm.value for arm in MONOTONIC) == STOP_TRIGGER_ARMS
    assert MONOTONIC[TriggerArm.QUEUE_EMPTY] is False
    assert all(MONOTONIC[a] for a in (TriggerArm.THREAD, TriggerArm.PR, TriggerArm.DEPLOY))


def test_operands() -> None:
    t = parse_trigger("thread", "T-a", project="p")
    assert t is not None and t.target == ThreadTarget("p", "T-a")
    t = parse_trigger("thread", "spirrow-magickit/T-a", project="p")
    assert t is not None and t.target == ThreadTarget("spirrow-magickit", "T-a")
    t = parse_trigger("queue-empty", "p2:implementer", project="p")
    assert t is not None and t.target == QueueTarget("p2", Role.IMPLEMENTER)
    t = parse_trigger("queue-empty", "p2", project="p")
    assert t is not None and t.target == QueueTarget("p2", None)
    t = parse_trigger("pr", "acme/widgets#7", project="p")
    assert t is not None and t.key == "pr:acme/widgets#7"
    for arm, ref in [
        ("thread", "not-a-thread"),
        ("thread", "p/"),
        ("pr", "acme/widgets#7x"),
        ("pr", "https://github.com/acme/widgets/pull/7"),
        ("queue-empty", "p:wizard"),
        ("time", "2026-10-03"),
    ]:
        assert parse_trigger(arm, ref, project="p") is None, (arm, ref)


def _park(thread_id: str, msg: str, arm: str, ref: str, wake: str = "Bohr") -> Park:
    trigger = parse_trigger(arm, ref, project="p")
    assert trigger is not None
    return Park(thread_id, msg, trigger, wake, f"STOP: blocked-on {arm}:{ref} wake:{wake}")


def test_monotonic_arms_wake_everyone_fired() -> None:
    parks = [_park("T-1", "msg-9", "thread", "T-a"), _park("T-2", "msg-3", "thread", "T-a")]
    sel = select_wakes(parks, {"thread:T-a": Fact.FIRED})
    assert [p.thread_id for p in sel.wake] == ["T-2", "T-1"]
    assert sel.held == []


def test_non_monotonic_arm_wakes_one_per_key_fifo() -> None:
    parks = [
        _park("T-late", "msg-90", "queue-empty", "p:implementer"),
        _park("T-early", "msg-12", "queue-empty", "p:implementer"),
        _park("T-other", "msg-50", "queue-empty", "q"),
    ]
    sel = select_wakes(
        parks, {"queue-empty:p:implementer": Fact.FIRED, "queue-empty:q": Fact.FIRED}
    )
    assert sorted(p.thread_id for p in sel.wake) == ["T-early", "T-other"]
    assert [(p.thread_id, why) for p, why in sel.held] == [("T-late", "non-monotonic-one-per-tick")]


def test_unknown_and_not_fired_hold() -> None:
    parks = [_park("T-1", "msg-1", "pr", "acme/w#1"), _park("T-2", "msg-2", "deploy", "r-1")]
    sel = select_wakes(parks, {"pr:acme/w#1": Fact.NOT_FIRED})
    assert sel.wake == []
    assert [why for _, why in sel.held] == ["not-fired", "unknown"]


def test_wake_message_routes_to_the_wake_and_is_not_a_park() -> None:
    park = _park("T-1", "msg-7", "thread", "T-a", wake="Heisenberg")
    body = render_wake(park)
    handoff = resolve_handoff(body, _ROSTER)
    assert handoff.kind is HandoffKind.ROLE and handoff.identity == "Heisenberg"
    assert handoff.stop_line is None


def test_a_wake_to_the_implementer_still_meets_guard_i() -> None:
    """decide.py's claim "including guard (i)": the relay is neither human nor naysayer.

    A park cannot launder a handoff the conductor would gate if it were written directly, so a
    wake naming the implementer reaches guard (i) exactly as a proposer's ``NEXT: <implementer>``
    would, and is redirected unless a carve-out applies (none can: the relay author is a machine).
    """
    from spirrow_mindwire.routing import GuardIVerdict, guard_proposer_to_implementer

    park = _park("T-1", "msg-7", "thread", "T-a", wake="Heisenberg")
    handoff = resolve_handoff(render_wake(park), _ROSTER)
    assert handoff.kind is HandoffKind.ROLE and handoff.role is Role.IMPLEMENTER
    assert PARK_WAKE_RELAY_AUTHOR not in _ROSTER
    verdict = guard_proposer_to_implementer(
        author_is_human=False,
        author_is_naysayer=False,
        control_state_is_run=True,
        message_is_attested=lambda: False,
        segment_declares_tier_c=lambda: False,
        naysayer_declared_no_tier_c=lambda: False,
        decider_vetoes=lambda: False,
    )
    assert verdict is GuardIVerdict.REDIRECT


# --------------------------------------------------------------------------- 4: D-7 runner


class _TickMcp:
    """A chatroom with threads, identities and deploy requests, recording every post."""

    def __init__(self) -> None:
        self.threads: dict[tuple[str, str], dict[str, Any]] = {}
        self.identities: dict[str, Any] = {}
        self.deploys: dict[str, str] = {}
        self.posts: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.resolved_on_post: set[str] = set()

    def thread(
        self,
        project: str,
        thread_id: str,
        *,
        author: str = "Bohr",
        content: str = "x\n\nNEXT: none",
        status: str = "active",
        msg_id: str = "msg-1",
    ) -> None:
        self.threads[(project, thread_id)] = {
            "status": status,
            "messages": [{"msg_id": msg_id, "author": author, "content": content}],
        }

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(name)
        if name == "chatroom_list_threads":
            items = [
                {"thread_id": tid}
                for (proj, tid), t in self.threads.items()
                if proj == arguments["project"] and t["status"] in arguments["status_filter"]
            ]
            return {"items": items, "total": len(items)}
        if name == "chatroom_get_thread":
            t = self.threads.get((arguments["project"], arguments["thread_id"]))
            if t is None:
                raise MagickitMcpError("ChatroomNotFoundError")
            return {"thread": {"status": t["status"]}, "messages": t["messages"]}
        if name == "get_identity":
            ident = self.identities.get(arguments["identity_name"])
            if ident is None:
                return {"status": "not_found"}
            if ident == "boom":
                raise MagickitMcpError("registry down")
            return {"status": "found", "identity": {"allowed_roles": ident}}
        if name == "deploy_status":
            status = self.deploys.get(arguments["request_id"])
            if status is None:
                raise MagickitMcpError("not found")
            return {"ok": True, "request": {"status": status}}
        if name == "chatroom_post_message":
            if arguments["thread_id"] in self.resolved_on_post:
                raise ThreadResolvedError("resolved")
            self.posts.append(arguments)
            return {"msg": {"msg_id": f"msg-{100 + len(self.posts)}"}}
        raise AssertionError(name)


class _Gh:
    def __init__(self, states: dict[str, PrState]) -> None:
        self.states = states

    async def fetch_pr_state(self, pr: PrRef) -> PrState:
        return self.states[f"{pr.owner}/{pr.repo}#{pr.number}"]


def _pr(resolution: PrResolution, merged: bool = False) -> PrState:
    return PrState(ref=PrRef("acme", "w", 1), resolution=resolution, merged=merged)


@pytest.mark.anyio
async def test_tick_classifies_every_open_none_head() -> None:
    mcp = _TickMcp()
    mcp.thread("p", "T-done", content="x\n\nSTOP: done\nNEXT: none")
    mcp.thread("p", "T-agent", content="x\n\nNEXT: none")
    mcp.thread("p", "T-malformed", content="x\n\nSTOP: dun\nNEXT: none")
    mcp.thread("p", "T-human", author="human", content="closing\n\nNEXT: none")
    mcp.thread("p", "T-live", content="x\n\nNEXT: Einstein")
    mcp.thread("p", "T-gone", status="resolved")
    mcp.identities["human"] = ["human"]
    mcp.identities["Bohr"] = ["proposer"]
    report = (await run_tick(mcp=mcp, gh=_Gh({}), project="p", roster=_ROSTER)).as_dict()
    assert report["threads"] == 5
    assert report["counts"] == {"done": 1, "blocked_on": 0, "unclassified": 2, "human_close": 1}
    assert sorted(report["unclassified"]) == ["T-agent", "T-malformed"]
    assert mcp.posts == [] and report["errors"] == []


@pytest.mark.anyio
async def test_tick_does_not_guess_an_unreadable_author_role() -> None:
    mcp = _TickMcp()
    mcp.thread("p", "T-a", author="Mystery", content="x\n\nNEXT: none")
    mcp.identities["Mystery"] = "boom"
    report = (await run_tick(mcp=mcp, gh=_Gh({}), project="p", roster=_ROSTER)).as_dict()
    assert sum(report["counts"].values()) == 0
    assert report["errors"] == [{"thread_id": "T-a", "reason": "author-role-unreadable: Mystery"}]


@pytest.mark.anyio
async def test_tick_wakes_fired_parks_and_holds_the_rest() -> None:
    mcp = _TickMcp()
    mcp.thread("p", "T-a", status="resolved")
    mcp.thread("p", "T-b")
    mcp.thread(
        "p",
        "T-1",
        msg_id="msg-5",
        content="w\n\nSTOP: blocked-on thread:T-a wake:Heisenberg\nNEXT: none",
    )
    mcp.thread("p", "T-2", content="w\n\nSTOP: blocked-on thread:T-b wake:Bohr\nNEXT: none")
    mcp.thread("p", "T-3", content="w\n\nSTOP: blocked-on pr:acme/w#1 wake:Bohr\nNEXT: none")
    mcp.thread("p", "T-4", content="w\n\nSTOP: blocked-on deploy:r-1 wake:Bohr\nNEXT: none")
    mcp.thread("p", "T-5", content="w\n\nSTOP: blocked-on thread:q/T-missing wake:Bohr\nNEXT: none")
    mcp.deploys["r-1"] = "running"
    gh = _Gh({"acme/w#1": _pr(PrResolution.CLOSED, merged=True)})
    report = (
        await run_tick(mcp=mcp, gh=gh, project="p", roster=_ROSTER, registered=frozenset({"T-1"}))
    ).as_dict()
    assert report["counts"]["blocked_on"] == 5
    woken = {w["thread_id"]: w for w in report["woken"]}
    assert set(woken) == {"T-1", "T-3"}
    assert woken["T-1"]["action"] == "posted" and woken["T-1"]["unregistered"] is False
    assert woken["T-3"]["unregistered"] is True and woken["T-3"]["fact"] == "merged"
    held = {h["thread_id"]: h["reason"] for h in report["held"]}
    assert held == {"T-2": "not-fired", "T-4": "not-fired", "T-5": "unknown"}
    posted = {p["thread_id"]: p for p in mcp.posts}
    assert posted["T-1"]["author"] == PARK_WAKE_RELAY_AUTHOR
    assert "role" not in posted["T-1"]
    assert posted["T-1"]["content"].endswith("NEXT: Heisenberg")
    assert "msg-5" in posted["T-1"]["content"]


@pytest.mark.anyio
@pytest.mark.parametrize("status", sorted(DEPLOY_TERMINAL_STATUSES))
async def test_a_terminal_deploy_fires_the_park(status: str) -> None:
    """The deploy arm's FIRED branch (PR #455 advisory: only ``running`` was covered)."""
    mcp = _TickMcp()
    mcp.thread("p", "T-4", content="w\n\nSTOP: blocked-on deploy:r-1 wake:Bohr\nNEXT: none")
    mcp.deploys["r-1"] = status
    report = (await run_tick(mcp=mcp, gh=_Gh({}), project="p", roster=_ROSTER)).as_dict()
    (woken,) = report["woken"]
    assert woken["thread_id"] == "T-4" and woken["fact"] == f"status={status}"
    (post,) = mcp.posts
    assert post["content"].endswith("NEXT: Bohr")


@pytest.mark.anyio
async def test_a_woken_park_is_not_woken_again() -> None:
    mcp = _TickMcp()
    mcp.thread("p", "T-a", status="resolved")
    mcp.thread("p", "T-1", content="w\n\nSTOP: blocked-on thread:T-a wake:Bohr\nNEXT: none")
    await run_tick(mcp=mcp, gh=_Gh({}), project="p", roster=_ROSTER)
    (post,) = mcp.posts
    mcp.threads[("p", "T-1")]["messages"].append(
        {"msg_id": "msg-101", "author": post["author"], "content": post["content"]}
    )
    report = (await run_tick(mcp=mcp, gh=_Gh({}), project="p", roster=_ROSTER)).as_dict()
    assert len(mcp.posts) == 1 and report["woken"] == []


@pytest.mark.anyio
async def test_queue_empty_reads_the_committed_state_and_wakes_one() -> None:
    mcp = _TickMcp()
    mcp.thread("p", "T-busy", content="x\n\nNEXT: Heisenberg")
    for tid, msg in (("T-1", "msg-20"), ("T-2", "msg-10")):
        mcp.thread(
            "p",
            tid,
            msg_id=msg,
            content="w\n\nSTOP: blocked-on queue-empty:p:proposer wake:Heisenberg\nNEXT: none",
        )
    mcp.thread(
        "p", "T-3", content="w\n\nSTOP: blocked-on queue-empty:p:implementer wake:Bohr\nNEXT: none"
    )
    report = (await run_tick(mcp=mcp, gh=_Gh({}), project="p", roster=_ROSTER)).as_dict()
    # No thread hands to the proposer → fired, but only the earliest park (msg-10) wakes.
    assert [w["thread_id"] for w in report["woken"]] == ["T-2"]
    held = {h["thread_id"]: (h["reason"], h["fact"]) for h in report["held"]}
    assert held["T-1"] == ("non-monotonic-one-per-tick", "live=0")
    assert held["T-3"] == ("not-fired", "live=1")  # T-busy hands to the implementer


@pytest.mark.anyio
async def test_a_resolved_target_thread_drops_the_wake_into_the_report() -> None:
    mcp = _TickMcp()
    mcp.thread("p", "T-a", status="resolved")
    mcp.thread("p", "T-1", content="w\n\nSTOP: blocked-on thread:T-a wake:Bohr\nNEXT: none")
    mcp.resolved_on_post.add("T-1")
    report = (await run_tick(mcp=mcp, gh=_Gh({}), project="p", roster=_ROSTER)).as_dict()
    (woken,) = report["woken"]
    assert woken["action"] == "skipped" and woken["reason"] == "thread-resolved"


def test_tick_never_closes_a_thread() -> None:
    assert not any("close" in tool for tool in TOOLS_USED)
    source = (Path(__file__).resolve().parents[1] / "src" / "spirrow_mindwire" / "park_wake").rglob(
        "*.py"
    )
    for path in source:
        assert "chatroom_close_thread" not in path.read_text(encoding="utf-8").replace(
            "never calls ``chatroom_close_thread``", ""
        ), path


def test_wake_author_is_registered_as_a_machine() -> None:
    path = Path(__file__).resolve().parents[1] / "spec" / "identity" / "legitimate_roles.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data["identities"] if isinstance(data, dict) else data
    (entry,) = [e for e in entries if e.get("name") == PARK_WAKE_RELAY_AUTHOR]
    assert entry["kind"] == "machine" and entry["legitimate"] == []
