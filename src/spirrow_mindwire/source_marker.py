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

**D1 (msg-805).** The five fields are option-value re-statements only,
never effect claims:

    <!-- source: tools=N · mcp=M · setting_sources=<value> · route=<value> · tier=<value> -->

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
    route           = the authority (``host:port``) of
                      ``options.env["ANTHROPIC_BASE_URL"]``; "unset" if the
                      key is absent, "empty" if present but empty,
                      "redacted" if present but not safely parseable. It says
                      **the session was constructed to send inference to
                      that host:port**; it does NOT say "that host
                      answered". Path / query / userinfo are stripped —
                      partly because they are noise, and partly because a
                      base URL may carry an inline credential and this line
                      lands in a durable, widely-readable chatroom post.
                      ``route=redacted`` is that guard firing: the value could
                      not be reduced to an authority without risking printing
                      the credential, so nothing is printed. It says **a base
                      URL was set and is malformed**; it does NOT say the
                      variable was missing (that is ``unset`` / ``empty``).
    tier            = ``options.model``, i.e. the model alias REQUESTED;
                      "unset" if no model kwarg was passed. It does NOT say
                      which backend processed the request. Lexora echoes the
                      tier alias back in its response ``model`` field
                      (measured, msg-950 §2), so no amount of reading the
                      response upgrades this into provenance.

Legend intentionally reads like a re-statement of the option values (D1
tautology). The marker's job is to make the caller auditable — not to
promise an effect.

**P-1a (msg-953 §2, Tier-C msg-954 §3): why ``route`` / ``tier`` were
added.** msg-950 M-4 had to run a *negation experiment* — point the adapter
at a dead endpoint and watch it fail — to rule out the whole class of
"``ANTHROPIC_BASE_URL`` never reached the subprocess / went to
api.anthropic.com / had the wrong port". These two fields make that class
visible on every post, permanently, for zero extra wiring: same builder,
same input object, same insertion point.

**★ ``attest:`` is a SEPARATE line, and that is load-bearing** (Tier-C
msg-954 §3). ``source:`` re-states configuration and is a pure tautology.
:class:`~spirrow_mindwire.value_objects.AttestationRecord` reports a
server-side *observation* and can be absent or wrong:

    <!-- attest: tier=<t> · backend=<b> · expected=<e> · route=<r> · probe=<p> · at=<iso> -->

Merging the two would dilute exactly the property that makes ``source:``
trustworthy — that it promises nothing. Keep them on separate lines with
separate prefixes so a reader (and a grep) can never mistake a
configuration re-statement for an observation.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from .value_objects import AttestationRecord

# Marker form is stable and machine-readable. HTML comment wrap so the line
# renders invisibly in markdown viewers while remaining greppable in the raw
# text; the ChatRoom body is markdown, so a non-rendering marker keeps the
# reply readable while still landing the audit line on every post.
SOURCE_MARKER_PREFIX = "<!-- source:"
SOURCE_MARKER_SUFFIX = "-->"

# Distinct prefix so the observation line can never be confused with — or
# grepped as — the configuration line (Tier-C msg-954 §3).
ATTESTATION_MARKER_PREFIX = "<!-- attest:"
ATTESTATION_MARKER_SUFFIX = "-->"

# The env var whose value the ``route`` field re-states. Named here rather than
# inlined so the marker and the adapter that sets it are greppable together.
_BASE_URL_ENV_KEY = "ANTHROPIC_BASE_URL"

# Rendered for ``route`` when the base URL was set but its userinfo boundary
# could not be resolved (see ``_route_authority``). A reader greps this to find
# posts made under a malformed — possibly credential-bearing — base URL.
_ROUTE_REDACTED = "redacted"

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

    Str-specific guard (PR #139 Tier B Finding 2): a bare string is
    ``__len__``-able and iterable — without the guard, ``setting_sources="user"``
    would render as ``user`` (via len=4 → join "+".join over its chars →
    ``u+s+e+r``). ``ClaudeAgentOptions`` types this as ``list[SettingSource]``
    at the SDK boundary, but we duck-type at this module boundary, so an
    unexpected string must be rendered verbatim — not iterated.
    """
    src = getattr(options, "setting_sources", None)
    if src is None:
        return "unset"
    if isinstance(src, str):
        # Bare string sneaked in (SDK typing does not allow it, but the
        # duck-typed boundary here must not silently mangle it into
        # per-char join). Return verbatim.
        return src
    try:
        length = len(src)
    except TypeError:
        return str(src)
    if length == 0:
        return "empty"
    return "+".join(str(x) for x in src)


def _route_authority(raw: str) -> str | None:
    """Reduce a base-URL string to its ``host:port`` authority.

    Returns ``None`` when the userinfo boundary cannot be resolved — see the
    fail-closed rule below. ``None`` is a third outcome, distinct from the
    empty string (which means the value reduced to no authority at all).

    Hand-rolled rather than :func:`urllib.parse.urlsplit` because the input is
    an operator-supplied env value, not a guaranteed-well-formed URL, and
    ``urlsplit`` mis-parses the most likely malformed case: ``urlsplit`` on a
    schemeless ``"100.79.84.62:8110"`` yields an empty ``netloc`` (a scheme
    must start with a letter, so the whole string lands in ``path``) and we
    would print nothing at all for a value that was in fact configured. The
    reader needs to see what was set, including when it was set wrong.

    The reductions, in order:

    1. drop everything up to and including ``://`` (the scheme)
    2. split at the first ``/``, ``?`` or ``#`` into authority + tail
       (path / query / fragment)
    3. **fail closed if the discarded tail still contains a ``@``**
    4. drop everything up to and including the last ``@`` (userinfo)

    Steps 3-4 are a **credential guard**, not cosmetics: a base URL of the
    form ``https://user:token@host/`` would otherwise copy a live secret into
    a durable chatroom post.

    **Why step 3 exists (PR #142 Tier B blocking).** Step 4 alone is only
    sound when the operator wrote a well-formed URL. A password containing a
    raw ``/``, ``?`` or ``#`` — a common env-var misconfiguration — moves the
    userinfo ``@`` past step 2's cut, so the cut lands *inside the credential*
    and step 4 then finds no ``@`` to remove. Measured on the pre-fix code:
    ``https://admin:p/assword@api.internal:8443/`` rendered ``admin:p``, i.e.
    the guard printed the secret it exists to suppress.

    Step 3 detects exactly that class, and does so by construction rather than
    by heuristic: userinfo is delimited by an ``@`` that necessarily follows
    the *whole* credential, so if step 2's cut ever lands inside the userinfo,
    that delimiting ``@`` is necessarily still in the discarded tail. "A ``@``
    survives in the tail" therefore covers every ordering that can leak.

    The converse does not hold — a genuine ``@`` in a path (
    ``https://host:8443/path@v2``) produces the same string shape as a
    password containing ``/``, and nothing in the string distinguishes them.
    That case is redacted as well. Losing the route display for an unusual but
    valid URL is the cheaper error: msg-957, "if the parser cannot safely
    disambiguate the userinfo boundary, it must fail closed or redact
    aggressively, not silently print the secret."
    """
    value = raw.strip()
    scheme_sep = value.find("://")
    if scheme_sep != -1:
        value = value[scheme_sep + 3 :]
    tail = ""
    for sep in ("/", "?", "#"):
        idx = value.find(sep)
        if idx != -1:
            tail += value[idx:]
            value = value[:idx]
    if "@" in tail:
        # The cut may have landed inside a credential. Unresolvable → redact.
        return None
    at = value.rfind("@")
    if at == -1:
        return value
    value = value[at + 1 :]
    # Userinfo consumed the entire value (e.g. "https://user:token@"). Do not
    # return "" — the caller renders that as ``route=empty``, which the legend
    # reserves as the fingerprint of a MISSING base URL. A credential-bearing
    # value must not forge that fingerprint.
    return value or None


def _route_field(options: Any) -> str:
    """Return the ``route`` field (D1 tautology — see the module legend).

    ``unset`` (no key / no env mapping) and ``empty`` (key present, empty
    value) are deliberately distinct, mirroring ``setting_sources``. They are
    not the same state operationally: :class:`~spirrow_mindwire.adapters.
    naysayer_sdk.NaysayerSdkAdapter` defaults ``inference_base_url`` to ``""``
    when ``MINDWIRE_NAYSAYER_BASE_URL`` is missing, so ``route=empty`` is the
    fingerprint of that specific misconfiguration rather than of an adapter
    that simply never sets the variable.

    ``redacted`` is the third failure value: a base URL was set, but its
    userinfo boundary could not be resolved, so printing any part of it risked
    printing a credential (see :func:`_route_authority`). It is deliberately
    its own token — collapsing it onto ``unset`` or ``empty`` would tell the
    reader the variable was missing when in fact it was set wrong, and those
    two states need different repairs.
    """
    env = getattr(options, "env", None)
    if not isinstance(env, dict):
        return "route=unset"
    raw = env.get(_BASE_URL_ENV_KEY)
    if raw is None:
        return "route=unset"
    authority = _route_authority(str(raw))
    if authority is None:
        return f"route={_ROUTE_REDACTED}"
    return f"route={authority}" if authority else "route=empty"


def _tier_field(options: Any) -> str:
    """Return the ``tier`` field (D1 tautology — see the module legend)."""
    model = getattr(options, "model", None)
    return f"tier={model}" if model else "tier=unset"


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
        _route_field(options),
        _tier_field(options),
    )
    return f"{SOURCE_MARKER_PREFIX} {_FIELD_SEP.join(parts)} {SOURCE_MARKER_SUFFIX}"


def render_attestation_marker(record: AttestationRecord) -> str:
    """Render the one-line attestation marker for a preflight observation.

    Input is an explicit :class:`~spirrow_mindwire.value_objects.
    AttestationRecord` — **not** an options object and **not** anything parsed
    out of a reply body. The type is the contract: a caller cannot accidentally
    attest from model output, because model output is a ``str`` and this
    signature does not accept one.

    ``backend`` and ``expected`` are both rendered even when they agree, so the
    line cannot be read as "verified" from its shape alone (see
    :class:`AttestationRecord`).
    """
    parts = (
        f"tier={record.tier}",
        f"backend={record.backend}",
        f"expected={record.expected}",
        f"route={record.route}",
        f"probe={record.probe}",
        f"at={record.at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    )
    return f"{ATTESTATION_MARKER_PREFIX} {_FIELD_SEP.join(parts)} {ATTESTATION_MARKER_SUFFIX}"


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


def append_markers(
    body: str,
    options: Any,
    attestation: AttestationRecord | None = None,
) -> str:
    """Append the harness marker block: ``source:`` always, ``attest:`` if present.

    The dispatcher's single entry point, so the "marker builder lives in one
    module" condition (msg-834 §2 (a)) still holds with two markers: the caller
    never joins lines itself.

    The attestation goes on the line **directly below** the source marker
    (single newline, so the two read as one audit block) and only when a record
    was supplied. ``attestation=None`` returns a result byte-identical to
    :func:`append_source_marker` — which is what makes P-1 a no-behaviour-change
    landing: nothing produces an ``AttestationRecord`` until P-2, so no live
    post grows an ``attest:`` line from this change alone.

    "No observation" must render as *nothing*. An empty or partial ``attest:``
    line would be worse than none at all, because a reader could take its mere
    presence for verification.
    """
    stamped = append_source_marker(body, options)
    if attestation is None:
        return stamped
    return f"{stamped}\n{render_attestation_marker(attestation)}"


__all__ = [
    "ATTESTATION_MARKER_PREFIX",
    "ATTESTATION_MARKER_SUFFIX",
    "SOURCE_MARKER_PREFIX",
    "SOURCE_MARKER_SUFFIX",
    "append_markers",
    "append_source_marker",
    "render_attestation_marker",
    "render_source_marker",
]
