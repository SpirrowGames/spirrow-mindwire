"""Contract: every flag the wrapper passes to a bounded probe is one the target script accepts.

``tests/Test-BoundedProbe.ps1`` stubs ``Invoke-BoundedUvProbe`` for most call sites, so it
cannot see an argv the target script's argparse would reject (exit 2, "unrecognized
arguments"). The PR #404 gate raised exactly that concern for ``--input`` / ``--candidates`` /
``--payload-file``. This test closes the blind spot mechanically: it reads every
``Invoke-BoundedUvProbe ... -Arguments @(...)`` call in ``deploy/run-conductor-scheduled.ps1``,
resolves the script the call runs (the nearest preceding
``Join-Path $repoRoot "scripts<sep>X.py"`` assignment, ``<sep>`` being a backslash, to the variable
in the first argv slot), and checks each ``'--flag'`` literal against
the script's real ``--help`` output. A new call site, or a renamed flag on either side, reds here.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WRAPPER = _REPO_ROOT / "deploy" / "run-conductor-scheduled.ps1"

_CALL = re.compile(
    r"Invoke-BoundedUvProbe\s+-Label\s+(?P<label>\S+).*?-Arguments\s+@\((?P<args>[^)]*)\)",
    re.DOTALL,
)
_ASSIGN = re.compile(
    r"\$(?P<var>\w+)\s*=\s*Join-Path\s+\$repoRoot\s+"
    r"\"scripts[\\/](?P<script>[\w.]+\.py)\""
)
_FLAG = re.compile(r"'(--[a-z][a-z0-9-]*)'")


def _call_sites() -> list[tuple[str, str, list[str]]]:
    text = _WRAPPER.read_text(encoding="utf-8")
    sites: list[tuple[str, str, list[str]]] = []
    for m in _CALL.finditer(text):
        args = m.group("args")
        first = args.split(",")[0].strip()
        assert first.startswith("$"), f"{m.group('label')}: first argv slot is not a script var"
        var = first[1:]
        script = None
        for a in _ASSIGN.finditer(text, 0, m.start()):
            if a.group("var") == var:
                script = a.group("script")
        assert script is not None, f"{m.group('label')}: no scripts/*.py assignment to ${var}"
        sites.append((m.group("label"), script, _FLAG.findall(args)))
    return sites


_SITES = _call_sites()


def test_every_call_site_was_found() -> None:
    # msg-5414 §3 names eight call sites; a parser miss must not silently shrink coverage.
    assert len(_SITES) == 8, _SITES


@pytest.mark.parametrize(("label", "script", "flags"), _SITES, ids=[s[0] for s in _SITES])
def test_target_script_accepts_every_flag(label: str, script: str, flags: list[str]) -> None:
    proc = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / script), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=_REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    accepted = set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", proc.stdout))
    missing = [f for f in flags if f not in accepted]
    assert not missing, f"{label} passes {missing} but scripts/{script} does not accept them"
