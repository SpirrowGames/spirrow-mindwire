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
   <reason>`` comment on the call is the escape hatch (see ``_open_mode_pos``).
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
from typing import Any, NamedTuple

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


# --------------------------------------------------------------------------- static check


_MAX_DEPTH = 8  # name -> value hops followed before giving up (also breaks ``m = m`` cycles)


def _mode_leaves(node: ast.expr, scope: _Scope | None = None, depth: int = 0) -> list[str | None]:
    """Every string a mode expression can take; ``None`` for a leaf that is not a literal.

    With a ``scope``, a bare name is followed to what it is plainly bound to there
    (``m = "w"`` -> ``"w"``); a name bound in a way that cannot be read stays ``None``.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _mode_leaves(node.body, scope, depth) + _mode_leaves(node.orelse, scope, depth)
    if scope is not None and isinstance(node, ast.Name) and depth < _MAX_DEPTH:
        found = scope.values_of(node.id)
        if found:
            values, where = found
            return [leaf for v in values for leaf in _mode_leaves(v, where, depth + 1)]
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

EXEMPT_MARK = "# newline-exempt:"
"""Escape hatch for a call the heuristic misreads (e.g. ``zf.open("a.txt", "w")`` on a
``ZipFile``, which is binary and takes no ``newline=``). Put it on a line of the call, followed
by the reason; a bare mark with no reason does not count."""


def _is_mode_literal(node: ast.expr) -> bool:
    """A literal (or ``x if c else y`` of literals) that can only be a mode string."""
    leaves = _mode_leaves(node)
    return all(m is not None and m and len(m) <= 4 and set(m) <= _MODE_CHARS for m in leaves)


_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


class _Binding(NamedTuple):
    """How the nearest scope that binds a name binds it.

    ``param`` is set for a function parameter (``default`` is its default, if any); ``values``
    holds every plain ``name = value`` in that scope, or is ``None`` when any binding there is
    one we cannot read. ``scope`` is the chain starting at the binding scope, so the values are
    resolved where they were written, not where the name was used.
    """

    param: ast.arg | None
    default: ast.expr | None
    values: list[ast.expr] | None
    scope: _Scope


def _named_targets(node: ast.AST) -> list[str]:
    """Names bound by a target / pattern node (``a``, ``(a, b)``, ``*a``, ``case [a, *b]``)."""
    out: list[str] = []
    for x in ast.walk(node):
        if isinstance(x, ast.Name):
            out.append(x.id)
        elif isinstance(x, (ast.MatchAs, ast.MatchStar)) and x.name is not None:
            out.append(x.name)
        elif isinstance(x, ast.MatchMapping) and x.rest is not None:
            out.append(x.rest)
    return out


class _Scope:
    """What the names visible at one call are bound to, as far as an AST can tell.

    Follows Python's lexical scoping: the scope a name resolves in is the nearest enclosing
    function, lambda or comprehension that binds it, then the module. A class body is a scope
    only for code directly in it (methods do not see it). Comprehensions are their own scope
    for their loop targets, while an ``:=`` inside one binds in the enclosing function (PEP 572).

    Only a plain ``name = value`` / ``name: T = value`` is readable. Anything else that binds the
    name in that scope — ``for``, ``with``, ``import``, ``+=``, unpacking, ``:=``, ``del``,
    ``except ... as``, a ``match`` capture, ``def`` / ``class``, ``global`` / ``nonlocal`` — makes
    the binding unreadable (``values is None``), and nothing is inferred from it.
    """

    def __init__(self, chain: list[ast.AST]) -> None:
        self.chain = chain

    @classmethod
    def at(cls, node: ast.AST, parents: dict[ast.AST, ast.AST]) -> _Scope:
        chain: list[ast.AST] = []
        child, cur = node, parents.get(node)
        while cur is not None:
            if isinstance(cur, _COMPS):
                # the first iterable is evaluated in the enclosing scope, not the comprehension's
                if not _within(node, cur.generators[0].iter):
                    chain.append(cur)
            elif isinstance(cur, _FUNCS):
                # decorators, defaults and annotations are evaluated in the enclosing scope
                if _in_body(child, cur):
                    chain.append(cur)
            elif isinstance(cur, ast.ClassDef):
                if not chain and _in_body(child, cur):
                    chain.append(cur)
            elif isinstance(cur, ast.Module):
                chain.append(cur)
            child, cur = cur, parents.get(cur)
        return cls(chain)

    @staticmethod
    def _bindings_in(scope: ast.AST) -> list[ast.AST]:
        """The nodes whose bindings belong to ``scope`` itself."""
        if isinstance(scope, _COMPS):
            return list(scope.generators)
        roots: list[ast.AST] = (
            [scope.body] if isinstance(scope, ast.Lambda) else list(getattr(scope, "body", []))
        )
        out: list[ast.AST] = []
        todo = list(roots)
        while todo:
            n = todo.pop()
            out.append(n)
            if isinstance(n, (*_FUNCS, ast.ClassDef)):
                # its name binds here; its decorators / defaults / bases are evaluated here
                todo += list(getattr(n, "decorator_list", []))
                if isinstance(n, _FUNCS):
                    todo += [d for d in n.args.defaults + n.args.kw_defaults if d is not None]
                else:
                    todo += n.bases + [k.value for k in n.keywords]
                continue
            if isinstance(n, _COMPS):
                # loop targets are the comprehension's own; an := inside binds here (PEP 572)
                todo.append(n.generators[0].iter)
                out += [x for x in _walk_comp(n) if isinstance(x, ast.NamedExpr)]
                continue
            todo.extend(ast.iter_child_nodes(n))
        return out

    @staticmethod
    def _param(fn: ast.AST, name: str) -> tuple[ast.arg, ast.expr | None] | None:
        if not isinstance(fn, _FUNCS):
            return None
        a = fn.args
        pos = a.posonlyargs + a.args
        defaults: list[ast.expr | None] = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
        pairs = [*zip(pos, defaults, strict=True), *zip(a.kwonlyargs, a.kw_defaults, strict=True)]
        pairs += [(v, None) for v in (a.vararg, a.kwarg) if v is not None]
        for arg, d in pairs:
            if arg.arg == name:
                return arg, d
        return None

    def lookup(self, name: str) -> _Binding | None:
        """The binding of ``name`` in the nearest scope that binds it, or ``None`` if none does."""
        for i, scope in enumerate(self.chain):
            here = _Scope(self.chain[i:])
            p = self._param(scope, name)
            if p is not None:
                return _Binding(p[0], p[1], None, here)
            values: list[ast.expr] = []
            opaque = False
            for n in self._bindings_in(scope):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name) and t.id == name:
                            values.append(n.value)
                        elif not isinstance(t, ast.Name) and name in _named_targets(t):
                            opaque = True
                elif isinstance(n, ast.AnnAssign):
                    if isinstance(n.target, ast.Name) and n.target.id == name and n.value:
                        values.append(n.value)
                elif isinstance(
                    n, (ast.For, ast.AsyncFor, ast.AugAssign, ast.NamedExpr, ast.comprehension)
                ):
                    opaque |= name in _named_targets(n.target)
                elif isinstance(n, (ast.With, ast.AsyncWith)):
                    opaque |= any(
                        name in _named_targets(i.optional_vars)
                        for i in n.items
                        if i.optional_vars is not None
                    )
                elif isinstance(n, ast.Delete):
                    opaque |= any(name in _named_targets(t) for t in n.targets)
                elif isinstance(n, (ast.Import, ast.ImportFrom)):
                    opaque |= any((al.asname or al.name.split(".")[0]) == name for al in n.names)
                elif isinstance(n, (ast.Global, ast.Nonlocal)):
                    opaque |= name in n.names
                elif isinstance(n, ast.ExceptHandler):
                    opaque |= n.name == name
                elif isinstance(n, ast.match_case):
                    opaque |= name in _named_targets(n.pattern)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    opaque |= n.name == name
            if opaque:
                return _Binding(None, None, None, here)
            if values:
                return _Binding(None, None, values, here)
        return None

    def values_of(self, name: str) -> tuple[list[ast.expr], _Scope] | None:
        """The plain values of ``name`` and the scope they resolve in, if all are readable."""
        b = self.lookup(name)
        if b is None or b.values is None:
            return None
        return b.values, b.scope


def _within(node: ast.AST, root: ast.AST) -> bool:
    return any(x is node for x in ast.walk(root))


def _in_body(child: ast.AST, scope: ast.AST) -> bool:
    """``child`` (a direct child of ``scope``) belongs to its body, not its signature/header."""
    body = getattr(scope, "body", None)
    if isinstance(body, list):
        return any(child is s for s in body)
    return child is body  # Lambda: body is a single expression


def _walk_comp(comp: ast.AST) -> list[ast.AST]:
    """Nodes of a comprehension, descending into nested comprehensions but not functions."""
    out: list[ast.AST] = []
    todo = list(ast.iter_child_nodes(comp))
    while todo:
        n = todo.pop()
        if isinstance(n, (*_FUNCS, ast.ClassDef)):
            continue
        out.append(n)
        todo.extend(ast.iter_child_nodes(n))
    return out


_PATH_TYPES = frozenset(
    {"Path", "PurePath", "PosixPath", "WindowsPath", "PurePosixPath", "PureWindowsPath"}
)


def _mentions_path_type(node: ast.expr | None) -> bool:
    """An annotation naming a ``pathlib`` type (``Path``, ``Path | None``, ``Optional[Path]``)."""
    if node is None:
        return False
    return any(
        (isinstance(x, ast.Name) and x.id in _PATH_TYPES)
        or (isinstance(x, ast.Attribute) and x.attr in _PATH_TYPES)
        for x in ast.walk(node)
    )


def _is_path_expr(node: ast.expr, scope: _Scope, depth: int = 0) -> bool:
    """``node`` is visibly a ``pathlib`` path: ``Path(...)``, ``a / b``, a ``Path``-annotated
    parameter, or a name every plain assignment of which is one of those."""
    if isinstance(node, ast.Call):
        return _mentions_path_type(node.func)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return True
    if isinstance(node, ast.Name) and depth < _MAX_DEPTH:
        b = scope.lookup(node.id)
        if b is None:
            return False
        if b.param is not None:
            return _mentions_path_type(b.param.annotation)
        return b.values is not None and all(_is_path_expr(v, b.scope, depth + 1) for v in b.values)
    return False


def _provably_mode(node: ast.expr, scope: _Scope, depth: int = 0) -> bool:
    """``node`` can only be a mode string: a mode literal, or a name bound only to mode literals
    (a parameter counts when its default is a mode literal)."""
    if _is_mode_literal(node):
        return True
    if isinstance(node, ast.IfExp):
        return _provably_mode(node.body, scope, depth) and _provably_mode(node.orelse, scope, depth)
    if isinstance(node, ast.Name) and depth < _MAX_DEPTH:
        b = scope.lookup(node.id)
        if b is None:
            return False
        if b.param is not None:
            return b.default is not None and _is_mode_literal(b.default)
        return b.values is not None and all(_provably_mode(v, b.scope, depth + 1) for v in b.values)
    return False


def _open_mode_pos(func: ast.expr, args: list[ast.expr], scope: _Scope) -> int | None:
    """Positional index of the mode for an ``open`` call, or ``None`` if it is not a file open.

    Builtin ``open`` and ``<module>.open`` take ``(file, mode)``; ``Path.open`` takes
    ``(mode, ...)``. For any other ``x.open(...)`` the receiver's type is not visible to an AST,
    so the call shape decides: a mode-shaped literal first means ``Path.open``; a non-mode
    literal (``zf.open("a.txt", ...)``) or two or more positionals mean ``(file, mode)``.

    A lone non-literal (``x.open(v)``) is ``Path``-style only on evidence: ``v`` is provably a
    mode (bound only to mode literals, or a parameter defaulting to one), or ``x`` is visibly a
    ``Path``. Without either it is ``(file,)`` with the default mode ``"r"``, i.e. a read — the
    PR-gate advisory on #356 @ 417b3be (``zf.open(file_var)`` must not be flagged). The cost is
    the mirror case: ``x.open(v)`` where neither side is visible — e.g. an unannotated
    parameter ``p`` opened with a caller-supplied mode — is no longer flagged.
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
    if _provably_mode(args[0], scope) or _is_path_expr(func.value, scope):
        return 0
    return 1


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
    tree = ast.parse(source, filename=filename)
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        scope = _Scope.at(node, parents)
        func = node.func
        kwargs = {k.arg: k.value for k in node.keywords if k.arg is not None}
        if "newline" in kwargs or any(k.arg is None for k in node.keywords):
            continue  # decided (or **kwargs, which we cannot see into)
        if _exempt(node, lines):
            continue
        if isinstance(func, ast.Attribute) and func.attr == "write_text":
            found.append(f"{filename}:{node.lineno} write_text")
            continue
        mode_pos = _open_mode_pos(func, node.args, scope)
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
        if any(_writes_text(m) for m in _mode_leaves(mode_node, scope)):
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
        # PR-gate advisory #356 @ 417b3be: a lone unknown argument to an unknown receiver is a
        # file name (a read), not a mode -- unless the argument or the receiver shows otherwise.
        ("p.open(m)", False),
        ("zf.open(file_var)", False),
        ("def f(name):\n    return zf.open(name)", False),
        ('name = "export.txt"\nzf.open(name)', False),
        ('m = "w"\np.open(m)', True),
        ('m = "r"\np.open(m)', False),
        ('m = "a" if resume else "w"\np.open(m)', True),
        ('m = "w"\np.open(m, newline="\\n")', False),
        ('def f(p, mode="w"):\n    p.open(mode)', True),
        ('def f(p, *, mode="w"):\n    p.open(mode)', True),
        # the default marks ``mode`` as a mode; the value is the caller's, so it may write
        ('def f(p, mode="r"):\n    p.open(mode)', True),
        ("def f(p: Path, m):\n    p.open(m)", True),
        ("def f(p: Path | None, m):\n    p.open(m)", True),
        ("def f(p: Optional[Path], m):\n    p.open(m)", True),
        ("def f(p: pathlib.Path, m):\n    p.open(m)", True),
        ("def f(zf: ZipFile, name):\n    zf.open(name)", False),
        ("Path(x).open(m)", True),
        ("(root / 'a.txt').open(m)", True),
        ("q = root / 'a.txt'\nq.open(m)", True),
        ('m = "w"\nopen(f, m)', True),
        ('m = "r"\nopen(f, m)', False),
        ('m = "w"\ndef g(p):\n    m = "r"\n    p.open(m)', False),
        ('m = "r"\ndef g(p):\n    p.open(m)', False),
        ('m = "w"\ndef g(p):\n    p.open(m)', True),
        ('m = "w"\nfor m in modes:\n    p.open(m)', False),
        ("for m in modes:\n    open(f, m)", True),
        ("m = m\nopen(f, m)", True),
        ("m = m\np.open(m)", False),
        # PR-gate #359 @ 948a718 (1): an enclosing function's binding is visible (closures).
        ('def outer():\n    m = "w"\n    def inner(p):\n        p.open(m)', True),
        ('def outer():\n    m = "r"\n    def inner(p):\n        p.open(m)', False),
        ('m = "r"\ndef outer():\n    m = "w"\n    def inner(p):\n        p.open(m)', True),
        ('def outer(m="w"):\n    def inner(p):\n        p.open(m)', True),
        ("def outer(q: Path):\n    def inner(m):\n        q.open(m)", True),
        # a value resolves where it was written, not where the name is used
        ('m = "w"\ndef outer():\n    k = m\n    def inner(m):\n        p.open(k)', True),
        # PR-gate #359 @ 948a718 (2): comprehension targets are the comprehension's own ...
        ('m = "w"\n_ = [m for m in ("r",)]\np.open(m)', True),
        ('m = "w"\n_ = {m: 1 for m in xs}\np.open(m)', True),
        ('m = "w"\n_ = list(m for m in xs)\np.open(m)', True),
        # ... but inside the comprehension the target shadows the outer name
        ('m = "w"\n_ = [p.open(m) for m in xs]', False),
        ('m = "w"\n_ = [open(f, m) for m in xs]', True),
        # ... the first iterable is evaluated outside; := inside binds outside (PEP 572)
        ('m = "w"\n_ = [x for x in p.open(m)]', True),
        ('m = "w"\n_ = [(m := y) for y in xs]\np.open(m)', False),
        # PR-gate #359 @ 948a718 (3): a match capture rebinds the name
        ('m = "w"\nmatch val:\n    case m:\n        pass\np.open(m)', False),
        ('m = "w"\nmatch val:\n    case [*m]:\n        pass\np.open(m)', False),
        ('m = "w"\nmatch val:\n    case {**m}:\n        pass\np.open(m)', False),
        ('m = "w"\nmatch val:\n    case [x] if x:\n        pass\np.open(m)', True),
        # other rebindings that must not be read as the plain value
        ('m = "w"\ndel m\np.open(m)', False),
        ('m = "w"\ndef m():\n    pass\np.open(m)', False),
        ('m = "w"\ntry:\n    pass\nexcept E as m:\n    pass\np.open(m)', False),
        # a class body is a scope for code directly in it, not for its methods
        ('class C:\n    m = "w"\n    p.open(m)', True),
        ('m = "r"\nclass C:\n    m = "w"\n    def f(self, p):\n        p.open(m)', False),
        # defaults are evaluated in the enclosing scope
        ('m = "w"\ndef f(p, x=p.open(m)):\n    m = "r"', True),
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
