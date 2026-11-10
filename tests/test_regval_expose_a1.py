"""Tests for scripts/regval_expose_a1.py (改訂 5 §9-a, 改訂 7 E-1 / E-2): order, early exit,
output contract at the fd level (stdout AND stderr), argument errors without echo.

Every string below that looks like data is a dummy built at run time; none is a registry value.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

DUMMY = "-".join(["dummy", "noise", "x" * 5]) + " with spaces"
DUMMY_WORD = "".join(["Dummy", "Word", "0" * 6])  # passes every allow-listed word shape


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "regval_expose_a1.py"
    spec = importlib.util.spec_from_file_location("regval_expose_a1", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ea = _load()
S1, S2 = "1" * 40, "2" * 40
REPO = "org/repo"


def _inputs(tmp_path: Path, lines: str) -> list[str]:
    shas = tmp_path / "shas.txt"
    shas.write_text(lines, encoding="utf-8")
    return ["--shas", str(shas), "--state", str(tmp_path / "state.json")]


def _fake(
    answers: dict[str, tuple[int, str, str]], calls: list[list[str]]
) -> Callable[..., object]:
    def run3(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        for key, ans in answers.items():
            if key in " ".join(argv):
                return ans
        return 1, "", f"{DUMMY} (HTTP 404)"

    return run3


def test_a1_runs_for_every_sha_before_anything_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(ea, "run3", _fake({"forks": (0, "", "")}, calls))
    rc = ea.main(_inputs(tmp_path, f"{REPO} {S1}\n{REPO} {S2}\n"))
    assert rc == 0
    joined = [" ".join(c) for c in calls]
    assert f"repos/{REPO}/commits/{S1}" in joined[0]
    assert f"repos/{REPO}/commits/{S2}" in joined[1]
    assert "forks" in joined[2]
    assert capfd.readouterr().out.rstrip().endswith("DONE nothing-reachable-observed")


def test_first_a1_yes_stops_the_run_and_is_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(ea, "run3", _fake({f"commits/{S1}": (0, S1 + "\n", "")}, calls))
    rc = ea.main(_inputs(tmp_path, f"{REPO} {S1}\n{REPO} {S2}\n"))
    assert rc == 10
    assert len(calls) == 1  # nothing probed after the first positive
    assert f"EARLY A1 {REPO} {S1}" in capfd.readouterr().out
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state[-1]["result"] == "yes"


def test_no_a1b_and_no_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """改訂 7 E-1: A-1b is gone — git is never started and --mirror is refused."""
    calls: list[list[str]] = []
    monkeypatch.setattr(ea, "run3", _fake({"forks": (0, "", "")}, calls))
    assert ea.main(_inputs(tmp_path, f"{REPO} {S1}\n")) == 0
    assert all(c[0] == "gh" for c in calls)
    capfd.readouterr()
    rc = ea.main([*_inputs(tmp_path, f"{REPO} {S1}\n"), "--mirror", f"{REPO}=m.git"])
    assert rc == 2
    assert capfd.readouterr().out == "ERR ArgError\n"


def test_a4_walks_paginated_forks_and_stops_on_first_yes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    answers = {
        "forks": (0, "a/f1\nb/f2\n", ""),
        f"repos/b/f2/commits/{S1}": (0, S1, ""),
    }
    monkeypatch.setattr(ea, "run3", _fake(answers, calls))
    rc = ea.main(_inputs(tmp_path, f"{REPO} {S1}\n"))
    assert rc == 10
    assert any("--paginate" in c for c in calls)
    assert "EARLY A4 b/f2" in capfd.readouterr().out


def test_failures_make_the_run_incomplete_and_never_print_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    def failing(argv: list[str]) -> tuple[int, str, str]:
        return 1, DUMMY, DUMMY

    monkeypatch.setattr(ea, "run3", failing)
    rc = ea.main(_inputs(tmp_path, f"{REPO} {S1}\n"))
    cap = capfd.readouterr()
    assert rc == 3
    assert DUMMY not in cap.out and DUMMY not in cap.err
    assert "INCOMPLETE" in cap.out


@pytest.mark.parametrize("line", [f"{REPO} {S1[:12]}", f"{DUMMY} {S1}", f"{REPO}", ""])
def test_bad_input_is_refused_without_echo(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], line: str
) -> None:
    rc = ea.main(_inputs(tmp_path, line + "\n"))
    cap = capfd.readouterr()
    assert rc == 2
    assert cap.out == "ERR ValueError\n"
    assert cap.err == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["--shas", "x", "--state", "y", f"--{DUMMY_WORD}"],  # unknown option
        ["--shas", "x", "--state", "y", DUMMY_WORD],  # unknown positional
        ["--shas", DUMMY_WORD],  # required --state missing
        ["-h"],
    ],
)
def test_argument_errors_are_fixed_text_on_both_streams(
    capfd: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """改訂 7 E-2: argparse would echo the argument to stderr; nothing but ERR ArgError."""
    rc = ea.main(argv)
    cap = capfd.readouterr()
    assert rc == 2
    assert cap.out == "ERR ArgError\n"
    assert cap.err == ""
    assert DUMMY_WORD not in cap.out + cap.err


def test_writes_inside_the_run_reach_neither_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Anything but emit — print, sys.stderr, raw fd 1 / fd 2 — goes to devnull."""
    import os
    import sys

    def noisy(argv: list[str]) -> tuple[int, str, str]:
        print(DUMMY)
        sys.stderr.write(DUMMY)
        os.write(1, DUMMY.encode())
        os.write(2, DUMMY.encode())
        return 1, "", "(HTTP 404)"

    monkeypatch.setattr(ea, "run3", noisy)
    ea.main(_inputs(tmp_path, f"{REPO} {S1}\n"))
    cap = capfd.readouterr()
    assert DUMMY not in cap.out and DUMMY not in cap.err
    assert "A1 org/repo" in cap.out
