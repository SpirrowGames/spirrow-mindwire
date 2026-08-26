"""Tests for MagickitChatroomGateway + parse_tool_result (ADR-06 §3.3, T12 PR-F)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mcp import ErrorData, McpError
from mcp.client.streamable_http import StreamableHTTPError

from spirrow_mindwire.dispatcher.gateway import ChatroomGateway
from spirrow_mindwire.magickit.client import (
    MagickitMcpError,
    StreamableHttpChatroomMcp,
    parse_tool_result,
)
from spirrow_mindwire.magickit.gateway import MagickitChatroomGateway
from spirrow_mindwire.value_objects import Role, ThreadRef

_TR = ThreadRef(project_id="spirrow-mindwire", thread_id="T-x", chatroom_uri="mc://t")


class _FakeCaller:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return self.result


class _FakeToolResult:
    def __init__(
        self,
        *,
        structured: Any = None,
        content: list[Any] | None = None,
        is_error: bool = False,
    ) -> None:
        self.structuredContent = structured
        self.content = content
        self.isError = is_error


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


@pytest.mark.anyio
async def test_post_reply_calls_chatroom_post_message() -> None:
    caller = _FakeCaller({"msg": {"msg_id": "msg-200"}})
    gw = MagickitChatroomGateway(caller)
    out = await gw.post_reply(
        _TR, author=Role.PROPOSER, body="hi", reply_to_msg_id="m1", idempotency_key="s:1"
    )
    assert out == "msg-200"
    name, args = caller.calls[0]
    assert name == "chatroom_post_message"
    assert args["project"] == "spirrow-mindwire"
    assert args["thread_id"] == "T-x"
    assert args["author"] == "proposer"  # I3: author = role name
    assert args["content"] == "hi"
    assert args["reply_to"] == "m1"
    assert args["msg_type"] == "report"


@pytest.mark.anyio
async def test_post_reply_omits_reply_to_when_none() -> None:
    caller = _FakeCaller({"msg": {"msg_id": "m"}})
    gw = MagickitChatroomGateway(caller)
    await gw.post_reply(
        _TR, author=Role.NAYSAYER, body="x", reply_to_msg_id=None, idempotency_key="s:1"
    )
    _, args = caller.calls[0]
    assert "reply_to" not in args


@pytest.mark.anyio
async def test_custom_reply_msg_type() -> None:
    caller = _FakeCaller({"msg": {"msg_id": "m"}})
    gw = MagickitChatroomGateway(caller, reply_msg_type="answer")
    await gw.post_reply(
        _TR, author=Role.PROPOSER, body="x", reply_to_msg_id=None, idempotency_key="s:1"
    )
    assert caller.calls[0][1]["msg_type"] == "answer"


@pytest.mark.anyio
async def test_post_reply_missing_msg_id_raises() -> None:
    gw = MagickitChatroomGateway(_FakeCaller({"oops": True}))
    with pytest.raises(MagickitMcpError):
        await gw.post_reply(
            _TR, author=Role.PROPOSER, body="x", reply_to_msg_id=None, idempotency_key="s:1"
        )


def test_conforms_to_chatroom_gateway_protocol() -> None:
    gw: ChatroomGateway = MagickitChatroomGateway(_FakeCaller({"msg": {"msg_id": "m"}}))
    assert gw is not None


def test_parse_tool_result_prefers_structured() -> None:
    assert parse_tool_result(_FakeToolResult(structured={"a": 1})) == {"a": 1}


def test_parse_tool_result_text_json_fallback() -> None:
    assert parse_tool_result(_FakeToolResult(content=[_FakeTextBlock('{"b": 2}')])) == {"b": 2}


def test_parse_tool_result_is_error_raises() -> None:
    with pytest.raises(MagickitMcpError):
        parse_tool_result(_FakeToolResult(is_error=True, content=[_FakeTextBlock("{}")]))


def test_parse_tool_result_no_content_raises() -> None:
    with pytest.raises(MagickitMcpError):
        parse_tool_result(_FakeToolResult(content=[]))


def test_parse_tool_result_skips_invalid_json_block() -> None:
    # An invalid-JSON text block is skipped; a later valid block wins.
    result = _FakeToolResult(content=[_FakeTextBlock("not json"), _FakeTextBlock('{"ok": 1}')])
    assert parse_tool_result(result) == {"ok": 1}


def test_parse_tool_result_all_invalid_json_raises_magickit_error() -> None:
    # Invalid JSON surfaces as MagickitMcpError, not a raw JSONDecodeError.
    with pytest.raises(MagickitMcpError):
        parse_tool_result(_FakeToolResult(content=[_FakeTextBlock("not json")]))


# --------------------------------------------------------------------------- #
# T-error-envelope-read-as-data DoD #1: parse_tool_result elevates a conclair
# error envelope to MagickitMcpError at the client boundary, so no caller has
# to remember to check for one (msg-1115 §4).
# --------------------------------------------------------------------------- #

# The envelopes below are verbatim from the live server on 2026-08-16 (see
# tests/test_orchestrator.py::_error_envelope). Elevating them here is the
# single source of truth the msg-1496 refactor pinned; a change to the
# envelope shape will break these tests before it silently breaks callers.

_LIVE_NOT_FOUND: dict[str, Any] = {
    "error_type": "ChatroomNotFoundError",
    "error": "Thread 'T-pr-review-spirrow-magickit-22' not found in project 'spirrow-mindwire'",
    "details": {
        "project": "spirrow-mindwire",
        "thread_id": "T-pr-review-spirrow-magickit-22",
    },
}


def test_parse_tool_result_elevates_envelope_from_structured_content() -> None:
    with pytest.raises(MagickitMcpError, match="ChatroomNotFoundError"):
        parse_tool_result(_FakeToolResult(structured=_LIVE_NOT_FOUND))


def test_parse_tool_result_elevates_envelope_from_text_block() -> None:
    import json

    with pytest.raises(MagickitMcpError, match="ChatroomNotFoundError"):
        parse_tool_result(_FakeToolResult(content=[_FakeTextBlock(json.dumps(_LIVE_NOT_FOUND))]))


def test_parse_tool_result_error_message_includes_the_far_end_reason() -> None:
    # DoD #4.4 (msg-1496): the warning at the report_observed call site must be
    # able to show ``error_type`` and the message; that requires the elevation
    # to carry both, not just the class name.
    with pytest.raises(MagickitMcpError) as excinfo:
        parse_tool_result(_FakeToolResult(structured=_LIVE_NOT_FOUND))
    text = str(excinfo.value)
    assert "ChatroomNotFoundError" in text
    assert "T-pr-review-spirrow-magickit-22" in text


def test_parse_tool_result_lets_a_payload_with_no_error_type_pass_through() -> None:
    # Only ``error_type`` sets the envelope apart from an ordinary payload —
    # a domain field that merely mentions the word "error" is not one.
    payload = {"thread": {"title": "x"}, "messages": []}
    assert parse_tool_result(_FakeToolResult(structured=payload)) == payload


# --------------------------------------------------------------------------- #
# T-error-envelope-followup DoD #2 (msg-1688 §3-4): the elevation exception's
# message is bounded by construction — top-level key names + ``error_type`` /
# ``error`` string values only, each truncated at 200 chars — and no payload
# value ever appears on any other path. The negative assertion (b) is the
# load-bearing one: a future "helpful" edit that adds ``repr(payload)`` to
# the message string must fail this test.
# --------------------------------------------------------------------------- #

_SENTINEL_NOT_TO_LEAK = "SENSITIVE_VALUE_MUST_NOT_APPEAR_IN_MESSAGE_c47f2c8b"
_SENTINEL_NESTED = "NESTED_VALUE_MUST_NOT_APPEAR_78a1"


def _elevate_payload(payload: dict[str, Any]) -> MagickitMcpError:
    """Trigger the envelope elevation and return the raised exception for introspection."""
    with pytest.raises(MagickitMcpError) as excinfo:
        parse_tool_result(_FakeToolResult(structured=payload))
    return excinfo.value


def test_dod2a_elevation_message_lists_every_top_level_key_by_name() -> None:
    """msg-1688 §4 (a): the top-level key list is what proves a false-positive elevation.

    Any legitimate payload that trips the detector shows its shape here — a
    reader can tell "success payload with a spurious ``error_type``" apart
    from "conclair error envelope" by reading the key set.
    """
    payload = {
        "error_type": "SomeError",
        "error": "why",
        "details": {"x": _SENTINEL_NESTED},
        "extra_domain_key": _SENTINEL_NOT_TO_LEAK,
        "another_key": [1, 2, 3],
    }
    exc = _elevate_payload(payload)
    text = str(exc)
    for key in payload:
        assert key in text, f"top-level key {key!r} missing from elevation message: {text!r}"


def test_dod2b_elevation_message_never_dumps_values_from_non_string_or_extra_keys() -> None:
    """msg-1688 §4 (b) — the negative pin.

    This is the test msg-1688 called "the real one" — the constraint's job is
    to reject a diff that "helpfully" adds ``repr(payload)`` or an extra key's
    value to the message. Positive asserts would let that diff pass silently.

    A payload's non-envelope keys carry sentinel strings; the elevation must
    surface **none** of them. ``error_type`` / ``error`` values ARE allowed
    (§3.2-3.3), so those are omitted from the sentinel set.
    """
    payload = {
        "error_type": "SomeError",
        "error": "why",
        "details": {"secret": _SENTINEL_NESTED, "actor": "internal-service-key"},
        "extra_domain_key": _SENTINEL_NOT_TO_LEAK,
    }
    exc = _elevate_payload(payload)
    text = str(exc)
    # Non-string values (dicts, lists, ...) must not appear via ``repr()``.
    assert _SENTINEL_NOT_TO_LEAK not in text, (
        f"extra key's value leaked into elevation message: {text!r}"
    )
    assert _SENTINEL_NESTED not in text, f"nested value leaked into elevation message: {text!r}"
    assert "internal-service-key" not in text, (
        f"nested actor value leaked into elevation message: {text!r}"
    )


def test_dod2b_elevation_message_omits_non_string_error_type_and_error_values() -> None:
    """§3.2-3.3 corollary: when ``error_type`` / ``error`` are not strings, the
    values are omitted entirely (the keys still appear in the top-level list).
    """
    payload = {
        "error_type": "RealError",  # keep the detector tripped
        "error": {"nested": _SENTINEL_NESTED},  # non-string → value omitted
        "extras": [_SENTINEL_NOT_TO_LEAK],
    }
    exc = _elevate_payload(payload)
    text = str(exc)
    assert _SENTINEL_NESTED not in text
    assert _SENTINEL_NOT_TO_LEAK not in text
    # Keys still present, so the schema mismatch remains diagnosable.
    assert "error" in text
    assert "extras" in text


def test_dod2c_elevation_message_truncates_error_and_error_type_at_200_chars() -> None:
    """msg-1688 §3.4: ``error_type`` / ``error`` values are capped at
    :data:`_ELEVATION_VALUE_LIMIT` (200 chars); a truncation marker follows.
    A conclair error message that happens to embed a full chatroom thread body
    must not blow the log line up unbounded.
    """
    big = "X" * 500
    payload = {
        "error_type": big,
        "error": big,
    }
    exc = _elevate_payload(payload)
    text = str(exc)
    # The full 500-char string must not appear intact.
    assert big not in text, "value was not truncated"
    # A prefix of the value + the truncation marker must appear (per key).
    assert "…[truncated]" in text
    # The message must remain of a bounded length that is a small multiple of
    # the cap (two capped values + short surrounding text) — hard-coding an
    # exact length would be brittle, but proving "far shorter than 500x2 raw"
    # keeps a future edit that removes truncation red.
    assert len(text) < 700, f"elevation message unexpectedly long: len={len(text)}"


def test_dod2d_elevation_message_is_total_over_mixed_key_types() -> None:
    """PR-gate on #178 round 6 (msg-1792 §1): the formatter must not raise on
    an envelope whose top-level dict happens to carry a non-``str`` key.

    Before the fix, ``sorted(payload.keys())`` inside ``_elevation_message``
    raised ``TypeError: '<' not supported between instances of 'int' and 'str'``
    on a payload like ``{"error_type": ..., 1: ...}``. That ``TypeError`` then
    propagated out of ``call_tool`` and — critically — was caught by
    :class:`LoopControlReader`'s ``except Exception`` in ``read()``, which
    converted it into a silent fail-safe ``hold`` whose log line named
    formatter internals rather than the envelope that had actually arrived.
    The formatter is a diagnostic tool; a diagnostic tool that crashes its
    caller during diagnosis is the failure mode this thread exists to close
    (msg-1115 §2's "silent success made worse"), so *this* is the pin that
    keeps ``_elevation_message`` total on any dict.

    Positive: the diagnostic message is produced and carries ``error_type``.
    Reachability: :func:`is_envelope` still accepts the mixed-key dict — the
    tolerance lives in the detector (msg-1792 §3), the cost is paid here.
    """
    from spirrow_mindwire.magickit.client import is_envelope

    payload: dict[Any, Any] = {
        "error_type": "SomeError",
        "error": "explanation",
        1: "int_key_context",
        (): "tuple_key_context",
    }
    # Detector reachability: msg-1792 §3 keeps ``is_envelope`` loose on purpose.
    assert is_envelope(payload) is True

    exc = _elevate_payload(payload)
    text = str(exc)
    # The formatter produced its normal diagnostic (not a TypeError, not an
    # AttributeError, not a masked hold): ``error_type`` value survives.
    assert "SomeError" in text, f"error_type value missing from diagnostic: {text!r}"


def test_dod2d_elevation_message_omits_non_str_key_values_from_mixed_keys() -> None:
    """PR-gate on #178 round 6 (msg-1792 §6 negative pin): making the formatter
    total over mixed-key dicts must not become a licence to dump the *values*
    at non-envelope keys.

    A "helpful" future edit that tries to render ``int`` / ``tuple`` key
    contents into the message must fail this test — same negative-assert style
    as :func:`test_dod2b_elevation_message_never_dumps_values_from_non_string_or_extra_keys`.
    Only ``error_type`` / ``error`` string values are ever admitted; other
    keys contribute names only (and now those names are stringified +
    truncated), never their values.
    """
    payload: dict[Any, Any] = {
        "error_type": "SomeError",
        "error": "explanation",
        1: _SENTINEL_NOT_TO_LEAK,  # int key, sentinel value must not appear
        (): _SENTINEL_NESTED,  # tuple key, sentinel value must not appear
    }
    exc = _elevate_payload(payload)
    text = str(exc)
    assert _SENTINEL_NOT_TO_LEAK not in text, (
        f"int-keyed value leaked into elevation message: {text!r}"
    )
    assert _SENTINEL_NESTED not in text, (
        f"tuple-keyed value leaked into elevation message: {text!r}"
    )


def test_dod2d_elevation_message_truncates_long_key_names() -> None:
    """PR-gate on #178 round 6 (msg-1792 §5(b)): the "bounded by construction"
    claim in :func:`_elevation_message`'s docstring must be true of key names
    too, not just of ``error_type`` / ``error`` values.

    A payload with a single 10KB top-level key name would otherwise defeat the
    log-bloat protection Einstein pinned on the values (msg-1687). Einstein's
    round-6 mandate strips the option of merely documenting the gap — the cap
    must apply uniformly.
    """
    from spirrow_mindwire.magickit.client import _ELEVATION_VALUE_LIMIT

    big_key = "K" * 10_000
    payload = {
        "error_type": "SomeError",
        big_key: "context",
    }
    exc = _elevate_payload(payload)
    text = str(exc)
    # The 10KB key must not appear intact.
    assert big_key not in text, "key name was not truncated"
    # And the whole message stays bounded (same length constraint style as
    # the value-truncation test — small multiple of the cap, not a hard-coded
    # exact length).
    assert len(text) < 4 * _ELEVATION_VALUE_LIMIT, (
        f"elevation message unexpectedly long: len={len(text)}"
    )


def test_dod2d_report_observed_survives_mixed_key_envelope(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PR-gate on #178 round 6 (msg-1792 §1 second half): before the fix, an
    envelope with mixed key types crashed ``report_observed`` with an
    *unhandled* ``TypeError`` (that method's catch is narrowly
    ``except MagickitMcpError`` per msg-1685 §2, so a ``TypeError`` from the
    formatter escaped and could kill the loop).

    This test pins that the entire round-trip — envelope arrives, formatter
    turns it into a bounded diagnostic, ``report_observed`` catches the
    resulting ``MagickitMcpError``, logs a stale-dashboard warning, returns
    normally — completes without raising, on a mixed-key input.
    """
    import logging

    from spirrow_mindwire.conductor.control import ControlState, LoopControlReader
    from spirrow_mindwire.magickit.client import raise_if_envelope

    class _MixedKeyEnvelopeMcp:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            raise_if_envelope(
                {
                    "error_type": "ChatroomWriteFailed",
                    "error": "storage backend refused the write",
                    1: "surprise int-keyed sibling",
                }
            )

    async def _run() -> None:
        reader = LoopControlReader(_MixedKeyEnvelopeMcp(), project="p")
        await reader.report_observed(ControlState.HOLD)  # must not raise

    with caplog.at_level(logging.WARNING, logger="spirrow_mindwire.conductor.control"):
        import anyio

        anyio.run(_run)
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    # The envelope's diagnostic reached the log (formatter was total, wrap
    # was caught by the narrow ``except MagickitMcpError``).
    assert "ChatroomWriteFailed" in log_text
    # And the stale-dashboard warning fired (report_observed's narrow catch
    # ran; no TypeError escaped).
    assert "stale" in log_text, f"expected stale-dashboard warning; got {log_text!r}"


def _max_elevation_message_length() -> int:
    """Compute the closed-form upper bound on :func:`_elevation_message` length.

    PR-gate on #178 round 8 (msg-1804 §3): the docstring gives the *shape* of
    the bound (``O((K+2) · LIMIT · EXP)``); this helper realises the exact
    coefficients from the three constants so a future edit that raises
    :data:`_ELEVATION_KEY_LIMIT`, :data:`_ELEVATION_VALUE_LIMIT`, or
    :data:`_ELEVATION_REPR_EXPANSION_MAX` recomputes the bound automatically —
    no test-side number to forget to update (msg-1804 §3-3).

    Breakdown per Python's ``repr`` for a ``str`` inside an f-string:

    - Each stringified value is at most ``LIMIT`` chars of arbitrary content
      plus the fixed-length ``_ELEVATION_TRUNCATION_MARKER`` when truncation
      fires. The arbitrary-content prefix can expand up to ``EXP`` chars per
      character under ``repr`` (Einstein's round-8 mandate: the marker itself
      is printable, so it takes marker-length chars after ``repr``).
    - ``repr('...')`` adds 2 quote characters.
    - List repr adds ``[``, ``]`` and ``, `` between items.
    - The ``parts`` in :func:`_elevation_message` are joined with a single
      space; the leading ``"magickit tool ..."`` is a fixed string.
    - ``total=`` is bounded by the digit count of any Python-buildable dict
      size — 30 digits is comfortably above the ~19 digits an ``int`` needs
      for a machine's addressable-memory ceiling.
    """
    from spirrow_mindwire.magickit.client import (
        _ELEVATION_KEY_LIMIT,
        _ELEVATION_REPR_EXPANSION_MAX,
        _ELEVATION_TRUNCATION_MARKER,
        _ELEVATION_VALUE_LIMIT,
    )

    marker_len = len(_ELEVATION_TRUNCATION_MARKER)
    # Per-item after repr: 2 quote chars + arbitrary body (LIMIT chars, each
    # expanded at most EXP times by repr) + the printable truncation marker.
    per_item = 2 + _ELEVATION_VALUE_LIMIT * _ELEVATION_REPR_EXPANSION_MAX + marker_len
    # List repr of K items: 2 brackets + K items + (K-1) separators ", ".
    list_repr = 2 + _ELEVATION_KEY_LIMIT * per_item + max(0, _ELEVATION_KEY_LIMIT - 1) * 2
    prefix_len = len("magickit tool returned an error envelope: ")
    total_part = len("total=") + 30  # 30 digits — comfortably above any real len(dict)
    keys_part = len("keys=") + list_repr
    type_part = len("error_type=") + per_item
    err_part = len("error=") + per_item
    # 3 space separators between the four parts.
    return prefix_len + total_part + 1 + keys_part + 1 + type_part + 1 + err_part


def test_dod3_elevation_message_length_within_computed_upper_bound() -> None:
    """PR-gate on #178 round 8 (msg-1804 §6-4 + Einstein's escape-expansion
    mandate): a worst-case payload's elevation message must fit under the
    formula computed from the constants.

    "Worst case" here means Einstein's round-8 clarification (the test must
    actually trigger maximum ``repr`` expansion — a payload of alphanumeric
    characters would prove a falsely-tight bound):

    - Payload has more keys than :data:`_ELEVATION_KEY_LIMIT` (so slicing must
      kick in),
    - Each key name is longer than :data:`_ELEVATION_VALUE_LIMIT` (so
      :func:`_bounded_str` truncation fires per key),
    - Every character is a supplementary-plane non-printable
      (``\\U000e0001`` — a tag character; ``repr`` expands each to 10 chars,
      matching :data:`_ELEVATION_REPR_EXPANSION_MAX`),
    - ``error_type`` and ``error`` values are similarly maximal.

    If any of those knobs changes — cap the values, tighten the escape max,
    add another key-list part — the bound recomputes automatically because
    :func:`_max_elevation_message_length` derives everything from the three
    constants (msg-1804 §3-3: no dual-managed magic number).

    Regression note (msg-1804 §6-8): before this round the message length was
    unbounded in the number of keys — 10 000 keys produced a ~100 KB string.
    A test with the same worst-case would have blown past *any* fixed upper
    bound; this test's own value falls under the recomputed bound only
    because :data:`_ELEVATION_KEY_LIMIT` now caps the key count.
    """
    from spirrow_mindwire.magickit.client import (
        _ELEVATION_KEY_LIMIT,
        _ELEVATION_VALUE_LIMIT,
    )

    # Einstein's mandate: use a character with maximum repr expansion.
    # \U000e0001 is a tag character on Plane 14 — not printable, so repr
    # emits \UXXXXXXXX (10 chars for 1 input char).
    worst_char = "\U000e0001"
    long_string = worst_char * (_ELEVATION_VALUE_LIMIT + 50)  # trips truncation

    # Payload with (K + 50) keys — exceeds the key cap so slicing engages.
    payload: dict[Any, Any] = {"error_type": long_string, "error": long_string}
    for i in range(_ELEVATION_KEY_LIMIT + 50):
        # Each key name is itself longer than LIMIT + built from the worst char,
        # so key-name truncation and repr expansion both fire.
        payload[f"{long_string}_{i:03d}"] = "value_ignored_by_formatter"

    exc = _elevate_payload(payload)
    actual = len(str(exc))
    upper = _max_elevation_message_length()
    assert actual <= upper, (
        f"elevation message exceeded the computed upper bound: "
        f"actual={actual} upper={upper} (K={_ELEVATION_KEY_LIMIT}, "
        f"LIMIT={_ELEVATION_VALUE_LIMIT})"
    )


def test_dod3_elevation_message_truthfulness_10000_keys() -> None:
    """PR-gate on #178 round 8 (msg-1804 §6-5): the enumerated key list is
    truncated to :data:`_ELEVATION_KEY_LIMIT`, but the ``total=`` field must
    still name the *true* payload key count.

    The message body must satisfy two invariants regardless of payload size:
    (i) ``total=<real-count>`` is present verbatim so an operator can read
    the drift back from the log line, and (ii) the enumerated key list holds
    at most :data:`_ELEVATION_KEY_LIMIT` items. Pinning both together is what
    turns the branch-free slice (msg-1804 §2) from an invisible cap into an
    audit trail: a reader compares the two numbers and knows "the log shows
    20 keys of the 10 002 that arrived".
    """
    import ast

    from spirrow_mindwire.magickit.client import _ELEVATION_KEY_LIMIT

    payload: dict[Any, Any] = {"error_type": "X", "error": "y"}
    for i in range(10_000):
        payload[f"k{i:05d}"] = "v"
    exc = _elevate_payload(payload)
    text = str(exc)
    # (i) the true total appears verbatim.
    assert f"total={len(payload)}" in text, (
        f"true total ({len(payload)}) missing from elevation message: {text!r}"
    )
    # (ii) enumerated key list is at most K items.
    keys_marker = " keys="
    ktype_marker = " error_type="
    start = text.index(keys_marker) + len(keys_marker)
    end = text.index(ktype_marker, start)
    shown_keys = ast.literal_eval(text[start:end])
    assert isinstance(shown_keys, list)
    assert len(shown_keys) <= _ELEVATION_KEY_LIMIT, (
        f"enumerated key list exceeded K={_ELEVATION_KEY_LIMIT}: len(shown)={len(shown_keys)}"
    )


def test_dod3_elevation_message_omits_values_of_dropped_keys() -> None:
    """PR-gate on #178 round 8 (msg-1804 §6-6): capping the enumerated key
    list must not become a licence to smuggle dropped keys' *values* into the
    message elsewhere.

    Same negative-assert style as
    :func:`test_dod2b_elevation_message_never_dumps_values_from_non_string_or_extra_keys`
    and :func:`test_dod2d_elevation_message_omits_non_str_key_values_from_mixed_keys`.
    A future "helpful" edit that appends ``... plus N dropped keys with
    values [...]`` to the message must fail this test.
    """
    from spirrow_mindwire.magickit.client import _ELEVATION_KEY_LIMIT

    payload: dict[Any, Any] = {"error_type": "SomeError", "error": "explanation"}
    # Add K + 50 keys, all with sentinel values. The sentinels for keys that
    # end up sliced OUT must not appear anywhere in the message.
    for i in range(_ELEVATION_KEY_LIMIT + 50):
        payload[f"extra_{i:03d}"] = f"{_SENTINEL_NOT_TO_LEAK}_{i:03d}"
    exc = _elevate_payload(payload)
    text = str(exc)
    # The sentinel prefix must not appear anywhere — none of the extra keys'
    # values are admitted to the message, regardless of whether their name
    # made the sorted-K slice.
    assert _SENTINEL_NOT_TO_LEAK not in text, (
        f"a dropped-key value leaked into elevation message: {text!r}"
    )


def test_dod3_elevation_message_is_deterministic_across_insertion_orders() -> None:
    """PR-gate on #178 round 8 (msg-1804 §6-7): two payloads with the same
    content but different insertion order must produce the same elevation
    message.

    Determinism follows from ``sorted(...)`` in the formatter, so this test
    passes against the pre-round-8 code as well (msg-1804 §6-8 caveat: this
    is a current-state pin, not a regression pin — pinned here so a future
    edit that "helpfully" preserves insertion order for readability
    reintroduces the non-determinism problem this bound cannot describe).
    """
    a: dict[Any, Any] = {"error_type": "E", "error": "why"}
    b: dict[Any, Any] = {"error": "why", "error_type": "E"}
    for i in range(30):  # exceeds K, so the slice matters
        a[f"k{i:02d}"] = i
    for i in reversed(range(30)):
        b[f"k{i:02d}"] = i
    assert str(_elevate_payload(a)) == str(_elevate_payload(b))


def test_dod2_report_observed_warning_stringifies_the_exception_not_the_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """msg-1688 §3 last bullet + §4 last paragraph: the ``report_observed``
    warning has no second formatter — it emits ``str(exc)``, so the value-safety
    rule applied at :func:`raise_if_envelope` governs the log line too.

    Verified against the actual code path (:class:`LoopControlReader` catching
    an envelope-elevated ``MagickitMcpError``); the sentinel from a non-envelope
    key must not surface in the warning either.
    """
    import logging

    from spirrow_mindwire.conductor.control import ControlState, LoopControlReader
    from spirrow_mindwire.magickit.client import raise_if_envelope

    class _EnvelopeMcp:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            raise_if_envelope(
                {
                    "error_type": "ChatroomWriteFailed",
                    "error": "storage backend refused the write",
                    "details": {"secret_actor_key": _SENTINEL_NOT_TO_LEAK},
                    "extras": _SENTINEL_NESTED,
                }
            )

    async def _run() -> None:
        reader = LoopControlReader(_EnvelopeMcp(), project="p")
        await reader.report_observed(ControlState.HOLD)

    with caplog.at_level(logging.WARNING, logger="spirrow_mindwire.conductor.control"):
        import anyio

        anyio.run(_run)
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "ChatroomWriteFailed" in log_text  # the diagnostic info survives
    assert _SENTINEL_NOT_TO_LEAK not in log_text
    assert _SENTINEL_NESTED not in log_text


# --------------------------------------------------------------------------- #
# T-error-envelope-followup DoD #3 (msg-1685 §2 + §4.3): the responsibility for
# converting a transport failure into :class:`MagickitMcpError` sits with
# :meth:`StreamableHttpChatroomMcp.call_tool`, not the ``report_observed``
# call-site catch. The tests below inject each named exception class through
# the ``_call_mcp`` seam and assert the wrap; a leak means the fix goes into
# the client's ``_TRANSPORT_EXCEPTIONS`` tuple, not into a call site.
# --------------------------------------------------------------------------- #


class _RaisingMcp(StreamableHttpChatroomMcp):
    """Test seam: raise a scripted exception from ``_call_mcp``.

    Injecting through the same method the production transport ends up calling
    keeps the test close to the boundary msg-1685 §2 named — the tuple in
    ``call_tool`` — rather than exercising a private helper.
    """

    def __init__(self, exc: BaseException) -> None:
        super().__init__(url="http://test.invalid/mcp")
        self._exc = exc

    async def _call_mcp(self, name: str, arguments: dict[str, Any]) -> Any:
        raise self._exc


@pytest.mark.anyio
async def test_call_tool_wraps_httpx_connect_error() -> None:
    # connect error — one of the enumerated classes.
    with pytest.raises(MagickitMcpError, match="ConnectError"):
        await _RaisingMcp(httpx.ConnectError("cannot connect")).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_httpx_read_timeout() -> None:
    # read timeout — httpx.TimeoutException is a subclass of httpx.HTTPError.
    with pytest.raises(MagickitMcpError, match="ReadTimeout"):
        await _RaisingMcp(httpx.ReadTimeout("read timed out")).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_httpx_non_2xx_status() -> None:
    # non-2xx — HTTPStatusError. Constructed with the request/response the
    # library expects; the important part is that the type is in the tuple.
    request = httpx.Request("POST", "http://test.invalid/mcp")
    response = httpx.Response(503, request=request)
    with pytest.raises(MagickitMcpError, match="HTTPStatusError"):
        await _RaisingMcp(
            httpx.HTTPStatusError("503", request=request, response=response)
        ).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_json_decode_error() -> None:
    # non-JSON body reaching this level — direct JSONDecodeError.
    with pytest.raises(MagickitMcpError, match="JSONDecodeError"):
        await _RaisingMcp(json.JSONDecodeError("expecting value", "not json", 0)).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_mcp_error() -> None:
    # mcp protocol error — server returned a JSON-RPC error object.
    err = McpError(ErrorData(code=-32000, message="server refused"))
    with pytest.raises(MagickitMcpError, match="McpError"):
        await _RaisingMcp(err).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_streamable_http_error() -> None:
    # mcp streamable-http transport error.
    with pytest.raises(MagickitMcpError, match="StreamableHTTPError"):
        await _RaisingMcp(StreamableHTTPError("resumption token missing")).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_os_error() -> None:
    # socket-level / broken pipe class of failure that surfaces below httpx.
    with pytest.raises(MagickitMcpError, match=r"OSError|ConnectionResetError"):
        await _RaisingMcp(ConnectionResetError("peer reset")).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_builtin_timeout_error() -> None:
    # asyncio.TimeoutError == TimeoutError (Py 3.11+); anyio's timeouts land here.
    with pytest.raises(MagickitMcpError, match="TimeoutError"):
        await _RaisingMcp(TimeoutError("op timed out")).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_unwraps_transport_error_inside_exception_group() -> None:
    """anyio task groups (used by the mcp streamable-http transport) wrap
    internal failures in an :class:`ExceptionGroup`. ``call_tool`` splits the
    group on the transport tuple and re-raises a bare :class:`MagickitMcpError`
    so the ``LoopControlReader.report_observed`` bare ``except MagickitMcpError``
    matches — the design point of msg-1685 §2.
    """
    inner = httpx.ConnectError("cannot connect")
    group = ExceptionGroup("mcp transport", [inner])
    with pytest.raises(MagickitMcpError, match="ConnectError"):
        await _RaisingMcp(group).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_wraps_transport_error_as_a_bare_magickit_mcp_error() -> None:
    """PR-gate #178 regression: raising a wrap from *inside* an ``except*`` block
    bundles it back into an :class:`ExceptionGroup`, which the caller's plain
    ``except MagickitMcpError`` will NOT match. This test does not rely on
    ``pytest.raises`` for the match — it inspects ``type(exc)`` directly, so a
    future edit that re-introduces ``except*`` (or otherwise lets a group escape)
    fails here even if the naïve match would pass.

    The invariant, in one sentence: what the caller catches must be a bare
    :class:`MagickitMcpError`, not any subclass or wrapping of it.
    """

    async def _catch(mcp: _RaisingMcp) -> BaseException:
        try:
            await mcp.call_tool("x", {})
        except BaseException as exc:
            return exc
        pytest.fail("call_tool did not raise")

    # 1. Bare transport exception (not in a group).
    bare_exc = await _catch(_RaisingMcp(httpx.ConnectError("nope")))
    assert type(bare_exc) is MagickitMcpError, (
        f"expected a bare MagickitMcpError, got {type(bare_exc).__name__}: {bare_exc!r}"
    )

    # 2. Transport exception wrapped in an anyio-style ExceptionGroup (the
    #    production case: the mcp streamable-http transport uses
    #    ``anyio.create_task_group`` internally, which wraps its inner failure
    #    in an :class:`ExceptionGroup` on exit).
    group_exc = await _catch(
        _RaisingMcp(ExceptionGroup("mcp task group", [httpx.ConnectError("nope")]))
    )
    assert type(group_exc) is MagickitMcpError, (
        "expected a bare MagickitMcpError from a transport-only group, "
        f"got {type(group_exc).__name__}: {group_exc!r}"
    )

    # 3. Nested groups (a group inside a group) — still bare wrap at the top.
    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [httpx.ConnectError("nope")])])
    nested_exc = await _catch(_RaisingMcp(nested))
    assert type(nested_exc) is MagickitMcpError, (
        "expected a bare MagickitMcpError from a nested transport-only group, "
        f"got {type(nested_exc).__name__}: {nested_exc!r}"
    )


@pytest.mark.anyio
async def test_call_tool_surfaces_transport_and_programming_side_by_side_in_mixed_group() -> None:
    """If an anyio task group ends with BOTH a transport failure and a
    programming bug in different tasks, the wrap must remain visible (the
    caller's ``except MagickitMcpError`` at least gets a fair chance) AND
    the programming error must not disappear (msg-1685 §2 — programming
    errors escape). The two are surfaced as siblings in a new group.
    """
    mixed = ExceptionGroup(
        "mcp task group",
        [httpx.ConnectError("nope"), TypeError("bug in caller args")],
    )
    with pytest.raises(BaseExceptionGroup) as excinfo:
        await _RaisingMcp(mixed).call_tool("x", {})
    # Both the wrap and the remainder are reachable.
    flat: list[BaseException] = []

    def _walk(e: BaseException) -> None:
        if isinstance(e, BaseExceptionGroup):
            for x in e.exceptions:
                _walk(x)
        else:
            flat.append(e)

    _walk(excinfo.value)
    kinds = {type(e).__name__ for e in flat}
    assert "MagickitMcpError" in kinds, f"transport wrap disappeared: {kinds}"
    assert "TypeError" in kinds, f"programming error disappeared: {kinds}"


@pytest.mark.anyio
async def test_call_tool_mixed_group_attaches_cause_to_wrap_not_to_group() -> None:
    """PR-gate #178 (rounds 2 & 3) regression: traceback bugs in the mixed-group path.

    Round 2 — bug A: **the wrap lost its cause.** Constructing the
    ``MagickitMcpError`` with ``_wrap_transport_error`` and putting it in a
    list gave it no ``__cause__``, severing the traceback chain to the
    underlying transport failure. Fixed by setting ``wrapped.__cause__``
    explicitly.

    Round 2 — bug B: **the group got a false cause.** ``raise <group> from
    first_leaf`` attached the transport exception as the group's
    ``__cause__``, falsely asserting every sibling (including the
    programming bug) was caused by the transport failure. Fixed by chaining
    the outer group ``from group`` (the original caught container).

    Round 3 — bug C: **sibling transport exceptions were silently dropped.**
    Chaining the wrap to ``first_leaf`` alone lost any other transport
    failures that ``split()`` had matched into the same sub-group. Fixed by
    chaining ``wrapped.__cause__`` to ``transport_part`` (the whole sub-group
    of matched transport exceptions), not to a single leaf.
    """
    transport_a = httpx.ConnectError("first-transport")
    transport_b = httpx.ReadTimeout("second-transport")
    bug_exc = TypeError("bug in caller args")
    # Two transport siblings plus a programming bug — enough to exercise all
    # three regressions in one test. If we had only one transport exception,
    # bug C would be invisible (transport_part.exceptions would contain
    # exactly that leaf, so "chain to leaf" and "chain to group containing
    # only that leaf" would look identical).
    mixed = ExceptionGroup("mcp task group", [transport_a, transport_b, bug_exc])

    with pytest.raises(BaseExceptionGroup) as excinfo:
        await _RaisingMcp(mixed).call_tool("x", {})

    group = excinfo.value

    # Bug B pin: the outer group must not falsely name a transport leaf as
    # its cause — the programming bug is a sibling, not a downstream effect.
    assert group.__cause__ is not transport_a, (
        "the mixed group should not carry a transport leaf as its __cause__ "
        "(that would falsely assert the programming bug was caused by the transport failure)"
    )
    assert group.__cause__ is not transport_b, (
        "the mixed group should not carry a transport leaf as its __cause__"
    )

    # Bug A pin: find the wrap and verify it kept a traceback chain at all.
    wraps = [e for e in group.exceptions if isinstance(e, MagickitMcpError)]
    assert len(wraps) == 1, f"expected exactly one wrap in the group, got {wraps!r}"
    wrap = wraps[0]
    assert wrap.__cause__ is not None, (
        "the wrap must have a __cause__ set (Bug A: _wrap_transport_error does "
        "not set __cause__ implicitly, so the mixed-group branch must set it by hand)"
    )

    # Bug C pin: the wrap's cause must preserve every matched transport
    # exception, not just the first leaf. ``transport_part`` from ``group.split``
    # is a BaseExceptionGroup containing both transport_a and transport_b.
    assert isinstance(wrap.__cause__, BaseExceptionGroup), (
        "the wrap must chain to the transport sub-group (so all matched siblings "
        f"survive), not to a single leaf (got __cause__={wrap.__cause__!r})"
    )
    cause_leaves = list(wrap.__cause__.exceptions)
    assert transport_a in cause_leaves, (
        f"first transport sibling missing from wrap.__cause__: {cause_leaves!r}"
    )
    assert transport_b in cause_leaves, (
        "second transport sibling silently dropped from wrap.__cause__ — "
        "this is the Bug C regression (chain to leaf instead of transport_part). "
        f"Got: {cause_leaves!r}"
    )
    # Negative: no programming bugs should have leaked into the cause chain
    # (they are siblings in the outer group, not a downstream effect of the
    # transport failure).
    assert bug_exc not in cause_leaves, (
        "the programming bug should NOT be in wrap.__cause__ (it's a sibling task "
        "failure, surfaced separately in the outer group)"
    )


@pytest.mark.anyio
async def test_call_tool_single_transport_wrap_chains_to_the_transport_error() -> None:
    """Sibling of the mixed-group traceback test — the same ``__cause__`` rule
    must hold on the (much more common) single-transport path too.

    - ``except _TRANSPORT_EXCEPTIONS as exc`` branch: ``raise wrap from exc``
      — the cause IS the bare transport exception (no group involved).
    - ``except BaseExceptionGroup`` transport-only branch: ``raise wrap from
      transport_part`` — the cause is the sub-group so every matched sibling
      is preserved (PR-gate #178 round 3).
    """
    # Bare transport case — the wrap's __cause__ is the bare exception itself.
    bare = httpx.ConnectError("nope")
    with pytest.raises(MagickitMcpError) as excinfo:
        await _RaisingMcp(bare).call_tool("x", {})
    assert excinfo.value.__cause__ is bare

    # Group-with-only-transport case — the wrap's __cause__ is the
    # transport sub-group (containing the leaf), not the leaf itself.
    # Round 3: chaining to the leaf would drop any additional matched siblings
    # (see ``test_call_tool_transport_only_group_preserves_all_sibling_transports``).
    grouped_inner = httpx.ConnectError("nope-grouped")
    group = ExceptionGroup("mcp task group", [grouped_inner])
    with pytest.raises(MagickitMcpError) as excinfo2:
        await _RaisingMcp(group).call_tool("x", {})
    cause = excinfo2.value.__cause__
    assert isinstance(cause, BaseExceptionGroup), (
        f"expected __cause__ to be the transport sub-group, got {cause!r}"
    )
    assert grouped_inner in cause.exceptions, (
        f"transport leaf missing from wrap.__cause__.exceptions: {list(cause.exceptions)!r}"
    )


@pytest.mark.anyio
async def test_call_tool_transport_only_group_preserves_all_sibling_transports() -> None:
    """PR-gate #178 (round 3) regression: with multiple transport siblings in
    the same ``ExceptionGroup``, the wrap must chain to the WHOLE
    ``transport_part``, not to the first peeled leaf.

    Under the round-2 code (``raise wrapped from first_leaf`` /
    ``wrapped.__cause__ = first``) the second sibling silently disappeared
    from the traceback — the exact silent-drop failure class this whole
    thread exists to close.

    Scenario: anyio task group where two concurrent tasks both fail with
    transport-class exceptions (e.g. connect error AND read timeout on
    parallel sub-requests). The wrap must let a log reader see both.
    """
    first_transport = httpx.ConnectError("first-connect")
    second_transport = httpx.ReadTimeout("second-timeout")
    group = ExceptionGroup("mcp task group", [first_transport, second_transport])

    with pytest.raises(MagickitMcpError) as excinfo:
        await _RaisingMcp(group).call_tool("x", {})

    wrap = excinfo.value
    # Still bare (the msg-1685 §2 contract) — the caller's `except
    # MagickitMcpError` at the call site must match, even under multi-sibling.
    assert type(wrap) is MagickitMcpError, (
        f"expected a bare MagickitMcpError, got {type(wrap).__name__}: {wrap!r}"
    )
    # The cause is the transport sub-group, containing BOTH leaves.
    cause = wrap.__cause__
    assert isinstance(cause, BaseExceptionGroup), (
        f"expected __cause__ to be the transport sub-group, got {cause!r}"
    )
    leaves = list(cause.exceptions)
    assert first_transport in leaves, (
        f"first transport sibling missing from wrap.__cause__: {leaves!r}"
    )
    assert second_transport in leaves, (
        "second transport sibling silently dropped — this is the round-3 "
        f"regression (chain to leaf instead of transport_part). Got: {leaves!r}"
    )


@pytest.mark.anyio
async def test_call_tool_lets_group_of_pure_programming_errors_escape_as_group() -> None:
    """A group with no transport members is re-raised as-is — the runtime's
    "programming errors escape" invariant holds even under grouping.
    """
    group = ExceptionGroup("pure bugs", [TypeError("a"), KeyError("b")])
    with pytest.raises(BaseExceptionGroup) as excinfo:
        await _RaisingMcp(group).call_tool("x", {})
    # No MagickitMcpError should have been introduced.
    kinds = {type(e).__name__ for e in excinfo.value.exceptions}
    assert "MagickitMcpError" not in kinds


@pytest.mark.anyio
async def test_call_tool_lets_programming_errors_escape() -> None:
    """The load-bearing counterpart to the wrap tests.

    msg-1685 §2 forbids ``except Exception`` in ``call_tool`` because it would
    silently convert a programming bug (``TypeError`` from a mis-shaped call,
    ``KeyError`` from a typo) into an "MCP failure" — the same silent-success
    hole the whole T-error-envelope-read-as-data thread exists to close.

    A bare ``TypeError`` from ``_call_mcp`` must propagate; the sibling test
    ``test_report_observed_lets_programming_errors_propagate`` in
    ``test_conductor_control.py`` pins the same rule at the call-site catch.
    """
    with pytest.raises(TypeError):
        await _RaisingMcp(TypeError("mis-shaped call")).call_tool("x", {})


@pytest.mark.anyio
async def test_call_tool_lets_key_error_escape_too() -> None:
    """Second programming-error class, same rule. A tuple that grew to catch
    ``KeyError`` (say, "network layer sometimes raises it") would fail here."""
    with pytest.raises(KeyError):
        await _RaisingMcp(KeyError("missing dict key")).call_tool("x", {})
