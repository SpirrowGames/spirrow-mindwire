"""T-next-line-carries-who-not-why Slice 1 (Bohr msg-4718 §1): the ``STOP:`` line, dark launch.

What this file pins, item by item:

1. the ``STOP:`` line above the final ``NEXT: none`` is parsed into a typed value — two accepted
   forms (``done`` / ``blocked-on <4 arms> wake:<agent>``), everything else that announces itself
   as ``STOP:`` is ``MALFORMED`` (including any ``human`` trigger or wake);
2. the conductor logs it on a measurement-only path — present / absent / malformed, and the
   ``STOP``-less ``NEXT: none`` shows up as ``stop_line=absent`` — and routes identically;
3. the Decider is not affected: whether a ``STOP:`` line exists does not change whether the
   author's ``NEXT: human`` enters the tierc hook;
4. the emission prompt (``_HANDOFF_PROTOCOL_CORE``) teaches ``STOP:`` only from Slice 3 on, and
   only in forms the parser accepts (item 4 below; Slice 3's own pins are in
   ``tests/test_stop_disposition_slice3.py``).
"""

from __future__ import annotations

import logging

import pytest
from test_conductor_core import _attested, _FakeChatroomMcp, _ScriptedDispatcher
from test_decider_step2 import _conductor_with, _dr, _run, _StubDecider, _v

from spirrow_mindwire.conductor import handoff as handoff_mod
from spirrow_mindwire.conductor.core import StopReason
from spirrow_mindwire.conductor.handoff import (
    STOP_TRIGGER_ARMS,
    HandoffKind,
    StopLine,
    StopStatus,
    build_handoff_protocol_block,
    resolve_handoff,
)
from spirrow_mindwire.decider.result import DecisionOutcome
from spirrow_mindwire.decider.verdict import TierCScope
from spirrow_mindwire.value_objects import Role

_ROSTER = {"Bohr": Role.PROPOSER, "Heisenberg": Role.IMPLEMENTER, "Einstein": Role.NAYSAYER}


def _stop(body: str, *, field: str | None = None) -> StopLine | None:
    return resolve_handoff(body, _ROSTER, next_participant=field).stop_line


# --------------------------------------------------------------------------- item 1: parse


def test_arms_are_exactly_the_four_agent_writable_arms() -> None:
    """msg-4716 §2 / msg-4718 §1-1: no ``time:`` (D-3″), no ``human`` (B-5)."""
    assert STOP_TRIGGER_ARMS == ("thread", "pr", "deploy", "queue-empty")


def test_done_is_parsed() -> None:
    assert _stop("finished\n\nSTOP: done\nNEXT: none") == StopLine(
        StopStatus.DONE, raw="STOP: done"
    )


@pytest.mark.parametrize(
    ("line", "arm", "operand", "wake"),
    [
        ("STOP: blocked-on thread:T-foo wake:Bohr", "thread", "T-foo", "Bohr"),
        (
            "STOP: blocked-on pr:acme/widgets#7 wake:einstein",
            "pr",
            "acme/widgets#7",
            "Einstein",
        ),
        (
            "STOP: blocked-on deploy:mindwire@abc123 wake:Einstein",
            "deploy",
            "mindwire@abc123",
            "Einstein",
        ),
        (
            "STOP: blocked-on queue-empty:implementer wake:Bohr",
            "queue-empty",
            "implementer",
            "Bohr",
        ),
    ],
)
def test_blocked_on_is_parsed_with_canonical_wake(
    line: str, arm: str, operand: str, wake: str
) -> None:
    stop = _stop(f"waiting\n\n{line}\nNEXT: none")
    assert stop == StopLine(
        StopStatus.BLOCKED_ON, trigger_arm=arm, trigger_operand=operand, wake=wake, raw=line
    )
    assert stop.status.presence == "present"


@pytest.mark.parametrize(
    "line",
    [
        # agent-written human trigger / wake (B-5, msg-4716 §2)
        "STOP: blocked-on human wake:Bohr",
        "STOP: blocked-on human:x wake:Bohr",
        "STOP: blocked-on thread:T-foo wake:human",
        "STOP: blocked-on thread:T-foo wake:Human",
        "STOP: blocked-on thread:T-foo wake:none",
        # wake is not a registered agent of the thread
        "STOP: blocked-on thread:T-foo wake:Schrodinger",
        # time: was removed (D-3″)
        "STOP: blocked-on time:2026-10-01T00:00Z wake:Bohr",
        # fixed form, read strictly
        "STOP: Done",
        "STOP: done.",
        "STOP: done (awaiting close)",
        "STOP: blocked-on thread:T-foo",
        "STOP: blocked-on thread: T-foo wake:Bohr",
        "STOP: blocked-on thread:T-foo  wake:Bohr",
        "STOP: blocked-on: thread:T-foo wake:Bohr",
        "STOP: blocked-on thread:T-foo wake:Bohr extra",
        "STOP: waiting for Takahito",
        "STOP:",
        # the keyword is detected loosely so these land in MALFORMED, not ABSENT
        "stop: done",
        "Stop: blocked-on thread:T-foo wake:Bohr",
        "**STOP: done**",
        # keyword and colon separated by decoration / whitespace (PR-gate #363 finding 1)
        "**STOP**: done",
        "STOP : done",
        "__STOP__: blocked-on thread:T-foo wake:Bohr",
        "`STOP`: done",
    ],
)
def test_malformed_lines(line: str) -> None:
    stop = _stop(f"body\n\n{line}\nNEXT: none")
    assert stop is not None
    assert stop.status is StopStatus.MALFORMED
    assert stop.status.presence == "malformed"
    assert stop.raw == line.strip()
    assert stop.trigger_arm is None and stop.wake is None


def test_leading_decoration_and_spacing_after_keyword_are_tolerated() -> None:
    """Only the keyword is loose; the body after it is fixed."""
    assert _stop("x\n\n  **STOP:done\nNEXT: none") == StopLine(StopStatus.DONE, raw="**STOP:done")
    assert _stop("x\n\nSTOP: done\r\nNEXT: none") == StopLine(StopStatus.DONE, raw="STOP: done")


@pytest.mark.parametrize(
    "body",
    [
        "done\n\nNEXT: none",
        "NEXT: none",
        "STOP: done\n\nNEXT: none",  # blank line between: look-back is n-1 only
        "STOP: done\nsomething else\nNEXT: none",
        "quoting `STOP: done` in prose\nNEXT: none",
        "TIER-C: scope\nNEXT: none",
    ],
)
def test_absent(body: str) -> None:
    stop = _stop(body)
    assert stop == StopLine(StopStatus.ABSENT)
    assert stop.status.presence == "absent"


def test_only_the_line_above_the_last_next_counts() -> None:
    body = (
        "STOP: done\nNEXT: none\n\nquoted later\n\nSTOP: blocked-on pr:a/b#1 wake:Bohr\nNEXT: none"
    )
    stop = _stop(body)
    assert stop is not None and stop.status is StopStatus.BLOCKED_ON


@pytest.mark.parametrize(
    "body",
    [
        "STOP: done\nNEXT: Bohr",
        "STOP: done\nNEXT: human",
        "STOP: done\nNEXT: pr-review acme/widgets#7",
        "STOP: done\nNEXT: Schrodinger",
        "STOP: done",
    ],
)
def test_stop_line_is_measured_only_on_none(body: str) -> None:
    h = resolve_handoff(body, _ROSTER)
    assert h.kind is not HandoffKind.NONE
    assert h.stop_line is None


def test_stop_line_does_not_change_the_rest_of_the_handoff() -> None:
    """Routing is unchanged: only ``stop_line`` differs between with/without."""
    for line in ("STOP: done", "STOP: blocked-on human wake:human", "STOP: garbage"):
        plain = resolve_handoff("x\n\nNEXT: none", _ROSTER)
        withline = resolve_handoff(f"x\n\n{line}\nNEXT: none", _ROSTER)
        assert withline.kind is plain.kind is HandoffKind.NONE
        assert (withline.token, withline.mismatch_reason, withline.tier_c_label) == (
            plain.token,
            plain.mismatch_reason,
            plain.tier_c_label,
        )


def test_field_route_keeps_the_body_stop_line() -> None:
    """A field ``none`` agreeing with the body keeps the body's STOP line (not miscounted)."""
    assert _stop("x\nSTOP: done\nNEXT: none", field="none") == StopLine(
        StopStatus.DONE, raw="STOP: done"
    )
    # field none, no body NEXT: → no STOP line was written → ABSENT
    assert _stop("x\nSTOP: done", field="none") == StopLine(StopStatus.ABSENT)
    # field none vs body Bohr → mismatch → HUMAN, not measured
    h = resolve_handoff("x\nSTOP: done\nNEXT: Bohr", _ROSTER, next_participant="none")
    assert h.kind is HandoffKind.HUMAN and h.stop_line is None


# --------------------------------------------------------------------------- item 2: measurement


def _none_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("conductor none terminal:")
    ]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("done\n\nNEXT: none", "stop_line=absent stop_disposition=absent"),
        ("done\n\nSTOP: done\nNEXT: none", "stop_line=present stop_disposition=done"),
        (
            "wait\n\nSTOP: blocked-on pr:acme/widgets#7 wake:Einstein\nNEXT: none",
            "stop_line=present stop_disposition=blocked_on stop_trigger=pr:acme/widgets#7 "
            "stop_wake=Einstein",
        ),
        (
            "wait\n\nSTOP: blocked-on human wake:human\nNEXT: none",
            "stop_line=malformed stop_disposition=malformed stop_trigger=None stop_wake=None "
            "stop_raw='STOP: blocked-on human wake:human'",
        ),
    ],
)
@pytest.mark.anyio
async def test_conductor_logs_stop_line_and_still_settles(
    caplog: pytest.LogCaptureFixture, body: str, expected: str
) -> None:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content=body)
    disp = _ScriptedDispatcher(mcp, {})
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.conductor.core"):
        outcome = await _conductor_with(mcp, disp, None).run()
    assert outcome.stop_reason is StopReason.SETTLED
    assert disp.dispatches == [] and mcp.posts == []
    (line,) = _none_lines(caplog)
    assert "author=Bohr author_role=proposer" in line
    assert expected in line


@pytest.mark.anyio
async def test_conductor_forwards_nothing_for_a_stop_line() -> None:
    """No rejection, no forwarding: the chatroom sees exactly what it sees without the line."""
    runs = []
    for body in ("done\n\nNEXT: none", "done\n\nSTOP: done\nNEXT: none"):
        mcp = _FakeChatroomMcp()
        mcp.seed(author="Bohr", content=body)
        disp = _ScriptedDispatcher(mcp, {})
        outcome = await _conductor_with(mcp, disp, None).run()
        runs.append((outcome.stop_reason, disp.dispatches, mcp.posts))
    assert runs[0] == runs[1]


# --------------------------------------------------------------------------- item 3: Decider


@pytest.mark.parametrize(
    "above",
    [
        None,
        "STOP: done",
        "STOP: blocked-on thread:T-foo wake:Einstein",
        "STOP: blocked-on human wake:human",
    ],
)
@pytest.mark.anyio
async def test_stop_line_does_not_change_decider_entry_on_next_human(above: str | None) -> None:
    """msg-4718 §1-3: ``author_requested_human`` (``core.py`` → ``decider/hook.py``) is the same
    with or without a STOP line; the proposer's ``NEXT: human`` enters the hook exactly once."""
    head = "revised\n\nNEXT: human" if above is None else f"revised\n\n{above}\nNEXT: human"
    baseline, bdisp, _ = await _run(None)
    stub = _StubDecider(_dr(DecisionOutcome.EVALUATED, _v(TierCScope.IN_GATE)))
    replies = {
        Role.NAYSAYER: [_attested("critique\n\nNEXT: Bohr")],
        Role.PROPOSER: [head],
    }
    outcome, disp, _ = await _run(stub, replies=replies)
    assert outcome.stop_reason is baseline.stop_reason is StopReason.HUMAN
    assert disp.dispatches == bdisp.dispatches
    assert len(stub.states) == 1
    assert stub.states[0].parsed_next == "human"


@pytest.mark.anyio
async def test_blocked_on_human_under_next_none_is_not_a_decider_entry() -> None:
    """B-5 (msg-4716 §2): the agent's STOP line cannot open a second road to the human — it is
    not the Decider's entry and it does not stop at the human either; it settles like any
    ``NEXT: none`` and is logged malformed."""
    stub = _StubDecider(_dr(DecisionOutcome.EVALUATED, _v(TierCScope.IN_GATE)))
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="x\n\nSTOP: blocked-on human wake:human\nNEXT: none")
    outcome = await _conductor_with(mcp, _ScriptedDispatcher(mcp, {}), stub).run()
    assert outcome.stop_reason is StopReason.SETTLED
    assert stub.states == []


def test_decider_files_do_not_read_the_stop_line() -> None:
    """The Decider is left untouched by Slice 1 (msg-4718 §1-3)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "spirrow_mindwire" / "decider"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "stop_line" not in text and "StopLine" not in text, path


# --------------------------------------------------------------------------- item 4: prompt


@pytest.mark.parametrize("role", list(Role))
def test_protocol_block_teaches_only_forms_the_parser_accepts(role: Role) -> None:
    """Slice 3 (msg-5175 §3) ships the prompt with the mechanism, so the block now teaches the two
    forms — and every concrete instance of what it teaches must parse as accepted, never as
    MALFORMED (the Slice 1 pin that the prompt is silent is retired with this)."""
    block = build_handoff_protocol_block(role)
    for form in handoff_mod.STOP_FORM_EXAMPLES:
        assert form in block
    for arm in STOP_TRIGGER_ARMS:
        assert f"`{arm}:" in block
    concrete = [
        "STOP: done",
        "STOP: blocked-on thread:T-foo wake:Einstein",
        "STOP: blocked-on thread:spirrow-magickit/T-foo wake:Einstein",
        "STOP: blocked-on pr:acme/widgets#7 wake:Einstein",
        "STOP: blocked-on deploy:req-123 wake:Bohr",
        "STOP: blocked-on queue-empty:spirrow-mindwire wake:Bohr",
        "STOP: blocked-on queue-empty:spirrow-mindwire:implementer wake:Bohr",
    ]
    for line in concrete:
        stop = _stop(f"x\n\n{line}\nNEXT: none")
        assert stop is not None
        assert stop.status in (StopStatus.DONE, StopStatus.BLOCKED_ON), line


def test_protocol_block_does_not_promise_a_close_or_a_park() -> None:
    """Einstein msg-4717: the prompt may promise only what the code does. Slice 3 does not close
    a thread on ``STOP: done`` nor set a ``parked`` status (no magickit tool does), so the text
    must not say it does."""
    core = handoff_mod._HANDOFF_PROTOCOL_CORE
    assert "resolved" not in core.replace("(that thread is resolved)", "")
    assert "parked" not in core
    assert "waits for its owner to close it" in core
