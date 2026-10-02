"""CLI contract of ``python -m spirrow_mindwire.stall_ledger`` (Bohr msg-5611 §1, §5).

The wrapper's ``Get-FailureClass`` runs this CLI through the bounded probe helper with
``--input <tmp>``; a hand run still pipes the tail on stdin. Both sources must yield the
same label, and both plumbing failures (missing file, undecodable bytes) must keep the
"print ``unknown``, exit 2" contract the PowerShell side relies on.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from spirrow_mindwire.stall_ledger.__main__ import main

_TAIL = (
    "some unrelated log line\n"
    "ClaudeCodeSdkDeliveryError: SDK is_error; subtype='error_during_execution'\n"
    "exit=1\n"
)
_LABEL = "sdk-error-during-execution"


def test_stdin_and_input_file_yield_the_same_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(_TAIL))
    assert main([]) == 0
    from_stdin = capsys.readouterr().out.strip()

    path = tmp_path / "tail.json"
    path.write_text(_TAIL, encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO("must not be read"))
    assert main(["--input", str(path)]) == 0
    from_file = capsys.readouterr().out.strip()

    assert from_stdin == _LABEL
    assert from_file == from_stdin


def test_input_file_is_read_as_utf8(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Non-ASCII around the signature: the file must be decoded as UTF-8, not the locale code page.
    path = tmp_path / "tail.json"
    path.write_bytes(("日本語 — ünïcödé\n" + _TAIL).encode("utf-8"))
    assert main(["--input", str(path)]) == 0
    assert capsys.readouterr().out.strip() == _LABEL


def test_missing_input_file_prints_unknown_and_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.json"
    assert main(["--input", str(missing)]) == 2
    out, err = capsys.readouterr()
    assert out.strip() == "unknown"
    assert "input file" in err
    assert "nope.json" in err


def test_invalid_utf8_input_file_prints_unknown_and_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bad.json"
    path.write_bytes(b"ClaudeCodeSdkDeliveryError \xff\xfe\xfa not utf-8")
    assert main(["--input", str(path)]) == 2
    out, err = capsys.readouterr()
    assert out.strip() == "unknown"
    assert "input file" in err


def test_undecodable_stdin_still_prints_unknown_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = io.TextIOWrapper(io.BytesIO(b"\xff\xfe\xfa"), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", raw)
    assert main([]) == 2
    out, err = capsys.readouterr()
    assert out.strip() == "unknown"
    assert "stdin unreadable" in err


def test_module_entry_point_accepts_input_flag(tmp_path: Path) -> None:
    """End to end through ``python -m`` — the exact module path the wrapper runs."""
    path = tmp_path / "tail.json"
    path.write_text(_TAIL, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "spirrow_mindwire.stall_ledger", "--input", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == _LABEL
