"""Run a console program from a scheduled task with no window, and stop it at a time limit.

Usage (as a Task Scheduler action, under ``pythonw.exe`` so this process has no window either)::

    pythonw.exe -I deploy\\headless_launch.py --limit-seconds N <program> [args...]

Why this exists (T-loop-host-console-windows-steal-focus, PR #576):

* An Interactive-logon task whose action is ``pwsh.exe`` opens a console window on every run, and
  that window takes keyboard focus from whatever the operator is typing into.
* ``conhost.exe --headless <pwsh>`` hides the window, but the task's ExecutionTimeLimit then stops
  only conhost. Task Scheduler terminates the process it started, not its children, so the pwsh
  under conhost keeps running. Measured on sg-tomtebo-01 on 2026-11-13 with a 1-minute limit:
  direct pwsh was gone at 66 s, while pwsh under conhost was still alive at 141 s.
* A Job object with KILL_ON_JOB_CLOSE does not fix that here. The host's pwsh is the Microsoft
  Store package, and a packaged process does not stay in the launcher's job, so closing the job
  does not reach it (measured the same day: an unpackaged child died with the job, the Store pwsh
  did not).

So this launcher enforces the limit itself. It is a GUI-subsystem process (pythonw) and starts the
program with CREATE_NO_WINDOW: a console with no window, which the program's own children inherit.
If the program is still running after ``--limit-seconds``, the launcher terminates that program,
the same single process Task Scheduler used to terminate when pwsh was the action. Its children
are left alone, exactly as before. Set the limit a little below the task's ExecutionTimeLimit, so
the launcher acts first and the task limit stays as an outer backstop.

The program's exit code becomes this process's exit code, so the task's LastTaskResult still
reports it. A program stopped at the limit exits with EXIT_LIMIT_REACHED.

Standard library only; run with ``-I`` so nothing from the current directory is imported.
"""

from __future__ import annotations

import subprocess
import sys

_CREATE_NO_WINDOW = 0x08000000

# Exit codes of the launcher itself (never ones a wrapper uses for success).
EXIT_USAGE = 2
EXIT_LIMIT_REACHED = 121
EXIT_LAUNCH_FAILED = 120


def parse(argv: list[str]) -> tuple[float, list[str]]:
    """Split ``--limit-seconds N <program> [args...]``. Raises ValueError on anything else."""
    if len(argv) < 3 or argv[0] != "--limit-seconds":
        raise ValueError("usage: headless_launch.py --limit-seconds N <program> [args...]")
    limit = float(argv[1])
    if not limit > 0:
        raise ValueError(f"--limit-seconds must be positive, got {argv[1]!r}")
    return limit, argv[2:]


def run(limit: float, program: list[str], *, creationflags: int = _CREATE_NO_WINDOW) -> int:
    try:
        proc = subprocess.Popen(program, creationflags=creationflags)
    except OSError:
        return EXIT_LAUNCH_FAILED
    try:
        return proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        proc.kill()  # TerminateProcess on the program only, as Task Scheduler did.
        proc.wait()
        return EXIT_LIMIT_REACHED


def main(argv: list[str]) -> int:
    try:
        limit, program = parse(argv)
    except ValueError:
        return EXIT_USAGE
    return run(limit, program)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
