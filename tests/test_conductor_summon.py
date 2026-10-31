"""Summon, the conductor's half (T-human-call-priority-interrupt PR-2).

Spec: Bohr msg-6755 (v1.6, §1 / §3 / §4) with msg-6800 (v1.7 contract) and msg-6802 (v1.8 scope);
the resume-notice lifecycle is msg-6751 (v1.4) and the stop-line pins are msg-6749 (v1.3) and
msg-6753 (v1.5). What this file pins:

* the ``active_summon`` contract, from the shared fixture the PowerShell half reads too;
* ``summon_yield_decision``, from the other shared fixture;
* the conductor stops on ``summon_yield`` at a round boundary — round 0 or later — and names the
  Summon on its stop line; the Summon itself runs; the owner project's Summon is in scope too,
  fail-open when unreachable and HOLD when malformed;
* the resume notice: on the first role turn only, only when main moved, and
  ``resume_notice=delivered`` whenever that first turn was sent — never when the run stopped first;
  together with ``yield_to=`` when the run yielded after delivering.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire.conductor.control import (
    ControlSnapshot,
    ControlState,
    parse_control_snapshot,
)
from spirrow_mindwire.conductor.core import Conductor, ConductorStopSnapshot, StopReason
from spirrow_mindwire.conductor.summon import (
    UNKNOWN_SHA,
    ActiveSummon,
    ResumeNotice,
    SummonReadKind,
    parse_active_summon,
    parse_active_summon_value,
    parse_resume_notice,
    parse_since,
    render_resume_notice,
    summon_yield_decision,
)
from spirrow_mindwire.loop_runner import format_adapter_error_stop_line
from spirrow_mindwire.thread_context import build_turn_prompt
from spirrow_mindwire.value_objects import Role

_DATA = Path(__file__).parent / "data" / "summon"
_CONTRACT = json.loads((_DATA / "active_summon_contract.json").read_text(encoding="utf-8"))
_DECISION = json.loads((_DATA / "summon_yield_decision.json").read_text(encoding="utf-8"))
_ROSTER = {"Bohr": Role.PROPOSER, "Heisenberg": Role.IMPLEMENTER, "Einstein": Role.NAYSAYER}
_CORE_LOGGER = "spirrow_mindwire.conductor.core"


def _summon(thread_id: str, since: str = "2026-10-09T01:00:00Z") -> ActiveSummon:
    parsed = parse_since(since)
    assert parsed is not None
    return ActiveSummon(thread_id=thread_id, since=parsed, by="takahito")


# --------------------------------------------------------------------------- shared fixtures


@pytest.mark.parametrize("case", _CONTRACT["cases"], ids=lambda c: c["name"])
def test_active_summon_contract_fixture(case: dict[str, Any]) -> None:
    read = parse_active_summon(case["payload"])
    expect = case["expect"]
    assert read.kind.value == expect["kind"]
    if expect["kind"] == "valid":
        assert read.summon is not None
        assert read.summon.thread_id == expect["thread_id"]
        assert read.summon.since == parse_since(expect["since"])
        assert read.summon.by == expect["by"]
    else:
        assert read.summon is None


def test_contract_fixture_names_its_canonical_location() -> None:
    """v1.8 (msg-6802): the canonical copy says where it is and that it changes first."""
    assert "CANONICAL" in _CONTRACT["_canonical"]
    assert "tests/data/summon/active_summon_contract.json" in _CONTRACT["_canonical"]
    assert _CONTRACT["_copies"]


@pytest.mark.parametrize("case", _DECISION["cases"], ids=lambda c: c["name"])
def test_summon_yield_decision_fixture(case: dict[str, Any]) -> None:
    own = parse_active_summon_value(case["own"]).summon
    owner = parse_active_summon_value(case["owner"]).summon
    got = summon_yield_decision(
        project=case["project"],
        thread_id=case["thread_id"],
        own_summon=own,
        owner_project=case["owner_project"],
        owner_summon=owner,
    )
    assert got == case["expect_yield_to"]


# --------------------------------------------------------------------------- control snapshot


def test_snapshot_reads_active_summon_off_the_same_payload() -> None:
    snap = parse_control_snapshot(
        {
            "desired_state": "run",
            "active_summon": {"thread_id": "S1", "since": "2026-10-09T01:00:00Z", "by": "h"},
        }
    )
    assert snap.state is ControlState.RUN
    assert snap.summon is not None and snap.summon.thread_id == "S1"


def test_snapshot_without_the_key_is_no_summon() -> None:
    snap = parse_control_snapshot({"desired_state": "supervised"})
    assert snap == ControlSnapshot(state=ControlState.SUPERVISED, summon=None)


def test_malformed_active_summon_fails_safe_to_hold() -> None:
    snap = parse_control_snapshot({"desired_state": "run", "active_summon": {"thread_id": "S1"}})
    assert snap == ControlSnapshot(state=ControlState.HOLD, summon=None)


def test_unusable_payload_is_hold_with_no_summon() -> None:
    assert parse_control_snapshot("garbage") == ControlSnapshot(state=ControlState.HOLD)
    assert parse_active_summon("garbage").kind is SummonReadKind.NONE


# --------------------------------------------------------------------------- resume notice parse


def test_resume_notice_absent_flags_mean_not_yielded() -> None:
    assert parse_resume_notice(None, None) is None


def test_resume_notice_parses_summons_and_range() -> None:
    n = parse_resume_notice("S1, S2", "aaa..bbb")
    assert n == ResumeNotice(summons=("S1", "S2"), main_from="aaa", main_to="bbb")
    assert not n.empty_range
    text = render_resume_notice(n)
    assert text is not None
    assert "S1, S2" in text and "aaa..bbb" in text
    assert "取り込みを確認" in text


# The wording is fixed here, word for word (PR-3, Bohr msg-6965 item 3 / item 4).
_MOVED_TEXT = (
    "[Summon 再開の注記] このスレッドは Summon S1, S2 に譲って停止していた。"
    "停止中に main が aaa..bbb 進んだ (Summon 以外の変更を含みうる)。"
    "続ける前に取り込みを確認すること。"
)
_UNMOVED_TEXT = (
    "[Summon 再開の注記] このスレッドは Summon S1 に譲って停止していた。"
    "停止中に main は変わっていない (aaa)。取り込みは不要。"
)


def test_moved_range_wording_is_fixed() -> None:
    n = parse_resume_notice("S1, S2", "aaa..bbb")
    assert n is not None
    assert render_resume_notice(n) == _MOVED_TEXT


def test_empty_range_has_its_own_wording() -> None:
    n = parse_resume_notice("S1", "aaa..aaa")
    assert n is not None and n.empty_range
    assert render_resume_notice(n) == _UNMOVED_TEXT


def test_unknown_range_takes_the_moved_wording() -> None:
    # unknown..unknown is never "empty": not knowing whether main moved is told as "it may have".
    n = parse_resume_notice("S1", "unknown..unknown")
    assert n is not None and not n.empty_range
    text = render_resume_notice(n)
    assert "unknown..unknown 進んだ" in text and "取り込みを確認" in text


def test_unparseable_range_is_unknown_and_never_empty() -> None:
    n = parse_resume_notice("S1", "garbage")
    assert n == ResumeNotice(summons=("S1",), main_from=UNKNOWN_SHA, main_to=UNKNOWN_SHA)
    assert not n.empty_range
    assert render_resume_notice(n) is not None


def test_prompt_renders_resume_notice_as_its_own_block() -> None:
    disp_mcp = _FakeChatroomMcp()
    disp_mcp.seed(author="human", content="go\n\nNEXT: Bohr")
    conductor = _make(disp_mcp, _ScriptedDispatcher(disp_mcp, {}))
    msg = {"msg_id": "m1", "author": "human", "content": "go", "timestamp": "2026-06-07T00:00:00Z"}
    event = conductor._to_event(msg, [msg], resume_notice="[Summon 再開の注記] X")
    prompt = build_turn_prompt(event, Role.PROPOSER, "CLOSING")
    assert "\n\n[Summon 再開の注記] X\n\nCLOSING" in prompt


def test_adapter_error_stop_line_carries_delivered() -> None:
    snap = ConductorStopSnapshot(
        rounds=1,
        last_msg_id="m2",
        forced=0,
        forced_saveable=0,
        error_code="x",
        resume_notice_delivered=True,
    )
    assert format_adapter_error_stop_line(snap).endswith(" resume_notice=delivered")


# --------------------------------------------------------------------------- conductor


class _SummonControl:
    """Yields the given snapshots in order, repeating the last one."""

    def __init__(self, *snaps: ControlSnapshot) -> None:
        self._snaps = snaps
        self.reads = 0
        self.observed: list[ControlState] = []

    async def read(self) -> ControlState:
        return (await self.read_snapshot()).state

    async def read_snapshot(self) -> ControlSnapshot:
        snap = self._snaps[min(self.reads, len(self._snaps) - 1)]
        self.reads += 1
        return snap

    async def report_observed(self, state: ControlState) -> None:
        self.observed.append(state)


class _OwnerSource:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.fetches = 0

    async def fetch(self) -> Any | None:
        self.fetches += 1
        return self._payload


def _make(
    mcp: _FakeChatroomMcp,
    disp: _ScriptedDispatcher,
    *,
    control: Any = None,
    owner_project: str | None = None,
    owner: Any = None,
    resume: ResumeNotice | None = None,
) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        control=control,
        summon_owner_project=owner_project,
        summon_owner_control=owner,
        resume_notice=resume,
    )


def _run_ready() -> tuple[_FakeChatroomMcp, _ScriptedDispatcher]:
    """A thread whose head hands to Bohr; Bohr's one reply settles it (NEXT: none)."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="go\n\nNEXT: Bohr")
    disp = _ScriptedDispatcher(mcp, {Role.PROPOSER: ["done\n\nNEXT: none"]})
    return mcp, disp


def _stop_line(caplog: pytest.LogCaptureFixture) -> str:
    lines = [r.getMessage() for r in caplog.records if "conductor stopped:" in r.getMessage()]
    assert lines, "no stop line logged"
    return lines[-1]


_RUN = ControlSnapshot(state=ControlState.RUN)


@pytest.mark.anyio
async def test_same_project_summon_stops_a_bystander_at_round_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_CORE_LOGGER)
    mcp, disp = _run_ready()
    control = _SummonControl(ControlSnapshot(state=ControlState.RUN, summon=_summon("S1")))
    outcome = await _make(mcp, disp, control=control).run()
    assert outcome.stop_reason is StopReason.SUMMON_YIELD
    assert outcome.rounds == 0
    assert disp.dispatches == []
    line = _stop_line(caplog)
    assert "reason=summon_yield" in line and line.endswith(" yield_to=S1")
    assert "resume_notice" not in line


@pytest.mark.anyio
async def test_the_summoned_thread_itself_runs() -> None:
    mcp, disp = _run_ready()
    control = _SummonControl(ControlSnapshot(state=ControlState.RUN, summon=_summon("T-cond")))
    outcome = await _make(mcp, disp, control=control).run()
    assert outcome.stop_reason is StopReason.SETTLED
    assert disp.dispatches == [(Role.PROPOSER, "m1")]


@pytest.mark.anyio
async def test_summon_seen_at_a_later_round_boundary_stops_there(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_CORE_LOGGER)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="go\n\nNEXT: Bohr")
    disp = _ScriptedDispatcher(mcp, {Role.PROPOSER: ["design\n\nNEXT: Einstein"]})
    control = _SummonControl(_RUN, ControlSnapshot(state=ControlState.RUN, summon=_summon("S9")))
    outcome = await _make(mcp, disp, control=control).run()
    assert outcome.stop_reason is StopReason.SUMMON_YIELD
    assert outcome.rounds == 1
    assert disp.dispatches == [(Role.PROPOSER, "m1")]
    assert _stop_line(caplog).endswith(" yield_to=S9")


@pytest.mark.anyio
async def test_hold_wins_over_summon() -> None:
    mcp, disp = _run_ready()
    control = _SummonControl(ControlSnapshot(state=ControlState.HOLD, summon=_summon("S1")))
    outcome = await _make(mcp, disp, control=control).run()
    assert outcome.stop_reason is StopReason.HOLD


@pytest.mark.anyio
async def test_owner_project_summon_is_in_scope() -> None:
    mcp, disp = _run_ready()
    owner = _OwnerSource(
        {
            "desired_state": "run",
            "active_summon": {"thread_id": "S2", "since": "2026-10-09T01:00:00Z", "by": "h"},
        }
    )
    outcome = await _make(
        mcp, disp, control=_SummonControl(_RUN), owner_project="other", owner=owner
    ).run()
    assert outcome.stop_reason is StopReason.SUMMON_YIELD
    assert owner.fetches == 1


@pytest.mark.anyio
async def test_owner_project_unreachable_fails_open() -> None:
    mcp, disp = _run_ready()
    owner = _OwnerSource(None)
    outcome = await _make(
        mcp, disp, control=_SummonControl(_RUN), owner_project="other", owner=owner
    ).run()
    assert outcome.stop_reason is StopReason.SETTLED


@pytest.mark.anyio
async def test_owner_project_malformed_summon_fails_safe_to_hold() -> None:
    mcp, disp = _run_ready()
    owner = _OwnerSource({"desired_state": "run", "active_summon": "broken"})
    control = _SummonControl(_RUN)
    outcome = await _make(mcp, disp, control=control, owner_project="other", owner=owner).run()
    assert outcome.stop_reason is StopReason.HOLD
    assert control.observed == [ControlState.HOLD]


@pytest.mark.anyio
async def test_resume_notice_rides_only_the_first_role_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_CORE_LOGGER)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="go\n\nNEXT: Bohr")
    disp = _ScriptedDispatcher(
        mcp,
        {
            Role.PROPOSER: ["design\n\nNEXT: Heisenberg", "again\n\nNEXT: none"],
            Role.IMPLEMENTER: ["back\n\nNEXT: Bohr"],
        },
    )
    resume = ResumeNotice(summons=("S1",), main_from="aaa", main_to="bbb")
    await _make(mcp, disp, control=_SummonControl(_RUN), resume=resume).run()
    assert len(disp.events) >= 2
    first, *rest = disp.events
    assert first.resume_notice is not None and "aaa..bbb" in first.resume_notice
    assert all(e.resume_notice is None for e in rest)
    assert _stop_line(caplog).endswith(" resume_notice=delivered")


@pytest.mark.anyio
async def test_empty_range_sends_the_unmoved_text_and_is_delivered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_CORE_LOGGER)
    mcp, disp = _run_ready()
    resume = ResumeNotice(summons=("S1",), main_from="aaa", main_to="aaa")
    outcome = await _make(mcp, disp, control=_SummonControl(_RUN), resume=resume).run()
    assert outcome.stop_reason is StopReason.SETTLED
    assert disp.events[0].resume_notice == _UNMOVED_TEXT
    assert all(e.resume_notice is None for e in disp.events[1:])
    assert _stop_line(caplog).endswith(" resume_notice=delivered")


@pytest.mark.anyio
async def test_hold_before_the_first_turn_is_not_delivered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_CORE_LOGGER)
    mcp, disp = _run_ready()
    resume = ResumeNotice(summons=("S1",), main_from="aaa", main_to="bbb")
    control = _SummonControl(ControlSnapshot(state=ControlState.HOLD))
    outcome = await _make(mcp, disp, control=control, resume=resume).run()
    assert outcome.stop_reason is StopReason.HOLD
    assert "resume_notice" not in _stop_line(caplog)


@pytest.mark.anyio
async def test_no_flags_no_notice_no_mark(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=_CORE_LOGGER)
    mcp, disp = _run_ready()
    await _make(mcp, disp, control=_SummonControl(_RUN)).run()
    assert disp.events[0].resume_notice is None
    assert "resume_notice" not in _stop_line(caplog)


@pytest.mark.anyio
async def test_delivered_then_yielded_reports_both(caplog: pytest.LogCaptureFixture) -> None:
    """v1.5 (msg-6753): delivery and a new yield in one run put both fields on one line."""
    caplog.set_level(logging.INFO, logger=_CORE_LOGGER)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="human", content="go\n\nNEXT: Bohr")
    disp = _ScriptedDispatcher(mcp, {Role.PROPOSER: ["design\n\nNEXT: Einstein"]})
    control = _SummonControl(_RUN, ControlSnapshot(state=ControlState.RUN, summon=_summon("S2")))
    resume = ResumeNotice(summons=("S1",), main_from="aaa", main_to="bbb")
    outcome = await _make(mcp, disp, control=control, resume=resume).run()
    assert outcome.stop_reason is StopReason.SUMMON_YIELD
    line = _stop_line(caplog)
    assert " yield_to=S2" in line
    assert line.endswith(" resume_notice=delivered")


# --------------------------------------------------------------------------- the sweep's probe


def _load_loop_control_probe() -> Any:
    import importlib.util
    import sys

    script = Path(__file__).resolve().parents[1] / "scripts" / "loop_control.py"
    spec = importlib.util.spec_from_file_location("loop_control_probe_module", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["loop_control_probe_module"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("payload_extra", "expected"),
    [
        ({}, None),
        ({"active_summon": None}, "null"),
        (
            {"active_summon": {"thread_id": "S1", "since": "2026-10-09T01:00:00Z", "by": "h"}},
            '{"thread_id": "S1", "since": "2026-10-09T01:00:00Z", "by": "h"}',
        ),
    ],
)
async def test_probe_hands_active_summon_over_as_json_text(
    monkeypatch: pytest.MonkeyPatch, payload_extra: dict[str, Any], expected: str | None
) -> None:
    """v1.6 §2: the sweep classifies the raw value itself; the probe keeps it a string so
    ConvertFrom-Json cannot turn ``since`` into a [datetime] on the way."""
    mod = _load_loop_control_probe()

    class _Mcp:
        def __init__(self, _url: str | None) -> None: ...

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            assert name == "loop_control_get"
            return {"desired_state": "run", "configured": True, **payload_extra}

    monkeypatch.setattr(mod, "StreamableHttpChatroomMcp", _Mcp)
    out = await mod.fetch_control("p", None)
    assert out["active_summon_json"] == expected
