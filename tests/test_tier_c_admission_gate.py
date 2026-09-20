"""Tests for :mod:`spirrow_mindwire.tier_c_admission_gate`.

The suite is structured to mirror the v8 pseudocode arms in msg-3710 §1
exactly. Every arm has at least one test that names its rule id
(``R0-RETRY:*`` / ``R1-*``), so a regression that mis-routes an arm is
caught with a specific failure rather than a diffuse "wrong verdict"
one. The RETRY prologue's three ``reason`` outcomes each have their own
test, and the RETRY-plus-legacy-label case (msg-3709 BLOCKING → msg-3710
0a fix) has a dedicated regression test that asserts BOTH log entries
in the correct order.

Design tests
------------

Beyond the branch coverage above, two design-level pins live here:

1. ``test_signature_has_no_verdict_parameter`` — the admission gate must
   never grow a way to read the *content* of a downstream verdict.
   The check is structural: a signature-inspection test on
   :func:`decide_admission`. If a future refactor adds a ``verdict``
   parameter (or any parameter whose name suggests one) the test fires.

2. ``test_admit_labels_immutable`` — the four Tier-C intents are the
   whole enum. Adding a fifth without updating msg-3630 §2.1 would
   corrupt the observation this whole design is measuring.

The pseudocode order (msg-3710 §1)
----------------------------------

Step 0 — RETRY binding:
    0a: legacy_auto normalisation
    0b: RETRY_ADMIT with reason ∈ {label_corrected,
        unsure_after_retry, second_time_force_admit}

Step 1 — Main label evaluation:
    1. no label                     → bounce(no-label)
    2. ADMIT_LABELS                  → admit
    3. UNSURE_LABEL                  → admit, ADMIT_UNSURE
    4. other:*                       → bounce(other-not-admitted)
    5. LEGACY_LABEL_MAP              → admit, LABEL_MIGRATION
    6. RELEASE_CROSS_REPO_LABEL      → bounce(release-cross-repo-...)
    7. else                           → bounce(unknown-label)
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from spirrow_mindwire.tier_c_admission_gate import (
    ADMIT_LABELS,
    LEGACY_LABEL_MAP,
    RELEASE_CROSS_REPO_HINT,
    RELEASE_CROSS_REPO_LABEL,
    UNSURE_LABEL,
    AdmissionVerdict,
    BounceReason,
    LogKind,
    RetryAdmitReason,
    decide_admission,
    extract_label,
    extract_retry_uuid,
)

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def _no_retries(_uuid: str, _author: str) -> bool:
    """A retry lookup that never matches — the ordinary main-path fixture."""
    return False


def _always_retry(_uuid: str, _author: str) -> bool:
    """A retry lookup that always matches — for exercising the RETRY prologue."""
    return True


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


class TestExtractLabel:
    """The label parser is the only entry to the decision tree.

    Parser bugs (case sensitivity, leading whitespace, colon variants)
    would cascade into every arm below, so we pin them here first.
    """

    def test_plain_label(self) -> None:
        body = "some body\nTIER-C: goal\nNEXT: human\n"
        assert extract_label(body) == "goal"

    def test_label_is_case_folded(self) -> None:
        body = "some body\nTIER-C: Goal\nNEXT: human\n"
        assert extract_label(body) == "goal"

    def test_tier_c_keyword_is_case_folded(self) -> None:
        body = "some body\ntier-c: goal\nNEXT: human\n"
        assert extract_label(body) == "goal"

    def test_leading_whitespace_tolerated(self) -> None:
        body = "some body\n  TIER-C:   goal  \nNEXT: human\n"
        assert extract_label(body) == "goal"

    def test_unsure_label_recognised_with_question_mark(self) -> None:
        body = "TIER-C: unsure:goal?\nNEXT: human\n"
        assert extract_label(body) == "unsure:goal?"

    def test_other_label_preserves_reason(self) -> None:
        body = "TIER-C: other: some free-form reason here\nNEXT: human\n"
        assert extract_label(body) == "other:some free-form reason here"

    def test_no_label_returns_none(self) -> None:
        assert extract_label("no tier-c label anywhere\nNEXT: human\n") is None


class TestExtractRetryUuid:
    """The RETRY prefix parser is opaque about UUID format on purpose.

    Any non-whitespace token counts. The lookup callable is the layer
    that validates the token against the RETRY store. This test-set
    pins the parser's opacity so a future refactor that starts
    validating UUID shape at parse time fires a regression.
    """

    def test_v4_style_uuid(self) -> None:
        body = "RETRY: 550e8400-e29b-41d4-a716-446655440000\nTIER-C: goal\n"
        assert extract_retry_uuid(body) == "550e8400-e29b-41d4-a716-446655440000"

    def test_short_opaque_token(self) -> None:
        body = "RETRY: abc123\nTIER-C: goal\n"
        assert extract_retry_uuid(body) == "abc123"

    def test_no_retry_prefix_returns_none(self) -> None:
        assert extract_retry_uuid("TIER-C: goal\nNEXT: human\n") is None

    def test_retry_not_on_its_own_line_is_ignored(self) -> None:
        body = "prose that mentions RETRY: not-a-line inline\nTIER-C: goal\n"
        assert extract_retry_uuid(body) is None


# ---------------------------------------------------------------------------
# Step 1 — main label evaluation
# ---------------------------------------------------------------------------


class TestMainLabelEvaluation:
    """One test per arm of the msg-3710 §1 step-1 table."""

    @pytest.mark.parametrize("label", sorted(ADMIT_LABELS))
    def test_admit_label_admits(self, label: str) -> None:
        body = f"TIER-C: {label}\nNEXT: human\n"
        result = decide_admission(body=body, author="alice", retry_lookup=_no_retries, now=NOW)
        assert result.verdict is AdmissionVerdict.ADMIT
        assert result.rule == "R1-admit"
        assert result.normalized_label == label
        assert result.log_entries == []

    def test_no_label_bounces(self) -> None:
        body = "no label anywhere\nNEXT: human\n"
        result = decide_admission(body=body, author="alice", retry_lookup=_no_retries, now=NOW)
        assert result.verdict is AdmissionVerdict.BOUNCE
        assert result.rule == "R1-no-label"
        assert result.bounce_reason is BounceReason.NO_LABEL
        assert len(result.log_entries) == 1
        assert result.log_entries[0].kind is LogKind.BOUNCED

    def test_unsure_label_admits_and_records_admit_unsure(self) -> None:
        body = f"TIER-C: {UNSURE_LABEL}\nNEXT: human\n"
        result = decide_admission(body=body, author="alice", retry_lookup=_no_retries, now=NOW)
        assert result.verdict is AdmissionVerdict.ADMIT
        assert result.rule == "R1-admit-unsure"
        assert result.normalized_label == UNSURE_LABEL
        assert len(result.log_entries) == 1
        assert result.log_entries[0].kind is LogKind.ADMIT_UNSURE

    def test_other_label_bounces(self) -> None:
        body = "TIER-C: other: some novel class\nNEXT: human\n"
        result = decide_admission(body=body, author="alice", retry_lookup=_no_retries, now=NOW)
        assert result.verdict is AdmissionVerdict.BOUNCE
        assert result.rule == "R1-other-not-admitted"
        assert result.bounce_reason is BounceReason.OTHER_NOT_ADMITTED
        entry = result.log_entries[0]
        assert entry.kind is LogKind.BOUNCED
        assert entry.payload["label"] == "other:some novel class"

    @pytest.mark.parametrize(("legacy", "expected"), sorted(LEGACY_LABEL_MAP.items()))
    def test_legacy_label_migrates_and_admits(self, legacy: str, expected: str) -> None:
        body = f"TIER-C: {legacy}\nNEXT: human\n"
        result = decide_admission(body=body, author="alice", retry_lookup=_no_retries, now=NOW)
        assert result.verdict is AdmissionVerdict.ADMIT
        assert result.rule == "R1-legacy-migration"
        assert result.normalized_label == expected
        assert len(result.log_entries) == 1
        entry = result.log_entries[0]
        assert entry.kind is LogKind.LABEL_MIGRATION
        assert entry.payload["from"] == legacy
        assert entry.payload["to"] == expected
        assert entry.payload["via"] == "main"

    def test_release_cross_repo_bounces_with_choice_hint(self) -> None:
        body = f"TIER-C: {RELEASE_CROSS_REPO_LABEL}\nNEXT: human\n"
        result = decide_admission(body=body, author="alice", retry_lookup=_no_retries, now=NOW)
        assert result.verdict is AdmissionVerdict.BOUNCE
        assert result.rule == "R1-release-cross-repo"
        assert result.bounce_reason is BounceReason.RELEASE_CROSS_REPO_NEEDS_AUTHOR_CHOICE
        assert result.bounce_hint == RELEASE_CROSS_REPO_HINT
        entry = result.log_entries[0]
        assert entry.kind is LogKind.BOUNCED
        assert entry.payload["hint"] == RELEASE_CROSS_REPO_HINT

    def test_unknown_label_bounces(self) -> None:
        body = "TIER-C: my-invented-label\nNEXT: human\n"
        result = decide_admission(body=body, author="alice", retry_lookup=_no_retries, now=NOW)
        assert result.verdict is AdmissionVerdict.BOUNCE
        assert result.rule == "R1-unknown-label"
        assert result.bounce_reason is BounceReason.UNKNOWN_LABEL
        entry = result.log_entries[0]
        assert entry.kind is LogKind.BOUNCED
        assert entry.payload["label"] == "my-invented-label"


# ---------------------------------------------------------------------------
# Step 0 — RETRY prologue
# ---------------------------------------------------------------------------


class TestRetryPrologue:
    """The RETRY prologue runs before Step 1 and always admits when the
    UUID matches an unresolved bounce (msg-3648 v1 §2; msg-3710 §1
    steps 0a → 0b).

    The three reason enum values on RETRY_ADMIT are covered by one
    test each, plus one test that pins the legacy-plus-retry
    combination (the msg-3709 BLOCKING that motivated msg-3710 v8).
    """

    def test_retry_with_admit_label_admits_as_label_corrected(self) -> None:
        body = "RETRY: 550e8400\nTIER-C: goal\nNEXT: human\n"
        result = decide_admission(body=body, author="alice", retry_lookup=_always_retry, now=NOW)
        assert result.verdict is AdmissionVerdict.ADMIT
        assert result.rule == "R0-RETRY:label_corrected"
        assert result.normalized_label == "goal"
        assert len(result.log_entries) == 1
        entry = result.log_entries[0]
        assert entry.kind is LogKind.RETRY_ADMIT
        assert entry.payload["reason"] == RetryAdmitReason.LABEL_CORRECTED.value

    def test_retry_with_unsure_label_admits_as_unsure_after_retry(self) -> None:
        body = f"RETRY: xyz\nTIER-C: {UNSURE_LABEL}\nNEXT: human\n"
        result = decide_admission(body=body, author="alice", retry_lookup=_always_retry, now=NOW)
        assert result.verdict is AdmissionVerdict.ADMIT
        assert result.rule == "R0-RETRY:unsure_after_retry"
        assert result.normalized_label == UNSURE_LABEL
        entry = result.log_entries[0]
        assert entry.kind is LogKind.RETRY_ADMIT
        assert entry.payload["reason"] == RetryAdmitReason.UNSURE_AFTER_RETRY.value

    @pytest.mark.parametrize(
        "label_line",
        [
            # An "other:" label that even after retry is not an admission
            # ticket — but the gate lets it through as a second-time force
            # admit to break the infinite loop.
            "TIER-C: other: still-novel-class",
            # A release-cross-repo retry the author refused to disambiguate.
            f"TIER-C: {RELEASE_CROSS_REPO_LABEL}",
            # An unknown label on retry.
            "TIER-C: my-invented-label",
            # No label at all on retry.
            "no label anywhere in the retry body",
        ],
    )
    def test_retry_with_non_admit_label_force_admits(self, label_line: str) -> None:
        body = f"RETRY: aaa\n{label_line}\nNEXT: human\n"
        result = decide_admission(body=body, author="alice", retry_lookup=_always_retry, now=NOW)
        assert result.verdict is AdmissionVerdict.ADMIT
        assert result.rule == "R0-RETRY:second_time_force_admit"
        entry = result.log_entries[-1]
        assert entry.kind is LogKind.RETRY_ADMIT
        assert entry.payload["reason"] == RetryAdmitReason.SECOND_TIME_FORCE_ADMIT.value

    def test_retry_with_legacy_label_emits_migration_then_retry_admit(
        self,
    ) -> None:
        """The msg-3709 BLOCKING regression pin (msg-3710 §1 step 0a fix).

        A RETRY carrying a legacy label must be treated as a
        ``label_corrected`` success — NOT as a second-time force
        admit — because the author retried with what USED to be a
        valid Tier-C label. The gate normalises the label first,
        then decides the RETRY outcome on the normalised value.

        The order of entries matters: LABEL_MIGRATION must come
        before RETRY_ADMIT so the log tells a coherent left-to-right
        story of what the gate did.
        """
        body = "RETRY: 550e8400\nTIER-C: scope\nNEXT: human\n"
        result = decide_admission(body=body, author="alice", retry_lookup=_always_retry, now=NOW)
        assert result.verdict is AdmissionVerdict.ADMIT
        assert result.rule == "R0-RETRY:label_corrected"
        assert result.normalized_label == "goal"
        assert [e.kind for e in result.log_entries] == [
            LogKind.LABEL_MIGRATION,
            LogKind.RETRY_ADMIT,
        ]
        migration, admit = result.log_entries
        assert migration.payload["from"] == "scope"
        assert migration.payload["to"] == "goal"
        assert migration.payload["via"] == "retry"
        assert admit.payload["normalized_label"] == "goal"
        assert admit.payload["reason"] == RetryAdmitReason.LABEL_CORRECTED.value

    def test_retry_prefix_without_matching_bounce_falls_through(self) -> None:
        """A RETRY prefix whose UUID does not match an unresolved bounce
        drops back to Step 1's ordinary label evaluation (msg-3710 §1
        step 0 fallthrough comment)."""
        body = "RETRY: 550e8400\nTIER-C: goal\nNEXT: human\n"
        result = decide_admission(body=body, author="alice", retry_lookup=_no_retries, now=NOW)
        assert result.verdict is AdmissionVerdict.ADMIT
        assert result.rule == "R1-admit"  # not R0-RETRY:*
        assert result.log_entries == []


# ---------------------------------------------------------------------------
# Structural pins
# ---------------------------------------------------------------------------


class TestStructuralInvariants:
    """Invariants pinned in the function shape, not its body."""

    def test_admit_labels_are_the_four_tier_c_intents(self) -> None:
        """msg-3630 §2.1 — the four intents are the entire ADMIT set."""
        assert frozenset({"goal", "cost", "irreversible", "merge-protected"}) == ADMIT_LABELS

    def test_legacy_map_covers_the_two_pre_v1_aliases(self) -> None:
        """msg-3646 D1 — scope→goal and billing→cost, and no others."""
        assert LEGACY_LABEL_MAP == {"scope": "goal", "billing": "cost"}

    def test_signature_has_no_verdict_parameter(self) -> None:
        """The admission gate must not read a downstream ``verdict``.

        Structural pin: if a future refactor adds a parameter whose
        name suggests it carries verdict content the test fires. The
        gate's job is to decide admission from the LABEL and the RETRY
        prefix alone — never from a verdict body the caller has pre-
        digested for it.
        """
        params = inspect.signature(decide_admission).parameters
        assert "verdict" not in params
        assert not any("verdict" in name for name in params)
