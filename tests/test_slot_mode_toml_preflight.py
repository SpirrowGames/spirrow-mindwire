"""Slot mode's role turn passes preflight check=config (T-ephemeral-impl-clone-slot-per-thread A7).

msg-10049: the first slot-mode tick in production refused every role turn with
``preflight refused exit=13 check=config: Unescaped '\\' in a string``. The sweep's
``Set-TomlValue`` (deploy/run-conductor-scheduled.ps1) wrote slot-prep's backslash slot path into
``[loop].repo_dir`` without TOML escaping. Nothing caught it before production because the only
test read the wrapper's source text; no test read the written file back the way preflight does.

These tests run the real ``Set-TomlValue`` (lifted from the sweep script by pwsh's parser) on a
temporary mindwire.toml laid out like the sweep's, then load it with ``tomllib`` and with
``load_settings`` — the call loop_runner's conductor mode makes before it refuses with
check=config. pwsh is a hard requirement of the repository gate (``.mindwire-gate`` runs the
PowerShell suites before pytest), so a missing pwsh fails these tests rather than skipping them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from spirrow_mindwire.config import load_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
SLOT_PATH = r"C:\workspace\slots\mindwire-impl\slot-01"

# The keys the sweep writes before every launch (run-conductor-scheduled.ps1, "All three must move
# together"), in the sections they live in, plus an unrelated section that must stay untouched.
BASE_TOML = """\
# production-like layout
[loop]
project = "spirrow-mindwire"
repo_dir = "C:/workspace/sandbox/mindwire-impl"

[conductor]
task_thread_id = "T-old"
"""

_DRIVER = r"""
$ErrorActionPreference = 'Stop'
$repo = $env:MW_T_REPO
. (Join-Path $repo 'deploy/lib/TomlString.ps1')
$errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $repo 'deploy/run-conductor-scheduled.ps1'), [ref]$null, [ref]$errs)
if ($errs) { throw 'sweep script does not parse' }
$fn = $ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $n.Name -eq 'Set-TomlValue' }, $true) | Select-Object -First 1
if (-not $fn) { throw 'Set-TomlValue not found' }
function Write-Log { param([string]$Message) }
Invoke-Expression $fn.Extent.Text
Set-TomlValue -Path $env:MW_T_CFG -Section $env:MW_T_SECTION `
    -Key $env:MW_T_KEY -Value $env:MW_T_VALUE
"""


def _pwsh() -> str:
    exe = shutil.which("pwsh")
    assert exe, "pwsh is required by the repository gate (.mindwire-gate) and by these tests"
    return exe


def _set_toml_value(cfg: Path, section: str, key: str, value: str) -> None:
    env = dict(os.environ)
    env.update(
        MW_T_REPO=str(REPO_ROOT),
        MW_T_CFG=str(cfg),
        MW_T_SECTION=section,
        MW_T_KEY=key,
        MW_T_VALUE=value,
    )
    proc = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-Command", _DRIVER],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"Set-TomlValue failed: {proc.stdout}\n{proc.stderr}"


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data dir laid out as the sweep's: <data_dir>/config/mindwire.toml, env cleared."""
    for name in list(os.environ):
        if name.startswith("MINDWIRE_"):
            monkeypatch.delenv(name, raising=False)
    cfg = tmp_path / "config" / "mindwire.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(BASE_TOML.encode("utf-8"))
    monkeypatch.setenv("MINDWIRE_PATHS__DATA_DIR", str(tmp_path))
    return cfg


def test_raw_backslash_path_is_the_msg_10049_failure(config_file: Path) -> None:
    """Control: the line the pre-A7 writer produced is what preflight refused."""
    text = config_file.read_text("utf-8").replace(
        'repo_dir = "C:/workspace/sandbox/mindwire-impl"', f'repo_dir = "{SLOT_PATH}"'
    )
    config_file.write_text(text, "utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads(text)
    with pytest.raises(Exception, match=r"(?i)escape"):
        load_settings()


def test_slot_mode_repo_dir_passes_preflight_config_check(config_file: Path) -> None:
    """The sweep's three writes for a slot-mode launch; the loader then reads repo_dir unchanged."""
    _set_toml_value(config_file, "loop", "project", "spirrow-mindwire")
    _set_toml_value(config_file, "loop", "repo_dir", SLOT_PATH)
    _set_toml_value(
        config_file, "conductor", "task_thread_id", "T-ephemeral-impl-clone-slot-per-thread"
    )

    data = tomllib.loads(config_file.read_text("utf-8"))
    assert data["loop"]["repo_dir"] == SLOT_PATH

    # load_settings() with no argument: the same default-path resolution loop_runner uses.
    settings = load_settings()
    assert str(settings.loop.repo_dir) == SLOT_PATH
    assert settings.loop.project == "spirrow-mindwire"
    assert settings.conductor.task_thread_id == "T-ephemeral-impl-clone-slot-per-thread"


def test_rewrite_of_same_slot_path_keeps_bytes(config_file: Path) -> None:
    _set_toml_value(config_file, "loop", "repo_dir", SLOT_PATH)
    first = config_file.read_bytes()
    _set_toml_value(config_file, "loop", "repo_dir", SLOT_PATH)
    assert config_file.read_bytes() == first
    # Everything but the one line is byte-for-byte the original.
    assert first.replace(
        SLOT_PATH.replace("\\", "\\\\").encode(), b"C:/workspace/sandbox/mindwire-impl"
    ) == (BASE_TOML.encode("utf-8"))


@pytest.mark.parametrize(
    "value",
    [
        'T-"quoted"',
        "C:\\trailing\\",
        "caf\u00e9 \U0001f600",
        "C:/workspace/sandbox/mindwire-impl",
    ],
)
def test_values_round_trip_through_tomllib(config_file: Path, value: str) -> None:
    _set_toml_value(config_file, "conductor", "task_thread_id", value)
    assert tomllib.loads(config_file.read_text("utf-8"))["conductor"]["task_thread_id"] == value
