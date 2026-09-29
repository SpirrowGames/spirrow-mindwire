"""Text written by ``scripts/`` is LF on every platform (T-fixture-writer-emits-crlf-on-windows).

Python's text mode translates ``"\\n"`` to ``os.linesep`` unless ``newline=`` says otherwise, so
on Windows a script that forgets it writes CRLF while git stores LF. The file then does not
reproduce byte-for-byte from the same input: #347 spent a commit correcting a sha that way
(Heisenberg msg-4378), and ``gen_adr_index.py`` carries the same fix for the same reason.

Two checks (Bohr msg-4426 §1; the static one kept by the human's decision after msg-4428):

1. **Static.** Every ``write_text(...)`` and every text-mode ``open(...)`` that writes (mode
   contains ``w``/``a``/``x``/``+`` and no ``b``) in ``scripts/*.py`` passes ``newline=``. A
   writer that genuinely wants the platform's line ending says so with ``newline=None``; what
   the test rejects is *not deciding*. Reads, binary writes and ``sys.stdout`` are not checked.
   A mode that cannot be read statically counts as a write (fail loud, not silent).
2. **Behaviour.** The two writers of ``build_tierc_eval_fixture.py`` that were unpinned
   (corrections JSON, ``harvest.json``) produce files with no ``\\r`` byte.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


# --------------------------------------------------------------------------- static check


def _mode_leaves(node: ast.expr) -> list[str | None]:
    """Every string a mode expression can take; ``None`` for a leaf that is not a literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _mode_leaves(node.body) + _mode_leaves(node.orelse)
    return [None]


def _writes_text(mode: str | None) -> bool:
    if mode is None:
        return True  # unreadable mode: assume it writes
    return "b" not in mode and any(c in mode for c in "wax+")


def _unpinned_writers(source: str, filename: str) -> list[str]:
    """``file:line`` of each text-writing call in ``source`` with no ``newline=`` keyword."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        kwargs = {k.arg: k.value for k in node.keywords if k.arg is not None}
        if "newline" in kwargs or any(k.arg is None for k in node.keywords):
            continue  # decided (or **kwargs, which we cannot see into)
        if isinstance(func, ast.Attribute) and func.attr == "write_text":
            found.append(f"{filename}:{node.lineno} write_text")
            continue
        if isinstance(func, ast.Name) and func.id == "open":
            mode_pos = 1  # builtin open(file, mode, ...)
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            mode_pos = 0  # Path.open(mode, ...)
        else:
            continue
        if "mode" in kwargs:
            mode_node: ast.expr | None = kwargs["mode"]
        elif len(node.args) > mode_pos:
            mode_node = node.args[mode_pos]
        else:
            mode_node = None  # default "r"
        if mode_node is None:
            continue
        if any(_writes_text(m) for m in _mode_leaves(mode_node)):
            found.append(f"{filename}:{node.lineno} open")
    return found


def test_every_script_text_writer_declares_newline() -> None:
    offenders: list[str] = []
    for path in sorted(SCRIPTS.glob("*.py")):
        offenders += _unpinned_writers(path.read_text(encoding="utf-8"), path.name)
    assert offenders == [], (
        "text writers without newline= (pass newline='\\n', or newline=None if the platform "
        f"line ending is really wanted): {offenders}"
    )


@pytest.mark.parametrize(
    ("snippet", "caught"),
    [
        ('p.write_text("x")', True),
        ('p.write_text("x", newline="\\n")', False),
        ('p.write_text("x", newline=None)', False),
        ('open(f, "w")', True),
        ('open(f, "a", encoding="utf-8")', True),
        ('open(f, mode="w")', True),
        ('open(f, "wb")', False),
        ("open(f)", False),
        ('open(f, "r")', False),
        ('open(f, "w", newline="\\n")', False),
        ('p.open("w")', True),
        ('p.open("a" if resume else "w", encoding="utf-8")', True),
        ('p.open("r" if x else "rb")', False),
        ("p.open(m)", True),
        ('p.open("r+")', True),
        ("p.write_bytes(b)", False),
    ],
)
def test_static_check_catches_what_it_claims(snippet: str, caught: bool) -> None:
    # Guard against a walker that passes because it sees nothing.
    assert bool(_unpinned_writers(snippet, "s.py")) is caught


# --------------------------------------------------------------------------- behaviour check


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"{name}_lf_module", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{name}_lf_module"] = mod
    spec.loader.exec_module(mod)
    return mod


builder = _load("build_tierc_eval_fixture")


async def _no_harvest(projects: Any) -> list[Any]:
    return []


def test_corrections_json_has_no_cr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(builder, "harvest", _no_harvest)
    monkeypatch.setattr(
        builder,
        "corrections_for",
        lambda **_: {"counts": {"keep": 1}, "rows": [{"k": "a"}, {"k": "b"}]},
    )
    fixture = tmp_path / "fixture.jsonl"
    fixture.write_bytes(b"")
    hv = tmp_path / "harvest.json"
    hv.write_bytes(
        json.dumps({"current_roster": {}, "harvested_at": "2026-09-27T16:33:33+00:00"}).encode()
    )
    out = tmp_path / "corrections.json"
    rc = builder.main(
        [
            "--corrections-for",
            str(fixture),
            "--reproduce",
            str(hv),
            "--corrections-out",
            str(out),
            "--selection-code-commit",
            "abc",
        ]
    )
    assert rc == 0
    data = out.read_bytes()
    assert data.count(b"\n") > 1  # multi-line, so a CRLF writer would have shown
    assert b"\r" not in data


def test_harvest_json_has_no_cr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import spirrow_mindwire.config as config

    monkeypatch.setattr(builder, "harvest", _no_harvest)
    monkeypatch.setattr(
        builder, "build_outputs", lambda threads, roster, head_m: ([], [], [], {"rows": [1, 2]})
    )
    monkeypatch.setattr(
        config, "load_settings", lambda: SimpleNamespace(conductor=SimpleNamespace(roster={}))
    )
    rc = builder.main(["--out-dir", str(tmp_path)])
    assert rc == 0
    data = (tmp_path / "harvest.json").read_bytes()
    assert data.count(b"\n") > 1
    assert b"\r" not in data
