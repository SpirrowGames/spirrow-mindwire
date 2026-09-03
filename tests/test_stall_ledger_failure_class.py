"""Failure-class classifier tests (Deliverable 6).

Each recognised signature has a positive test whose input string is verbatim
from the shape observed in the log (msg-2354 §1 M-2), so a future edit that
narrows a pattern too far fails against the actual live shape.

The tests are also the guard against pattern greed (PR-gate #219 objection 1):
the sdk-error-during-execution signature MUST NOT match when its two anchors
are on different lines. That regression is pinned in
``TestPatternDisciplineNoCrossLineBridging``.
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
        # The two identifiers on the SAME line, exactly as msg-2354 §1 M-2
        # sampled it. If a future SDK version splits them across two lines, a
        # new signature is needed — the current one MUST NOT be widened to
        # match cross-line, that is the regression #219 objection 1 blocked.
        tail = (
            "some earlier line\n"
            "ClaudeCodeSdkDeliveryError: SDK is_error; subtype='error_during_execution'\n"
            "exit=1 / stop_reason 空 / rounds 空 / last_msg 空"
        )
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


class TestPatternDisciplineNoCrossLineBridging:
    """PR-gate #219 objection 1 regression guard.

    Every signature that correlates two identifiers MUST require them on the
    SAME line. A pattern that lets ``.`` cross a newline (either via
    ``re.DOTALL`` or via a wide unbounded ``.*?``) will silently bond an
    earlier stray token to a later unrelated one and misclassify the row into
    the wrong group — the exact opposite of what Deliverable 6 exists to do
    (msg-2470 §4 warns against "全部 1 の列" ⁠— every entry in its own bin;
    cross-line matching produces the mirror-image failure, "every unrelated
    entry crammed into the same bin"). One row per shape, one shape per row.
    """

    def test_sdk_class_and_error_subtype_on_different_lines_do_not_match(self) -> None:
        """``ClaudeCodeSdkDeliveryError`` up-log and ``subtype='error_during_execution'``
        much later on an UNRELATED failure must NOT bond into
        ``sdk-error-during-execution``. The earlier SDK exception should still
        classify as generic (its line stands alone), and the later unrelated
        line is not caught by any signature — so the winner is the *first*
        matching signature (``sdk-is-error-generic``), NOT the specific
        cross-line combination."""
        tail = (
            "2026-08-30T10:00:00Z ClaudeCodeSdkDeliveryError: SDK is_error; "
            "subtype='pending_confirmation'\n"
            "... 40 intervening log lines of a completely unrelated failure ...\n"
            "2026-08-30T10:00:04Z Some other component: subtype='error_during_execution'"
        )
        # Under the tight (single-line) pattern the SDK generic wins because
        # it matches ``ClaudeCodeSdkDeliveryError`` on line 1 in isolation.
        # Under the old greedy pattern this would have bridged into
        # ``sdk-error-during-execution``.
        assert classify_failure(tail) == "sdk-is-error-generic"

    def test_conductor_and_timeout_on_different_lines_do_not_match(self) -> None:
        """Same shape as above, applied to the ``conductor-timeout`` signature.

        The intervening lines carry neither anchor of the correlated pair
        (``conductor`` … ``timeout``) on their own, so under the tight pattern
        the row does not classify. The alternative arm (the fixed token
        ``TimeoutError``) is *not* referenced in this input — that arm is
        checked separately in ``TestKnownShapes.test_conductor_timeout``, and
        including it here would test something else than the cross-line
        discipline this class exists to pin.
        """
        tail = (
            "conductor started tick\n"
            "... 40 lines of unrelated activity ...\n"
            "some component: an ordinary timeout was set for a database call"
        )
        assert classify_failure(tail) == UNKNOWN_LABEL

    def test_preflight_and_attestation_on_different_lines_do_not_match(self) -> None:
        tail = (
            "preflight probe started\n"
            "... unrelated log noise ...\n"
            "attestation was successful for a totally different check"
        )
        assert classify_failure(tail) == UNKNOWN_LABEL

    def test_lease_and_conflict_on_different_lines_do_not_match(self) -> None:
        tail = (
            "lease acquired for owner=self\n"
            "... unrelated log noise ...\n"
            "later: conflict resolution algorithm ran on some other subsystem"
        )
        assert classify_failure(tail) == UNKNOWN_LABEL
