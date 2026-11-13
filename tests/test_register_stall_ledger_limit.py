"""Register-StallLedgerTask.ps1 refuses a task limit that leaves no positive launcher time.

PR #576 gate rounds 3-4 (msg-9271, msg-9274): the launcher limit is the task limit minus a 30 s
margin, and the launcher rejects a limit that is not positive, so a task limit of 30 s or less
would register a task that exits at once on every tick. These tests run the script itself with
-DryRun (pwsh is required by .mindwire-gate, on CI and on the loop host). They run it on a copy
whose ExecutionTimeLimit is rewritten, with a fake checkout and a fake pythonw, so nothing is
registered.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "Register-StallLedgerTask.ps1"
_PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(_PWSH is None, reason="pwsh not installed")


def _dry_run(tmp_path: Path, limit: str) -> subprocess.CompletedProcess[str]:
    text = _SCRIPT.read_text(encoding="utf-8")
    pattern = r"^\$ExecutionTimeLimit = 'PT14M'$"
    assert len(re.findall(pattern, text, re.MULTILINE)) == 1
    script = tmp_path / "Register-StallLedgerTask.ps1"
    script.write_text(
        re.sub(pattern, f"$ExecutionTimeLimit = '{limit}'", text, flags=re.MULTILINE),
        encoding="utf-8",
    )
    checkout = tmp_path / "checkout"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "deploy" / "run-stall-ledger-tick.ps1").write_text("exit 0\n", encoding="utf-8")
    (checkout / "deploy" / "headless_launch.py").write_text("", encoding="utf-8")
    pythonw = tmp_path / "base python" / "pythonw.exe"
    pythonw.parent.mkdir()
    pythonw.write_text("", encoding="utf-8")
    assert _PWSH is not None
    return subprocess.run(
        [
            _PWSH,
            "-NoProfile",
            "-File",
            str(script),
            "-Checkout",
            str(checkout),
            "-Pythonw",
            str(pythonw),
            "-DryRun",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_a_limit_at_the_margin_is_refused_before_dry_run_output(tmp_path: Path) -> None:
    proc = _dry_run(tmp_path, "PT30S")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "leaves no launcher time" in out
    # Nothing that looks like a successful DryRun was printed.
    assert "--limit-seconds" not in proc.stdout
    assert "Execute" not in proc.stdout


def test_one_second_above_the_margin_is_accepted(tmp_path: Path) -> None:
    proc = _dry_run(tmp_path, "PT31S")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--limit-seconds 1 " in " ".join(proc.stdout.split())


def test_the_registered_limit_dry_runs_with_the_margin(tmp_path: Path) -> None:
    proc = _dry_run(tmp_path, "PT14M")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--limit-seconds 810 " in " ".join(proc.stdout.split())
