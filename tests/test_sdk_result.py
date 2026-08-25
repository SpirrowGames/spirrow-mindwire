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
from typing import Any

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
