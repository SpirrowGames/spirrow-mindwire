"""T-decider-conductor-hook DECIDED 2e-1b — the Tier-C admission gate, enforced.

Spec: Bohr msg-5141 (2e-1b), msg-5143 (tests rewritten to T1-T4; ``build_retry_lookup`` under
enforce, ``never_retry`` would fail T1), Einstein msg-5142 / msg-5144, Takahito decide (operator
proxy) approving 2e-1a/b/c and 2e-2.

Scenario shape (``_run``): Bohr design → Einstein critique (attested, so the naysayer has been
consulted) → Bohr's ``NEXT: human`` head. ``_route`` stops that head at the human; under enforce
the gate decides whether it reaches the human or goes back to Bohr.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import _ROSTER as ROSTER
from test_conductor_core import _attested, _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref
from test_loop_runner import (
    _conductor_settings,
    _exec_caps,
    _FakeChatroom,
    _FakeMcp,
    _naysayer_caps,
    _proposer_caps,
    _StubAdapter,
)

from spirrow_mindwire.conductor import tierc_gate as tierc_gate_mod
from spirrow_mindwire.conductor.core import CONDUCTOR_RELAY_AUTHOR, Conductor, StopReason
from spirrow_mindwire.conductor.handoff import parse_next_token
from spirrow_mindwire.conductor.tierc_gate import (
    TIERC_BOUNCE_HEADER,
    TierCGate,
    bounced_msg_id,
    is_bounce_notice,
    render_bounce_body,
)
from spirrow_mindwire.config import (
    MindwireSettings,
    PathsConfig,
    TierCGateConfig,
    load_settings,
    resolve_tier_c_decisions_log_path,
)
from spirrow_mindwire.decider.hook import never_retry
from spirrow_mindwire.loop_runner import build_conductor
from spirrow_mindwire.tier_c_admission_gate import (
    AdmissionVerdict,
    BounceReason,
    LogKind,
    decide_admission,
)
from spirrow_mindwire.tier_c_decisions_log import append_log_entries
from spirrow_mindwire.value_objects import Role

UNLABELLED = "revised\n\nNEXT: human"
LABELLED = "revised\n\nTIER-C: merge-protected\nNEXT: human"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class _Uuids:
    """Deterministic bounce tokens u-1, u-2, ... so a scripted author can quote one back."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"u-{self.n}"


def _gate(tmp_path: Path) -> TierCGate:
    return TierCGate(
        log_path=tmp_path / "state" / "tier_c_decisions_log.jsonl", uuid_factory=_Uuids()
    )


def _rows(gate: TierCGate) -> list[dict[str, Any]]:
    if not gate.log_path.exists():
        return []
    return [json.loads(line) for line in gate.log_path.read_text(encoding="utf-8").splitlines()]


async def _run(
    gate: TierCGate | None,
    *,
    head: str = UNLABELLED,
    head_author: str = "Bohr",
    proposer_replies: list[str] | None = None,
    naysayer_replies: list[str] | None = None,
    max_rounds: int = 12,
) -> tuple[Any, _ScriptedDispatcher, _FakeChatroomMcp]:
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    mcp.seed(author="Einstein", content=_attested("critique\n\nNEXT: Bohr"))
    mcp.seed(author=head_author, content=head)
    disp = _ScriptedDispatcher(
        mcp,
        {
            Role.PROPOSER: list(proposer_replies or []),
            Role.NAYSAYER: list(naysayer_replies or []),
        },
    )
    conductor = Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=ROSTER,
        naysayer_identity="Einstein",
        max_rounds=max_rounds,
        tierc_gate=gate,
    )
    return await conductor.run(), disp, mcp


def _bounce_posts(mcp: _FakeChatroomMcp) -> list[dict[str, Any]]:
    return [
        p
        for p in mcp.posts
        if p["author"] == CONDUCTOR_RELAY_AUTHOR and is_bounce_notice(p["content"])
    ]


# --------------------------------------------------------------------------- bounce basics


@pytest.mark.anyio
async def test_unlabelled_human_is_bounced_to_its_author_and_logged(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    outcome, disp, mcp = await _run(gate)
    bounces = _bounce_posts(mcp)
    assert len(bounces) == 1
    body = bounces[0]["content"]
    assert parse_next_token(body) == "Bohr"
    assert "RETRY: u-1" in body.splitlines()
    assert BounceReason.NO_LABEL.value in body
    # Bohr was dispatched on the notice itself, then stayed silent → NO_PROGRESS on the notice.
    assert disp.dispatches[-1][0] is Role.PROPOSER
    assert outcome.stop_reason is StopReason.NO_PROGRESS
    rows = _rows(gate)
    assert [r["kind"] for r in rows] == [LogKind.BOUNCED.value]
    assert rows[0]["author"] == "Bohr"
    assert rows[0]["retry_uuid"] == "u-1"
    assert rows[0]["thread"] == "T-cond"
    assert rows[0]["msg_id"] == "m3"


# --------------------------------------------------------------------------- T1-T4 (msg-5143)


@pytest.mark.anyio
async def test_t1_retry_with_the_bounce_uuid_reaches_the_human(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    outcome, _, mcp = await _run(gate, proposer_replies=["still mine\n\nRETRY: u-1\nNEXT: human"])
    assert outcome.stop_reason is StopReason.HUMAN
    assert len(_bounce_posts(mcp)) == 1
    kinds = [r["kind"] for r in _rows(gate)]
    assert kinds == [LogKind.BOUNCED.value, LogKind.RETRY_ADMIT.value]
    assert _rows(gate)[1]["reason"] == "second_time_force_admit"


def test_t1_fails_under_never_retry() -> None:
    """msg-5143: ``never_retry`` would refuse every RETRY — the reason enforce must not use it."""
    body = "still mine\n\nRETRY: u-1\nNEXT: human"
    d = decide_admission(
        body=body, author="Bohr", retry_lookup=never_retry, now=NOW, bounce_uuid="u-2"
    )
    assert d.verdict is AdmissionVerdict.BOUNCE


@pytest.mark.anyio
async def test_t2_new_unlabelled_human_without_retry_is_bounced_again(tmp_path: Path) -> None:
    """Einstein msg-5142: a prior bounce gives the author no standing pass."""
    gate = _gate(tmp_path)
    outcome, _, mcp = await _run(
        gate,
        proposer_replies=[
            "decided it myself\n\nNEXT: Einstein",
            "another question\n\nNEXT: human",
        ],
        naysayer_replies=[_attested("fine\n\nNEXT: Bohr")],
    )
    assert len(_bounce_posts(mcp)) == 2
    assert [r["kind"] for r in _rows(gate)] == [LogKind.BOUNCED.value] * 2
    assert [r["retry_uuid"] for r in _rows(gate)] == ["u-1", "u-2"]
    assert outcome.stop_reason is StopReason.NO_PROGRESS


@pytest.mark.anyio
async def test_t3_a_redeemed_uuid_does_not_admit_twice(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    # Round 1: u-1 bounced and redeemed (RETRY_ADMIT) — written as the gate itself would.
    first = gate.admit(body=UNLABELLED, author="Bohr", thread="T-cond", msg_id="m0", now=NOW)
    assert first.verdict is AdmissionVerdict.BOUNCE
    redeemed = gate.admit(
        body="RETRY: u-1\nNEXT: human", author="Bohr", thread="T-cond", msg_id="m0b", now=NOW
    )
    assert redeemed.verdict is AdmissionVerdict.ADMIT
    # Later: the same token, no label → bounced.
    outcome, _, mcp = await _run(gate, head="again\n\nRETRY: u-1\nNEXT: human")
    assert len(_bounce_posts(mcp)) == 1
    assert _rows(gate)[-1]["kind"] == LogKind.BOUNCED.value
    assert outcome.stop_reason is StopReason.NO_PROGRESS


@pytest.mark.anyio
@pytest.mark.parametrize("token", ["u-1", "u-unknown"])
async def test_t4_other_authors_or_unknown_uuid_is_label_checked(
    tmp_path: Path, token: str
) -> None:
    gate = _gate(tmp_path)
    # u-1 belongs to Heisenberg, not Bohr.
    other = gate.admit(body=UNLABELLED, author="Heisenberg", thread="T-x", msg_id="h1", now=NOW)
    assert other.verdict is AdmissionVerdict.BOUNCE
    outcome, _, mcp = await _run(gate, head=f"mine\n\nRETRY: {token}\nNEXT: human")
    assert len(_bounce_posts(mcp)) == 1
    last = _rows(gate)[-1]
    assert last["kind"] == LogKind.BOUNCED.value
    assert last["author"] == "Bohr"
    assert outcome.stop_reason is StopReason.NO_PROGRESS


# --------------------------------------------------------------------------- unchanged paths


@pytest.mark.anyio
async def test_gate_exception_fails_open_to_the_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    baseline, bdisp, bmcp = await _run(None)

    def boom(**_: Any) -> Any:
        raise RuntimeError("gate down")

    monkeypatch.setattr(tierc_gate_mod, "decide_admission", boom)
    with caplog.at_level(logging.WARNING):
        outcome, disp, mcp = await _run(_gate(tmp_path))
    assert outcome == baseline
    assert outcome.stop_reason is StopReason.HUMAN
    assert disp.dispatches == bdisp.dispatches
    assert mcp.posts == bmcp.posts
    assert "fail-open" in caplog.text


@pytest.mark.anyio
async def test_log_write_failure_fails_open_and_posts_no_bounce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(tierc_gate_mod, "append_log_entries", boom)
    outcome, _, mcp = await _run(_gate(tmp_path))
    assert outcome.stop_reason is StopReason.HUMAN
    assert _bounce_posts(mcp) == []


@pytest.mark.anyio
async def test_correct_label_is_unchanged(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    baseline = await _run(None, head=LABELLED)
    enforced = await _run(gate, head=LABELLED)
    assert enforced[0] == baseline[0]
    assert enforced[0].stop_reason is StopReason.HUMAN
    assert enforced[1].dispatches == baseline[1].dispatches
    assert enforced[2].posts == baseline[2].posts
    assert _rows(gate) == []  # a plain admit writes nothing


@pytest.mark.anyio
@pytest.mark.parametrize("author", ["pr-gate-relay", CONDUCTOR_RELAY_AUTHOR, "operator", "human"])
async def test_relay_operator_and_human_are_unchanged(tmp_path: Path, author: str) -> None:
    gate = _gate(tmp_path)
    baseline = await _run(None, head=UNLABELLED, head_author=author)
    enforced = await _run(gate, head=UNLABELLED, head_author=author)
    assert enforced[0] == baseline[0]
    assert enforced[1].dispatches == baseline[1].dispatches
    assert enforced[2].posts == baseline[2].posts
    assert _rows(gate) == []


@pytest.mark.anyio
async def test_off_is_byte_for_byte_and_writes_nothing(tmp_path: Path) -> None:
    outcome, _, mcp = await _run(None)
    assert outcome.stop_reason is StopReason.HUMAN
    assert _bounce_posts(mcp) == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.anyio
async def test_forced_naysayer_consult_is_not_gated(tmp_path: Path) -> None:
    """Only a turn ``_route`` stopped at the human is gated; the forced consult runs first."""
    gate = _gate(tmp_path)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content=UNLABELLED)  # no naysayer yet → forced consult
    disp = _ScriptedDispatcher(mcp, {Role.NAYSAYER: [_attested("ok\n\nTIER-C: goal\nNEXT: human")]})
    conductor = Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=ROSTER,
        naysayer_identity="Einstein",
        tierc_gate=gate,
    )
    outcome = await conductor.run()
    assert disp.dispatches[0][0] is Role.NAYSAYER
    assert outcome.stop_reason is StopReason.HUMAN
    assert _bounce_posts(mcp) == []


@pytest.mark.anyio
async def test_retry_after_bounce_does_not_force_a_second_consult(tmp_path: Path) -> None:
    """A bounced ``NEXT: human`` never reached the human, so it is not a segment boundary."""
    gate = _gate(tmp_path)
    outcome, disp, _ = await _run(
        gate, proposer_replies=["relabelled\n\nTIER-C: goal\nNEXT: human"]
    )
    assert outcome.stop_reason is StopReason.HUMAN
    assert all(role is not Role.NAYSAYER for role, _ in disp.dispatches)


@pytest.mark.anyio
async def test_notice_that_does_not_land_stops_at_the_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_id(self: Conductor, body: str) -> dict[str, Any]:
        return {"msg_id": "", "author": CONDUCTOR_RELAY_AUTHOR, "content": body}

    monkeypatch.setattr(Conductor, "_post_as_conductor_relay", no_id)
    outcome, disp, _ = await _run(_gate(tmp_path))
    assert outcome.stop_reason is StopReason.HUMAN
    assert all(role is not Role.PROPOSER for role, _ in disp.dispatches)


@pytest.mark.anyio
async def test_implementer_author_is_dispatched_directly_not_through_guard_i(
    tmp_path: Path,
) -> None:
    gate = _gate(tmp_path)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_attested("critique\n\nNEXT: Heisenberg"))
    mcp.seed(author="Heisenberg", content="done?\n\nNEXT: human")
    disp = _ScriptedDispatcher(
        mcp, {Role.IMPLEMENTER: ["PR up\n\nTIER-C: merge-protected\nNEXT: human"]}
    )
    conductor = Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=ROSTER,
        naysayer_identity="Einstein",
        tierc_gate=gate,
    )
    outcome = await conductor.run()
    assert disp.dispatches[0][0] is Role.IMPLEMENTER
    assert outcome.stop_reason is StopReason.HUMAN
    assert len(_bounce_posts(mcp)) == 1


# --------------------------------------------------------------------------- notice body


def test_notice_parse_is_hijack_safe() -> None:
    d = decide_admission(
        body=UNLABELLED, author="Bohr", retry_lookup=never_retry, now=NOW, bounce_uuid="u-9"
    )
    body = render_bounce_body(author="Bohr", decision=d, bounced_msg_id="m3")
    assert bounced_msg_id(body) == "m3"
    assert body.startswith(TIERC_BOUNCE_HEADER)
    assert parse_next_token(body) == "Bohr"
    next_lines = [ln for ln in body.splitlines() if ln.startswith("NEXT:")]
    assert next_lines == ["NEXT: Bohr"]
    # No whole-line TIER-C: declaration (the G1 latch reads those).
    assert not any(ln.strip().upper().startswith("TIER-C:") for ln in body.splitlines())


# --------------------------------------------------------------------------- hook + config


@pytest.mark.anyio
async def test_decider_hook_sees_the_live_retry_store_under_enforce(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    states: list[Any] = []

    class _Spy:
        tierc_mode = "shadow"

        def is_target(self, state: Any) -> bool:
            states.append(state)
            return False

    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    mcp.seed(author="Einstein", content=_attested("critique\n\nNEXT: Bohr"))
    mcp.seed(author="Bohr", content=UNLABELLED)
    disp = _ScriptedDispatcher(mcp, {Role.PROPOSER: ["RETRY: u-1\nNEXT: human"]})
    conductor = Conductor(
        mcp=mcp,
        dispatcher=disp,
        thread_ref=_thread_ref(),
        roster=ROSTER,
        naysayer_identity="Einstein",
        decider=_Spy(),  # type: ignore[arg-type]
        tierc_gate=gate,
    )
    await conductor.run()
    verdicts = [s.gate_result.verdict for s in states]
    assert verdicts == [AdmissionVerdict.BOUNCE, AdmissionVerdict.ADMIT]
    assert states[1].gate_result.kind is LogKind.RETRY_ADMIT


def test_tierc_gate_config_defaults_off_and_accepts_enforce(tmp_path: Path) -> None:
    assert MindwireSettings().tierc_gate.mode == "off"
    assert TierCGateConfig(mode="enforce").mode == "enforce"
    cfg = tmp_path / "mindwire.toml"
    cfg.write_text('[tierc_gate]\nmode = "enforce"\n', encoding="utf-8")
    assert load_settings(cfg).tierc_gate.mode == "enforce"
    with pytest.raises(ValueError):
        TierCGateConfig(mode="bounce")  # type: ignore[arg-type]


def test_decisions_log_path_is_under_state(tmp_path: Path) -> None:
    s = MindwireSettings(paths=PathsConfig(data_dir=tmp_path))
    assert resolve_tier_c_decisions_log_path(s) == tmp_path / "state" / "tier_c_decisions_log.jsonl"


def _build(settings: MindwireSettings) -> Any:
    return build_conductor(
        settings,
        mcp=_FakeMcp(_FakeChatroom()),
        proposer=_StubAdapter("p", _proposer_caps()),
        implementer=_StubAdapter("i", _exec_caps()),
        naysayer=_StubAdapter("n", _naysayer_caps()),
    )


def test_build_conductor_logs_both_startup_lines_off(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MINDWIRE_DECIDER_BACKEND", raising=False)
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.loop_runner"):
        cond = _build(_conductor_settings())
    assert cond.conductor._tierc_gate is None
    msgs = [r.getMessage() for r in caplog.records]
    assert "tierc_gate: off" in msgs
    assert "decider: backend=off tierc=shadow questions=tierc-v2 built=no" in msgs


def test_build_conductor_wires_enforce_and_logs_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    s = _conductor_settings()
    s = s.model_copy(
        update={
            "paths": PathsConfig(data_dir=tmp_path),
            "tierc_gate": TierCGateConfig(mode="enforce"),
        }
    )
    with caplog.at_level(logging.INFO, logger="spirrow_mindwire.loop_runner"):
        cond = _build(s)
    gate = cond.conductor._tierc_gate
    assert isinstance(gate, TierCGate)
    assert gate.log_path == tmp_path / "state" / "tier_c_decisions_log.jsonl"
    assert any(r.getMessage().startswith("tierc_gate: enforce") for r in caplog.records)


def test_log_rows_round_trip_through_retry_lookup(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    d = decide_admission(
        body=UNLABELLED, author="Bohr", retry_lookup=never_retry, now=NOW, bounce_uuid="u-7"
    )
    append_log_entries(gate.log_path, d.log_entries, thread="T", msg_id="m")
    assert gate.retry_lookup("u-7", "Bohr") is True
    assert gate.retry_lookup("u-7", "Einstein") is False


# --------------------------------------------------------------------------- 2e-1a measurement


def _load_measure() -> Any:
    import importlib.util
    import sys

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "tierc_gate_measure_module", root / "scripts" / "tierc_gate_measure.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tierc_gate_measure_module"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_measure_buckets_counts_and_ids_only() -> None:
    measure = _load_measure()
    raw = measure.builder.RawMessage

    def m(i: int, author: str, content: str, day: int = 20) -> Any:
        return raw(
            msg_id=f"msg-{i}",
            author=author,
            content=content,
            timestamp=datetime(2026, 9, day, 12, i, tzinfo=UTC),
            role=None,
            next_participant=None,
        )

    secret = "SECRET-BODY-TEXT"
    msgs = [
        m(1, "Bohr", f"{secret}\n\nNEXT: Einstein"),
        m(2, "Bohr", f"{secret}\n\nNEXT: human"),  # i (no-label)
        m(3, "Einstein", f"{secret}\n\nTIER-C: other: x\nNEXT: human"),  # i (other)
        m(4, "Heisenberg", f"{secret}\n\nTIER-C: billing\nNEXT: human"),  # ii
        m(5, "Bohr", f"{secret}\n\nTIER-C: goal\nNEXT: human"),  # iii
        m(6, "Bohr", f"{secret}\n\nTIER-C: unsure:goal?\nNEXT: human"),  # iii + unsure
        m(7, "pr-gate-relay", f"{secret}\n\nNEXT: human"),  # iv
        m(8, "operator", f"{secret}\n\nNEXT: human"),  # other_author
        m(9, "Bohr", f"{secret}\n\nNEXT: human", day=1),  # out of window
    ]
    out = measure.classify(
        [("spirrow-mindwire", "T-x", msgs)],
        dict(ROSTER),
        since=datetime(2026, 9, 16, tzinfo=UTC),
        until=datetime(2026, 9, 30, tzinfo=UTC),
    )
    assert out["counts"] == {
        "i_bounce": 2,
        "ii_migrated": 1,
        "iii_admitted": 2,
        "iii_unsure": 1,
        "iv_pr_gate_relay": 1,
        "other_author": 1,
        "role_total": 5,
    }
    assert out["i_bounce_by_reason"] == {"no-label": 1, "other-not-admitted": 1}
    assert out["msg_ids"]["i_bounce"] == [
        "spirrow-mindwire/T-x#msg-2",
        "spirrow-mindwire/T-x#msg-3",
    ]
    assert secret not in json.dumps(out)
    assert "x" not in out["i_bounce_by_reason"]


# --------------------------------------------------------------------------- #398 advisory


def _consulted(msgs: list[tuple[str, str]]) -> bool:
    mcp = _FakeChatroomMcp()
    for author, content in msgs:
        mcp.seed(author=author, content=content)
    conductor = Conductor(
        mcp=mcp,
        dispatcher=_ScriptedDispatcher(mcp, {}),
        thread_ref=_thread_ref(),
        roster=ROSTER,
        naysayer_identity="Einstein",
    )
    thread = mcp._messages  # the seeded dicts, ids m1..mN
    return conductor._naysayer_consulted(thread)


def _notice_for(msg_id: str) -> str:
    d = decide_admission(
        body=UNLABELLED, author="Bohr", retry_lookup=never_retry, now=NOW, bounce_uuid="u-1"
    )
    return render_bounce_body(author="Bohr", decision=d, bounced_msg_id=msg_id)


def test_bounced_human_is_not_a_boundary_even_with_an_interleaved_post() -> None:
    """PR-gate advisory on #398: a post between the ``NEXT: human`` and its notice."""
    msgs = [
        ("Bohr", "design\n\nNEXT: Einstein"),
        ("Einstein", _attested("critique\n\nNEXT: Bohr")),
        ("Bohr", UNLABELLED),  # m3, bounced
        ("operator", "fyi: unrelated note"),  # m4, interleaved
        (CONDUCTOR_RELAY_AUTHOR, _notice_for("m3")),  # m5
        ("Bohr", "relabelled\n\nTIER-C: goal\nNEXT: human"),  # m6, the head
    ]
    assert _consulted(msgs) is True


def test_a_notice_naming_another_message_does_not_unbound_this_one() -> None:
    msgs = [
        ("Bohr", "design\n\nNEXT: Einstein"),
        ("Einstein", _attested("critique\n\nNEXT: Bohr")),
        ("Bohr", UNLABELLED),  # m3 — reached the human (no notice names it)
        (CONDUCTOR_RELAY_AUTHOR, _notice_for("m99")),
        ("Bohr", "next\n\nTIER-C: goal\nNEXT: human"),
    ]
    assert _consulted(msgs) is False


def test_a_role_quoting_a_notice_does_not_mark_a_bounce() -> None:
    msgs = [
        ("Bohr", "design\n\nNEXT: Einstein"),
        ("Einstein", _attested("critique\n\nNEXT: Bohr")),
        ("Bohr", UNLABELLED),  # m3
        ("Heisenberg", _notice_for("m3")),  # not conductor-relay
        ("Bohr", "next\n\nTIER-C: goal\nNEXT: human"),
    ]
    assert _consulted(msgs) is False
