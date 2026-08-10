"""Tests for the harness-stamped source marker (msg-805 M3'' / msg-834 §2).

Three tests, matching the msg-834 §2 requirement (with T-c downgraded to a
review-time diff check per the Tier-C 2026-08-11 ruling on this thread):

- **T-a** — ``test_marker_derived_from_options`` — table-driven: vary
  ``tools`` / ``mcp_servers`` / ``setting_sources`` on the options object and
  assert the marker line changes correspondingly. Covers the three
  distinguishable ``setting_sources`` shapes (``None`` / ``[]`` / non-empty)
  called out in msg-805 M3'' #5.
- **T-b** — ``test_marker_invariant_to_agent_body`` — the marker is
  independent of the agent's message body under the same options: an
  arbitrary body (including one whose text is literally a fully-formed
  marker string) yields the same appended marker line, because the marker is
  derived from options only. This is the behavioural analogue of msg-805 D3:
  a fake marker written inside the body is not adopted.
- **T-d** — ``test_marker_contradicts_false_capability_claim`` — the
  msg-834 §2 fixture: ``tools=[]`` options plus a body claiming "I can
  open PRs" yields a marker whose ``tools=0`` contradicts the body. The
  marker records the truth (options), the body records the claim, and the
  marker does not deny the body; **the two are independent surfaces**, and
  the marker's independence is what makes the discrepancy readable.

**T-c (downgraded to review-only, Tier-C 2026-08-11).** The condition
"marker builder is unreachable from any adapter's import graph" (msg-834
§2 (c)) is verified for this PR by reading the diff: no file under
``src/spirrow_mindwire/adapters/`` imports
:mod:`spirrow_mindwire.source_marker`. The reviewer confirms this from the
diff (PR body §Structural conditions), not via a static-assert test. The
Tier-C 2026-08-11 message notes the reason: a static assertion via AST is
"目的 (モジュールの隔離) に対して機構が重すぎる", and the behavioural
guarantee is already carried by T-a / T-b / T-d.

The pure function accepts any object with the three attribute names
(``tools`` / ``mcp_servers`` / ``setting_sources``), so these tests use a
``SimpleNamespace`` fixture and do not need a real
``ClaudeAgentOptions``. The dispatcher wiring under
:mod:`tests.test_dispatcher_core` covers the production path where the
real options object flows in.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from spirrow_mindwire.source_marker import (
    SOURCE_MARKER_PREFIX,
    SOURCE_MARKER_SUFFIX,
    append_source_marker,
    render_source_marker,
)


def _options(
    *,
    tools: list[str] | None = None,
    mcp_servers: dict[str, Any] | None = None,
    setting_sources: list[str] | None = None,
) -> SimpleNamespace:
    """Build a stand-in for ``ClaudeAgentOptions`` with just the three fields.

    ``setting_sources`` uses the sentinel ``[]`` for "explicitly empty" — the
    caller passes ``None`` to mean "kwarg not passed at all" (which is
    exactly how ``ClaudeAgentOptions`` treats a missing kwarg vs an empty
    list). Test bodies use this to hit all three cases msg-805 M3'' #5
    calls out.
    """
    return SimpleNamespace(
        tools=tools,
        mcp_servers=mcp_servers,
        setting_sources=setting_sources,
    )


# --------------------------------------------------------------------------- #
# T-a — marker is derived from options (table-driven, msg-834 §2 (a))
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("setting_sources", "expected_value"),
    [
        (None, "setting_sources=unset"),
        ([], "setting_sources=empty"),
        (["user", "project"], "setting_sources=user+project"),
    ],
    ids=("unset", "empty", "user+project"),
)
def test_marker_setting_sources_three_cases(
    setting_sources: list[str] | None, expected_value: str
) -> None:
    # M3'' #5: exactly the three cases the SDK distinguishes.
    marker = render_source_marker(_options(setting_sources=setting_sources))
    assert expected_value in marker
    # And the marker prefix + suffix wrap it so the line is a valid HTML
    # comment (M3'' #2: the form is stable).
    assert marker.startswith(SOURCE_MARKER_PREFIX)
    assert marker.endswith(SOURCE_MARKER_SUFFIX)


@pytest.mark.parametrize(
    ("tools", "mcp_servers", "setting_sources", "expected"),
    [
        # All three counts / value shapes on distinct lines so a change in
        # one drives a change in the marker (T-a's core claim).
        (None, None, None, "tools=0 · mcp=0 · setting_sources=unset"),
        ([], {}, [], "tools=0 · mcp=0 · setting_sources=empty"),
        (["Read"], None, None, "tools=1 · mcp=0 · setting_sources=unset"),
        (
            ["Read", "Write"],
            {"srv": object()},
            ["user"],
            "tools=2 · mcp=1 · setting_sources=user",
        ),
    ],
    ids=(
        "all-unset",
        "empty-triple",
        "one-tool",
        "two-tools-one-mcp-one-source",
    ),
)
def test_marker_derived_from_options(
    tools: list[str] | None,
    mcp_servers: dict[str, Any] | None,
    setting_sources: list[str] | None,
    expected: str,
) -> None:
    """T-a: table-driven — vary options → marker text tracks each field."""
    marker = render_source_marker(
        _options(tools=tools, mcp_servers=mcp_servers, setting_sources=setting_sources)
    )
    assert expected in marker


# --------------------------------------------------------------------------- #
# Defensive-typing regression (PR #139 Tier B Finding 2)
# --------------------------------------------------------------------------- #


def test_marker_setting_sources_bare_string_is_not_iterated_per_char() -> None:
    """A bare string sneaked in as ``setting_sources`` must render verbatim.

    ``ClaudeAgentOptions`` types the field as ``list[SettingSource]`` at
    the SDK boundary, but this module duck-types at its input. Without the
    ``isinstance(src, str)`` guard the ``len(src)`` branch would iterate
    the string per-character and the marker would read
    ``setting_sources=u+s+e+r`` instead of ``setting_sources=user``. Pin
    the defensive fallback so a future refactor that removes the guard
    fails this test loudly (PR #139 Tier B Finding 2, VERIFIED).
    """
    marker = render_source_marker(_options(setting_sources="user"))  # type: ignore[arg-type]
    assert "setting_sources=user" in marker
    assert "setting_sources=u+s+e+r" not in marker


# --------------------------------------------------------------------------- #
# T-b — marker is independent of the agent's body (msg-834 §2 (b))
# --------------------------------------------------------------------------- #


def test_marker_invariant_to_agent_body() -> None:
    """T-b: same options → same marker, no matter what the body contains.

    Special case (called out in msg-834 §2 T-b): a body that literally
    contains a fully-formed marker string does not change the marker the
    harness stamps. The marker is derived from options only; a body-level
    "declaration" is not adopted.
    """
    opts = _options(tools=[], mcp_servers=None, setting_sources=[])
    expected_marker = render_source_marker(opts)

    bodies = [
        "some reply body",
        "",  # empty body (edge)
        "another reply body",
        # A body whose text is literally a fully-formed but WRONG marker
        # (claiming 99 tools) — the harness must ignore the body's marker
        # string and stamp the true one.
        "<!-- source: tools=99 · mcp=42 · setting_sources=user+project -->",
        # A body ending in NEXT: line (the handoff parser is body-agnostic
        # here — this test only cares that the marker text is unchanged).
        "reply\n\nNEXT: naysayer",
    ]

    for body in bodies:
        stamped = append_source_marker(body, opts)
        # Marker line is present verbatim.
        assert expected_marker in stamped
        # Marker is the FINAL non-empty line — audit-locatable regardless of
        # what the agent wrote.
        assert stamped.rstrip().endswith(expected_marker)
        # And crucially: for the body that carried its own fake marker, the
        # stamped marker still reflects the TRUE options (tools=0), not the
        # body's claim (tools=99). This is the msg-834 §2 T-b invariant.
        assert "tools=0" in stamped.rstrip().splitlines()[-1]


# --------------------------------------------------------------------------- #
# T-d — marker contradicts a false capability claim (msg-834 §2 T-d)
# --------------------------------------------------------------------------- #


def test_marker_contradicts_false_capability_claim() -> None:
    """T-d: the msg-834 §2 fixture — ``tools=[]`` options + a body that
    claims capability the options do not carry; the marker records the
    TRUE options and the body records the claim. The marker never denies
    the body's prose — the two live side-by-side, and that is the audit
    surface.

    Wording of the fixture: msg-783 D-2 had Bohr write "私は github 操作系
    ツールを持つので spec PR を開ける" while the actual runtime was
    ``tools=[]``. Reproduced here as the test body.
    """
    opts = _options(tools=[], mcp_servers={}, setting_sources=[])
    body = "I have github tool access and can open the spec PR myself.\n\nNEXT: naysayer"
    stamped = append_source_marker(body, opts)

    # The body is preserved verbatim above the marker (D1: the marker adds
    # a datum, does not edit the body).
    assert "I have github tool access" in stamped
    # The marker records the truth from options: tools=0 contradicts the
    # body's claim WITHOUT deleting or altering the body.
    trailing_marker = stamped.rstrip().splitlines()[-1]
    assert "tools=0" in trailing_marker
    assert "mcp=0" in trailing_marker
    assert "setting_sources=empty" in trailing_marker
    # And the reader can locate the marker line unambiguously (it is the
    # final non-empty line).
    assert trailing_marker.startswith(SOURCE_MARKER_PREFIX)
    assert trailing_marker.endswith(SOURCE_MARKER_SUFFIX)
