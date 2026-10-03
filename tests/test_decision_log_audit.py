"""T-tier-c-admission-gate U4b — the decisions log compared with the message bodies.

Spec: Bohr msg-6244 / 6246 / 6248 / 6250 / 6252 / 6254 / 6256, endorsed by Einstein msg-6257.
Tests are numbered as the spec numbers them (msg-6246 tests 1-5, msg-6250 test 7,
msg-6252 test 6, msg-6254 test 4, msg-6256 tests 8-9). No pass/fail verdict exists (msg-6252),
so every test asserts counts.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from test_conductor_core import _ROSTER as ROSTER
from test_conductor_core import _attested, _FakeChatroomMcp, _ScriptedDispatcher, _thread_ref

from spirrow_mindwire.conductor.core import Conductor, roster_role
from spirrow_mindwire.decision_log_audit import ThreadAudit, audit_thread
from spirrow_mindwire.tier_c_decisions_log import DecisionKey, logged_decision_keys
from spirrow_mindwire.value_objects import Role

T = "T-audit"
LINE = "DECIDED: rename — cheap"
KEY_M1: DecisionKey = ("m1", "DECIDED", "rename", "cheap")


def _msg(msg_id: str, author: str, content: str) -> dict[str, Any]:
    return {"msg_id": msg_id, "author": author, "content": content}


def _audit(
    messages: list[dict[str, Any]],
    logged: Mapping[DecisionKey, int],
    *,
    roster: Mapping[str, Role] = ROSTER,
    status: str = "active",
) -> ThreadAudit:
    return audit_thread(
        thread=T, status=status, messages=messages, logged=Counter(logged), roster=roster
    )


def _nonzero(audit: ThreadAudit) -> dict[str, int]:
    return {k: v for k, v in audit.counts().items() if v and k not in ("expected", "logged")}


def test_resolver_is_the_conductors_case_insensitive_roster_lookup() -> None:
    """msg-6248: one resolver, shared with the conductor."""
    assert roster_role(ROSTER, "heisenberg") is Role.IMPLEMENTER
    assert roster_role(ROSTER, "pr-gate-relay") is None


def test_1_log_row_for_a_msg_id_not_in_the_thread_is_unmatched() -> None:
    audit = _audit([_msg("m1", "Heisenberg", "no decisions")], {("m9", "DECIDED", "x", "y"): 1})
    assert _nonzero(audit) == {"unmatched_log_row": 1}


def test_2_log_row_whose_message_lacks_the_line_is_unmatched() -> None:
    audit = _audit([_msg("m1", "Heisenberg", LINE)], {KEY_M1: 1, ("m1", "DECIDED", "a", "b"): 1})
    assert _nonzero(audit) == {"unmatched_log_row": 1}
    assert dict(audit.unmatched_log_row) == {("m1", "DECIDED", "a", "b"): 1}


def test_3_line_twice_in_a_message_logged_once_is_missing_one() -> None:
    audit = _audit([_msg("m1", "Heisenberg", f"{LINE}\n{LINE}")], {KEY_M1: 1})
    assert _nonzero(audit) == {"missing": 1}


def test_4_demoted_author_row_is_roster_changed_and_a_foreign_what_stays_unmatched() -> None:
    """msg-6254: m1's author (Einstein) is now naysayer; the second row's ``what`` is not in m1."""
    audit = _audit(
        [_msg("m1", "Einstein", LINE)],
        {KEY_M1: 1, ("m1", "DECIDED", "not in body", "r"): 1},
    )
    assert _nonzero(audit) == {"roster_changed": 1, "unmatched_log_row": 1}
    assert dict(audit.roster_changed) == {KEY_M1: 1}


def test_4b_author_who_left_the_roster_counts_as_demoted() -> None:
    """msg-6254: a resolved role of ``None`` is not allowed either."""
    audit = _audit([_msg("m1", "Gone", LINE)], {KEY_M1: 1})
    assert audit.counts()["roster_changed"] == 1
    assert audit.counts()["unmatched_log_row"] == 0
    assert dict(audit.unattributed_author) == {"m1": 1}


def test_5_exact_match_gives_all_zero_counts() -> None:
    audit = _audit(
        [
            _msg("m1", "Heisenberg", LINE),
            _msg("m2", "Bohr", "DEFERRED: split — later"),
            _msg("m3", "Einstein", "no decisions here"),
        ],
        {KEY_M1: 1, ("m2", "DEFERRED", "split", "later"): 1},
    )
    assert audit.clean
    assert _nonzero(audit) == {}
    assert audit.counts()["expected"] == 2


@pytest.mark.parametrize("status", ["active", "closed"])
def test_6_promoted_author_with_no_rows_is_missing_and_carries_thread_status(status: str) -> None:
    """msg-6250 / 6252: Einstein (naysayer when posting) is now an implementer; the log is empty."""
    roster = {**ROSTER, "Einstein": Role.IMPLEMENTER}
    audit = _audit([_msg("m1", "Einstein", LINE)], {}, roster=roster, status=status)
    assert _nonzero(audit) == {"missing": 1}
    (row,) = audit.to_json()["missing"]
    assert row["msg_id"] == "m1"
    assert row["thread_status"] == status


@pytest.mark.anyio
async def test_7_after_the_conductor_backfills_the_counts_are_zero(tmp_path: Path) -> None:
    """msg-6250 test 7: run the conductor's recorder once, then U4b — every count is zero."""
    log_path = tmp_path / "state" / "tier_c_decisions_log.jsonl"
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Bohr", content=f"design\n{LINE}\n\nNEXT: Einstein")
    mcp.seed(author="Einstein", content=_attested("critique\n\nNEXT: Bohr"))
    mcp.seed(author="Heisenberg", content="DEFERRED: split — later\n\nNEXT: Bohr")
    thread = _thread_ref().thread_id
    messages = list(mcp._messages)
    before = audit_thread(
        thread=thread,
        status="active",
        messages=messages,
        logged=logged_decision_keys(log_path, thread=thread),
        roster=ROSTER,
    )
    assert before.counts()["missing"] == 2
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
    after = audit_thread(
        thread=thread,
        status="active",
        messages=messages,
        logged=logged_decision_keys(log_path, thread=thread),
        roster=ROSTER,
    )
    assert after.clean
    assert after.counts()["logged"] == 2


def test_8_forged_duplicates_of_a_demoted_authors_line_are_bounded_by_the_body() -> None:
    """msg-6256: the line is in m1 once, the log holds 3 copies, m1's author is now naysayer."""
    audit = _audit([_msg("m1", "Einstein", LINE)], {KEY_M1: 3})
    assert _nonzero(audit) == {"roster_changed": 1, "unmatched_log_row": 2}


def test_9_surplus_copy_of_an_allowed_authors_line_is_unmatched() -> None:
    audit = _audit([_msg("m1", "Heisenberg", LINE)], {KEY_M1: 2})
    assert _nonzero(audit) == {"unmatched_log_row": 1}
    assert audit.counts()["missing"] == 0


def test_malformed_counts_only_for_allowed_authors_and_is_not_a_mismatch() -> None:
    audit = _audit(
        [
            _msg("m1", "Heisenberg", "DECIDED: ascii - hyphen"),
            _msg("m2", "Einstein", "DECIDED: quoted - by the naysayer"),
        ],
        {},
    )
    assert _nonzero(audit) == {"malformed": 1}
    assert dict(audit.malformed) == {"m1": 1}


def test_logged_counter_passed_in_is_not_consumed() -> None:
    logged: Counter[DecisionKey] = Counter({KEY_M1: 2})
    audit_thread(
        thread=T,
        status="active",
        messages=[_msg("m1", "Heisenberg", LINE)],
        logged=logged,
        roster=ROSTER,
    )
    assert logged == Counter({KEY_M1: 2})


# --------------------------------------------------------------------------- the scan script


def _script() -> Any:
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[1] / "scripts" / "tierc_acceptance_scan.py"
    spec = importlib.util.spec_from_file_location("tierc_acceptance_scan_module", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tierc_acceptance_scan_module"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_script_reports_unmeasured_not_zero_when_the_log_is_absent(tmp_path: Path) -> None:
    report = _script().scan([(T, "active", [])], log_path=tmp_path / "none.jsonl", roster=ROSTER)
    assert report["comparison"].startswith("unmeasured:")


def test_script_totals_and_lists_only_non_clean_threads(tmp_path: Path) -> None:
    import json

    log_path = tmp_path / "log.jsonl"
    rows = [
        {"kind": "DECIDED", "thread": "T-a", "msg_id": "m1", "what": "rename", "reason": "cheap"},
        {"kind": "DECIDED", "thread": "T-b", "msg_id": "m9", "what": "x", "reason": "y"},
        {"kind": "BOUNCED", "thread": "T-b", "author": "Bohr"},
    ]
    log_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    fetched = [
        ("T-a", "active", [_msg("m1", "Heisenberg", LINE)]),
        ("T-b", "closed", [_msg("m1", "Bohr", "DEFERRED: split — later")]),
    ]
    report = _script().scan(fetched, log_path=log_path, roster=ROSTER)
    assert report["threads_scanned"] == 2
    assert report["totals"]["missing"] == 1
    assert report["totals"]["unmatched_log_row"] == 1
    assert [t["thread"] for t in report["threads"]] == ["T-b"]
    assert report["threads"][0]["missing"][0]["thread_status"] == "closed"
    assert report["log_kinds"] == {"DECIDED": 2, "DEFERRED": 0, "BOUNCED": 1}
