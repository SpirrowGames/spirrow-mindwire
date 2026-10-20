"""Guard (i): G1 discharge by a recorder, and naming what closed carve-out ③.

Spec (thread T-guard-i-latches-on-discharged-tierc-line): operator msg-7182 (the production stop on
spirrow-lexora T-streaming-ledger-row msg-880), Bohr msg-7202 §2 (a)(b)(c) and its revision
msg-7204 (B1: name EVERY open declaration and every closed offline condition; B2: a resolution
orphaned by a human boundary is a no-op reported as ``unmatched``; A1: one id per
``TIER-C-RESOLVED:`` line; Step 0: the read-only census), endorsed by Einstein (msg-7203 review,
then the endorsement of msg-7204).

The msg-880 shape is rebuilt with production-shaped ids: last human post msg-749, ``TIER-C:
irreversible`` in msg-751 and msg-754, the operator's out-of-chat record msg-874, the attested
naysayer proceed msg-880.
"""

from __future__ import annotations

import importlib.util
import itertools
import logging
import sys
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import (
    _ROSTER,
    _attested,
    _ClearingDecider,
    _conductor,
    _FakeChatroomMcp,
    _FakeControl,
    _ScriptedDispatcher,
    _thread_ref,
    _VerdictDecider,
)

from spirrow_mindwire.conductor.control import ControlState
from spirrow_mindwire.conductor.core import CONDUCTOR_RELAY_AUTHOR, Conductor, StopReason
from spirrow_mindwire.conductor.human_identity import is_human_identity
from spirrow_mindwire.conductor.tier_c_segment import scan_tier_c_segment
from spirrow_mindwire.decider.verdict import TierCVerdictKind
from spirrow_mindwire.routing import (
    GuardICondition,
    carve_out_iii_admissible,
    diagnose_carve_out_iii,
)
from spirrow_mindwire.value_objects import Role

_PROCEED_BODY = "sound, build it\n\nTIER-C-CHECK: none\nNEXT: Heisenberg"


def _is_human(author: str) -> bool:
    return is_human_identity(author, "human")


def _m(msg_id: str, author: str, content: str) -> dict[str, Any]:
    return {"msg_id": msg_id, "author": author, "content": content}


def _resolved(target: str, *, by: str = "human", on: str = "2026-10-20") -> str:
    return f"TIER-C-RESOLVED: {target} decided-by={by} on={on}"


def _lexora_880(*extra: dict[str, Any], record: str = "probe done, Takahito approved") -> list[Any]:
    """T-streaming-ledger-row up to msg-880, with ``extra`` posts inserted before the proceed."""
    return [
        _m("msg-749", "human", "go ahead with the design\n\nNEXT: Bohr"),
        _m(
            "msg-751",
            "Bohr",
            "need a write probe on prod vLLM\n\nTIER-C: irreversible\nNEXT: human",
        ),
        _m("msg-754", "Einstein", _attested("agree, ask\n\nTIER-C: irreversible\nNEXT: human")),
        _m("msg-874", "operator", f"{record}\n\nNEXT: Bohr"),
        *extra,
        _m("msg-879", "Bohr", "PR-C design\n\nNEXT: Einstein"),
        _m("msg-880", "Einstein", _attested(_PROCEED_BODY)),
    ]


def _scan(messages: list[dict[str, Any]], recorders: tuple[str, ...] = ("operator",)) -> Any:
    return scan_tier_c_segment(messages, is_human=_is_human, recorders=recorders)


# --------------------------------------------------------------------------- #
# routing: the diagnosis is the decision's mirror, never a second decision
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("naysayer", "run", "attested", "declared", "checked"),
    list(itertools.product([True, False], repeat=5)),
)
def test_diagnosis_is_empty_exactly_when_carve_out_iii_is_admissible(
    naysayer: bool, run: bool, attested: bool, declared: bool, checked: bool
) -> None:
    admissible = carve_out_iii_admissible(
        author_is_naysayer=naysayer,
        control_state_is_run=run,
        message_is_attested=lambda: attested,
        segment_declares_tier_c=lambda: declared,
        naysayer_declared_no_tier_c=lambda: checked,
    )
    closures = diagnose_carve_out_iii(
        head_msg_id="msg-1",
        author_is_naysayer=naysayer,
        control_state_is_run=run,
        message_is_attested=lambda: attested,
        open_tier_c_declarations=lambda: ("msg-0",) if declared else (),
        naysayer_declared_no_tier_c=lambda: checked,
    )
    assert (not closures) is admissible


def test_diagnosis_lists_every_offline_closure_in_guard_order() -> None:
    closures = diagnose_carve_out_iii(
        head_msg_id="msg-9",
        author_is_naysayer=True,
        control_state_is_run=False,
        message_is_attested=lambda: False,
        open_tier_c_declarations=lambda: ("msg-3", "msg-5"),
        naysayer_declared_no_tier_c=lambda: False,
    )
    assert [c.condition for c in closures] == [
        GuardICondition.NOT_RUN,
        GuardICondition.NOT_ATTESTED,
        GuardICondition.G1,
        GuardICondition.G2,
    ]
    assert closures[2].evidence_ids == ("msg-3", "msg-5")


def test_diagnosis_does_not_read_the_attest_or_g2_of_a_non_naysayer_post() -> None:
    def boom() -> bool:
        raise AssertionError("must not be observed for a non-naysayer author")

    closures = diagnose_carve_out_iii(
        head_msg_id="msg-9",
        author_is_naysayer=False,
        control_state_is_run=True,
        message_is_attested=boom,
        open_tier_c_declarations=lambda: ("msg-3",),
        naysayer_declared_no_tier_c=boom,
    )
    assert [c.condition for c in closures] == [GuardICondition.NOT_NAYSAYER, GuardICondition.G1]


# --------------------------------------------------------------------------- #
# G1 scan: the msg-880 reconstruction and the discharge rule
# --------------------------------------------------------------------------- #


def test_msg_880_as_it_was_latches_on_both_declarations() -> None:
    scan = _scan(_lexora_880(), recorders=())
    assert scan.declares_tier_c
    assert scan.open_declarations == ("msg-751", "msg-754")
    assert scan.boundary_msg_id == "msg-749"


def test_one_resolution_of_two_declarations_keeps_the_other_open() -> None:
    scan = _scan(_lexora_880(record="approved\n" + _resolved("msg-754")))
    assert scan.open_declarations == ("msg-751",)
    assert scan.resolved_declarations == ("msg-754",)


def test_both_resolutions_unlatch_g1() -> None:
    record = "approved\n" + _resolved("msg-751") + "\n" + _resolved("msg-754")
    scan = _scan(_lexora_880(record=record))
    assert not scan.declares_tier_c
    assert scan.resolved_declarations == ("msg-751", "msg-754")
    assert scan.unmatched == () and scan.malformed == ()


def test_without_recorders_configured_the_line_discharges_nothing() -> None:
    record = "approved\n" + _resolved("msg-751") + "\n" + _resolved("msg-754")
    scan = _scan(_lexora_880(record=record), recorders=())
    assert scan.open_declarations == ("msg-751", "msg-754")
    assert scan.ignored_non_recorder == ("msg-874",)


def test_a_naysayer_cannot_discharge_by_writing_the_line() -> None:
    forged = _m(
        "msg-878",
        "Einstein",
        "Takahito approved\n" + _resolved("msg-751") + "\n" + _resolved("msg-754"),
    )
    scan = _scan(_lexora_880(forged))
    assert scan.open_declarations == ("msg-751", "msg-754")
    assert scan.ignored_non_recorder == ("msg-878",)


def test_a_resolution_naming_another_id_is_unmatched_and_discharges_nothing() -> None:
    scan = _scan(_lexora_880(record="approved\n" + _resolved("msg-999")))
    assert scan.open_declarations == ("msg-751", "msg-754")
    assert scan.unmatched == ("msg-999",)


def test_a_resolution_posted_before_its_declaration_does_not_discharge_it() -> None:
    messages = [
        _m("msg-749", "human", "go\n\nNEXT: Bohr"),
        _m("msg-750", "operator", "pre-approved\n" + _resolved("msg-751") + "\n\nNEXT: Bohr"),
        _m("msg-751", "Bohr", "x\n\nTIER-C: irreversible\nNEXT: human"),
        _m("msg-880", "Einstein", _attested(_PROCEED_BODY)),
    ]
    scan = _scan(messages)
    assert scan.open_declarations == ("msg-751",)
    assert scan.unmatched == ("msg-751",)


def test_a_message_cannot_resolve_its_own_declaration() -> None:
    messages = [
        _m("msg-749", "human", "go\n\nNEXT: Bohr"),
        _m("msg-751", "operator", "TIER-C: irreversible\n" + _resolved("msg-751") + "\nNEXT: Bohr"),
        _m("msg-880", "Einstein", _attested(_PROCEED_BODY)),
    ]
    assert _scan(messages).open_declarations == ("msg-751",)


def test_a_new_declaration_after_the_resolution_latches_again() -> None:
    record = "approved\n" + _resolved("msg-751") + "\n" + _resolved("msg-754")
    later = _m("msg-877", "Bohr", "one more probe\n\nTIER-C: irreversible\nNEXT: Einstein")
    scan = _scan(_lexora_880(later, record=record))
    assert scan.open_declarations == ("msg-877",)


def test_a_human_post_between_declaration_and_resolution_orphans_the_resolution() -> None:
    # msg-7204 B2: the human boundary already unlatched G1; the later resolution is a safe no-op.
    messages = [
        _m("msg-751", "Bohr", "x\n\nTIER-C: irreversible\nNEXT: human"),
        _m("msg-752", "human", "approved\n\nNEXT: Bohr"),
        _m("msg-874", "operator", "recorded\n" + _resolved("msg-751") + "\n\nNEXT: Bohr"),
        _m("msg-880", "Einstein", _attested(_PROCEED_BODY)),
    ]
    scan = _scan(messages)
    assert not scan.declares_tier_c
    assert scan.boundary_msg_id == "msg-752"
    assert scan.unmatched == ("msg-751",)


@pytest.mark.parametrize(
    "line",
    [
        "TIER-C-RESOLVED: msg-751, msg-754 decided-by=human on=2026-10-20",  # A1: one id per line
        "TIER-C-RESOLVED: msg-751,msg-754 decided-by=human on=2026-10-20",
        "TIER-C-RESOLVED: msg-751 decided-by=operator on=2026-10-20",  # not the human
        "TIER-C-RESOLVED: msg-751 decided-by=human on=2026-13-40",  # not a date
        "TIER-C-RESOLVED: msg-751 decided-by=human",  # missing field
        "tier-c-resolved: msg-751 decided-by=human on=2026-10-20",  # keyword case
        "TIER-C-RESOLVED: msg-751 decided-by=human on=2026-10-20 trailing",
    ],
)
def test_a_malformed_resolution_is_ignored_and_reported(line: str) -> None:
    scan = _scan(_lexora_880(record=f"approved\n{line}\n" + _resolved("msg-754")))
    assert scan.open_declarations == ("msg-751",)
    assert scan.malformed == ("msg-874",)


def test_the_resolution_line_is_not_itself_a_declaration() -> None:
    messages = [
        _m("msg-749", "human", "go\n\nNEXT: Bohr"),
        _m("msg-874", "operator", _resolved("msg-1") + "\n\nNEXT: Bohr"),
    ]
    scan = _scan(messages)
    assert not scan.declares_tier_c
    assert scan.unmatched == ("msg-1",)


# --------------------------------------------------------------------------- #
# Conductor: the relay names what closed; a recorder's line lets msg-880 through
# --------------------------------------------------------------------------- #


class _IdMcp(_FakeChatroomMcp):
    """The fake chatroom, seeded with production-shaped msg ids (``msg-N``)."""

    def seed_as(self, msg_id: str, *, author: str, content: str) -> None:
        self.seed(author=author, content=content)
        self._messages[-1]["msg_id"] = msg_id


def _seed_lexora(mcp: _IdMcp, *, record: str = "probe done, Takahito approved") -> None:
    for msg in _lexora_880(record=record):
        mcp.seed_as(msg["msg_id"], author=msg["author"], content=msg["content"])


def _relays(mcp: _FakeChatroomMcp) -> list[str]:
    return [m["content"] for m in mcp._messages if m["author"] == CONDUCTOR_RELAY_AUTHOR]


def _recording_conductor(mcp: Any, disp: Any, *, decider: Any) -> Conductor:
    return Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=_ROSTER,
        naysayer_identity="Einstein",
        tier_c_recorders=("operator",),
        control=_FakeControl(ControlState.RUN),
        decider=decider,
    )


@pytest.mark.anyio
async def test_msg_880_today_redirects_and_the_relay_names_g1_and_both_declarations(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mcp = _IdMcp()
    _seed_lexora(mcp)
    decider = _ClearingDecider()
    disp = _ScriptedDispatcher(mcp, {})
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.conductor.core"):
        outcome = await _conductor(
            mcp, disp, control=_FakeControl(ControlState.RUN), decider=decider
        ).run()
    assert Role.IMPLEMENTER not in [role for role, _ in disp.dispatches]
    assert outcome.stop_reason is StopReason.HUMAN
    assert decider.proceed_calls == 0
    (relay,) = _relays(mcp)
    assert "あるいは" not in relay.split("実装へ進める経路")[0]
    assert "G1 — 未解消の `TIER-C:` 宣言 msg-751, msg-754 (最後の human 投稿 msg-749 以降)" in relay
    assert "G3 — 未評価" in relay
    assert "G2 —" not in relay and "attest なし" not in relay
    assert any(
        "guard_i_redirect head=msg-880" in r.getMessage()
        and "closed_by=g1" in r.getMessage()
        and "g1:msg-751/msg-754" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.anyio
async def test_msg_880_with_the_operator_recording_both_resolutions_reaches_the_implementer() -> (
    None
):
    mcp = _IdMcp()
    record = "Takahito approved 2026-10-20\n" + _resolved("msg-751") + "\n" + _resolved("msg-754")
    _seed_lexora(mcp, record=record)
    decider = _ClearingDecider()
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["built\n\nNEXT: none"]})
    outcome = await _recording_conductor(mcp, disp, decider=decider).run()
    assert [role for role, _ in disp.dispatches] == [Role.IMPLEMENTER]
    assert decider.proceed_calls == 1  # G1 open → G3 consulted, no veto → HONOR
    assert outcome.stop_reason is StopReason.SETTLED


@pytest.mark.anyio
async def test_msg_880_with_one_resolution_names_only_the_remaining_declaration() -> None:
    mcp = _IdMcp()
    _seed_lexora(mcp, record="approved\n" + _resolved("msg-754"))
    disp = _ScriptedDispatcher(mcp, {})
    await _recording_conductor(mcp, disp, decider=_ClearingDecider()).run()
    (relay,) = _relays(mcp)
    assert "未解消の `TIER-C:` 宣言 msg-751 (" in relay
    assert "msg-754" not in relay.split("G1 —")[1].split("\n")[0]


@pytest.mark.anyio
async def test_g1_and_g2_closed_together_are_both_named() -> None:
    mcp = _IdMcp()
    _seed_lexora(mcp)
    mcp._messages[-1]["content"] = _attested("sound, build it\n\nNEXT: Heisenberg")
    disp = _ScriptedDispatcher(mcp, {})
    await _conductor(mcp, disp, control=_FakeControl(ControlState.RUN)).run()
    (relay,) = _relays(mcp)
    assert "G1 — 未解消" in relay
    assert "G2 — msg-880 の `NEXT:` 直上の行が `TIER-C-CHECK: none` ではありません" in relay
    assert "G3 — 未評価" in relay


@pytest.mark.anyio
async def test_g3_veto_alone_is_named_without_the_not_evaluated_line() -> None:
    mcp = _IdMcp()
    mcp.seed_as("msg-1", author="human", content="kickoff\n\nNEXT: Bohr")
    mcp.seed_as("msg-2", author="Bohr", content="design\n\nNEXT: Einstein")
    mcp.seed_as("msg-3", author="Einstein", content=_attested(_PROCEED_BODY))
    decider = _VerdictDecider(TierCVerdictKind.CONFIRMED, ask_score=0.7)
    disp = _ScriptedDispatcher(mcp, {})
    await _conductor(mcp, disp, control=_FakeControl(ControlState.RUN), decider=decider).run()
    (relay,) = _relays(mcp)
    assert "G3 — msg-3: Jev が人に聞くべきと判定 (p=0.70)" in relay
    assert "未評価" not in relay
    assert "G1 —" not in relay


@pytest.mark.anyio
async def test_the_relay_never_declares_or_resolves_a_tier_c_itself() -> None:
    # The relay mentions both grammars; neither may start a line, or it would latch / discharge G1.
    mcp = _IdMcp()
    _seed_lexora(mcp, record="approved\nTIER-C-RESOLVED: msg-751, msg-754 decided-by=human on=x")
    disp = _ScriptedDispatcher(mcp, {})
    await _recording_conductor(mcp, disp, decider=_ClearingDecider()).run()
    (relay,) = _relays(mcp)
    for line in relay.splitlines():
        assert not line.lstrip().upper().startswith(("TIER-C:", "TIER-C-RESOLVED")), line
    assert "書式に合わない解消記録を含む投稿: msg-874" in relay


def test_a_roster_persona_cannot_be_a_recorder() -> None:
    with pytest.raises(ValueError, match="tier_c_recorders must not name a roster persona"):
        Conductor(
            mcp=_FakeChatroomMcp(),
            dispatcher=_ScriptedDispatcher(_FakeChatroomMcp(), {}),
            thread_ref=_thread_ref(),
            roster=_ROSTER,
            naysayer_identity="Einstein",
            tier_c_recorders=("operator", "einstein"),
        )


# --------------------------------------------------------------------------- #
# Census (msg-7202 §2 (c), msg-7204 Step 0): read-only, the conductor's own rules
# --------------------------------------------------------------------------- #

_CENSUS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "guard_i_latch_census.py"


def _load_census() -> Any:
    spec = importlib.util.spec_from_file_location("_guard_i_census_test_module", _CENSUS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CENSUS = _load_census()


def test_census_at_msg_880_reports_g1_as_the_first_closed_condition() -> None:
    later = [_m("msg-881", CONDUCTOR_RELAY_AUTHOR, "x\n\nNEXT: Einstein")]
    row = _CENSUS.census_thread(
        "T-streaming-ledger-row",
        _lexora_880() + later,
        is_human=_is_human,
        recorders=(),
        naysayer="Einstein",
        until="msg-880",
    )
    assert row["head_msg_id"] == "msg-880"
    assert row["latched"] is True
    assert row["first_closed"] == "g1"
    assert row["head_closed"] == [{"condition": "g1", "evidence": ["msg-751", "msg-754"]}]
    assert row["last_human_msg_id"] == "msg-749"


def test_census_flags_a_thread_parked_on_human_after_a_guard_i_relay() -> None:
    from spirrow_mindwire.conductor.core import GUARD_I_REDIRECT_TITLE

    messages = [
        *_lexora_880(),
        _m("msg-881", CONDUCTOR_RELAY_AUTHOR, f"{GUARD_I_REDIRECT_TITLE} (x)\n\nNEXT: Einstein"),
        _m("msg-882", "Einstein", "why?\n\nTIER-C: unsure:goal?\nNEXT: human"),
    ]
    row = _CENSUS.census_thread(
        "T", messages, is_human=_is_human, recorders=(), naysayer="Einstein"
    )
    assert row["stopped"] is True
    assert row["guard_i_relays_since_human"] == ["msg-881"]
    assert row["open_declarations"] == ["msg-751", "msg-754", "msg-882"]


@pytest.mark.anyio
async def test_census_run_lists_only_latched_or_stopped_threads_and_records_read_errors() -> None:
    clean = [_m("msg-1", "human", "go\n\nNEXT: Bohr"), _m("msg-2", "Bohr", "d\n\nNEXT: Einstein")]

    class _Mcp:
        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            if name == "chatroom_list_threads":
                return {"items": [{"thread_id": t} for t in ("T-latched", "T-clean", "T-broken")]}
            if args["thread_id"] == "T-broken":
                raise RuntimeError("boom")
            return {"messages": _lexora_880() if args["thread_id"] == "T-latched" else clean}

    result = await _CENSUS.run_census(
        _Mcp(),
        projects=["spirrow-lexora"],
        threads=[],
        is_human=_is_human,
        recorders=(),
        naysayer="Einstein",
        until=None,
        emit_all=False,
        limit=1000,
    )
    assert [r["thread_id"] for r in result["rows"]] == ["T-latched"]
    assert result["errors"][0]["thread_id"] == "T-broken"
    assert result["counts"] == {"polled": 3, "latched": 1, "stopped": 0}
