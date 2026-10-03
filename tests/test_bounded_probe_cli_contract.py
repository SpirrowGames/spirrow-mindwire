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

A call whose argv starts with ``'-m', '<module>'`` (``Get-FailureClass`` -> ``python -m
spirrow_mindwire.stall_ledger``, Bohr msg-5611 §4) is resolved to that module instead and checked
against ``python -m <module> --help``. When the module argv names a subcommand right after the
module (``'-m', 'spirrow_mindwire.cli', 'clone-check', ...``, T-clone-guard-pin-ignored-only-in-
mindwire D-3c), the check runs ``python -m <module> <subcommand> --help``, where the subcommand's
own flags are listed.
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
_MODULE = re.compile(r"^'-m'\s*,\s*'(?P<module>[\w.]+)'(?:\s*,\s*'(?P<sub>[a-z][a-z0-9-]*)')?")

# A target is either ``scripts/<name>.py`` or ``-m <module>``; this prefix marks the latter.
_MODULE_PREFIX = "-m "


def _call_sites() -> list[tuple[str, str, list[str]]]:
    text = _WRAPPER.read_text(encoding="utf-8")
    sites: list[tuple[str, str, list[str]]] = []
    for m in _CALL.finditer(text):
        args = m.group("args")
        mod = _MODULE.match(args.strip())
        if mod is not None:
            target = _MODULE_PREFIX + mod.group("module")
            if mod.group("sub"):
                target += " " + mod.group("sub")
            sites.append((m.group("label"), target, _FLAG.findall(args)))
            continue
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
    # msg-5414 §3 names eight call sites and msg-5611 §3 adds Get-FailureClass as the ninth; a
    # parser miss must not silently shrink coverage. T-pr-event-advances-thread (1b) adds the
    # tenth: Invoke-PrEventAdvanceTick -> -m spirrow_mindwire.pr_event_advance.
    # T-clone-guard-pin-ignored-only-in-mindwire adds two: Invoke-HeadSkipRevertLaunch (D-3a) and
    # Invoke-CloneCheck -> -m spirrow_mindwire.cli clone-check (D-3c).
    # T-sweep-intake-and-quarantine-stalls (msg-5889 D-2) adds Invoke-UnregisteredThreadsProbe.
    assert len(_SITES) == 13, _SITES


def test_unregistered_threads_call_site_resolves_to_the_d2_script() -> None:
    sites = {label: (target, flags) for label, target, flags in _SITES}
    assert sites.get("'unregistered-threads'") == ("unregistered_threads.py", ["--sweep-config"])


def test_clone_check_call_site_resolves_to_the_cli_subcommand() -> None:
    sites = {label: (target, flags) for label, target, flags in _SITES}
    assert sites.get("'clone-check'") == ("-m spirrow_mindwire.cli clone-check", ["--repo-dir"])
    assert sites.get("'head-skip-revert-launch'") == (
        "head_skip_decide.py",
        ["--state-file", "--mode", "--payload-file"],
    )


def test_pr_event_advance_call_site_resolves_to_the_1b_module() -> None:
    sites = {label: (target, flags) for label, target, flags in _SITES}
    assert sites.get('"pr-event-advance-$Project"') == (
        "-m spirrow_mindwire.pr_event_advance",
        ["--project", "--sweep-config"],
    )


def test_failure_class_call_site_resolves_to_the_classifier_module() -> None:
    sites = {label: (target, flags) for label, target, flags in _SITES}
    assert sites.get("'failure-class'") == ("-m spirrow_mindwire.stall_ledger", ["--input"])


def _help_command(target: str) -> list[str]:
    if target.startswith(_MODULE_PREFIX):
        return [sys.executable, "-m", *target[len(_MODULE_PREFIX) :].split(), "--help"]
    return [sys.executable, str(_REPO_ROOT / "scripts" / target), "--help"]


@pytest.mark.parametrize(("label", "script", "flags"), _SITES, ids=[s[0] for s in _SITES])
def test_target_script_accepts_every_flag(label: str, script: str, flags: list[str]) -> None:
    proc = subprocess.run(
        _help_command(script),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=_REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    accepted = set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", proc.stdout))
    missing = [f for f in flags if f not in accepted]
    assert not missing, f"{label} passes {missing} but {script} does not accept them"
