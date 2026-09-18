"""F-1 (T-scoped-driver-verdict-never-reaches-chatroom) — the drift alarm.

Two invariants keep the detection rule in ``docs/gate-validity-and-crossthread-rules.md``
from silently breaking:

* **T1** — ``SCOPED_REVIEW_BODY_MARKER`` exists at the canonical import path documented
  in Artifact C (``spirrow_mindwire.naysayer.pr_review``) and carries the exact literal
  the doc promises. A rename or a value change here silently breaks every downstream
  detector that imports the constant and calls ``body.startswith(...)`` on it.
* **T2** — ``scripts/naysayer_review_scoped.py`` stamps the marker at index 0 of
  ``posted_body``, BEFORE ``prepend_gate_notice``. The script is read as source and
  parsed with :mod:`ast` — never imported (``scripts/`` is not a package; importing the
  file would pull in the orchestrator/driver stack and any accidental construction of
  the gate would cost a billed Gemini call — same rationale as
  :mod:`tests.test_gate_command_doc_consistency`).

If either assertion goes red, the invariant the F-1 detection rule keys on is no longer
true, and the ``body.startswith(SCOPED_REVIEW_BODY_MARKER)`` exemption in
``docs/gate-validity-and-crossthread-rules.md`` §2 will silently mis-classify
scoped-driver reviews. Do not fix the assertion — fix the code the assertion describes.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from spirrow_mindwire.naysayer.pr_review import SCOPED_REVIEW_BODY_MARKER

_REPO = Path(__file__).resolve().parents[1]
_SCOPED_DRIVER = _REPO / "scripts" / "naysayer_review_scoped.py"
_F1_DOC = _REPO / "docs" / "gate-validity-and-crossthread-rules.md"

# The exact literal the F-1 doc's detection rule keys on. A change here without a matching
# doc edit is the drift T1 exists to catch.
_EXPECTED_MARKER = "<!-- naysayer:scoped-driver -->"


def test_t1_marker_constant_is_the_documented_literal() -> None:
    """T1: the imported constant equals the literal quoted in the F-1 doc.

    Downstream detectors follow the doc's ``from spirrow_mindwire.naysayer.pr_review
    import SCOPED_REVIEW_BODY_MARKER`` line; if the constant's value drifts away from
    the string the doc promises, every ``startswith`` caller silently starts missing
    the scoped-driver marker.
    """
    assert SCOPED_REVIEW_BODY_MARKER == _EXPECTED_MARKER
    # The doc must also quote the same literal — a doc-only rename is drift too.
    doc_text = _F1_DOC.read_text(encoding="utf-8")
    assert _EXPECTED_MARKER in doc_text, (
        "docs/gate-validity-and-crossthread-rules.md must quote the exact marker literal "
        f"{_EXPECTED_MARKER!r} that detectors will match on."
    )


def _read_scoped_driver_ast() -> ast.Module:
    """Parse the scoped driver as source. Never import — see module docstring."""
    src = _SCOPED_DRIVER.read_text(encoding="utf-8")
    return ast.parse(src, filename=str(_SCOPED_DRIVER))


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
    """
    found: list[ast.AST] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "posted_body":
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


def test_t3_notice_stderr_lines_are_emitted_before_ci_gate() -> None:
    """B (msg-3373 §3.2): the three-line stderr NOTICE prints before any CI-gate return.

    Structural check: locate the first ``print(..., file=sys.stderr)`` triple in
    ``main()`` and confirm all three carry the ``[scoped-naysayer] NOTICE`` /
    ``verdict lives only in stdout`` / ``detectors keyed on 'ledger thread + reviews'``
    lines. These lines must be reachable on every invocation — including the CI-red /
    timeout(exit 3) / empty(exit 4) early-return paths — which is why they sit
    immediately after ``parse_args()`` and before any awaited call. Reading the source
    is enough to enforce ordering: a later PR that moves them after ``await
    github.fetch_ci_status(...)`` would let the CI-red return skip them silently.
    """
    src = _SCOPED_DRIVER.read_text(encoding="utf-8")
    # The three sentinel substrings the doc / decide committed to.
    for phrase in (
        "NOTICE: this driver does NOT post to the chatroom",
        "verdict lives only in stdout",
        "detectors keyed on 'ledger thread + reviews'",
    ):
        assert phrase in src, f"scoped driver source must carry the stderr phrase {phrase!r}"

    # Ordering: the NOTICE block must appear before the first ``await`` inside main().
    # A regex is safer than parsing here — it only asks "does the NOTICE precede the
    # first await?" and answers False when someone reorders them.
    notice_pos = src.find("NOTICE: this driver does NOT post to the chatroom")
    first_await = re.search(r"^\s+await\s", src, flags=re.MULTILINE)
    assert first_await is not None, "scoped driver must still contain an await call"
    assert notice_pos < first_await.start(), (
        "stderr NOTICE must be emitted BEFORE the first awaited call so CI-red / "
        "timeout / empty-reply early returns cannot skip it."
    )
