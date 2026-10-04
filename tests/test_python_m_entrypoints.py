"""The two modules the wrapper launches with ``python -m`` must actually start that way.

T-composer-entrypoint-missing-drops-decision-cards D-1': ``deploy/run-conductor.ps1`` and the
sweep wrapper's composer call no longer go through the ``mindwire-loop`` /
``mindwire-compose-decision`` console-script exes (a missing exe made uv fall back to a stale
global install). They run ``uv run --no-sync python -m <module>`` instead, which only works while
each module keeps its ``if __name__ == "__main__"`` block. These tests fail if either one loses it.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    ["spirrow_mindwire.loop_runner", "spirrow_mindwire.decision_request.cli"],
)
def test_module_runs_under_python_m(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # argparse prints usage only when main() really ran; a module without a __main__ block exits 0
    # silently, which is exactly the regression this test exists to catch.
    assert "usage:" in result.stdout.lower(), result.stdout
