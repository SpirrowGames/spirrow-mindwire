"""Failure-class classifier tests (Deliverable 6).

Each recognised signature has a positive test whose input string is verbatim
from the shape observed in the log (msg-2354 §1 M-2), so a future edit that
narrows a pattern too far fails against the actual live shape.
"""

from __future__ import annotations

from spirrow_mindwire.stall_ledger import classify_failure
from spirrow_mindwire.stall_ledger.failure_class import (
    UNKNOWN_LABEL,
    describe_label,
    known_labels,
)


class TestKnownShapes:
    def test_sdk_error_during_execution_verbatim(self) -> None:
        """msg-2354 §1 M-2 top signature. If this test fails, either the pattern
        was tightened too far or a new SDK version renamed the subtype — either
        way, the ledger's biggest group-by bucket just went silent."""
        tail = [
            "some earlier line",
            "ClaudeCodeSdkDeliveryError: SDK is_error; subtype='error_during_execution'",
            "exit=1 / stop_reason 空 / rounds 空 / last_msg 空",
        ]
        assert classify_failure(tail) == "sdk-error-during-execution"

    def test_sdk_error_with_double_quoted_subtype(self) -> None:
        """Same shape but with double quotes — some log formatters emit either."""
        tail = 'ClaudeCodeSdkDeliveryError: subtype="error_during_execution" ...'
        assert classify_failure(tail) == "sdk-error-during-execution"

    def test_sdk_error_without_recognised_subtype_falls_to_generic(self) -> None:
        tail = "ClaudeCodeSdkDeliveryError: SDK is_error; subtype='pending_confirmation'"
        assert classify_failure(tail) == "sdk-is-error-generic"

    def test_conductor_timeout(self) -> None:
        assert classify_failure("conductor tick exceeded timeout of 300s") == "conductor-timeout"
        assert classify_failure("TimeoutError: read op") == "conductor-timeout"

    def test_preflight_attestation(self) -> None:
        """msg-2354 §1 M-2 named this as the ONE remaining legitimate quarantine
        entry. Naming it separately from the SDK bucket is the whole reason
        Deliverable 6 exists."""
        assert (
            classify_failure("naysayer preflight attestation failed for tier=naysayer")
            == "preflight-attestation-failed"
        )

    def test_lease_conflict(self) -> None:
        assert classify_failure("LeaseHeldError: owner=other-loop") == "lease-conflict"
        assert classify_failure("lease is held by another owner") == "lease-conflict"


class TestUnknownAndEmpty:
    def test_none_input_is_unknown(self) -> None:
        assert classify_failure(None) == UNKNOWN_LABEL

    def test_empty_string_is_unknown(self) -> None:
        assert classify_failure("") == UNKNOWN_LABEL

    def test_whitespace_only_is_unknown(self) -> None:
        assert classify_failure("   \n\n\t") == UNKNOWN_LABEL

    def test_empty_iterable_is_unknown(self) -> None:
        assert classify_failure([]) == UNKNOWN_LABEL

    def test_iterable_with_none_lines_survives(self) -> None:
        """The PowerShell wrapper may hand us an array with $null entries — the
        classifier must tolerate that shape, not crash the sweep."""
        # This test hands a list containing strings only (mypy safety), since the
        # public signature is ``Sequence[str] | Iterable[str] | str | None``.
        tail = ["", "ClaudeCodeSdkDeliveryError: subtype='error_during_execution'"]
        assert classify_failure(tail) == "sdk-error-during-execution"

    def test_unrecognised_error_is_unknown_not_absorbed(self) -> None:
        """The whole reason ``unknown`` exists is so the classifier never
        pretends to recognise a shape it does not. If this test fails, one of
        the pattern arms grew too greedy and is silently stealing rows."""
        assert (
            classify_failure("SomeNovelException: exit=1 / cause=??? / stack ...") == UNKNOWN_LABEL
        )


class TestKnownLabelsShape:
    def test_labels_are_unique(self) -> None:
        labels = known_labels()
        assert len(labels) == len(set(labels))

    def test_unknown_label_is_included(self) -> None:
        assert UNKNOWN_LABEL in known_labels()

    def test_describe_label_round_trip(self) -> None:
        for label in known_labels():
            desc = describe_label(label)
            assert isinstance(desc, str) and desc.strip()

    def test_describe_unknown_says_did_not_match(self) -> None:
        assert "did not match" in describe_label(UNKNOWN_LABEL).lower()


class TestPatternOrderingIsSpecificFirst:
    """The specific ``sdk-error-during-execution`` pattern must WIN over the
    generic ``sdk-is-error-generic`` pattern. This is the primary invariant of
    the ordered signature list — if it breaks, we lose the group-by that
    Deliverable 6 exists for."""

    def test_specific_wins_over_generic(self) -> None:
        tail = "ClaudeCodeSdkDeliveryError: subtype='error_during_execution'"
        assert classify_failure(tail) == "sdk-error-during-execution"
