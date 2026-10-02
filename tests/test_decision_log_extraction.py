"""T-tier-c-admission-gate U4a — the conductor writes ``DECIDED:`` / ``DEFERRED:`` lines to the log.

Spec: Bohr msg-5655 (U4a: parser, runs in every gate mode, tests) and msg-5657 (shared
allowlist ``DECISION_LOG_AUTHOR_ROLES = {proposer, implementer}``, line-start grammar,
``unattributed_author``, ``author_role`` recorded, four added tests),
endorsed by Einstein msg-5656 / msg-5658.
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
from test_tierc_gate_enforce import _build

from spirrow_mindwire.conductor.core import Conductor
from spirrow_mindwire.conductor.gate_records import ADVISORY_SELF_TRIAGE_INSTRUCTION
from spirrow_mindwire.config import PathsConfig
from spirrow_mindwire.tier_c_admission_gate import LogKind
from spirrow_mindwire.tier_c_decisions_log import (
    DECISION_LOG_AUTHOR_ROLES,
    DecisionLine,
    decision_log_entries,
    has_decision_entries,
    scan_decision_lines,
)
from spirrow_mindwire.value_objects import Role

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

#: The shape of an advisory-only APPROVE relay (msg-5328): the instruction quotes the grammar
#: inline, inside backticks, on the line above ``NEXT:``.
RELAY_BODY = (
    "PR-gate (Tier B independent naysayer) — o/r#1 @ abc\n\nVERDICT: APPROVE (ci=success)\n\n"
    f"{ADVISORY_SELF_TRIAGE_INSTRUCTION}\n\nNEXT: Heisenberg"
)

IMPLEMENTER_HEAD = (
    "fixed the advisory\n\n"
    "DECIDED: advisory 1 (rename) — cheap, fixed in the same PR\n"
    "  DEFERRED: advisory 2 (split module) — unrelated to this PR's scope\n\n"
    "TIER-C: merge-protected\nNEXT: human"
)


# --------------------------------------------------------------------------- grammar (pure)


def test_allowlist_is_proposer_and_implementer() -> None:
    assert frozenset({Role.PROPOSER, Role.IMPLEMENTER}) == DECISION_LOG_AUTHOR_ROLES


def test_scan_reads_line_start_prefixes_with_leading_whitespace() -> None:
    scan = scan_decision_lines(IMPLEMENTER_HEAD)
    assert scan.lines == (
        DecisionLine(LogKind.DECIDED, "advisory 1 (rename)", "cheap, fixed in the same PR"),
        DecisionLine(LogKind.DEFERRED, "advisory 2 (split module)", "unrelated to this PR's scope"),
    )
    assert scan.malformed == ()


def test_scan_ignores_mid_sentence_backticks_quotes_and_fences() -> None:
    body = (
        "Bohr wrote DECIDED: x — y in the middle of a sentence.\n"
        "`DECIDED: quoted — in backticks`\n"
        "> DECIDED: block-quoted — by a reviewer\n"
        "```\n"
        "DECIDED: inside a fence — example only\n"
        "```\n"
        "decided: lower case — not the prefix\n"
    )
    scan = scan_decision_lines(body)
    assert scan.empty


def test_scan_splits_at_the_first_em_dash() -> None:
    (line,) = scan_decision_lines("DECIDED: a — b — c").lines
    assert (line.what, line.reason) == ("a", "b — c")


@pytest.mark.parametrize(
    "raw",
    [
        "DECIDED: no separator at all",
        "DECIDED: nothing after the dash —",
        "DECIDED: blank reason —   ",
        "DEFERRED: — no what",
        "DECIDED: ascii hyphen - is not the separator",
    ],
)
def test_malformed_lines_are_returned_not_dropped(raw: str) -> None:
    scan = scan_decision_lines(f"intro\n{raw}\nNEXT: human")
    assert scan.lines == ()
    assert scan.malformed == (raw.strip(),)
    assert not scan.empty


def test_relay_instruction_text_is_not_a_decision_line() -> None:
    """The pr-gate-relay advisory instruction quotes the grammar inline, inside backticks."""
    assert "DECIDED:" in RELAY_BODY
    assert scan_decision_lines(RELAY_BODY).empty


def test_entries_record_author_role_and_refuse_a_non_allowlisted_role() -> None:
    scan = scan_decision_lines(IMPLEMENTER_HEAD)
    entries = decision_log_entries(scan, author="Heisenberg", author_role=Role.IMPLEMENTER, now=NOW)
    assert [e.kind for e in entries] == [LogKind.DECIDED, LogKind.DEFERRED]
    assert dict(entries[0].payload) == {
        "ts": NOW.isoformat(),
        "author": "Heisenberg",
        "author_role": "implementer",
        "what": "advisory 1 (rename)",
        "reason": "cheap, fixed in the same PR",
    }
    with pytest.raises(ValueError, match="naysayer"):
        decision_log_entries(scan, author="Einstein", author_role=Role.NAYSAYER, now=NOW)


# --------------------------------------------------------------------------- conductor extraction


def _log(tmp_path: Path) -> Path:
    return tmp_path / "state" / "tier_c_decisions_log.jsonl"


def _rows(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


async def _run(
    log_path: Path | None, *, head: str, head_author: str
) -> tuple[Conductor, _FakeChatroomMcp]:
    """Bohr design → attested Einstein critique → the head under test. No gate (``mode = off``)."""
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    mcp.seed(author="Einstein", content=_attested("critique\n\nNEXT: Bohr"))
    mcp.seed(author=head_author, content=head)
    conductor = Conductor(
        mcp=mcp,
        dispatcher=_ScriptedDispatcher(mcp, {}),
        thread_ref=_thread_ref(),
        roster=ROSTER,
        naysayer_identity="Einstein",
        max_rounds=1,
        decisions_log_path=log_path,
    )
    await conductor.run()
    return conductor, mcp


@pytest.mark.anyio
async def test_implementer_lines_are_logged_one_entry_per_line_with_the_gate_off(
    tmp_path: Path,
) -> None:
    log_path = _log(tmp_path)
    conductor, _ = await _run(log_path, head=IMPLEMENTER_HEAD, head_author="Heisenberg")
    rows = _rows(log_path)
    head_id = "m3"  # the third seeded message
    assert [r["kind"] for r in rows] == ["DECIDED", "DEFERRED"]
    assert {r["author_role"] for r in rows} == {"implementer"}
    assert {r["author"] for r in rows} == {"Heisenberg"}
    assert {r["thread"] for r in rows} == {_thread_ref().thread_id}
    assert {r["msg_id"] for r in rows} == {head_id}
    assert conductor.decision_log_counts == {}


@pytest.mark.anyio
async def test_proposer_lines_are_logged_with_author_role_proposer(tmp_path: Path) -> None:
    log_path = _log(tmp_path)
    head = "plan\n\nDECIDED: split U4 into two PRs — each reviews on its own\n\nNEXT: Einstein"
    await _run(log_path, head=head, head_author="Bohr")
    rows = _rows(log_path)
    assert len(rows) == 1
    assert rows[0]["author_role"] == "proposer"
    assert rows[0]["what"] == "split U4 into two PRs"


@pytest.mark.anyio
async def test_naysayer_quoting_a_decision_produces_no_entry(tmp_path: Path) -> None:
    log_path = _log(tmp_path)
    head = _attested("Bohr's line reads:\nDECIDED: drop §2.7 — drift risk\n\nNEXT: Bohr")
    conductor, _ = await _run(log_path, head=head, head_author="Einstein")
    assert _rows(log_path) == []
    assert conductor.decision_log_counts == {}


@pytest.mark.anyio
async def test_relay_instruction_produces_no_entry_and_no_count(tmp_path: Path) -> None:
    log_path = _log(tmp_path)
    conductor, _ = await _run(log_path, head=RELAY_BODY, head_author="pr-gate-relay")
    assert _rows(log_path) == []
    assert conductor.decision_log_counts == {}


@pytest.mark.anyio
async def test_author_with_no_role_increments_unattributed_author(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log_path = _log(tmp_path)
    head = "note\n\nDECIDED: something — by an author outside the roster\n\nNEXT: human"
    with caplog.at_level(logging.WARNING, logger="spirrow_mindwire.conductor.core"):
        conductor, _ = await _run(log_path, head=head, head_author="pr-gate-relay")
    assert _rows(log_path) == []
    assert conductor.decision_log_counts == {"unattributed_author": 1}
    assert any("unattributed_author" in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_malformed_line_is_skipped_and_counted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log_path = _log(tmp_path)
    head = (
        "done\n\nDECIDED: kept the helper —\nDEFERRED: advisory 3 — out of scope\n\n"
        "TIER-C: merge-protected\nNEXT: human"
    )
    with caplog.at_level(logging.WARNING, logger="spirrow_mindwire.conductor.core"):
        conductor, _ = await _run(log_path, head=head, head_author="Heisenberg")
    rows = _rows(log_path)
    assert [r["kind"] for r in rows] == ["DEFERRED"]
    assert conductor.decision_log_counts == {"malformed": 1}
    assert any("malformed" in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_same_head_seen_twice_is_logged_once(tmp_path: Path) -> None:
    log_path = _log(tmp_path)
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content="design\n\nNEXT: Einstein")
    mcp.seed(author="Einstein", content=_attested("critique\n\nNEXT: Bohr"))
    mcp.seed(author="Heisenberg", content=IMPLEMENTER_HEAD)
    for _ in range(2):  # two ticks over the same stopped head
        await Conductor(
            mcp=mcp,
            dispatcher=_ScriptedDispatcher(mcp, {}),
            thread_ref=_thread_ref(),
            roster=ROSTER,
            naysayer_identity="Einstein",
            max_rounds=1,
            decisions_log_path=log_path,
        ).run()
    rows = _rows(log_path)
    assert len(rows) == 2
    assert has_decision_entries(log_path, thread=_thread_ref().thread_id, msg_id=rows[0]["msg_id"])


@pytest.mark.anyio
async def test_repeated_ticks_on_one_instance_count_and_warn_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """PR #418 gate advisory: a long-lived Conductor re-reading the same stopped head must not
    re-count or re-warn its malformed / unattributed lines on every tick."""
    log_path = _log(tmp_path)
    head = "done\n\nDECIDED: kept the helper —\n\nTIER-C: merge-protected\nNEXT: human"
    with caplog.at_level(logging.WARNING, logger="spirrow_mindwire.conductor.core"):
        conductor, _ = await _run(log_path, head=head, head_author="Heisenberg")
        await conductor.run()
        await conductor.run()
    assert conductor.decision_log_counts == {"malformed": 1}
    assert sum("malformed" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.anyio
async def test_failed_write_is_retried_on_the_next_tick(tmp_path: Path) -> None:
    blocker = tmp_path / "state"
    blocker.write_text("a file where the state directory should be", encoding="utf-8")
    log_path = blocker / "log.jsonl"
    conductor, _ = await _run(log_path, head=IMPLEMENTER_HEAD, head_author="Heisenberg")
    blocker.unlink()
    blocker.mkdir()
    await conductor.run()
    assert [r["kind"] for r in _rows(log_path)] == ["DECIDED", "DEFERRED"]


@pytest.mark.anyio
async def test_no_log_path_writes_nothing(tmp_path: Path) -> None:
    conductor, _ = await _run(None, head=IMPLEMENTER_HEAD, head_author="Heisenberg")
    assert list(tmp_path.iterdir()) == []
    assert conductor.decision_log_counts == {}


@pytest.mark.anyio
async def test_log_write_failure_does_not_stop_the_turn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "state"
    blocker.write_text("a file where the state directory should be", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="spirrow_mindwire.conductor.core"):
        await _run(blocker / "log.jsonl", head=IMPLEMENTER_HEAD, head_author="Heisenberg")
    assert any("failed" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- wiring


def test_build_conductor_wires_the_log_path_with_the_gate_off(tmp_path: Path) -> None:
    from test_loop_runner import _conductor_settings

    s = _conductor_settings().model_copy(update={"paths": PathsConfig(data_dir=tmp_path)})
    assert s.tierc_gate.mode == "off"
    cond = _build(s).conductor
    assert cond._tierc_gate is None
    assert cond._decisions_log_path == tmp_path / "state" / "tier_c_decisions_log.jsonl"
