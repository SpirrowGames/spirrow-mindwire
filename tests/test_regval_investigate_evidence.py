"""Evidence-pipeline tests for scripts/regval_investigate.py — PR-gate #565 round 1 and
改訂 5 S-2 / S-3 / S-4, 改訂 6 S-6, 改訂 7 E-1 / E-2, Obj-6-1 (C2CAND).

Every string below that looks like data is a dummy built at run time; none is a registry value.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

DUMMY = "-".join(["dummy", "noise", "z" * 5]) + " with spaces"
DUMMY_WORD = "".join(["Dummy", "Word", "1" * 6])
CFG = ["--sandbox", "sb", "--docs-root", "docs"]
SHA = "ab" * 20
REV = "cd" * 20


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "regval_investigate.py"
    spec = importlib.util.spec_from_file_location("regval_investigate_ev", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rv = _load()


# --- S-2 / E-2: output protection --------------------------------------------------------------


def test_checker_output_reaches_neither_stream(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A checker that prints, writes sys.stderr, or writes fd 1 / fd 2 leaks nothing."""
    src = (
        "import os, sys\n"
        f"print({DUMMY!r})\n"
        f"sys.stderr.write({DUMMY!r})\n"
        f"os.write(1, {DUMMY!r}.encode())\n"
        f"os.write(2, {DUMMY!r}.encode())\n"
        "def scan():\n"
        f"    print({DUMMY!r}); os.write(2, {DUMMY!r}.encode())\n"
    )
    monkeypatch.setattr(rv, "git_obj", lambda *a: (0, src))

    def step(_a: object) -> None:
        g = rv.load_guard(REV)
        g.scan()
        rv.emit(rv.T("AFTER"))

    monkeypatch.setattr(rv, "cmd_table", step)
    rc = rv.main(["table", "--state", str(tmp_path / "s.json"), *CFG])
    cap = capfd.readouterr()
    assert rc == 0
    assert cap.out == "AFTER\n"
    assert cap.err == ""
    # the protection is undone afterwards
    print("visible")
    assert capfd.readouterr().out == "visible\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["table", "--state", "s", *CFG, f"--{DUMMY_WORD}"],
        ["table", "--state", "s", *CFG, DUMMY_WORD],
        [DUMMY_WORD, "--state", "s", *CFG],  # invalid choice is echoed by argparse
        ["table", "--state", DUMMY_WORD],  # required --sandbox / --docs-root missing
        ["-h"],
    ],
)
def test_argument_errors_are_fixed_text_on_both_streams(
    capfd: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    rc = rv.main(argv)
    cap = capfd.readouterr()
    assert rc == 2
    assert cap.out == "ERR ArgError\n"
    assert cap.err == ""


# --- S-3 / finding 1: read conditions -------------------------------------------------------


def test_child_env_drops_repository_redirecting_variables() -> None:
    env = rv.child_env(
        {"GIT_DIR": "x", "GIT_WORK_TREE": "y", "GIT_OBJECT_DIRECTORY": "z", "PATH": "p"}
    )
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
    assert "GIT_OBJECT_DIRECTORY" not in env
    assert env["PATH"] == "p"
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_git_obj_disables_ext_diff_and_textconv(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str, str]:
        seen.append(argv)
        return 0, "", ""

    monkeypatch.setattr(rv, "run3", fake)
    rv.git_obj("store", "show", SHA)
    rv.git_obj("store", "log", SHA)
    rv.git_obj("store", "rev-parse", f"--disambiguate={SHA[:9]}")
    assert "--no-ext-diff" in seen[0] and "--no-textconv" in seen[0]
    assert "--no-ext-diff" in seen[1] and "--no-textconv" in seen[1]
    assert "--no-ext-diff" not in seen[2]
    with pytest.raises(ValueError):
        rv.git_obj("store", "rev-parse", "--disambiguate=HEAD")


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()


def test_replace_refs_do_not_substitute_objects(tmp_path: Path) -> None:
    """PR-gate #565 finding 1: refs/replace in the store cannot change what a full sha reads."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "original")
    original = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "replacement")
    replacement = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "replace", original, replacement)
    assert _git(tmp_path, "show", "-s", "--format=%s", original) == "replacement"  # the hazard
    rc, out = rv.git_obj(str(tmp_path), "show", "-s", "--format=%s", original)
    assert rc == 0
    assert out.strip() == "original"


def test_store_check_marks_a_shallow_store_unknown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(rv, "_FAILS", 0)
    monkeypatch.setattr(rv, "git_obj", lambda *a: (0, "true\n"))
    monkeypatch.setattr(rv, "run", lambda argv: (0, "x\nx\n"))
    assert rv.store_check("table", "proj", "s") is False
    out = capsys.readouterr().out
    assert "STORE proj shallow replace 2" in out
    assert "ERR table proj shallow" in out
    assert rv._FAILS == 1


# --- finding 2 / S-4: failures are not observations -----------------------------------------


def test_findings_of_commit_raises_on_unreadable_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rv, "git_obj", lambda *a: (128, ""))
    with pytest.raises(rv.ReadFailedError):
        rv.findings_of_commit(None, "proj", SHA, None)


def _table_world(monkeypatch: pytest.MonkeyPatch, log_rc: int, show_rc: int) -> None:
    monkeypatch.setattr(rv, "docs_rev", lambda p: REV)
    monkeypatch.setattr(rv, "store_check", lambda *a: True)
    monkeypatch.setattr(rv, "load_guard", lambda rev: SimpleNamespace())
    monkeypatch.setattr(rv, "registry_at", lambda g, rev: object())
    monkeypatch.setattr(rv, "preserved_projects", lambda p: ["proj"])
    monkeypatch.setattr(rv, "preserved_url", lambda b: "https://github.com/o/r.git")
    monkeypatch.setattr(rv, "preserved_tips", lambda b: [SHA])

    def git_obj(store: str, sub: str, *args: str) -> tuple[int, str]:
        if sub == "log":
            return log_rc, f"{SHA} 2026-10-03T00:00:00+00:00 2026-10-03T00:00:00+00:00\n"
        return show_rc, ""

    monkeypatch.setattr(rv, "git_obj", git_obj)


@pytest.mark.parametrize(("log_rc", "show_rc"), [(0, 128), (128, 0)])
def test_table_with_a_failed_read_writes_no_state_and_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    tmp_path: Path,
    log_rc: int,
    show_rc: int,
) -> None:
    _table_world(monkeypatch, log_rc, show_rc)
    state = tmp_path / "s.json"
    rc = rv.main(["table", "--state", str(state), *CFG, "--preserve", str(tmp_path)])
    out = capfd.readouterr().out
    assert rc == 3
    assert not state.exists()
    assert "TABLE not-written" in out
    assert out.rstrip().endswith("INCOMPLETE 1")


def test_table_complete_writes_state(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _table_world(monkeypatch, 0, 0)
    state = tmp_path / "s.json"
    rc = rv.main(["table", "--state", str(state), *CFG, "--preserve", str(tmp_path)])
    assert rc == 0
    assert json.loads(state.read_text(encoding="utf-8")) == []


def _registry_world(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, findings: Any, blob: str | None
) -> list[str]:
    monkeypatch.setattr(rv, "docs_rev", lambda p: REV)
    monkeypatch.setattr(rv, "store_check", lambda *a: True)
    monkeypatch.setattr(rv, "load_guard", lambda rev: SimpleNamespace())
    reg = SimpleNamespace(values={"ITEM_A": "v"})
    monkeypatch.setattr(rv, "registry_at", lambda g, rev: reg)
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    monkeypatch.setattr(rv, "registry_history", lambda d: [(t0, REV)])
    monkeypatch.setattr(rv, "reflog_entries", lambda f: [(t0, REV, "commit")])
    monkeypatch.setattr(rv, "findings_of_commit", findings)
    monkeypatch.setattr(rv, "blob_of", lambda rev: blob)
    state = tmp_path / "s.json"
    row = {
        "n": 1,
        "project": "proj",
        "repo": "o/r",
        "sha": SHA,
        "path": "a.md",
        "line": 3,
        "cls": "value",
        "item": "ITEM_A",
        "author": "2026-10-03T00:00:00+00:00",
        "committer": "2026-10-03T00:00:00+00:00",
    }
    state.write_text(json.dumps([row]), encoding="utf-8")
    return ["registry", "--state", str(state), *CFG, "--preserve", str(tmp_path)]


def test_registry_failed_object_read_is_not_row_not_reproduced(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def unreadable(*a: object) -> list[Any]:
        raise rv.ReadFailedError("rc128")

    rc = rv.main(_registry_world(monkeypatch, tmp_path, unreadable, "e" * 40))
    out = capfd.readouterr().out
    assert rc == 3
    assert "row-not-reproduced" not in out
    assert "ERR registry 1 show-rc128" in out


def test_registry_b2_is_a_head_comparison_and_unreadable_blob_is_unknown(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    finding = SimpleNamespace(kind="registered as {{ITEM_A}}", match="v")
    argv = _registry_world(monkeypatch, tmp_path, lambda *a: [("a.md", 3, finding)], None)
    rc = rv.main(argv)
    out = capfd.readouterr().out
    assert rc == 3
    assert "B2WT unestablished" in out
    assert f"B2HEAD 1 author {REV} unknown" in out
    assert "other-blob" not in out
    assert "B2EV" not in out


def test_registry_b2_head_same_blob(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    finding = SimpleNamespace(kind="registered as {{ITEM_A}}", match="v")
    argv = _registry_world(monkeypatch, tmp_path, lambda *a: [("a.md", 3, finding)], "e" * 40)
    assert rv.main(argv) == 0
    out = capfd.readouterr().out
    assert f"B1 1 author {REV} match" in out
    assert f"B2HEAD 1 author {REV} head-same-blob" in out


# --- finding 5 / S-6 / Obj-6-1: transcript evidence -----------------------------------------

CLONE = "C:/ws/proj-impl"
COMMITTER = datetime(2026, 10, 3, 0, 0, 0, tzinfo=UTC)


def _session(
    command: str, result: str, cwd: str = CLONE, ts: str = "2026-10-03T00:01:00Z"
) -> list[dict[str, Any]]:
    return [
        {
            "type": "assistant",
            "cwd": cwd,
            "message": {
                "content": [
                    {"type": "text", "text": f"[main {SHA[:7]}] quoted in prose"},
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "Bash",
                        "input": {"command": command},
                    },
                ]
            },
        },
        {
            "type": "user",
            "timestamp": ts,
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": result}]
            },
        },
    ]


def _verdict(monkeypatch: pytest.MonkeyPatch, recs: list[dict[str, Any]], resolved: str) -> str:
    monkeypatch.setattr(rv, "git_obj", lambda *a: (0, resolved))
    pairs = rv.tool_pairs(recs)
    verdicts = [rv.c2_candidate(p, SHA, COMMITTER, {rv._norm(CLONE)}, "store") for p in pairs]
    return verdicts[0] if verdicts else "none"


GOOD_OUT = f"[main {SHA[:9]}] add notes\n 1 file changed"


def test_c2_simple_commit_in_the_clone_is_a_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _verdict(monkeypatch, _session('git commit -m "notes"', GOOD_OUT), SHA) == "cand"


def test_c2_pasted_output_in_user_prose_is_not_a_pair() -> None:
    recs = [{"type": "user", "message": {"content": f"[main {SHA[:9]}] pasted"}}]
    assert rv.tool_pairs(recs) == []


@pytest.mark.parametrize(
    "command",
    [
        f"printf '[main {SHA[:9]}] x\\n'  # git commit",
        "cd ../other-impl && git commit -m x",
        "git -C ../other-impl commit -m x",
        "git commit -m x; true",
        "GIT_DIR=../other/.git git commit -m x",
    ],
)
def test_c2_non_simple_or_elsewhere_is_only_a_mention(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    assert _verdict(monkeypatch, _session(command, GOOD_OUT), SHA) == "mention"


def test_c2_ambiguous_short_sha_is_only_a_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    other = SHA[:9] + "0" * 31
    recs = _session("git commit -m x", GOOD_OUT)
    assert _verdict(monkeypatch, recs, f"{SHA}\n{other}\n") == "mention"


def test_c2_outside_the_time_window_is_only_a_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = _session("git commit -m x", GOOD_OUT, ts="2026-10-03T00:30:00Z")
    assert _verdict(monkeypatch, recs, SHA) == "mention"


def test_c2_error_result_is_only_a_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    recs = _session("git commit -m x", GOOD_OUT)
    recs[1]["message"]["content"][0]["is_error"] = True
    assert _verdict(monkeypatch, recs, SHA) == "mention"


def test_c2_failed_disambiguation_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rv, "git_obj", lambda *a: (128, ""))
    pair = rv.tool_pairs(_session("git commit -m x", GOOD_OUT))[0]
    assert rv.c2_candidate(pair, SHA, COMMITTER, {rv._norm(CLONE)}, "s") == "fail"


def test_guard_marks_count_only_in_commit_tool_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """C-3a: a guard message quoted in prose or in another tool's output is not counted."""
    mark = "Commit stopped by conventions"
    recs = _session("git commit -m x", GOOD_OUT)
    recs[0]["message"]["content"][0]["text"] = mark  # assistant prose
    recs.insert(
        0, {"type": "user", "timestamp": "2026-10-03T00:00:10Z", "message": {"content": mark}}
    )
    recs.insert(
        0,
        {
            "type": "assistant",
            "cwd": CLONE,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu0",
                        "name": "Bash",
                        "input": {"command": "cat notes.md"},
                    }
                ]
            },
        },
    )
    recs.insert(
        1,
        {
            "type": "user",
            "timestamp": "2026-10-03T00:00:20Z",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "tu0", "content": mark}]
            },
        },
    )
    monkeypatch.setattr(rv, "git_obj", lambda *a: (0, SHA))
    monkeypatch.setattr(rv, "store_check", lambda *a: True)
    monkeypatch.setattr(rv, "store_of", lambda p: CLONE)
    monkeypatch.setattr(
        rv, "transcripts", lambda p: [("proj-dir", "0" * 8 + "-0000-0000-0000-" + "0" * 12, recs)]
    )
    monkeypatch.setattr(rv, "docs_rev", lambda p: REV)
    state = tmp_path / "s.json"
    row = {"n": 1, "project": "proj", "sha": SHA, "committer": "2026-10-03T00:00:00+00:00"}
    state.write_text(json.dumps([row]), encoding="utf-8")
    monkeypatch.setattr(rv, "_FAILS", 0)
    rv.cmd_creation(SimpleNamespace(state=str(state), preserve=str(tmp_path)))
    out = capsys.readouterr().out
    assert f"C2CAND {SHA}" in out
    assert "C2EV" not in out
    assert "C3aCAND " + SHA + " 00000000-0000-0000-0000-000000000000 commit_stopped 0" in out
