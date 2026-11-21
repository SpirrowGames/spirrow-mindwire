"""T-tier-c-six-articles-security-boundary — security holds read back from the decisions log.

msg-9665 §1.1⁷ (structural refusals each get their own hold; an identical body appends),
msg-9655 §1.1'' (semantic hold on a Jev LIKELY_NOT of a security head; ``HOLD-RELEASE:`` by a
human), msg-9650 §4 (``ESCALATED``). PR #595 gate: while held nothing passes (Obj-1) and only a
human releases a hold (Obj-2) until part 1b; a released hold absorbs no later refusal (Obj-3).
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_conductor_core import _thread_ref
from test_tierc_jev_bounce import LIKELY_NOT as LIKELY_NOT_RESULT
from test_tierc_jev_bounce import _bounces, _rows, _run
from test_tierc_jev_bounce import _Decider as _JevDecider
from test_tierc_jev_bounce import _gate as _jev_gate

from spirrow_mindwire.conductor.tierc_gate import TierCGate, render_bounce_body
from spirrow_mindwire.tier_c_admission_gate import (
    ADMIT_LABELS,
    AdmissionDecision,
    AdmissionVerdict,
    BounceReason,
    LogKind,
)
from spirrow_mindwire.tier_c_decisions_log import iter_rows
from spirrow_mindwire.tier_c_security_holds import hold_release_ids, open_holds

NOW = datetime(2026, 11, 20, tzinfo=UTC)
T = "T-x"
A = "Bohr"


def _gate(tmp_path: Path) -> TierCGate:
    counter = itertools.count(1)
    return TierCGate(log_path=tmp_path / "log.jsonl", uuid_factory=lambda: f"u{next(counter)}")


def _admit(
    gate: TierCGate, body: str, msg_id: str, *, released: tuple[str, ...] = ()
) -> AdmissionDecision:
    return gate.admit(
        body=body, author=A, thread=T, msg_id=msg_id, now=NOW, released_holds=released
    )


_PLAIN_GOAL = "plain\nTIER-C: goal\nNEXT: human\n"


def _sec(text: str, basis: str = "") -> str:
    return f"{text}\n{basis}TIER-C: security\nNEXT: human\n"


def test_structural_refusal_opens_a_hold_and_the_next_head_is_held(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    first = _admit(gate, _sec("A の漏洩"), "m1")
    assert first.verdict is AdmissionVerdict.BOUNCE
    (hold,) = gate.holds(thread=T, author=A)
    assert (hold.hold_id, hold.held_msg_id, hold.hold_kind) == ("u1", "m1", "structural")
    # a RETRY relabelled other: no longer force-admits
    second = _admit(gate, "RETRY: u1\nTIER-C: other: leak\nNEXT: human\n", "m2")
    assert second.bounce_reason is BounceReason.SECURITY_HOLD


def test_unrelated_structural_refusal_while_held_gets_its_own_hold(tmp_path: Path) -> None:
    """msg-9665 §6⁶: hold A open, B without a basis is refused, Jev is not asked, B gets its own
    structural hold and A stays."""
    gate = _gate(tmp_path)
    _admit(gate, _sec("A の漏洩"), "m1")
    _admit(gate, _sec("別件 B の漏洩"), "m2")
    holds = gate.holds(thread=T, author=A)
    assert [h.held_msg_id for h in holds] == ["m1", "m2"]


def test_identical_body_resent_appends_instead_of_a_new_hold(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    _admit(gate, _sec("B の漏洩"), "m1")
    _admit(gate, "RETRY: u1\r\n" + _sec("B  の漏洩").replace("\n", "\r\n"), "m2")
    (hold,) = gate.holds(thread=T, author=A)
    assert [h["msg_id"] for h in hold.history] == ["m2"]


def test_human_hold_release_releases_only_the_named_hold(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    _admit(gate, _sec("A"), "m1")
    _admit(gate, _sec("B"), "m2")
    assert [h.hold_id for h in gate.holds(thread=T, author=A, released_holds=["u1"])] == ["u2"]
    assert hold_release_ids("ok\nHOLD-RELEASE: u1\r\nHOLD-RELEASE: u2\n") == ["u1", "u2"]


def test_holds_are_per_thread_and_author(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    _admit(gate, _sec("A"), "m1")
    assert gate.holds(thread="T-other", author=A) == []
    assert gate.holds(thread=T, author="Einstein") == []


def test_a_supplemented_head_is_only_a_recheck_until_released(tmp_path: Path) -> None:
    """Part 1b (Bohr msg-9886 §2): while held, the supplement passes the label gate only as
    ``RH-recheck`` (the conductor's per-hold Jev check decides delivery) and the hold stays; once a
    human releases it, the same head is an ordinary admit."""
    gate = _gate(tmp_path)
    _admit(gate, _sec("A"), "m1")
    held = _admit(gate, _sec("A", "SECURITY-BASIS: exposure src/a.py:3\n"), "m2")
    assert held.verdict is AdmissionVerdict.ADMIT
    assert held.rule == "RH-recheck"
    assert [h.hold_id for h in gate.holds(thread=T, author=A)] == ["u1"]
    ok = _admit(gate, _sec("A", "SECURITY-BASIS: exposure src/a.py:3\n"), "m3", released=("u1",))
    assert ok.verdict is AdmissionVerdict.ADMIT
    assert ok.rule != "RH-recheck"


def test_escalated_releases_no_hold(tmp_path: Path) -> None:
    """PR #595 gate Obj-2 (fix_cut): ``ESCALATED`` carries no release; only a human does."""
    gate = _gate(tmp_path)
    _admit(gate, _sec("A"), "m1")
    other = TierCGate(log_path=gate.log_path, uuid_factory=lambda: "x")
    ok = other.admit(
        body="TIER-C: goal\nNEXT: human\n", author="Einstein", thread=T, msg_id="m2", now=NOW
    )
    other.escalated(author="Einstein", decision=ok, thread=T, msg_id="m2", now=NOW)
    row = next(r for r in iter_rows(gate.log_path) if r["kind"] == LogKind.ESCALATED.value)
    assert "released_holds" not in row
    assert [h.hold_id for h in gate.holds(thread=T, author=A)] == ["u1"]


def test_a_released_hold_does_not_absorb_a_later_identical_refusal(tmp_path: Path) -> None:
    """PR #595 gate Obj-3: u1 released by a human, the same body refused again → its own hold."""
    gate = _gate(tmp_path)
    _admit(gate, _sec("A"), "m1")
    _admit(gate, _sec("A"), "m2", released=("u1",))
    (hold,) = gate.holds(thread=T, author=A, released_holds=["u1"])
    assert (hold.hold_id, hold.held_msg_id, hold.history) == ("u2", "m2", [])


def test_releasing_a_hold_does_not_resurrect_its_earlier_duplicates(tmp_path: Path) -> None:
    """PR #595 gate round 2: u1 and its identical re-send u2 (before release) are one hold, and
    releasing u1 releases that one hold; the historical u2 does not come back as its own."""
    gate = _gate(tmp_path)
    _admit(gate, _sec("A"), "m1")
    _admit(gate, _sec("A"), "m2")
    (hold,) = gate.holds(thread=T, author=A)
    assert (hold.hold_id, [h["msg_id"] for h in hold.history]) == ("u1", ["m2"])
    assert gate.holds(thread=T, author=A, released_holds=["u1"]) == []
    # The next head after the release stamps it in the log and is not held.
    third = _admit(gate, "plain\nTIER-C: goal\nNEXT: human\n", "m3", released=("u1",))
    assert third.verdict is AdmissionVerdict.ADMIT
    assert gate.holds(thread=T, author=A, released_holds=["u1"]) == []


def test_duplicates_before_and_after_a_release_split_at_the_release(tmp_path: Path) -> None:
    """Refusals before the release stay in u1's history; one after it opens its own hold, and
    that stays true on every later replay."""
    gate = _gate(tmp_path)
    _admit(gate, _sec("A"), "m1")
    _admit(gate, _sec("A"), "m2")
    _admit(gate, _sec("A"), "m3", released=("u1",))
    for _ in range(2):
        (hold,) = gate.holds(thread=T, author=A, released_holds=["u1"])
        assert (hold.hold_id, hold.held_msg_id, hold.history) == ("u3", "m3", [])
    stamped = [r for r in iter_rows(tmp_path / "log.jsonl") if r.get("holds_released")]
    assert [(r["msg_id"], r["holds_released"]) for r in stamped] == [("m3", ["u1"])]


def test_a_plain_admit_persists_the_release_for_replay_without_the_thread_line(
    tmp_path: Path,
) -> None:
    """PR #595 gate round 3: a plain admit writes no decision row, so the release gets a
    ``HOLD_RELEASED`` row of its own; replay without resupplying the release keeps u1 released,
    and an identical refusal after it opens a hold of its own."""
    gate = _gate(tmp_path)
    _admit(gate, _sec("A"), "m1")
    _admit(gate, _sec("A"), "m2")
    plain = _admit(gate, _PLAIN_GOAL, "m3", released=("u1",))
    assert plain.verdict is AdmissionVerdict.ADMIT
    assert not plain.log_entries
    (row,) = [r for r in iter_rows(gate.log_path) if r.get("holds_released")]
    assert (row["kind"], row["msg_id"], row["author"], row["holds_released"]) == (
        LogKind.HOLD_RELEASED.value,
        "m3",
        A,
        ["u1"],
    )
    assert gate.holds(thread=T, author=A) == []
    # A second admit with the same thread line stamps nothing again.
    _admit(gate, _PLAIN_GOAL, "m4", released=("u1",))
    assert len([r for r in iter_rows(gate.log_path) if r.get("holds_released")]) == 1
    _admit(gate, _sec("A"), "m5")
    (hold,) = gate.holds(thread=T, author=A)
    assert (hold.hold_id, hold.held_msg_id, hold.history) == ("u5", "m5", [])


def test_jev_likely_not_on_security_opens_a_semantic_hold(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    body = _sec("x", "SECURITY-BASIS: widen 不明\n")
    gate.jev_bounce(
        author=A, label="security", ask_score=0.1, thread=T, msg_id="m1", now=NOW, body=body
    )
    (hold,) = gate.holds(thread=T, author=A)
    assert (hold.hold_kind, hold.held_basis) == ("semantic", "SECURITY-BASIS: widen 不明")


def test_jev_likely_not_on_other_labels_opens_no_hold(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    gate.jev_bounce(author=A, label="goal", ask_score=0.1, thread=T, msg_id="m1", now=NOW, body="x")
    assert gate.holds(thread=T, author=A) == []


def test_history_rows_append_to_named_holds(tmp_path: Path) -> None:
    """The row shape the hold-relation step writes (msg-9663 §1.1⁶ (a))."""
    import json

    gate = _gate(tmp_path)
    _admit(gate, _sec("A"), "m1")
    with gate.log_path.open("a", encoding="utf-8") as fh:
        row = {
            "kind": "BOUNCED",
            "thread": T,
            "msg_id": "m3",
            "author": A,
            "reason": "jev-likely-not",
            "retry_uuid": "u9",
            "hold_history": [
                {"hold_id": "u1", "relation": "supplemented", "outcome": "not-delivered"}
            ],
        }
        fh.write(json.dumps(row) + "\n")
    (hold,) = open_holds(gate.log_path, thread=T, author=A)
    assert hold.history == [
        {"hold_id": "u1", "relation": "supplemented", "outcome": "not-delivered", "msg_id": "m3"}
    ]


def test_bounce_notice_counts_the_admitted_labels(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    decision = _admit(gate, "NEXT: human\n", "m1")
    body = render_bounce_body(author=A, decision=decision, bounced_msg_id="m1")
    assert f"Tier-C の{len(ADMIT_LABELS)}種類" in body
    assert "4種類" not in body


@pytest.mark.anyio
@pytest.mark.parametrize("decider", [None, _JevDecider(LIKELY_NOT_RESULT)])
async def test_conductor_does_not_deliver_a_relabelled_head_while_held(
    tmp_path: Path, decider: _JevDecider | None
) -> None:
    """PR #595 gate Obj-1: Bohr's security request was refused for lack of a basis; the same
    request relabelled ``goal`` is bounced by the gate itself — with no decider at all, too —
    and no ``ESCALATED`` row is written."""
    gate = _jev_gate(tmp_path)
    gate.admit(
        body=_sec("A の漏洩"), author="Bohr", thread=_thread_ref().thread_id, msg_id="m0", now=NOW
    )
    _, _, mcp = await _run(gate, decider, head="A の件\n\nTIER-C: goal\nNEXT: human")
    (notice,) = _bounces(mcp)
    assert BounceReason.SECURITY_HOLD.value in notice["content"]
    assert LogKind.ESCALATED.value not in [r["kind"] for r in _rows(gate)]
