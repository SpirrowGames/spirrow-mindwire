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
   A mode that cannot be read statically counts as a write (fail loud, not silent). Where the
   receiver of ``x.open(...)`` is misread (an AST cannot see types), a ``# newline-exempt:
   <reason>`` comment on the call is the escape hatch (see ``_open_mode_pos``). The check looks
   at each call expression on its own: it resolves no names, so it has no scoping rules.
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


_MODE_CHARS = frozenset("rwxabt+")

# ``<module>.open(file, mode, ...)``: the mode is the SECOND positional, as for builtin open.
_MODULE_OPENS = frozenset({"gzip", "bz2", "lzma", "io", "codecs"})

# Receivers whose ``open`` never yields a text stream (fd / archive handle).
_NEVER_TEXT = frozenset({"os", "tarfile"})

# Constructors whose result has ``Path.open(mode, ...)``.
_PATH_CTORS = frozenset({"Path", "PosixPath", "WindowsPath"})

EXEMPT_MARK = "# newline-exempt:"
"""Escape hatch for a call the heuristic misreads (e.g. ``zf.open("a.txt", "w")`` on a
``ZipFile``, which is binary and takes no ``newline=``). Put it on a line of the call, followed
by the reason; a bare mark with no reason does not count."""


def _is_mode_literal(node: ast.expr) -> bool:
    """A literal (or ``x if c else y`` of literals) that can only be a mode string."""
    leaves = _mode_leaves(node)
    return all(m is not None and m and len(m) <= 4 and set(m) <= _MODE_CHARS for m in leaves)


def _says_mode(node: ast.expr) -> bool:
    """The argument reads as a mode on its face: it is named ``mode`` / ``*_mode``
    (``mode``, ``file_mode``, ``args.mode``), or is a ternary with such a branch or a
    mode-literal branch (``m if c else "w"``). Names are not resolved."""
    if isinstance(node, ast.IfExp):
        return _says_mode(node.body) or _says_mode(node.orelse)
    if isinstance(node, ast.Constant):
        return _is_mode_literal(node)
    ident = getattr(node, "id", None) or getattr(node, "attr", None)
    return isinstance(ident, str) and (ident.lower() == "mode" or ident.lower().endswith("_mode"))


def _looks_like_path(node: ast.expr) -> bool:
    """The receiver is a ``pathlib`` path on its face: ``Path(...)``, ``pathlib.Path(...)``, a
    classmethod on the class (``Path.cwd()``, ``pathlib.Path.home()``) or ``a / b``. Names are not
    resolved."""
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, ast.Div)
    if isinstance(node, ast.Call):
        ctor = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if ctor in _PATH_CTORS:
            return True
        # PR-gate #359 @ ff8af95: ``Path.cwd()`` names ``cwd``, not ``Path``; look at its owner.
        if isinstance(node.func, ast.Attribute):
            owner = node.func.value
            return (getattr(owner, "id", None) or getattr(owner, "attr", None)) in _PATH_CTORS
    return False


def _open_mode_pos(func: ast.expr, args: list[ast.expr]) -> int | None:
    """Positional index of the mode for an ``open`` call, or ``None`` if it is not a file open.

    Builtin ``open`` and ``<module>.open`` take ``(file, mode)``; ``Path.open`` takes
    ``(mode, ...)``. For any other ``x.open(...)`` the receiver's type is not visible to an AST,
    so the call shape decides: a mode-shaped literal first means ``Path.open``; a non-mode
    literal (``zf.open("a.txt", ...)``) or two or more positionals mean ``(file, mode)``.

    A lone non-literal (``x.open(v)``) is ``(file,)`` with the default mode ``"r"`` -- a read,
    not flagged (PR-gate advisory on #356 @ 417b3be: ``zf.open(file_var)``) -- unless the call
    itself says otherwise: ``v`` reads as a mode (``_says_mode``) or ``x`` is a path expression
    (``_looks_like_path``). Only the call expression is looked at. Nothing is followed to an
    assignment, a parameter or an annotation, so there is no scoping to get wrong; the cost is
    that ``m = "w"; p.open(m)`` and ``def f(p: Path, m): p.open(m)`` are NOT flagged. Name the
    argument ``mode``, or pass ``newline=``, and the check sees it.
    """
    if isinstance(func, ast.Name):
        return 1 if func.id == "open" else None
    if not (isinstance(func, ast.Attribute) and func.attr == "open"):
        return None
    if isinstance(func.value, ast.Name):
        if func.value.id in _NEVER_TEXT:
            return None  # os.open -> fd; tarfile.open("w:gz") -> archive, never text mode
        if func.value.id in _MODULE_OPENS:
            return 1
    if not args or _is_mode_literal(args[0]):
        return 0
    if isinstance(args[0], ast.Constant) or len(args) >= 2:
        return 1
    return 0 if _says_mode(args[0]) or _looks_like_path(func.value) else 1


def _exempt(node: ast.Call, lines: list[str]) -> bool:
    for line in lines[node.lineno - 1 : (node.end_lineno or node.lineno)]:
        _, mark, reason = line.partition(EXEMPT_MARK)
        if mark and reason.strip():
            return True
    return False


def _unpinned_writers(source: str, filename: str) -> list[str]:
    """``file:line`` of each text-writing call in ``source`` with no ``newline=`` keyword."""
    found: list[str] = []
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        kwargs = {k.arg: k.value for k in node.keywords if k.arg is not None}
        if "newline" in kwargs or any(k.arg is None for k in node.keywords):
            continue  # decided (or **kwargs, which we cannot see into)
        if _exempt(node, lines):
            continue
        if isinstance(func, ast.Attribute) and func.attr == "write_text":
            found.append(f"{filename}:{node.lineno} write_text")
            continue
        mode_pos = _open_mode_pos(func, node.args)
        if mode_pos is None:
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
        # PR-gate advisory #356 @ 417b3be: a lone non-literal argument to an unknown receiver is
        # a file name (a read), not a mode ...
        ("p.open(m)", False),
        ("zf.open(file_var)", False),
        ("zf.open(member.filename)", False),
        ("zf.open(model_path)", False),  # "mode" inside a longer word is not a mode name
        ("def f(name):\n    return zf.open(name)", False),
        # ... unless the call itself says otherwise: the argument reads as a mode ...
        ("p.open(mode)", True),
        ("p.open(args.mode)", True),
        ("p.open(file_mode)", True),
        ("p.open(MODE)", True),
        ('p.open(m if x else "w")', True),
        ('p.open(m if x else "r")', True),  # "r" marks a mode; ``m`` is unknown, so may write
        ("p.open(mode if x else other)", True),
        ('p.open(mode, newline="\\n")', False),
        ("zf.open(name if x else other)", False),
        ('zf.open(name if x else "export.txt")', False),
        # ... or the receiver is a path expression
        ("Path(x).open(m)", True),
        ("pathlib.Path(x).open(m)", True),
        ("Path.cwd().open(m)", True),  # PR-gate #359 @ ff8af95: classmethod constructors
        ("Path.home().open(m)", True),
        ("pathlib.Path.cwd().open(m)", True),
        ("zf.cwd().open(m)", False),
        ("(root / 'a.txt').open(m)", True),
        ("(root // n).open(m)", False),
        ("make(x).open(m)", False),
        # Names are NOT resolved (human decision C on #359): pinned so the boundary is a
        # decision, not an accident. The mode position is unknown, so these are not flagged.
        ('m = "w"\np.open(m)', False),
        ("def f(p: Path, m):\n    p.open(m)", False),
        ("q = root / 'a.txt'\nq.open(m)", False),
        # In a known mode position an unreadable mode is still a write, whatever it is bound to.
        ('m = "r"\nopen(f, m)', True),
        ("for m in modes:\n    open(f, m)", True),
        ("def f(p, mode):\n    p.open(mode)", True),
        # PR-gate #359 @ b7e4f9f / operator msg-4791: a name that is only READ by a statement
        # (subscript slice, attribute base, ``case Cls(...)``) must not hide a writer.
        ('mode = "w"\nd[mode] = 1\np.open(mode)', True),
        ('mode = "w"\nmode.attr = 1\np.open(mode)', True),
        ('mode = "w"\nmatch v:\n    case mode.X:\n        pass\np.open(mode)', True),
        ('mode = "w"\nmatch v:\n    case mode(x=a):\n        pass\np.open(mode)', True),
        # Einstein msg-4796: nor may a walrus inside a subscript target
        ('d[(mode := "w")] = 2\np.open(mode)', True),
        ('p.open("r+")', True),
        # PR-gate #356 bb73652: module / archive opens take (file, mode), not (mode, ...).
        ('gzip.open(path, "rb")', False),
        ("gzip.open(path)", False),
        ('gzip.open(path, "wt")', True),
        ('gzip.open(path, "wt", newline="\\n")', False),
        ('io.open(path, "w")', True),
        ('tarfile.open(name, "w:gz")', False),
        ("os.open(path, os.O_WRONLY)", False),
        ('zf.open("export.txt")', False),
        ('zf.open("export.txt", "r")', False),
        ('zf.open("export.txt", "w")', True),
        ('zf.open("export.txt", "w")  # newline-exempt: ZipFile.open is binary', False),
        ('zf.open("export.txt", "w")  # newline-exempt:', True),
        ('src.open(path, "rb")', False),
        ('src.open(path, "w")', True),
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
