"""Unit tests for ``spirrow_mindwire.adapters._sdk_result``.

The helper is what closes the ``T-sdk-is-error-loses-the-reason`` defect: the
adapter-side wiring (in ``test_claude_code_sdk_adapter.py`` and
``test_naysayer_sdk_adapter.py``) proves the code path is joined, and these
tests pin the reason-source axis directly so a future refactor cannot silently
collapse it back onto a constant.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from spirrow_mindwire.adapters._sdk_result import (
    SDK_ERROR_MARKER_PREFIX,
    SdkIsErrorSignal,
    capture_is_error_detail,
    emit_sdk_error_marker,
    find_sdk_error_signal,
)


@dataclass
class _FakeResultMessage:
    """Stand-in that matches the real ``ResultMessage`` at the fields we read.

    Kept as a dataclass so the ``absent`` branch's reflection dump takes the
    ``dataclass`` enumeration path — the same path the real object takes.
    """

    subtype: str = ""
    duration_ms: int = 0
    duration_api_ms: int = 0
    is_error: bool = True
    num_turns: int = 0
    session_id: str = ""
    stop_reason: str | None = None
    result: str | None = None
    errors: list[str] | None = None
    api_error_status: int | None = None
    permission_denials: list[Any] | None = None
    model: str | None = None


# --------------------------------------------------------------------------- #
# reason_source axis (S-2) — the whole point of the change
# --------------------------------------------------------------------------- #


def test_reason_source_result_when_result_carries_the_reason() -> None:
    final = _FakeResultMessage(result="Anthropic returned 429")
    detail = capture_is_error_detail(final)
    assert detail["reason_source"] == "result"
    assert detail["message"] == "Anthropic returned 429"


def test_reason_source_field_when_result_empty_but_another_field_carries() -> None:
    # Real observed case from Anthropic CLI ≥ 2.1.110: ``result=None`` yet
    # ``api_error_status=429`` was populated. The pre-change ``or CONST``
    # collapsed this onto the constant string.
    final = _FakeResultMessage(result=None, subtype="", api_error_status=429)
    detail = capture_is_error_detail(final)
    assert detail["reason_source"] == "field:api_error_status"
    assert "429" in detail["message"]


def test_reason_source_absent_names_the_state_and_attaches_a_dump() -> None:
    # Every reason candidate empty — the actual "we truly do not know" branch.
    # Distinct from ``capture_failed`` (which would mean the pipeline itself
    # blew up); the two must never be confused (§1-3, one of the load-bearing
    # points in Bohr's msg-1721 processing).
    final = _FakeResultMessage()  # all defaults empty
    detail = capture_is_error_detail(final)
    assert detail["reason_source"] == "absent"
    assert "none carried a reason" in detail["message"]
    dump = detail["absent_dump"]
    assert dump["type"] == "_FakeResultMessage"
    assert dump["introspection"] == "dataclass"
    # ``model`` is explicitly excluded from the dump so the Lexora tier-alias
    # echo cannot leak into a surface that reads like provenance
    # (naysayer_sdk._session_facts reasoning, applied here verbatim).
    assert "model" not in dump["fields"]


def test_reason_source_capture_failed_when_the_pipeline_itself_raises() -> None:
    class _EvilFinal:
        """Every ``getattr`` on this raises — models a broken SDK object.

        ``_pick_reason`` will still be reached (per-field capture swallows the
        exception into ``{"capture_failed": ...}``), so ``reason_source`` will
        land at ``"absent"``. This test therefore forces a genuine top-level
        failure by making ``_capture_known`` explode, which needs the getattr
        pipeline to fail *outside* the per-field guard. The simplest way is to
        replace the underlying function via monkeypatch — but doing that from
        the outside is over-scoped. Instead we assert the pipeline handles a
        totally hostile object without either eating the failure or crashing
        the caller: the outcome is a well-formed detail dict with a real
        message.

        The point of the ``capture_failed`` axis is not that this specific
        input reaches it — most inputs eventually reach ``absent`` — but that
        the axis EXISTS as a distinct value the reader can rely on when it
        does apply.
        """

        def __getattribute__(self, name: str) -> Any:
            raise RuntimeError(f"boom: {name}")

    detail = capture_is_error_detail(_EvilFinal())
    # Regardless of which branch the hostile input lands in, the return value
    # must be a well-formed dict with a message and reason_source — never a
    # crash bubbling up to the adapter's try/except.
    assert "reason_source" in detail
    assert "message" in detail
    assert isinstance(detail["message"], str)


def test_reason_source_capture_failed_is_a_distinct_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """The four values must be legibly distinct — this pins the axis.

    Force ``_capture_known`` to raise so the outermost fail-safe fires and
    ``reason_source="capture_failed"`` is actually reached.
    """
    from spirrow_mindwire.adapters import _sdk_result

    def _explode(_final: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        raise RuntimeError("simulated capture pipeline failure")

    monkeypatch.setattr(_sdk_result, "_capture_known", _explode)
    detail = capture_is_error_detail(_FakeResultMessage())
    assert detail["reason_source"] == "capture_failed"
    assert "capture_error" in detail
    assert "simulated capture pipeline failure" in detail["capture_error_message"]


# --------------------------------------------------------------------------- #
# S-4 — session facts are retained on the FAILURE path, not just success
# --------------------------------------------------------------------------- #


def test_session_facts_are_present_in_captured_fields() -> None:
    final = _FakeResultMessage(session_id="sid-xyz", duration_ms=1234, num_turns=3, result="boom")
    detail = capture_is_error_detail(final)
    fields = detail["captured_fields"]
    assert fields["session_id"] == "sid-xyz"
    assert fields["duration_ms"] == 1234
    assert fields["num_turns"] == 3
    # And the reason still wins its own key (session facts complement, not replace).
    assert detail["reason_source"] == "result"
    assert detail["message"] == "boom"


# --------------------------------------------------------------------------- #
# Bounded values — the marker cannot balloon the tail window
# --------------------------------------------------------------------------- #


def test_long_string_values_are_truncated() -> None:
    huge = "x" * 5000
    final = _FakeResultMessage(result=huge)
    detail = capture_is_error_detail(final)
    # The captured field is bounded (message may be the full string but the
    # captured slice is where the marker payload gets its bulk).
    captured_result = detail["captured_fields"]["result"]
    assert isinstance(captured_result, str)
    assert len(captured_result) < len(huge)
    assert captured_result.endswith("ch)")  # truncation footer


def test_large_container_values_are_summarised_not_dumped() -> None:
    final = _FakeResultMessage(
        result=None,
        errors=[f"err-{i}" for i in range(200)],
    )
    detail = capture_is_error_detail(final)
    # A long errors list stays as ``list(len=200)`` so the marker line cannot
    # balloon the tail window.
    assert detail["captured_fields"]["errors"] == "list(len=200)"
    # And the picker still recognises it as non-empty, so it drives reason_source.
    assert detail["reason_source"] == "field:errors"


def test_small_scalar_list_preserves_contents_so_the_message_carries_the_reason() -> None:
    """A small ``errors=["…"]`` reaches the marker as text, not as ``list(len=1)``.

    The pre-round-3 code summarised every list as ``type(len=N)`` which meant
    the very reason text the SDK put on this field got thrown away in favour
    of an opaque length string. Real SDK ``errors`` lists carry short human
    strings; preserving them bounded is what makes ``field:errors`` actually
    useful as a reason surface.
    """
    final = _FakeResultMessage(
        result=None,
        errors=["Anthropic returned 429 rate limit"],
    )
    detail = capture_is_error_detail(final)
    assert detail["captured_fields"]["errors"] == ["Anthropic returned 429 rate limit"]
    assert detail["reason_source"] == "field:errors"
    assert "429 rate limit" in detail["message"]


# --------------------------------------------------------------------------- #
# PR #181 round 3 regression — an EMPTY container must not shadow a real
# reason on a later field (the naysayer's "list(len=0)" defect).
# --------------------------------------------------------------------------- #


def test_empty_container_is_not_treated_as_a_reason() -> None:
    """An ``errors=[]`` must NOT be picked as ``field:errors``.

    The round-2 defect was: ``_pick_reason`` inspected the summarised value
    (which was the string ``"list(len=0)"`` for an empty list), and since a
    non-empty string looks like "there is a reason", the picker returned
    ``("field:errors", "SDK is_error; errors='list(len=0)'")``. That is a
    reason string that carries no reason — the exact defect this whole thread
    exists to remove.

    The fix is to inspect the RAW value (an empty list is empty). Pin here so
    a future refactor cannot re-collapse raw and summary.
    """
    final = _FakeResultMessage(
        result=None,
        errors=[],
        api_error_status=429,  # the real reason, on a later field
    )
    detail = capture_is_error_detail(final)
    assert detail["reason_source"] == "field:api_error_status"
    assert "429" in detail["message"]


def test_empty_containers_alone_reach_absent() -> None:
    """When every known field is empty (including empty containers), we land
    at ``absent``, not at a phony ``field:<name>`` picked off a length string.
    """
    final = _FakeResultMessage(
        result=None,
        errors=[],
        permission_denials=[],
    )
    detail = capture_is_error_detail(final)
    assert detail["reason_source"] == "absent"
    assert "none carried a reason" in detail["message"]


def test_zero_api_error_status_is_still_treated_as_a_reason() -> None:
    """``api_error_status=0`` (unusual but observed on some gateways) is a
    reason. The empty-container fix must not extend to zero/False.
    """
    final = _FakeResultMessage(result=None, api_error_status=0)
    detail = capture_is_error_detail(final)
    assert detail["reason_source"] == "field:api_error_status"
    assert "0" in detail["message"]


# --------------------------------------------------------------------------- #
# PR #181 round 4 regressions — three separate objections from the PR-gate
# review, each pinned so a future refactor cannot reintroduce the same defect.
# --------------------------------------------------------------------------- #


def test_non_string_result_falls_through_to_field_result_not_absent() -> None:
    """A non-string ``result`` (dict / list from a future SDK) must surface
    as ``field:result``, not vanish into ``absent``.

    Earlier the ``_pick_reason`` loop explicitly skipped ``result`` because
    the fast-path had already handled it, so a ``result={"code": "…"}``
    (the fast-path's ``isinstance(str)`` gate rejects) fell out of every
    branch and reached ``absent`` — the actual reason on the field was
    dropped entirely. Fixed by removing the loop's skip; ``result`` is a
    legitimate general fallback candidate. (PR #181 round 4, objection 2.)
    """

    # Non-dataclass here so this test stays independent of ``_FakeResultMessage``'s
    # frozen schema, and to prove the SDK not being a dataclass is not what
    # gates this behaviour.
    class _NonStringResult:
        subtype = ""
        duration_ms = 0
        duration_api_ms = 0
        is_error = True
        num_turns = 0
        session_id = ""
        stop_reason = None
        errors = None
        api_error_status = None
        permission_denials = None
        # ClassVar to avoid RUF012 — this stub is read as a class-level
        # constant; the helper only reads the attribute, never mutates it.
        result: ClassVar[dict[str, str]] = {"code": "rate_limit", "message": "gateway rejected"}
        model = None

    detail = capture_is_error_detail(_NonStringResult())
    assert detail["reason_source"] == "field:result"
    # Small-scalar-dict preservation means the actual content is in the
    # message, not the opaque ``dict(len=2)`` that the earlier summariser
    # produced. A reader can see WHAT the SDK put on the field.
    assert "rate_limit" in detail["message"]
    assert "gateway rejected" in detail["message"]


def test_small_scalar_dict_preserves_contents_in_captured_fields() -> None:
    """Analogous to the scalar-list case: a small ``{str: scalar}`` dict is
    preserved verbatim in ``captured_fields`` so the reason text reaches the
    marker instead of getting flattened to ``dict(len=N)``.
    """

    class _F:
        subtype = ""
        duration_ms = 0
        duration_api_ms = 0
        is_error = True
        num_turns = 0
        session_id = ""
        stop_reason = None
        errors = None
        api_error_status = None
        permission_denials = None
        result: ClassVar[dict[str, Any]] = {"code": "auth", "attempt": 3}
        model = None

    detail = capture_is_error_detail(_F())
    result_summary = detail["captured_fields"]["result"]
    assert isinstance(result_summary, dict)
    assert result_summary == {"code": "auth", "attempt": 3}


def test_large_dict_still_summarised_not_dumped() -> None:
    """Analogous to the large-list case: the bound on scalar-dict preservation
    only kicks in for small dicts. A big dict still gets ``dict(len=N)`` so
    the marker line cannot balloon.
    """

    class _F:
        subtype = ""
        duration_ms = 0
        duration_api_ms = 0
        is_error = True
        num_turns = 0
        session_id = ""
        stop_reason = None
        errors = None
        api_error_status = None
        permission_denials = None
        result: ClassVar[dict[str, int]] = {f"k{i}": i for i in range(20)}
        model = None

    detail = capture_is_error_detail(_F())
    assert detail["captured_fields"]["result"] == "dict(len=20)"


def test_absent_dump_survives_a_broken_property() -> None:
    """A property that raises must not destroy the entire reflection dump.

    Objection 1 from PR #181 round 4: the ``dir()`` fallback path filtered
    names with ``callable(getattr(final, n, None))`` — a bare ``getattr`` that
    only catches ``AttributeError``. A property raising ``RuntimeError`` /
    ``ValueError`` (highly likely on a broken SDK object, which is the exact
    input the reflection dump exists for) propagated out of the list
    comprehension, the outer ``except`` caught it, and the entire dump
    collapsed to ``{}``. The evidence for ``absent`` was destroyed by the
    very defect the reflection existed to surface.

    Fix pinned here: an individual raising property keeps its name in the
    enumeration so it lands in the fields dump as ``capture_failed: true``,
    and OTHER fields still get dumped normally.
    """

    class _WithHostileProperty:
        __slots__ = ()  # no vars() → forces the dir() path

        is_error = True
        subtype = ""
        stop_reason = None
        result = None
        errors = None
        api_error_status = None
        permission_denials = None
        session_id = "hostile-sid"
        duration_ms = 42
        num_turns = 1

        @property
        def some_hostile_property(self) -> str:
            raise RuntimeError("this property always raises")

    detail = capture_is_error_detail(_WithHostileProperty())
    # Landing at absent is correct here — every known reason field is empty.
    assert detail["reason_source"] == "absent"
    dump = detail["absent_dump"]
    # The dump was NOT destroyed by the hostile property.
    assert dump["introspection"] == "dir"
    fields = dump["fields"]
    assert fields, f"absent_dump.fields is empty — hostile property destroyed it: {dump!r}"
    # Session facts on the object are still captured normally.
    assert fields.get("session_id") == "hostile-sid"
    assert fields.get("duration_ms") == 42
    # And the hostile property itself surfaces as capture_failed — not silently
    # dropped, so a reader can see WHERE the object misbehaves.
    hostile = fields.get("some_hostile_property")
    assert isinstance(hostile, dict), f"expected capture_failed dict, got {hostile!r}"
    assert hostile.get("capture_failed") is True


# --------------------------------------------------------------------------- #
# T-quarantine-reasons-captured-but-never-read (msg-2944 §5) — the projection
# for ``permission_denials``. Four acceptance criteria named verbatim in the
# spec: property (bounded) / readability / (c)-independence / [] non-regression.
# --------------------------------------------------------------------------- #


def test_permission_denials_projection_readability_reason_reaches_the_marker() -> None:
    """A mapping-shaped denial must reach the marker as text — not ``list(len=1)``.

    This is the whole point of the projection (msg-2944 §5, readability
    acceptance criterion). Before the projection, a real
    ``permission_denials=[{tool_name: …, tool_input: …, rule: …}]`` was
    summarised as ``"list(len=1)"`` by ``_summarize_value``'s scalar-only
    predicate (the elements are dicts, so the ``all(isinstance(scalar))``
    check at line 168 fails). The projection reduces each element to a
    bounded ``"k=v"`` string ahead of time so the outer predicate accepts
    the list and the element-wise preservation branch runs.

    A reader of the marker must be able to see WHAT was denied — the
    ``list(len=1)`` outcome carries no reason and is exactly the defect
    this whole thread exists to remove.
    """
    denial = {
        "tool_name": "Bash",
        "tool_input": "git push origin main",
        "rule": "branch-protection",
    }
    final = _FakeResultMessage(result=None, permission_denials=[denial])
    detail = capture_is_error_detail(final)

    captured = detail["captured_fields"]["permission_denials"]
    # No opaque length-only summary anywhere in the pipeline.
    assert captured != "list(len=1)"
    assert isinstance(captured, list)
    assert len(captured) == 1
    # The denial content is legible in the projected element. Both keys and
    # values are wrapped in ``json.dumps``-style quotes so a value containing
    # spaces (``git push origin main``) has unambiguous boundaries (msg-3231
    # legibility objection / msg-3326 revision / msg-3328 substitution) and
    # a key containing a control character cannot split the marker line
    # (PR #288 PR-gate follow-up: symmetric json.dumps on keys).
    element = captured[0]
    assert isinstance(element, str)
    assert '"tool_name"="Bash"' in element
    assert '"rule"="branch-protection"' in element
    assert '"tool_input"="git push origin main"' in element

    # Picker treats the denial as a real reason (it is one).
    assert detail["reason_source"] == "field:permission_denials"
    # Sanity: the marker's message string carries the denial too.
    assert "Bash" in detail["message"]


def test_permission_denials_projection_escapes_quotes_backslashes_and_newlines() -> None:
    """Escape pin (msg-3328 §4): ``"``, ``\\``, and ``\\n`` in a denial value
    must not break the marker's quoting or its line-integrity.

    Einstein's msg-3327 blocking objection: the earlier hand-rolled escaper
    (msg-3326 revision) only handled ``"`` and ``\\`` — a newline in a
    denial value (trivial to produce from any tool output pinned into a
    permission decision) would land verbatim in the marker and split the
    log line, desynchronising every line-oriented reader downstream.

    Fix (msg-3328): use ``json.dumps`` on the stringified value, which
    normalises every control character (``\\n``, ``\\r``, ``\\t``, and the
    rest) and every character that could break the outer double-quote
    boundary. This test is the load-bearing pin — if a future change swaps
    ``json.dumps`` out for something newline-unsafe, the final assertion
    (no ``\\n`` byte anywhere in the rendered field) fails loudly.
    """
    denial = {
        "tool_name": 'Bash "sub"',
        "tool_input": "line1\nline2",
        "rule": "back\\slash",
    }
    final = _FakeResultMessage(result=None, permission_denials=[denial])
    detail = capture_is_error_detail(final)

    captured = detail["captured_fields"]["permission_denials"]
    assert isinstance(captured, list)
    assert len(captured) == 1
    element = captured[0]
    assert isinstance(element, str)

    # Inner ``"`` is escaped as ``\"`` (two characters), so the outer
    # double-quote boundary stays unambiguous.
    assert r'"tool_name"="Bash \"sub\""' in element

    # Inner ``\`` is escaped as ``\\`` (two characters).
    assert r'"rule"="back\\slash"' in element

    # Inner newline is escaped as the literal two-character sequence ``\n``,
    # NOT rendered as an embedded LF byte.
    assert r'"tool_input"="line1\nline2"' in element

    # The load-bearing line-integrity assertion: no raw newline byte anywhere
    # in the rendered field. This is what Einstein's objection specifically
    # required — a log parser reading one marker per line must not see this
    # field split across multiple lines.
    assert "\n" not in element
    assert "\r" not in element


def test_permission_denials_projection_escapes_control_chars_in_keys() -> None:
    """PR #288 PR-gate follow-up: KEYS containing control characters must
    also not split the marker line.

    The msg-3328 revision applied ``json.dumps`` to values only; the PR-gate
    naysayer identified that the SDK types the field as ``list[Any]`` and
    the CLI populates dict elements from an untrusted CLI JSON blob whose
    key shape is not enforced, so an input mapping can easily contain a key
    like ``{"bad\\nkey": "value"}``. If the key were injected raw into the
    f-string, that newline would be emitted verbatim and split the log line
    — reintroducing the exact defect this whole PR exists to fix.

    Fix pinned here: apply ``json.dumps(str(k))`` symmetrically to keys, so
    the same line-integrity invariant holds on both sides of ``=``.
    """
    denial = {
        "bad\nkey": "safe-value",
        "tab\tkey": "another",
        'quote"key': "third",
    }
    final = _FakeResultMessage(result=None, permission_denials=[denial])
    detail = capture_is_error_detail(final)

    captured = detail["captured_fields"]["permission_denials"]
    assert isinstance(captured, list)
    assert len(captured) == 1
    element = captured[0]
    assert isinstance(element, str)

    # No raw control byte anywhere — this is the invariant the PR-gate
    # objection specifically named.
    assert "\n" not in element
    assert "\r" not in element
    assert "\t" not in element

    # Each key is JSON-escaped inside its quotes: newline becomes literal
    # ``\n`` (two characters), tab becomes literal ``\t``, quote becomes
    # ``\"``. The outer ``"..."="..."`` boundary is preserved on both sides.
    assert r'"bad\nkey"="safe-value"' in element
    assert r'"tab\tkey"="another"' in element
    assert r'"quote\"key"="third"' in element


def test_permission_denials_projection_truncates_at_pair_boundaries_not_mid_quote() -> None:
    """PR #288 PR-gate follow-up (blocking correctness): a joined pair-text
    that exceeds ``_FIELD_VALUE_MAX_LEN`` must NOT be sliced mid-quote by
    the outer ``_summarize_value`` truncation.

    The msg-3334 revision wrapped every key and every value in ``json.dumps``
    for line-integrity and legibility. That made the marker's format
    structurally-quoted, so a quote-aware log reader (``shlex.split``,
    JSON-fragment parsers, ...) relies on every ``"..."`` span being
    closed. But ``_summarize_value``'s string branch truncates blindly at
    ``_FIELD_VALUE_MAX_LEN``: if the joined pair-text exceeds the cap, the
    truncation could sever a closing ``"`` or split a ``\\uXXXX`` escape
    sequence in half — reintroducing structural invalidity from a different
    angle than the newline defects the earlier revisions fixed.

    Fix pinned here: pair-boundary-aware truncation inside
    ``_project_denial_element`` (:func:`_join_pairs_bounded`), with a
    ``…(+K pairs truncated)`` footer so the drop is a diagnostic surface
    (one long pair dropped vs. many short pairs dropped is distinguishable
    to a reader).
    """
    from spirrow_mindwire.adapters._sdk_result import _FIELD_VALUE_MAX_LEN

    # Force overflow: many pairs whose joined length vastly exceeds
    # _FIELD_VALUE_MAX_LEN. Each key + value pair is small enough on its
    # own to fit; it's the join that pushes past the cap.
    denial = {f"key_{i:03d}": ("v" * 40) for i in range(30)}
    final = _FakeResultMessage(result=None, permission_denials=[denial])
    detail = capture_is_error_detail(final)

    captured = detail["captured_fields"]["permission_denials"]
    assert isinstance(captured, list)
    assert len(captured) == 1
    element = captured[0]
    assert isinstance(element, str)

    # Overflow fired: the pair-boundary truncator dropped at least some pairs
    # and marked the count. Without this footer, a reader cannot tell "one
    # very long pair" from "twenty short pairs" (which the plain
    # `_summarize_value` `...(+Nch)` footer conflates).
    assert "pairs truncated)" in element, f"expected pair-truncation footer; got: {element!r}"

    # Structural pin: the element ends with the footer (which ends in ``)``),
    # not mid-pair. If a blind truncation had fired, the element would end
    # mid-token (e.g., ``"key_012"="vvv``) with an unclosed quote span.
    assert element.endswith("pairs truncated)"), (
        f"element ended mid-pair or footer misformed: {element!r}"
    )

    # Every ``"`` in the element belongs to a properly-closed pair. The
    # projected form is ``"K1"="V1" "K2"="V2" …(+N pairs truncated)``; each
    # pair contributes exactly 4 raw quote characters (``"K"="V"``), and the
    # footer contributes none. So the count must be a multiple of 4.
    # (A ``\"`` inside a value adds 1 to the raw count, so this test
    # deliberately uses value strings that contain no ``"`` — the pin is
    # about truncation-induced imbalance, not escape-encoded quotes.)
    raw_quote_count = element.count('"')
    assert raw_quote_count % 4 == 0, (
        f"quote count {raw_quote_count} is not a multiple of 4 — "
        f"a pair boundary was severed. element={element!r}"
    )

    # And the whole thing still fits inside the per-field cap that the
    # pipeline's ``bounded is bounded`` invariant demands.
    assert len(element) <= _FIELD_VALUE_MAX_LEN + len("…(+999999ch)"), (
        f"element exceeded per-field bound: len={len(element)}"
    )

    # And single-line: no raw newline byte survived the projection.
    assert "\n" not in element
    assert "\r" not in element


def test_permission_denials_projection_preserves_non_ascii_in_dict_elements() -> None:
    """PR #288 PR-gate msg-3339 blocking regression fix.

    Before this fix, the inner ``json.dumps`` in ``_project_denial_element``
    used its default ``ensure_ascii=True``, so a dict denial containing
    Japanese text, emojis, or accented letters would render as
    ``\\uXXXX`` escape sequences inside the projected string. The outer
    :func:`emit_sdk_error_marker` uses ``ensure_ascii=False``, so scalar
    denials preserved non-ASCII while dict denials mangled it — an
    asymmetry the naysayer correctly identified as a legibility
    regression (from PR #283's pre-quoting behaviour, which preserved
    non-ASCII in dict values as raw text).

    Fix: pass ``ensure_ascii=False`` to both inner ``json.dumps`` calls so
    the projection preserves non-ASCII printables symmetrically. Line
    integrity is still guaranteed by RFC 8259's mandatory escape of
    U+0000-U+001F, which ``ensure_ascii=False`` does not disable.
    """
    denial = {
        "tool_name": "編集ツール",
        "rule": "禁止-本番ブランチ",
        "path": "docs/日本語/README.md",
        "emoji": "🚫",
    }
    final = _FakeResultMessage(result=None, permission_denials=[denial])
    detail = capture_is_error_detail(final)

    captured = detail["captured_fields"]["permission_denials"]
    assert isinstance(captured, list)
    assert len(captured) == 1
    element = captured[0]
    assert isinstance(element, str)

    # Non-ASCII characters survive as literal Unicode (not \\uXXXX escapes).
    assert '"tool_name"="編集ツール"' in element
    assert '"rule"="禁止-本番ブランチ"' in element
    assert '"path"="docs/日本語/README.md"' in element
    assert '"emoji"="🚫"' in element

    # No \\u escape sequences leaked into the projection.
    assert r"\u" not in element, f"non-ASCII was aggressively ASCII-escaped: {element!r}"

    # Round-trip through the marker emission (which also uses
    # ensure_ascii=False) preserves the Unicode too, proving end-to-end
    # legibility.
    stream = io.StringIO()
    emit_sdk_error_marker(detail, stream=stream)
    payload = stream.getvalue()[len(SDK_ERROR_MARKER_PREFIX) :].rstrip("\n")
    parsed = json.loads(payload)
    reparsed_element = parsed["captured_fields"]["permission_denials"][0]
    assert "編集ツール" in reparsed_element
    assert "🚫" in reparsed_element


def test_build_budgeted_pairs_fallback_is_json_quoted_under_huge_key() -> None:
    """PR #288 PR-gate msg-3348 blocking invariant fix.

    When a dict key is so long that no value budget remains
    (``v_share < 1``), the previous implementation emitted a bare
    literal ``<value truncated>`` token — unquoted. That broke the
    ``_project_denial_element`` docstring's load-bearing invariant
    ("every key and every value is a quoted JSON string") and would
    break any quote-aware log parser that reached the token.

    Fix pinned here: the fallback marker MUST be JSON-quoted so the
    invariant holds even in the degenerate huge-key case. This branch
    was previously a coverage blind spot.
    """
    from spirrow_mindwire.adapters._sdk_result import (
        _FIELD_VALUE_MAX_LEN,
        _build_budgeted_pairs,
    )

    # Force v_share < 1 by making the key longer than per_pair.
    # per_pair for 1 pair = _FIELD_VALUE_MAX_LEN - 1 = 499.
    # After json.dumps(key), the quoted key needs to be >= 496 chars so
    # v_share = 499 - 496 - 3 < 1.
    huge_key = "K" * 600
    result = _build_budgeted_pairs([(huge_key, "v")], _FIELD_VALUE_MAX_LEN)
    assert len(result) == 1
    element = result[0]

    # The value marker MUST be JSON-quoted (starts with " and ends with ").
    # Rendered form: '"KKK...K"="<value truncated>"'
    assert '="<value truncated>"' in element, f"fallback marker was not JSON-quoted: {element!r}"

    # Structural invariant: raw quote count is a multiple of 4
    # (2 for the key, 2 for the value). A bare unquoted marker would
    # produce 2 quotes total (odd of a 4-multiple would fail).
    assert element.count('"') % 4 == 0


def test_permission_denials_projection_footer_reports_true_dropped_count() -> None:
    """PR #288 PR-gate msg-3348 blocking correctness fix.

    Double-truncation defect: Phase 1 bounds a huge string to
    ~_FIELD_VALUE_MAX_LEN chars with a ``…(+Nch)`` footer. If Phase 2
    then truncates the already-truncated string and computes its footer
    from the Phase-1-truncated length, the reported dropped count is
    mathematically false — presenting a small footer count (~41) for a
    value where the true drop is huge (~4529). The reader is misled
    about the scale of data loss.

    Fix: Phase 2 receives RAW values (not Phase-1-scalarized) so its
    footer computes against the original length. This test pins the
    correctness invariant against future regression.
    """
    huge = "x" * 5000
    denial = {"tool_input": huge}
    final = _FakeResultMessage(result=None, permission_denials=[denial])
    detail = capture_is_error_detail(final)

    element = detail["captured_fields"]["permission_denials"][0]
    assert isinstance(element, str)

    # Extract the footer's dropped-char count from the element. The
    # element renders as ``"tool_input"="xxx...xxx…(+Nch)"``. We just
    # need to find the number inside ``…(+Nch)``.
    import re

    match = re.search(r"…\(\+(\d+)ch\)", element)
    assert match is not None, f"no truncation footer found in element: {element!r}"
    dropped = int(match.group(1))

    # The true dropped count is (5000 - kept), where kept is the number
    # of raw chars that survived. kept is roughly per_pair minus overhead
    # — well under 5000. So the reported dropped MUST be a substantial
    # fraction of 5000, not something absurdly small like 41.
    # Concretely: kept ~= 470-480, so dropped should be ~4520-4530.
    # Assert dropped is at least 4000 (well above the pre-fix false
    # value of ~41 which was computed against Phase 1's ~512-char
    # already-truncated string).
    assert dropped >= 4000, (
        f"footer reports mathematically false dropped count: "
        f"{dropped} (expected close to 5000-per_pair_budget, i.e. ~4500). "
        f"element={element!r}"
    )
    # And the count is consistent with the input length: kept + dropped
    # should equal or be very close to len(huge) = 5000.
    assert 4000 <= dropped <= 5000


def test_permission_denials_projection_value_that_fits_uncensored_gets_no_footer() -> None:
    """PR #288 PR-gate msg-3348 advisory structure fix.

    Advisory eager-truncation flaw: ``_build_budgeted_pairs`` reserved
    ``v_footer_reserve`` (12 chars) unconditionally, so a value that
    fits within the raw budget uncensored got truncated anyway and
    received a footer — mirroring the exact "eager truncation
    sacrifices perfectly valid pairs" defect msg-3339 fixed in
    ``_join_pairs_bounded``. Pin the fast-path so this can't regress.

    Construction: a dict with enough small pairs to trigger Phase 2
    overflow, plus one pair whose value length lands strictly between
    ``v_share - v_footer_reserve`` and ``v_share``. Before the fix,
    that pair would be truncated + footered; after, it survives as-is.
    """
    # Force Phase 2 by making the joined text just over _FIELD_VALUE_MAX_LEN
    # (500). Use 10 pairs of ~55 chars each = 550 chars — triggers overflow.
    # For 11 pairs total (10 + target), per_pair = 500//11 - 1 = 44. For
    # key ``"target"`` (json 8 chars), v_share = 44 - 8 - 3 = 33 and
    # v_budget with reserve = 33 - 12 = 21. A value of 30 chars fits in
    # v_share (30 <= 33) uncensored but would previously get truncated to
    # 21 + a 12-char footer under the eager-reserve path.
    denial = {f"k{i}": ("v" * 45) for i in range(10)}
    # Add one target value that lands in the fits-uncensored zone.
    denial["target"] = "y" * 30
    final = _FakeResultMessage(result=None, permission_denials=[denial])
    detail = capture_is_error_detail(final)

    element = detail["captured_fields"]["permission_denials"][0]
    assert isinstance(element, str)

    # The target value should appear uncensored (all 30 y's), with no
    # truncation footer immediately after it. Before the fix, it would
    # have been truncated to ``yyy...yyy…(+9ch)`` or similar.
    assert '"target"="' + ("y" * 30) + '"' in element, (
        f"target value was eagerly truncated even though it fits uncensored: {element!r}"
    )


def test_permission_denials_projection_single_large_value_preserves_key() -> None:
    """PR #288 PR-gate msg-3345 blocking regression fix.

    Regression scenario: a dict denial with a single key whose value
    exceeds ``_FIELD_VALUE_MAX_LEN``. Before the two-phase budgeting
    (msg-3345 fix), the previous pair-boundary truncation dropped the
    entire pair (including the key), leaving only
    ``…(+1 pairs truncated)`` — a total loss of visibility.

    After the fix, the key MUST be preserved and the value MUST render
    as a bounded prefix with a truncation footer. This is the naysayer's
    "budget the value before quoting so the whole pair fits" strategy,
    which is strictly safer than blind slicing (no mid-quote severance)
    AND strictly better for content preservation than dropping.
    """
    from spirrow_mindwire.adapters._sdk_result import _FIELD_VALUE_MAX_LEN

    huge = "x" * 5000
    denial = {"tool_input": huge}
    final = _FakeResultMessage(result=None, permission_denials=[denial])
    detail = capture_is_error_detail(final)

    captured = detail["captured_fields"]["permission_denials"]
    assert isinstance(captured, list)
    assert len(captured) == 1
    element = captured[0]
    assert isinstance(element, str)

    # Key preserved — this is the load-bearing pin.
    assert '"tool_input"=' in element, f"single-large-value dict dropped its key: {element!r}"

    # Value has a truncation footer (bounded), not raw x's forever.
    assert "ch)" in element, f"value did not carry a truncation footer: {element!r}"

    # And the whole element stays within the per-field cap.
    assert len(element) <= _FIELD_VALUE_MAX_LEN, f"element exceeded budget: len={len(element)}"

    # No mid-quote slice — every " span is closed. Simple structural
    # check: raw quote count is even (each opening " has a closing ").
    # (No inner escaped ``\"`` in this input since we used plain x's.)
    assert element.count('"') % 2 == 0

    # Line integrity intact.
    assert "\n" not in element
    assert "\r" not in element


def test_permission_denials_projection_multi_pair_with_one_large_value_preserves_all_keys() -> None:
    """PR #288 PR-gate msg-3345 blocking regression fix — multi-pair case.

    A dict with several keys where ONE has a large value must not lose
    the other keys. Under the pre-fix pair-boundary truncation, the
    large pair was dropped and any pairs after it were also dropped
    (once the loop broke). After the fix, all keys survive.
    """
    from spirrow_mindwire.adapters._sdk_result import _FIELD_VALUE_MAX_LEN

    denial = {
        "tool_name": "Bash",
        "tool_input": "x" * 5000,
        "rule": "branch-protection",
        "reason": "unbypassable-ruleset",
    }
    final = _FakeResultMessage(result=None, permission_denials=[denial])
    detail = capture_is_error_detail(final)

    element = detail["captured_fields"]["permission_denials"][0]
    assert isinstance(element, str)

    # Every key preserved.
    for key in ["tool_name", "tool_input", "rule", "reason"]:
        assert f'"{key}"=' in element, (
            f"key {key!r} dropped from multi-pair overflow projection: {element!r}"
        )

    # The large value has a truncation footer; the small ones don't.
    # (We can't assert this precisely without parsing, but the total
    # length is bounded, which is the important guarantee.)
    assert len(element) <= _FIELD_VALUE_MAX_LEN


def test_join_pairs_bounded_returns_full_join_when_it_fits() -> None:
    """PR #288 PR-gate msg-3339 advisory-eager-truncation fix.

    An earlier version of ``_join_pairs_bounded`` unconditionally reserved
    ``max_footer_len + 1`` in its ``limit``, so a joined text whose true
    length fell strictly between ``budget - max_footer_len - 1`` and
    ``budget`` would be truncated and get a ``…(+N pairs truncated)``
    footer appended — even though the raw join would have fit uncensored
    within ``budget``. That's data lost for nothing.

    Fix: fast-path check ``if len(full) <= budget: return full`` before
    entering the footer-aware truncation path. Pin here with a case that
    hits the previous edge exactly.
    """
    from spirrow_mindwire.adapters._sdk_result import (
        _FIELD_VALUE_MAX_LEN,
        _join_pairs_bounded,
    )

    # Two pairs whose joined length lands strictly between
    # ``budget - max_footer_len - 1`` and ``budget``. max_footer_len for
    # 2 pairs = len("…(+2 pairs truncated)") = 21. Previous limit for
    # budget=500 was 500-21-1=478. Craft a join length of exactly 490 —
    # inside budget but outside the old limit.
    p1 = '"k1"="' + "a" * 240 + '"'  # len 248
    p2 = '"k2"="' + "b" * 233 + '"'  # len 241
    # Full join: 248 + 1 (space) + 241 = 490. Fits in budget=500,
    # exceeds old limit=478. Pre-fix behaviour would have dropped p2.
    result = _join_pairs_bounded([p1, p2], _FIELD_VALUE_MAX_LEN)
    assert result == p1 + " " + p2, (
        f"eager truncation regressed — pair was dropped even though the "
        f"full join fits in budget. len(result)={len(result)}, "
        f"len(full)={len(p1) + 1 + len(p2)}, budget={_FIELD_VALUE_MAX_LEN}"
    )
    assert "pairs truncated)" not in result
    assert len(result) <= _FIELD_VALUE_MAX_LEN


def test_join_pairs_bounded_respects_budget_when_footer_alone_exceeds_it() -> None:
    """PR #288 PR-gate msg-3345 advisory boundary-completeness fix.

    Edge case unreachable in practice (``_FIELD_VALUE_MAX_LEN`` = 500,
    max footer ~21 chars) but the boundary math should be complete:
    when ``budget`` is exceptionally small (< ``max_footer_len + 1``),
    the plain-text footer alone can exceed budget. Before this fix, the
    helper returned an over-budget footer and relied on the outer
    ``_summarize_value``'s blind slice to enforce the boundary
    retroactively. After the fix, the helper hard-slices the footer as
    a last resort so its return contract ("no longer than budget") is
    always honoured.
    """
    from spirrow_mindwire.adapters._sdk_result import _join_pairs_bounded

    # Pass a tiny budget that's smaller than the footer.
    pairs = ['"tool"="Bash"']
    result = _join_pairs_bounded(pairs, budget=10)
    assert len(result) <= 10, (
        f"tiny-budget edge case broke the contract: len={len(result)}, result={result!r}"
    )


def test_join_pairs_bounded_reserves_room_for_joining_space() -> None:
    """PR #288 PR-gate msg-3336 advisory-off-by-one disposition.

    ``_join_pairs_bounded`` returns ``" ".join(kept) + " " + footer`` when
    it truncates. An earlier version reserved only the worst-case footer
    width in its budget calculation and forgot the single joining space,
    so the return could exceed ``budget`` by exactly 1 character. That
    ugly overshoot would then get blind-sliced by the outer
    :func:`_summarize_value`, chopping the ``ch)`` off the plain-text
    footer. Structurally safe (the footer is plain text with no quote to
    sever) but sloppy, and it defeated the exactness the helper was
    written for.

    Fix: reserve ``max_footer_len + 1`` in the budget. Pin here so a
    future refactor cannot silently regress the boundary math.
    """
    from spirrow_mindwire.adapters._sdk_result import (
        _FIELD_VALUE_MAX_LEN,
        _join_pairs_bounded,
    )

    # Build a set of small pairs and pick a budget that WOULD have hit the
    # boundary exactly with the old math (limit = budget - max_footer_len).
    # 30 pairs @ 15 chars each + 29 joining spaces + 1 footer-join space +
    # footer width. The exact result must be <= _FIELD_VALUE_MAX_LEN.
    pairs = [f'"k{i:02d}"="v{i:02d}"' for i in range(30)]
    result = _join_pairs_bounded(pairs, _FIELD_VALUE_MAX_LEN)
    assert len(result) <= _FIELD_VALUE_MAX_LEN, (
        f"joined length {len(result)} exceeds budget {_FIELD_VALUE_MAX_LEN} — "
        f"the joining-space reservation regressed. result={result!r}"
    )


def test_scalar_denial_with_newline_stays_single_line_via_outer_emission() -> None:
    """PR #288 PR-gate msg-3336 blocking-claim disposition.

    Claim: a scalar denial element containing ``\\n`` (e.g.
    ``permission_denials=["error\\nmsg"]``) would cause the raw newline
    byte to reach the log verbatim and split the marker line.

    Verdict: the claim is factually incorrect. This test pins the actual
    behaviour empirically so a future maintainer reading the code (or a
    future PR-gate pass) can see the reasoning:

    1. The scalar bypass path in ``_project_denial_element`` does return
       the string unchanged (``_scalarize_denial_value`` bounds it but
       does not escape control characters). So ``captured_fields`` DOES
       contain the raw LF byte.

    2. HOWEVER, the marker is emitted via :func:`emit_sdk_error_marker`,
       which calls ``json.dumps(detail, ensure_ascii=False)`` on the
       entire detail dict. Per RFC 8259 §7, every character U+0000 through
       U+001F in a JSON string value MUST be escaped — and the Python
       ``json`` module honours that mandate regardless of ``ensure_ascii``
       (which only affects non-ASCII printables ≥ U+0080). The LF becomes
       the two-character ``\\n`` escape in the emitted marker.

    3. The marker line therefore contains exactly ONE LF byte (the
       trailing terminator written by ``emit_sdk_error_marker`` itself);
       the payload area contains none.

    4. Round-tripping the emitted payload via ``json.loads`` recovers the
       original raw LF, proving structural validity.

    Bohr's msg-3328 §3 explicitly dispositioned scalar quoting as YAGNI
    on exactly this reasoning: "declining on YAGNI grounds ... widens the
    diff without addressing an observed problem". This test pins the
    "no observed problem" claim.
    """
    final = _FakeResultMessage(result=None, permission_denials=["error\nmsg"])
    detail = capture_is_error_detail(final)

    # (1) Captured field contains the raw LF — scalar bypass path.
    assert detail["captured_fields"]["permission_denials"] == ["error\nmsg"]

    # (2)-(3) Marker line is single-line — outer json.dumps escaped the LF.
    stream = io.StringIO()
    emit_sdk_error_marker(detail, stream=stream)
    output = stream.getvalue()
    assert output.count("\n") == 1, f"marker line was split by an unescaped LF: {output!r}"
    assert output.endswith("\n")

    # (4) Round-trip parses cleanly.
    payload = output[len(SDK_ERROR_MARKER_PREFIX) :].rstrip("\n")
    parsed = json.loads(payload)
    assert parsed["captured_fields"]["permission_denials"] == ["error\nmsg"]


def test_scalar_denial_is_bounded_by_summarize_value() -> None:
    """PR #288 PR-gate msg-3336 secondary-claim disposition.

    Secondary claim: the scalar bypass path breaks the ``bounded is
    bounded`` invariant.

    Verdict: also factually incorrect. Scalar strings ARE bounded, just
    on a different path than the dict case — via
    :func:`_scalarize_denial_value` which routes strings through
    :func:`_summarize_value`'s string-truncation branch directly. Pin
    empirically: a 50000-char scalar denial produces a captured value at
    most ``_FIELD_VALUE_MAX_LEN`` plus the ``…(+Nch)`` footer.
    """
    from spirrow_mindwire.adapters._sdk_result import _FIELD_VALUE_MAX_LEN

    huge = "z" * 50_000
    final = _FakeResultMessage(result=None, permission_denials=[huge])
    detail = capture_is_error_detail(final)

    captured = detail["captured_fields"]["permission_denials"][0]
    assert isinstance(captured, str)
    assert len(captured) <= _FIELD_VALUE_MAX_LEN + len("…(+999999ch)")
    # Truncation footer is present so a reader can see length was clipped.
    assert captured.endswith("ch)")


def test_permission_denials_projection_c_independence_never_merged_into_errors() -> None:
    """The (c) constraint from msg-2944 §1 pinned as a hard test.

    Einstein msg-2943 (c) verdict: authorisation-failure and
    domain-invariant-violation surfaces must NEVER be merged into a single
    diagnostic pipeline. In this codebase that means ``permission_denials``
    is emitted as an INDEPENDENT top-level key inside ``captured_fields``,
    and its contents are never merged into ``errors[]``. Pin here so a
    future refactor cannot silently collapse them (msg-3156: "a test that
    fails if a future change collapses them").
    """
    final = _FakeResultMessage(
        result=None,
        permission_denials=[
            {"tool_name": "Write", "rule": "read-only-branch"},
        ],
        errors=["something else entirely"],
    )
    detail = capture_is_error_detail(final)
    fields = detail["captured_fields"]

    # Independent top-level keys, both present, structurally separate.
    assert "permission_denials" in fields
    assert "errors" in fields
    assert fields["permission_denials"] != fields["errors"]

    # The denial content did not leak into errors[].
    errors_summary = fields["errors"]
    assert errors_summary == ["something else entirely"]
    for entry in errors_summary:
        assert "tool_name" not in entry
        assert "read-only-branch" not in entry

    # And symmetrically, the errors[] content did not leak into the denial.
    denials_summary = fields["permission_denials"]
    assert isinstance(denials_summary, list)
    for entry in denials_summary:
        assert "something else entirely" not in entry


def test_permission_denials_projection_empty_list_non_regression() -> None:
    """``permission_denials=[]`` must behave EXACTLY as it did pre-change.

    C-group observation (msg-2771 §4 / msg-2944 §5 non-regression criterion):
    one of the 10 live sessions carried ``permission_denials=[]``. The
    projection must not turn that into ``["+0 more"]`` or any other
    non-empty artefact — an empty list stays an empty list, so
    :func:`_is_empty_reason_value` keeps returning True and the picker
    falls through to the next candidate exactly as before.
    """
    final = _FakeResultMessage(
        result=None,
        permission_denials=[],
        api_error_status=429,  # a real reason on a later field
    )
    detail = capture_is_error_detail(final)

    # Empty stays empty in both raw-adjacent surfaces.
    assert detail["captured_fields"]["permission_denials"] == []
    # The picker skips the empty denial list and lands on the real reason.
    assert detail["reason_source"] == "field:api_error_status"
    assert "429" in detail["message"]


def test_permission_denials_projection_is_bounded_under_pathological_input() -> None:
    """Property test: pathological denials cannot balloon the marker.

    msg-2944 §5 acceptance criterion: "deep nesting / huge strings / many
    keys / circular refs" must not blow past the current per-field length
    cap or crash. The bound this test enforces is the SAME constant as
    every other captured string (``_FIELD_VALUE_MAX_LEN`` = 500 + the
    truncation footer). PR #181 round 3's ``bounded is bounded`` guarantee
    is preserved by the projection, not weakened.
    """
    from spirrow_mindwire.adapters._sdk_result import _FIELD_VALUE_MAX_LEN

    huge_string = "z" * 50_000
    deep_nest: Any = {"level": 0}
    cursor = deep_nest
    for i in range(1, 100):
        cursor["nested"] = {"level": i}
        cursor = cursor["nested"]

    # Circular reference at a value slot.
    cyc: dict[str, Any] = {"self": None, "tool_name": "cyc"}
    cyc["self"] = cyc

    many_keys = {f"key_{i}": f"value_{i}" for i in range(500)}

    denials = [
        {"tool_name": "Bash", "tool_input": huge_string, "rule": "r1"},
        {"tool_name": "Deep", "tool_input": deep_nest, "rule": "r2"},
        {"tool_name": "Cyclic", "tool_input": cyc, "rule": "r3"},
        {"tool_name": "Wide", "tool_input": many_keys, "rule": "r4"},
        # Non-dict, non-scalar element (an arbitrary object).
        object(),
        # Scalar element.
        "raw string denial",
        # None element.
        None,
    ]
    final = _FakeResultMessage(result=None, permission_denials=denials)

    # No exception, no infinite recursion — the pipeline returns cleanly.
    detail = capture_is_error_detail(final)

    captured = detail["captured_fields"]["permission_denials"]
    assert isinstance(captured, list)
    # Each element is a string, and each string is bounded by the same
    # per-field cap as every other captured string.
    max_allowed_len = _FIELD_VALUE_MAX_LEN + len("…(+999999ch)")
    for element in captured:
        assert isinstance(element, str), f"unexpected type in projection: {type(element)!r}"
        assert len(element) <= max_allowed_len, (
            f"projected denial exceeded per-field bound: len={len(element)}"
        )

    # And the picker still recognises the (non-empty) list as a reason.
    assert detail["reason_source"] == "field:permission_denials"


def test_permission_denials_projection_truncates_long_lists_with_overflow_marker() -> None:
    """A denial list longer than ``_SMALL_LIST_ELEM_LIMIT`` truncates with a
    trailing ``"+K more"`` string so the count of dropped entries survives.

    The overflow marker itself is diagnostic: "one denial" vs "twenty
    denials, first eight preserved" is a distinction a reader must be able
    to make from the marker alone.
    """
    from spirrow_mindwire.adapters._sdk_result import _SMALL_LIST_ELEM_LIMIT

    denials = [{"tool_name": f"tool_{i}", "rule": "r"} for i in range(20)]
    final = _FakeResultMessage(result=None, permission_denials=denials)
    detail = capture_is_error_detail(final)

    captured = detail["captured_fields"]["permission_denials"]
    assert isinstance(captured, list)
    # Final list length MUST fit within ``_SMALL_LIST_ELEM_LIMIT`` so the
    # outer scalar-only predicate in ``_summarize_value`` still accepts it.
    # The projection reserves one slot for the overflow marker inside that
    # bound (kept = LIMIT - 1, then + 1 marker = LIMIT total).
    assert len(captured) == _SMALL_LIST_ELEM_LIMIT
    kept = _SMALL_LIST_ELEM_LIMIT - 1
    assert captured[-1] == f"+{20 - kept} more"
    # And the preserved entries still carry their content (both keys and
    # values are json.dumps-quoted per msg-3328 + PR #288 PR-gate follow-up).
    assert '"tool_name"="tool_0"' in captured[0]


def test_permission_denials_projection_scalar_only_list_passes_through_normally() -> None:
    """A denial list that already contains only scalars must not double-project.

    The projection prepends ``str()`` on scalars via ``_scalarize_denial_value``,
    which for a plain string just runs it through :func:`_summarize_value`
    (bounded, but otherwise identity). This test pins that a "reasonable"
    input reaches the marker as-is — the projection is only supposed to
    change the *unreasonable* dict case.
    """
    final = _FakeResultMessage(result=None, permission_denials=["denied: Bash"])
    detail = capture_is_error_detail(final)
    captured = detail["captured_fields"]["permission_denials"]
    assert captured == ["denied: Bash"]
    assert detail["reason_source"] == "field:permission_denials"


# --------------------------------------------------------------------------- #
# emit_sdk_error_marker + find_sdk_error_signal — the transport (S-6)
# --------------------------------------------------------------------------- #


def test_emit_marker_writes_a_single_line_json_with_the_expected_prefix() -> None:
    stream = io.StringIO()
    emit_sdk_error_marker({"reason_source": "result", "message": "x"}, stream=stream)
    output = stream.getvalue()
    assert output.startswith(SDK_ERROR_MARKER_PREFIX)
    payload = output[len(SDK_ERROR_MARKER_PREFIX) :].rstrip("\n")
    parsed = json.loads(payload)
    assert parsed["reason_source"] == "result"
    assert parsed["message"] == "x"
    assert output.endswith("\n")
    # Single line — no embedded newlines.
    assert output.count("\n") == 1


def test_emit_marker_swallows_stream_failures() -> None:
    """A closed stdout must not turn the underlying SDK failure into a different one."""

    class _BrokenStream:
        def write(self, _text: str) -> int:
            raise OSError("closed")

        def flush(self) -> None:
            raise OSError("closed")

    # No exception propagates — the marker is a diagnostic, not a control
    # message, and swallowing it is the correct trade. The Any cast is to
    # satisfy mypy: we deliberately pass a stream that violates the ``TextIO``
    # contract in exactly the way ``emit_sdk_error_marker`` promises to survive.
    from typing import cast

    emit_sdk_error_marker(
        {"reason_source": "result", "message": "x"}, stream=cast(Any, _BrokenStream())
    )


def test_find_sdk_error_signal_walks_the_cause_chain() -> None:
    detail = {"reason_source": "result", "message": "the reason"}
    sig = SdkIsErrorSignal(detail)
    wrapper1 = RuntimeError("layer 1")
    wrapper2 = ValueError("layer 2")
    try:
        try:
            try:
                raise sig
            except SdkIsErrorSignal as e:
                raise wrapper1 from e
        except RuntimeError as e:
            raise wrapper2 from e
    except ValueError as caught:
        found = find_sdk_error_signal(caught)
        assert found is sig
        assert found.detail == detail


def test_find_sdk_error_signal_returns_none_when_absent() -> None:
    assert find_sdk_error_signal(RuntimeError("no signal here")) is None


def test_find_sdk_error_signal_is_cycle_safe() -> None:
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a  # cycle
    # Must not loop forever; must return None (no signal in the chain).
    assert find_sdk_error_signal(a) is None


# --------------------------------------------------------------------------- #
# Signal type preserves ``except RuntimeError`` catch behaviour
# --------------------------------------------------------------------------- #


def test_signal_is_a_runtime_error_subclass() -> None:
    """Pre-change callers wrote ``except RuntimeError``; that must still catch.

    Downgrading the signal to a bare ``Exception`` (or worse, ``BaseException``)
    would silently change what the adapter's outer except catches, and there is
    no compiler check for that in Python. Pinning it here is the check.
    """
    sig = SdkIsErrorSignal({"reason_source": "result", "message": "x"})
    assert isinstance(sig, RuntimeError)
    assert isinstance(sig, Exception)
