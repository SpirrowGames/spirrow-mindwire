"""Harness-stamped source marker for every SDK-adapter reply.

**Msg-805 §M3''.** The one place a ``ClaudeAgentOptions`` object is turned
into a short audit line the ChatRoom reader can trust — trust because the
line is derived from the options the harness actually passed to the SDK
session, never from anything the model wrote in its reply body.

**Msg-834 §2 (T-a / T-b / T-d + downgraded T-c → review-only) via Tier-C
2026-08-11.** Three tests live in ``tests/test_source_marker.py`` and one
review-time diff check governs the remaining structural condition:

    (a) marker を組み立てる関数は 1 箇所のみ — this module
    (b) marker は post 時に harness が本文の外側で付与する — appended by
        :mod:`spirrow_mindwire.dispatcher.core` right before the gateway
        posts; the agent body is never parsed to extract a marker
    (c) marker builder は adapter モジュールから import されない — review
        this PR's diff: no ``adapters/*.py`` (implementer / claude_code_sdk
        / naysayer_sdk) imports :mod:`spirrow_mindwire.source_marker`; the
        dispatcher does

**D3 (msg-805).** The agent is not the marker's declarant — the harness
is. That non-symmetry is the whole point: ``Bohr`` writing ``docs=0``
inside a reply body is exactly the failure mode this module was born to
close (msg-805 D3, and this thread's opening self-example). ADR-12
``embodiment`` is agent-self-declared; this marker is *not*. Do not
implement it as agent output.

**D1 (msg-805).** The three fields are option-value re-statements only,
never effect claims:

    <!-- source: tools=N · mcp=M · setting_sources=<value> -->

    tools           = len(options.tools). ``tools=0`` says the SDK
                      built-in surface was disabled at construction; it
                      does NOT say "the agent cannot act."
    mcp             = len(options.mcp_servers). ``mcp=0`` says no MCP
                      server config was passed; it does NOT say "no MCP
                      surface reached the session at runtime."
    setting_sources = "unset" if the kwarg was not passed; "empty" if
                      passed as []; otherwise the list contents joined by
                      "+". ``setting_sources=unset`` says the kwarg was
                      omitted; it does NOT say "CLAUDE.md is / is not
                      loaded" (that inference is a downstream reader's
                      job, and depends on the CLI version — see
                      ``spec/process/README.md`` §N.3 for the human-facing
                      observation record).

Legend intentionally reads like a re-statement of the option values (D1
tautology). The marker's job is to make the caller auditable — not to
promise an effect.
"""

from __future__ import annotations

from typing import Any

# Marker form is stable and machine-readable. HTML comment wrap so the line
# renders invisibly in markdown viewers while remaining greppable in the raw
# text; the ChatRoom body is markdown, so a non-rendering marker keeps the
# reply readable while still landing the audit line on every post.
SOURCE_MARKER_PREFIX = "<!-- source:"
SOURCE_MARKER_SUFFIX = "-->"

# U+00B7 MIDDLE DOT with a single ASCII space on each side. Chosen so the
# separator does not collide with the ``=`` in a field value; the exact form
# is what msg-805 quotes ("tools=0 · mcp=0 · setting_sources=unset").
_FIELD_SEP = " · "


def _tools_field(options: Any) -> str:
    tools = getattr(options, "tools", None)
    return f"tools={len(tools) if tools is not None else 0}"


def _mcp_field(options: Any) -> str:
    mcp_servers = getattr(options, "mcp_servers", None)
    return f"mcp={len(mcp_servers) if mcp_servers is not None else 0}"


def _setting_sources_value(options: Any) -> str:
    """Return the display value for the ``setting_sources`` option (D1 tautology).

    The three cases are the ones the SDK actually distinguishes: no kwarg vs
    an empty list vs a non-empty list. Anything else (e.g. a value other
    than ``list`` / ``None``) falls back to ``str(value)`` — the marker's job
    is to reflect the actual option, not to validate it.
    """
    src = getattr(options, "setting_sources", None)
    if src is None:
        return "unset"
    try:
        length = len(src)
    except TypeError:
        return str(src)
    if length == 0:
        return "empty"
    return "+".join(str(x) for x in src)


def render_source_marker(options: Any) -> str:
    """Render the one-line source marker for a ``ClaudeAgentOptions`` object.

    **The input is the SDK options object the adapter actually passes to the
    client** — not a re-declared copy, literal, or config file re-read.
    Msg-834 §2 (a): "marker を組み立てる関数は 1 箇所のみ。入力は SDK に実
    際に渡す ``ClaudeAgentOptions`` インスタンスそのもの" — this is why
    :meth:`ImplementerSdkAdapter._make_options` / the sibling SDK adapters
    store the exact ``ClaudeAgentOptions`` object they hand to the SDK on
    the per-session record, and the dispatcher retrieves that same object
    through a public getter.

    Duck-typed at this module boundary (``Any``): this module deliberately
    does NOT import :class:`claude_agent_sdk.ClaudeAgentOptions`, so tests
    can hand it a plain ``SimpleNamespace`` fixture with the same attribute
    shape and the pure function still works. The dispatcher passes the real
    options object.
    """
    parts = (
        _tools_field(options),
        _mcp_field(options),
        f"setting_sources={_setting_sources_value(options)}",
    )
    return f"{SOURCE_MARKER_PREFIX} {_FIELD_SEP.join(parts)} {SOURCE_MARKER_SUFFIX}"


def append_source_marker(body: str, options: Any) -> str:
    """Append the derived marker to ``body`` on its own trailing line.

    The marker is separated from the body by a blank line so a markdown
    viewer renders it as its own paragraph (invisible: it is an HTML
    comment) rather than glued to the last line. The dispatcher calls this
    inside ``_handle_reply`` on the ``ReplyDraft.body`` returned by the
    adapter, so every posted reply carries the marker as its final
    non-empty line — the agent's body content is never modified in place.

    A trailing ``NEXT: <name>`` line the adapter emits keeps working: the
    handoff parser (:func:`spirrow_mindwire.conductor.handoff.parse_next_token`)
    takes the LAST ``NEXT:`` line in the body, and this marker never
    contains that token. Placement after the ``NEXT:`` line is therefore
    handoff-safe.
    """
    marker = render_source_marker(options)
    if not body:
        return marker
    return f"{body.rstrip()}\n\n{marker}"


__all__ = [
    "SOURCE_MARKER_PREFIX",
    "SOURCE_MARKER_SUFFIX",
    "append_source_marker",
    "render_source_marker",
]
