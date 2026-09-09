"""The gate's *executable* contract is ``argparse``; every prose copy is a checked mirror.

Machine check behind D-1 (``T-naysayer-skill-doc-has-two-divergent-copies`` msg-2846).

#244 (``14e3e5f``, merged as ``efb88f4``) made ``--design-thread`` a **required** argument of
``scripts/naysayer_review.py`` and updated no prose. Three separate copies of the command line
kept telling operators to run the old form; the first one to obey a copy got
``error: the following arguments are required: --design-thread``, exit 2. Nothing failed at the
time the divergence was introduced, because nothing was comparing the prose to the flags.

So the source of truth is the driver's ``argparse`` block, and prose may only *mirror* it:

* **A** — the driver's own module docstring ``Run::`` block names every required flag.
* **B** — ``.claude/skills/naysayer-review/SKILL.md``'s fenced command line names every
  required flag.
* **C** — a census: those two files are the *only* places in the repository carrying a runnable
  ``naysayer_review.py`` command line. A third copy is the next drift source, so the count is
  what is pinned, not just the content of the two known copies.
* **D** — no conditional hedge about ``--design-thread`` survives in a file that mentions the
  flag. The hedge that actually caused this ("Check the driver's ``--help`` before assuming
  which of the two forms this checkout takes") reads as "maybe not yet" for a condition that is
  already satisfied.

Two deliberate limits, both load-bearing:

* The driver is read as **source text and parsed with :mod:`ast` — never imported**. Importing
  it pulls in the orchestrator/driver stack, so an unrelated import failure would turn this
  *documentation* check red, and any accidental construction of the gate costs a billed Gemini
  call. ``scripts/`` is not an importable package anyway, which is why
  :mod:`tests.test_pr_review_sweep_phase0_cli` loads its script by path.
* ``~/.claude/**`` is **not** checked. CI runs on ``ubuntu-latest`` with only ``actions/checkout``,
  so the user-level skills directory does not exist there: a check over it would be vacuously
  green on CI (worse than absent — it would look enforced) or permanently red. Machine checks
  cannot cross the repository boundary; that half is a human-applied change, tracked in the
  thread.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_DRIVER = _REPO / "scripts" / "naysayer_review.py"
_SKILL = _REPO / ".claude" / "skills" / "naysayer-review" / "SKILL.md"

# The two files allowed to carry a runnable command line (check C). Relative POSIX paths so the
# failure message reads the same on Windows and on the CI runner.
_ALLOWED_COMMAND_FILES = frozenset(
    {
        ".claude/skills/naysayer-review/SKILL.md",
        "scripts/naysayer_review.py",
    }
)

# A *runnable* command line: the script path immediately preceded by an interpreter. This is the
# discriminator that separates "here is what to type" from the many prose mentions of the script
# by name (docs/adr/…, src/…, docs/operator-board-design.md), which are not instructions and must
# not be dragged into the census. It also does not match ``naysayer_review_scoped.py``, a
# different script with its own command line.
_COMMAND_RE = re.compile(r"python[0-9.]*\s+\S*naysayer_review\.py")

# Verbatim hedges, not keywords. A repo-wide ban on a phrase like "lands on" would false-positive
# on unrelated prose — measured: 22 hits, including docs/deploy.md:462 ("an operator's eye lands
# on it") and spec/NAYSAYER_PRINCIPLES.md:113 ("lands on the blocking side"). Scoping to files
# that mention the flag *and* matching whole clauses keeps this specific.
_BANNED_HEDGES = (
    "before assuming which of the two forms",
    "lands on `main`, the driver",
)

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "node_modules",
        ".idea",
        ".vscode",
        "build",
        "dist",
    }
)


def _repo_text_files() -> list[tuple[str, str]]:
    """Every readable text file in the repo as ``(relative posix path, content)``.

    ``os.walk`` with in-place pruning rather than ``Path.rglob``: ``rglob`` descends into
    ``.venv`` before the filter can drop it, which on a synced checkout is tens of thousands of
    files.
    """
    found: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(_REPO):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable: cannot carry a command line to read
            found.append((path.relative_to(_REPO).as_posix(), text))
    return found


def _required_long_options() -> frozenset[str]:
    """Long options the driver's ``argparse`` marks ``required=True`` — the actual contract."""
    tree = ast.parse(_DRIVER.read_text(encoding="utf-8"))
    required: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        is_required = any(
            kw.arg == "required" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
        if not is_required:
            continue
        for arg in node.args:
            flag = arg.value if isinstance(arg, ast.Constant) else None
            if isinstance(flag, str) and flag.startswith("--"):
                required.add(flag)
    return frozenset(required)


def _missing_flags(text: str, flags: frozenset[str]) -> list[str]:
    """Flags absent from ``text``, matched on a word boundary so ``--pr`` ≠ ``--project``."""
    return sorted(f for f in flags if not re.search(rf"{re.escape(f)}\b", text))


def _join_continuations(lines: list[str], start: int) -> str:
    """One shell command, including ``\\``-continued lines."""
    parts = [lines[start]]
    index = start
    while parts[-1].rstrip().endswith("\\") and index + 1 < len(lines):
        index += 1
        parts.append(lines[index])
    return "\n".join(parts)


def _docstring_run_block() -> str:
    """The indented block under ``Run::`` in the driver's module docstring."""
    docstring = ast.get_docstring(ast.parse(_DRIVER.read_text(encoding="utf-8")))
    assert docstring is not None, f"{_DRIVER} has no module docstring to mirror the flags in"
    lines = docstring.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "Run::"]
    if not starts:
        pytest.fail(f"{_DRIVER} module docstring has no 'Run::' block to check (check A)")
    block: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line.strip() and not line[:1].isspace():
            break  # back to column 0 = the next paragraph, block over
        block.append(line)
    return "\n".join(block)


def _skill_command_line() -> str:
    """The fenced command line in the repo skill."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    in_fence = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and _COMMAND_RE.search(line):
            return _join_continuations(lines, index)
    pytest.fail(f"{_SKILL} has no fenced naysayer_review.py command line to check (check B)")


def test_required_flags_are_actually_discoverable() -> None:
    """Guard against A and B passing vacuously.

    If the driver's argparse is ever restructured so this extraction yields nothing (a loop over
    a table, a shared parser factory), A and B would go green while checking nothing. An empty
    contract is the one result that must not be treated as "consistent".
    """
    required = _required_long_options()
    assert required, (
        f"no required long options parsed out of {_DRIVER} — either argparse moved or the "
        "extraction broke. Checks A and B would be vacuous, so this fails instead."
    )


def test_a_driver_docstring_names_every_required_flag() -> None:
    """Check A: the driver's own ``Run::`` example is runnable as written."""
    required = _required_long_options()
    missing = _missing_flags(_docstring_run_block(), required)
    assert not missing, (
        f"{_DRIVER} docstring 'Run::' block omits required flag(s) {missing}. The example as "
        "written would exit 2 at argparse."
    )


def test_b_repo_skill_command_line_names_every_required_flag() -> None:
    """Check B: the skill an operator copies from is runnable as written.

    This is the check that would have caught #244: it made ``--design-thread`` required and left
    every prose copy on the old form.
    """
    required = _required_long_options()
    missing = _missing_flags(_skill_command_line(), required)
    assert not missing, (
        f"{_SKILL} command line omits required flag(s) {missing}. An operator copying it gets "
        "'error: the following arguments are required' and exit 2."
    )


def test_c_only_two_files_carry_a_runnable_gate_command() -> None:
    """Check C (census): pin the *number* of copies, not just the content of the known ones.

    Content checks alone let a third copy appear somewhere unchecked and drift on its own — which
    is exactly how this thread's defect was produced.
    """
    carriers = {rel for rel, text in _repo_text_files() if _COMMAND_RE.search(text)}
    assert carriers == set(_ALLOWED_COMMAND_FILES), (
        "the set of files carrying a runnable naysayer_review.py command line changed.\n"
        f"  expected: {sorted(_ALLOWED_COMMAND_FILES)}\n"
        f"  found:    {sorted(carriers)}\n"
        "A new copy is a new drift source: fold it into one of the two mirrors, or make it a "
        "pointer to them. Do not widen this list without saying who keeps the new copy true."
    )


def test_d_no_conditional_hedge_survives_where_the_flag_is_documented() -> None:
    """Check D: the condition is satisfied, so prose must not still be waiting for it.

    ``#244`` landed on ``main`` as ``efb88f4``. A sentence saying the flag *will become* required
    now misinforms in the direction that costs an operator a failed run.
    """
    scanned = [
        (rel, text)
        for rel, text in _repo_text_files()
        if "--design-thread" in text and rel != Path(__file__).relative_to(_REPO).as_posix()
    ]
    # The self-exclusion above is only for this checker, which must quote the banned phrases to
    # ban them. It is excluded from D alone — never from the census in C.
    assert scanned, (
        "no file documents --design-thread any more, so check D has nothing to police. That is "
        "either a deletion to undo or a rename to teach this check about."
    )
    offenders = [
        (rel, hedge)
        for rel, text in scanned
        for hedge in _BANNED_HEDGES
        if hedge in " ".join(text.split())  # normalized: the hedges are line-wrapped in the wild
    ]
    assert not offenders, (
        f"conditional hedge(s) about --design-thread still present: {offenders}. The condition "
        "was satisfied by efb88f4 (#244) — state the requirement, do not defer it to --help."
    )
