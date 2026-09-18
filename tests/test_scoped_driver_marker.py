"""F-1 (T-scoped-driver-verdict-never-reaches-chatroom) — the drift alarm.

Three invariants keep the detection rule in ``docs/gate-validity-and-crossthread-rules.md``
from silently breaking:

* **T1** — ``SCOPED_REVIEW_BODY_MARKER`` exists at the canonical import path documented
  in Artifact C (``spirrow_mindwire.naysayer.pr_review``) AND the F-1 doc quotes the same
  literal. Enforcing this without adding a third copy of the string to the test file
  (PR-gate on #289 msg-3382: "tri-management of the marker literal") — the constant
  imported at module load IS the source of truth; the doc must contain that same string,
  and the test contains no hardcoded copy.
* **T2** — ``scripts/naysayer_review_scoped.py`` stamps the marker at index 0 of
  ``posted_body``, BEFORE ``prepend_gate_notice``. The script is read as source and
  parsed with :mod:`ast` — never imported (``scripts/`` is not a package; importing the
  file would pull in the orchestrator/driver stack and any accidental construction of
  the gate would cost a billed Gemini call — same rationale as
  :mod:`tests.test_gate_command_doc_consistency`).
* **T3** — the three-line stderr NOTICE (Artifact B) is emitted in ``main()`` BEFORE the
  first awaited call inside ``main()``. Also AST-based — a textual regex over the whole
  source cannot see function boundaries (an ``await`` inside a helper defined before
  ``main()`` would decoy the check) nor common Python syntax (``x = await f()`` starts
  with ``x``, not ``await``, so a line-anchored regex like ``^\\s+await\\s`` silently
  misses it — PR-gate on #289 msg-3382, correctness objection).

If any assertion goes red, the invariant the F-1 detection rule keys on is no longer
true, and the ``body.startswith(SCOPED_REVIEW_BODY_MARKER)`` exemption in
``docs/gate-validity-and-crossthread-rules.md`` §2 will silently mis-classify
scoped-driver reviews. Do not fix the assertion — fix the code the assertion describes.
"""

from __future__ import annotations

import ast
from pathlib import Path

from spirrow_mindwire.naysayer.pr_review import SCOPED_REVIEW_BODY_MARKER

_REPO = Path(__file__).resolve().parents[1]
_SCOPED_DRIVER = _REPO / "scripts" / "naysayer_review_scoped.py"
_F1_DOC = _REPO / "docs" / "gate-validity-and-crossthread-rules.md"


def test_t1_marker_constant_matches_the_documented_literal() -> None:
    """T1: the imported constant appears verbatim inside the F-1 doc; both are HTML-comment shaped.

    Two assertions, both keyed on the imported constant itself (no hardcoded copy in
    this file — PR-gate on #289 §2 asked us to remove the third source of truth). A
    legitimate rename of the constant value + doc together stays green; a doc-only
    rename or a constant-only rename goes red.

    The HTML-comment shape check (``<!-- ... -->``) is a structural guard, not a
    duplicate of the literal: if the constant is ever changed to something that GitHub
    markdown WOULD render (say, a bold header), the raw ``body`` would still carry the
    string for a ``startswith`` check but the human-readable review UI would suddenly
    show F-1 diagnostic bytes — a regression the marker's shape rules out.
    """
    doc_text = _F1_DOC.read_text(encoding="utf-8")
    assert SCOPED_REVIEW_BODY_MARKER in doc_text, (
        f"docs/gate-validity-and-crossthread-rules.md must quote "
        f"SCOPED_REVIEW_BODY_MARKER={SCOPED_REVIEW_BODY_MARKER!r} verbatim so detectors "
        "reading the doc find the exact string the constant carries."
    )
    assert SCOPED_REVIEW_BODY_MARKER.startswith("<!--"), (
        f"SCOPED_REVIEW_BODY_MARKER={SCOPED_REVIEW_BODY_MARKER!r} must be an HTML comment "
        "(starts with '<!--') so it renders as empty in GitHub's PR review UI."
    )
    assert SCOPED_REVIEW_BODY_MARKER.endswith("-->"), (
        f"SCOPED_REVIEW_BODY_MARKER={SCOPED_REVIEW_BODY_MARKER!r} must end with '-->' so "
        "the HTML comment closes and no downstream body content is accidentally hidden."
    )


def _read_scoped_driver_ast() -> ast.Module:
    """Parse the scoped driver as source. Never import — see module docstring."""
    src = _SCOPED_DRIVER.read_text(encoding="utf-8")
    return ast.parse(src, filename=str(_SCOPED_DRIVER))


def _find_main_func(module: ast.Module) -> ast.AsyncFunctionDef:
    """Locate the top-level ``async def main`` — the only place T3 cares about."""
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main":
            return node
    raise AssertionError(
        f"{_SCOPED_DRIVER.name} must define a top-level `async def main` "
        "for the F-1 stderr-notice ordering check to have a scope."
    )


def _finds_import_of_marker(module: ast.Module) -> bool:
    for node in ast.walk(module):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module != "spirrow_mindwire.naysayer.pr_review":
            continue
        for alias in node.names:
            if alias.name == "SCOPED_REVIEW_BODY_MARKER":
                return True
    return False


def _find_posted_body_assignment(module: ast.Module) -> ast.AST:
    """Return the RHS of the ``posted_body = ...`` assignment inside ``main()``.

    The scoped driver has exactly one ``posted_body`` assignment (verified by the second
    assertion below). If a second one is ever added, this helper fails loudly rather
    than silently picking the wrong one.

    Accepts both :class:`ast.Assign` (``posted_body = ...``) and :class:`ast.AnnAssign`
    (``posted_body: str = ...``) — the latter shape is what a routine type-annotation
    refactor produces, and a strict ``isinstance(node, ast.Assign)`` filter would
    silently miss it and report zero matches. PR-gate on #289 msg-3387 §3, structure
    advisory. An ``ast.AnnAssign`` without a value (a pure annotation like
    ``posted_body: str``) has ``node.value is None`` and is skipped — it is not an
    assignment we can extract a RHS from.
    """
    found: list[ast.AST] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "posted_body":
                    found.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "posted_body"
            and node.value is not None
        ):
            found.append(node.value)
    assert len(found) == 1, (
        f"expected exactly one 'posted_body' assignment in {_SCOPED_DRIVER.name}, "
        f"found {len(found)}"
    )
    return found[0]


def test_t2_posted_body_is_prefixed_with_the_marker() -> None:
    """T2: the scoped driver stamps ``SCOPED_REVIEW_BODY_MARKER`` at index 0 of ``posted_body``.

    Read as source, parsed with :mod:`ast`. Two structural facts must hold together —
    both are load-bearing for the F-1 detection rule:

    1. The script imports ``SCOPED_REVIEW_BODY_MARKER`` from the canonical path
       (``spirrow_mindwire.naysayer.pr_review``); a local literal would be drift.
    2. The ``posted_body = ...`` assignment is an f-string whose *first* formatted
       part is the imported ``SCOPED_REVIEW_BODY_MARKER`` name. The gate-notice call
       (``prepend_gate_notice(...)``) appears LATER in the same f-string, so a
       truncation / length-cap invocation still leaves the marker at ``body[0]``.
    """
    module = _read_scoped_driver_ast()

    assert _finds_import_of_marker(module), (
        "scripts/naysayer_review_scoped.py must import SCOPED_REVIEW_BODY_MARKER from "
        "spirrow_mindwire.naysayer.pr_review — the F-1 rule forbids literal duplication."
    )

    rhs = _find_posted_body_assignment(module)
    assert isinstance(rhs, ast.JoinedStr), (
        "posted_body RHS must be an f-string that stamps the marker at index 0 followed "
        "by prepend_gate_notice(...); non-f-string shapes cannot express both in one "
        "assignment reliably."
    )

    # The f-string's first value part must reference SCOPED_REVIEW_BODY_MARKER as a Name
    # (i.e. an f-string {SCOPED_REVIEW_BODY_MARKER} placeholder). Any preceding literal
    # string part is a bug — the marker must sit at byte offset 0.
    parts = rhs.values
    assert parts, "posted_body f-string must have at least one value part"
    first = parts[0]
    assert isinstance(first, ast.FormattedValue), (
        "first f-string part of posted_body must be a formatted {SCOPED_REVIEW_BODY_MARKER} "
        "placeholder (not a literal), so the marker sits at index 0 of the output."
    )
    assert isinstance(first.value, ast.Name), (
        "first {} in posted_body f-string must be a bare Name node, not an expression."
    )
    assert first.value.id == "SCOPED_REVIEW_BODY_MARKER", (
        f"first {{}} in posted_body f-string must be SCOPED_REVIEW_BODY_MARKER, "
        f"got {first.value.id!r}."
    )

    # ``prepend_gate_notice`` must still be called somewhere in that f-string — the
    # existing invariant that the gate notice reaches the posted body when the review
    # was truncated / length-capped must NOT be dropped by the marker stamp.
    found_prepend = False
    for part in parts:
        if isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Call):
            func = part.value.func
            if isinstance(func, ast.Name) and func.id == "prepend_gate_notice":
                found_prepend = True
                break
    assert found_prepend, (
        "posted_body f-string must still invoke prepend_gate_notice(body, decision) — "
        "stamping the marker must not silently drop the truncation gate notice."
    )


def _print_calls_carrying_phrase(func: ast.AsyncFunctionDef, phrase: str) -> list[ast.Call]:
    """Return every ``print(...)`` call inside ``func`` whose string args contain
    ``phrase`` as a substring.

    ``ast.walk(func)`` visits every descendant node, so a ``print`` sitting inside an
    if/try block still counts. Each positional argument to ``print(...)`` must be a
    single :class:`ast.Constant` string node for this check to be sound; that is the
    shape the scoped driver uses today.

    Note on implicit string concatenation (PR-gate on #289 msg-3387 §1, docs advisory):
    the ``"a" " b"`` shape used at the module's line 130/131/135/136 is NOT preserved
    as separate nodes in the AST — CPython's parser folds implicit adjacent string
    literals at compile time into a single :class:`ast.Constant` whose ``value`` is
    already ``"a b"``. So no reconstruction from multiple constants is happening or
    needed. What we're extracting is the single, pre-folded constant per argument.

    On unspecified iteration order (PR-gate on #289 msg-3387 §2, structure advisory):
    :func:`ast.walk` does not guarantee traversal order across siblings, so
    concatenating text pulled out of nested nodes (f-strings' ``FormattedValue``
    interleaved with literal ``Constant`` parts, ``ast.BinOp`` string additions, etc.)
    would jam substrings together in an unspecified order and silently produce false
    negatives on the substring match. We defend against that by restricting each
    argument to a bare ``ast.Constant`` string — every other shape fails loudly via
    the ``other_shape`` marker below, forcing whoever refactored the script to update
    this helper deliberately rather than let a scrambled ``text`` pass silently.
    """
    hits: list[ast.Call] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "print"):
            continue
        text_parts: list[str] = []
        other_shape = False
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                text_parts.append(arg.value)
            else:
                # Any non-string-Constant positional arg (f-string, BinOp, Name, Call,
                # bytes, number, ...) means the caller reshaped their print args in a
                # way this helper cannot reason about deterministically. Skip THIS
                # print call rather than pretend we know its text. The T3 assertions
                # then fire on "no matching print found for phrase X", telling the
                # refactorer to update this helper for the new shape.
                other_shape = True
                break
        if other_shape:
            continue
        if phrase in "".join(text_parts):
            hits.append(node)
    return hits


def test_t3_notice_stderr_lines_are_emitted_before_first_await_in_main() -> None:
    """B (msg-3373 §3.2): the three-line stderr NOTICE prints before any awaited call.

    AST-based ordering check — the PR-gate correctness objection on #289 (msg-3382)
    caught two ways a source-text regex silently misses awaits:

    1. ``x = await f()`` starts the line with ``x``, not ``await``, so a regex like
       ``^\\s+await\\s`` finds no match on that line. The first ``await`` in the scoped
       driver's ``main()`` today is exactly that shape (``ci = await
       github.fetch_ci_status(pr)``) — a line-anchored regex misses it and settles for
       a later ``await github.submit_review(...)``, hiding the true first-await
       position from the test.
    2. A regex over the whole module can't see function boundaries; an ``await`` in a
       helper defined before ``main()`` (e.g., a future ``async def _fetch_scope()``)
       would decoy the check and false-positive.

    The AST check locates ``main()``, walks its body for the first :class:`ast.Await`
    node, and asserts that every ``print(...)`` carrying one of the three sentinel
    stderr phrases has a source-line position strictly less than that first-await line.
    """
    module = _read_scoped_driver_ast()
    main_func = _find_main_func(module)

    awaits = [n for n in ast.walk(main_func) if isinstance(n, ast.Await)]
    assert awaits, (
        f"{_SCOPED_DRIVER.name}::main must still contain at least one await — the "
        "T3 ordering check is trivially green without one, which would hide regressions."
    )
    first_await_line = min(a.lineno for a in awaits)

    sentinel_phrases = (
        "NOTICE: this driver does NOT post to the chatroom",
        "verdict lives only in stdout",
        "detectors keyed on 'ledger thread + reviews'",
    )
    for phrase in sentinel_phrases:
        prints = _print_calls_carrying_phrase(main_func, phrase)
        assert prints, (
            f"main() must contain a print(...) call carrying the F-1 stderr phrase "
            f"{phrase!r}; without it the CI-red / timeout / empty-reply early-return "
            "paths give the operator no notice at all."
        )
        earliest = min(p.lineno for p in prints)
        assert earliest < first_await_line, (
            f"print(...) carrying {phrase!r} (line {earliest}) must precede the first "
            f"await in main() (line {first_await_line}) so early-return paths never "
            "skip the NOTICE."
        )
