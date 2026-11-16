"""deploy/headless_launch.py: argument parsing, exit-code pass-through, and the time limit.

Thread: T-loop-host-console-windows-steal-focus (PR #576 gate msg-9268). The scheduled-task
behaviour on Windows (no window, the limit really stops a hung wrapper, LastTaskResult) is
measured by tests/Test-StallLedgerTaskHeadless.ps1 on the loop host; these tests pin the launcher's
own logic and run on any OS.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

_PATH = Path(__file__).resolve().parents[1] / "deploy" / "headless_launch.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("headless_launch", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hl = _load()
# CREATE_NO_WINDOW is Windows only; elsewhere Popen refuses non-zero creationflags.
_FLAGS = 0x08000000 if sys.platform == "win32" else 0


def test_parse_splits_limit_and_program() -> None:
    assert hl.parse(["--limit-seconds", "810", "pwsh", "-File", "a b.ps1"]) == (
        810.0,
        ["pwsh", "-File", "a b.ps1"],
    )


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["pwsh"],
        ["--limit-seconds", "10"],
        ["--limit", "10", "pwsh"],
        ["--limit-seconds", "0", "pwsh"],
        ["--limit-seconds", "-5", "pwsh"],
        ["--limit-seconds", "ten", "pwsh"],
    ],
)
def test_bad_arguments_are_a_usage_error(argv: list[str]) -> None:
    assert hl.main(argv) == hl.EXIT_USAGE


def test_exit_code_passes_through() -> None:
    code = hl.run(30, [sys.executable, "-c", "raise SystemExit(7)"], creationflags=_FLAGS)
    assert code == 7


def test_a_program_past_the_limit_is_stopped() -> None:
    started = time.monotonic()
    code = hl.run(1, [sys.executable, "-c", "import time; time.sleep(60)"], creationflags=_FLAGS)
    assert code == hl.EXIT_LIMIT_REACHED
    assert time.monotonic() - started < 30


def test_a_program_that_cannot_start_is_reported() -> None:
    code = hl.run(5, [str(_PATH.parent / "no-such-program.exe")], creationflags=_FLAGS)
    assert code == hl.EXIT_LAUNCH_FAILED


def test_launcher_exit_codes_are_distinct_and_not_success() -> None:
    codes = {hl.EXIT_USAGE, hl.EXIT_LIMIT_REACHED, hl.EXIT_LAUNCH_FAILED}
    assert len(codes) == 3
    assert 0 not in codes


def test_launcher_uses_only_the_standard_library() -> None:
    # It runs under `pythonw -I` from the daemon checkout, outside any venv.
    text = _PATH.read_text(encoding="utf-8")
    imports = {
        line.split()[1].split(".")[0]
        for line in text.splitlines()
        if line.startswith(("import ", "from "))
    }
    assert imports <= {"__future__", "subprocess", "sys"}


def test_the_stopped_program_is_the_one_started() -> None:
    # kill() targets the program itself (as Task Scheduler did), not a process tree.
    text = _PATH.read_text(encoding="utf-8")
    assert "proc.kill()" in text
    assert "taskkill" not in text
