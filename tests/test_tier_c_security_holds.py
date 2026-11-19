"""T-tier-c-six-articles-security-boundary — security holds read back from the decisions log.

msg-9665 §1.1⁷ (structural refusals each get their own hold; an identical body appends),
msg-9661 §1.1⁵ (one hold per request; released only after delivery), msg-9655 §1.1'' (semantic
hold on a Jev LIKELY_NOT of a security head; ``HOLD-RELEASE:`` by a human), msg-9650 §4
(``ESCALATED``).
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path

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


def test_escalated_releases_named_holds_and_is_logged(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    _admit(gate, _sec("A"), "m1")
    ok = _admit(gate, _sec("A", "SECURITY-BASIS: exposure src/a.py:3\n"), "m2")
    assert ok.rule == "RH-recheck"
    gate.escalated(author=A, decision=ok, thread=T, msg_id="m2", now=NOW, released_holds=["u1"])
    assert gate.holds(thread=T, author=A) == []
    row = next(r for r in iter_rows(gate.log_path) if r["kind"] == LogKind.ESCALATED.value)
    assert (row["label"], row["rule"], row["msg_id"], row["released_holds"]) == (
        "security",
        "RH-recheck",
        "m2",
        ["u1"],
    )


def test_escalated_without_release_keeps_the_hold(tmp_path: Path) -> None:
    """msg-9665 / msg-9659: a different matter reaching the human releases nothing."""
    gate = _gate(tmp_path)
    _admit(gate, _sec("A"), "m1")
    ok = _admit(gate, "TIER-C: goal\nNEXT: human\n", "m2")
    gate.escalated(author=A, decision=ok, thread=T, msg_id="m2", now=NOW)
    assert len(gate.holds(thread=T, author=A)) == 1


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
