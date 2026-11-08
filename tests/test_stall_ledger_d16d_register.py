"""D-16d-0: ``Register-StallLedgerTask.ps1`` reports success only after reading the task back.

Spec: T-stalled-pr-has-no-detector msg-8359 §1 as revised by msg-8361 Obj-1-3. Measured in
msg-7374: with a task already present, ``Register-ScheduledTask`` failed ("already exists"),
the script did not stop (a CDXML function does not see the script's
``$ErrorActionPreference``) and printed ``registered``. The fix:

* ``-Force -ErrorAction Stop`` on the registration, so this script's definition replaces any
  existing one;
* success is decided by a ``Get-ScheduledTask`` read-back of Execute / Arguments /
  RepetitionInterval / ExecutionTimeLimit -- never by whether the registration threw;
* the only success line is ``verified ...``, printed after that read-back; a missing task or a
  mismatch throws.

The CI runner is Linux (no ScheduledTasks module), so these read the script as text.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "Register-StallLedgerTask.ps1"


def _code() -> list[str]:
    text = SCRIPT.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def _lines_matching(pattern: str) -> list[int]:
    return [i for i, line in enumerate(_code()) if re.search(pattern, line)]


def _register_call() -> str:
    """The Register-ScheduledTask invocation, backtick continuations joined."""
    code = _code()
    (start,) = _lines_matching(r"^\s*Register-ScheduledTask\b")
    parts = [code[start]]
    i = start
    while parts[-1].rstrip().endswith(("`", "|")):
        i += 1
        parts.append(code[i])
    return " ".join(parts)


def test_registration_replaces_and_surfaces_errors() -> None:
    call = _register_call()
    assert re.search(r"(^|\s)-Force(\s|$)", call)
    assert re.search(r"-ErrorAction\s+Stop\b", call)


def test_registration_failure_does_not_decide_the_outcome() -> None:
    # The registration is wrapped so that a throw falls through to the read-back.
    code = _code()
    (reg,) = _lines_matching(r"^\s*Register-ScheduledTask\b")
    trys = [i for i in _lines_matching(r"^\s*try\s*\{") if i < reg]
    assert trys, "Register-ScheduledTask must sit inside a try block"
    catches = [i for i in _lines_matching(r"\}\s*catch\b") if i > reg]
    assert catches, "a failed registration must be caught, not end the script"
    catch_body = "\n".join(code[catches[0] : catches[0] + 4])
    assert "throw" not in catch_body


def test_read_back_checks_the_four_fields() -> None:
    text = "\n".join(_code())
    assert re.search(r"Get-ScheduledTask\s+-TaskName\s+\$TaskName", text)
    for field, source in [
        ("Execute", r"\$task\.Actions\[0\]\.Execute"),
        ("Arguments", r"\$task\.Actions\[0\]\.Arguments"),
        ("RepetitionInterval", r"\$task\.Triggers\[0\]\.Repetition\.Interval"),
        ("ExecutionTimeLimit", r"\$task\.Settings\.ExecutionTimeLimit"),
    ]:
        assert re.search(rf"{field}\s*=\s*\[string\]{source}", text), field
    for field, intended in [
        ("Execute", r"\$pwshPath"),
        ("Arguments", r"\$argString"),
        ("RepetitionInterval", r"\$HeartbeatInterval"),
        ("ExecutionTimeLimit", r"\$ExecutionTimeLimit"),
    ]:
        assert re.search(rf"^\s*{field}\s*=\s*{intended}\s*$", text, re.MULTILINE), field
    # A missing task and a mismatch both refuse.
    assert re.search(r"if\s*\(\s*-not\s+\$task\s*\)", text)
    assert re.search(r"throw\s+\"task \$TaskName does not exist", text)
    assert re.search(r"throw\s+\"task \$TaskName does not match", text)


def test_the_only_success_line_comes_after_the_read_back() -> None:
    # No "registered" claim survives anywhere in the code.
    assert not _lines_matching(r"Write-Output\s+\"registered\b")
    success = _lines_matching(r"Write-Output\s+\"verified\s+\$TaskName\b")
    assert len(success) == 1
    (read_back,) = _lines_matching(r"Get-ScheduledTask\s+-TaskName")
    (mismatch,) = _lines_matching(r"throw\s+\"task \$TaskName does not match")
    assert read_back < mismatch < success[0]
    # Nothing else writes to the output stream after DryRun's early return.
    outputs = _lines_matching(r"^\s*Write-Output\b")
    assert outputs == success
