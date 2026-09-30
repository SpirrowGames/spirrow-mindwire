"""U2 (T-reconcile-field-mismatch-flag-overloaded msg-5094): the fixture builder reads the
positive ``Handoff.author_requested_human`` fact, not ``mismatch_reason is None``.

The stored column keeps its name ``author_wrote_next_human`` (msg-5094 §3); only its derivation
changes. On every ``HUMAN`` head the resolver produces the two predicates agree (§2), so the
committed ``eval/tierc/`` data is unchanged — the regression pin below builds the one ``HUMAN``
handoff on which they differ.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.conductor.handoff import Handoff, HandoffKind, MismatchReason
from spirrow_mindwire.value_objects import Role

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"{name}_u2_module", ROOT / "scripts" / f"{name}.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{name}_u2_module"] = mod
    spec.loader.exec_module(mod)
    return mod


builder = _load("build_tierc_eval_fixture")

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
ROSTER = {"Bohr": Role.PROPOSER, "Einstein": Role.NAYSAYER}


def _msg(i: int, author: str, content: str, role: str | None = None) -> Any:
    return builder.RawMessage(
        msg_id=f"msg-{i}",
        author=author,
        content=content,
        timestamp=T0 + timedelta(minutes=i),
        role=role,
        next_participant=None,
    )


def _row(head_content: str, field: str | None) -> dict[str, Any]:
    msgs = [
        _msg(1, "Fermi", "goal\n\nNEXT: Bohr"),
        _msg(2, "Bohr", "design\n\nNEXT: Einstein", role="proposer"),
        _msg(3, "Einstein", "ok\n\nNEXT: Bohr", role="naysayer"),
        dataclasses.replace(_msg(4, "Bohr", head_content, role="proposer"), next_participant=field),
    ]
    return dict(
        builder.build_eval_row(
            project="spirrow-mindwire",
            thread_id="T-x",
            messages=msgs,
            head_index=3,
            current_roster=ROSTER,
            now=msgs[3].timestamp,
            set_name="eval",
        )
    )


# One case list, and the branch check runs on the very call that produces the column: the
# resolver is wrapped, so each case asserts the ``Handoff`` that ``build_eval_row`` itself resolved
# from these exact inputs (not a proxy resolve on other strings). ``AUTHOR`` marks the
# author-requested branch.
AUTHOR = "author_requested_human"
FOUR_HUMAN_PATHS = [
    pytest.param("decide\n\nTIER-C: goal\nNEXT: human", None, AUTHOR, True, id="body-next-human"),
    pytest.param("decide, no body token", "human", AUTHOR, True, id="field-human"),
    pytest.param(
        "design\n\nNEXT: Einstein",
        "none",
        MismatchReason.TARGET_DIVERGENCE,
        False,
        id="target-divergence",
    ),
    pytest.param(
        "design\n\nNEXT: Einstein",
        "Schrodinger",
        MismatchReason.FIELD_UNRESOLVABLE,
        False,
        id="field-unresolvable",
    ),
]


@pytest.mark.parametrize(("head_content", "field", "branch", "expected"), FOUR_HUMAN_PATHS)
def test_column_on_the_four_human_paths(
    monkeypatch: pytest.MonkeyPatch,
    head_content: str,
    field: str | None,
    branch: object,
    expected: bool,
) -> None:
    seen: list[Handoff] = []
    real = builder.resolve_handoff

    def spy(*args: Any, **kwargs: Any) -> Handoff:
        h: Handoff = real(*args, **kwargs)
        seen.append(h)
        return h

    monkeypatch.setattr(builder, "resolve_handoff", spy)
    row = _row(head_content, field)

    # The case reaches the resolver branch it is named for, on the builder's own call.
    assert len(seen) == 1
    handoff = seen[0]
    assert handoff.kind is HandoffKind.HUMAN
    if branch == AUTHOR:
        assert handoff.author_requested_human is True
        assert handoff.mismatch_reason is None
    else:
        assert handoff.author_requested_human is False
        assert handoff.mismatch_reason is branch

    assert row["author_wrote_next_human"] is expected


def test_human_without_author_request_or_mismatch_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression pin (the #370 ``core.py`` shape): a ``HUMAN`` escalation that is neither a
    mismatch nor the author's request must not count as the author asking for the human. The
    old ``mismatch_reason is None`` derivation returns ``True`` here."""
    plain = Handoff(HandoffKind.HUMAN)
    assert plain.mismatch_reason is None and plain.author_requested_human is False
    monkeypatch.setattr(builder, "resolve_handoff", lambda *a, **k: plain)
    row = _row("decide\n\nNEXT: human", None)
    assert row["author_wrote_next_human"] is False
    assert row["live_entry"] is False
