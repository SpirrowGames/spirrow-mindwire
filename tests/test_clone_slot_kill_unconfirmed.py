"""Slot-mode conductor exit 7 -> ``clone_slot_session_unknown`` park: the Python reader's side.

T-ephemeral-impl-clone-slot-per-thread, Bohr msg-8246 (Obj-19-1), step 2b of msg-8499. The park,
the operator notice and the best-effort D-7 row are written by the wrapper
(``Get-SlotKillUnconfirmedPark`` / ``Complete-SlotThreadPark`` in
``deploy/run-conductor-scheduled.ps1``, tested by ``tests/Test-SlotKillUnconfirmedPark.ps1``).
These tests pin the other end: the row the wrapper writes reaches the thread's next implementer
through the existing reader, worded for an orphaned conductor tree, and the wrapper's constants
stay equal to the Python ones.
"""

from __future__ import annotations

import re
from pathlib import Path

from spirrow_mindwire.clone_retreat_log import (
    KILL_UNCONFIRMED_DETAIL,
    QUARANTINE_REASONS,
    SESSION_UNKNOWN_PARK_CODE,
    build_slot_quarantine_record,
    load_pending_notice,
    pending_record,
    render_slot_notice,
)
from spirrow_mindwire.clone_slot import PARK_SESSION_UNKNOWN

WRAPPER = Path(__file__).resolve().parent.parent / "deploy" / "run-conductor-scheduled.ps1"

# One row exactly as Complete-SlotThreadPark writes it (ConvertTo-Json -Compress, field order of
# its [ordered] table, LF-terminated).
WRAPPER_ROW = (
    '{"kind":"slot_quarantine","record_id":"T-demo@2026-11-16T08:00:00.000000+00:00#0123456789ab",'
    '"at":"2026-11-16T08:00:00.000000+00:00","thread_id":"T-demo","repo":"spirrow-mindwire",'
    '"slot_path":"C:\\\\slots\\\\spirrow-mindwire\\\\de9405c2c959","action":"parked",'
    '"quarantine_path":null,"quarantine_reason":"provision_failed",'
    '"park_code":"clone_slot_session_unknown","patch":null,"untracked":null,"bundle":null,'
    '"clone_seconds":null,"slot_bytes":null,"detail":"kill-unconfirmed (exit=7)"}\n'
)


def _wrapper_value(name: str) -> str:
    m = re.search(rf"^\${name} = '([^']*)'\s*$", WRAPPER.read_text(encoding="utf-8"), re.M)
    assert m, f"${name} not assigned in {WRAPPER.name}"
    return m.group(1)


def test_wrapper_constants_match_python() -> None:
    assert _wrapper_value("SlotKillUnconfirmedParkCode") == PARK_SESSION_UNKNOWN
    assert _wrapper_value("SlotKillUnconfirmedParkCode") == SESSION_UNKNOWN_PARK_CODE
    assert _wrapper_value("SlotKillUnconfirmedDetail") == KILL_UNCONFIRMED_DETAIL
    assert KILL_UNCONFIRMED_DETAIL == "kill-unconfirmed (exit=7)"


def test_wrapper_slot_park_codes_equal_clone_slot_park_constants() -> None:
    # Step 2c (msg-8499 / msg-8231): the wrapper's slot park-code registry is exactly the set of
    # clone_slot.PARK_* values, so a code added on one side reds the gate until the other follows.
    from spirrow_mindwire import clone_slot

    text = WRAPPER.read_text(encoding="utf-8")
    m = re.search(r"^\$SlotParkCodes = @\((.*?)^\)", text, re.M | re.S)
    assert m, "$SlotParkCodes not assigned in the wrapper"
    wrapper_codes = re.findall(r"'([^']+)'", m.group(1))
    assert len(wrapper_codes) == len(set(wrapper_codes))
    python_codes = {
        v for k, v in vars(clone_slot).items() if k.startswith("PARK_") and isinstance(v, str)
    }
    assert set(wrapper_codes) == python_codes


def test_wrapper_row_reason_is_a_known_d7_reason() -> None:
    assert "provision_failed" in QUARANTINE_REASONS


def test_wrapper_row_is_read_as_the_pending_notice(tmp_path: Path) -> None:
    log = tmp_path / "clone-retreats.jsonl"
    log.write_bytes(WRAPPER_ROW.encode("utf-8"))
    row = pending_record(log, "T-demo")
    assert row is not None and row["park_code"] == SESSION_UNKNOWN_PARK_CODE
    notice = load_pending_notice(log, "T-demo")
    assert notice is not None
    text = notice.text
    assert "park code: clone_slot_session_unknown" in text
    assert "detail: kill-unconfirmed (exit=7)" in text
    assert "left untouched at C:\\slots\\spirrow-mindwire\\de9405c2c959" in text
    assert "the slot was not moved or changed" in text
    assert "`mindwire-loop`" in text and "`claude`" in text and "then unpark" in text
    # Not the unreadable-session-list wording: `claude agents` did run fine here.
    assert "`claude agents --json --all` runs" not in text


def test_prep_session_unknown_park_keeps_its_wording() -> None:
    # msg-8234 判断 2's own park (the session list unreadable for 3 ticks) is unchanged.
    row = build_slot_quarantine_record(
        thread_id="T-demo",
        repo="spirrow-mindwire",
        slot_path=Path("C:/slots/x"),
        action="parked",
        quarantine_reason="provision_failed",
        park_code=SESSION_UNKNOWN_PARK_CODE,
        detail="`claude agents --json --all` could not be read for 3 ticks (boom)",
    )
    text = render_slot_notice(row)
    assert "`claude agents --json --all` runs and prints the session list" in text
    assert "mindwire-loop" not in text
