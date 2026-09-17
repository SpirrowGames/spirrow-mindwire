"""Classifier tests (D-2', msg-2470 §4).

Each of the five classes has a positive test AND at least one exclusion test
(showing the classifier picks the intended class when a broader class could have
absorbed the row). The order-matters property from ``classifier.classify`` is
pinned explicitly.
"""

from __future__ import annotations

from spirrow_mindwire.stall_ledger import Class, classify
from spirrow_mindwire.stall_ledger.classifier import ClassifierInput


class TestClassifyPositives:
    def test_quarantined_is_terminal(self) -> None:
        c = classify(
            ClassifierInput(
                is_quarantined=True,
                is_externally_blocked=True,
                verdict_is_indefinite=True,
                ci_became_definitive=True,
                has_next_participant=True,
                external_prereqs_hold=True,
            )
        )
        assert c is Class.QUARANTINED

    def test_externally_blocked_beats_refireable(self) -> None:
        """M-3: PR #206 with DIRTY merge state must not be classified as
        ``refireable`` — a re-fire against a DIRTY PR accomplishes nothing
        (msg-2354 §1). The classifier must name the actual blocker."""
        c = classify(
            ClassifierInput(
                is_externally_blocked=True,
                verdict_is_indefinite=True,
                ci_became_definitive=True,
            )
        )
        assert c is Class.EXTERNALLY_BLOCKED

    def test_refireable_when_verdict_indefinite_and_ci_definitive(self) -> None:
        c = classify(ClassifierInput(verdict_is_indefinite=True, ci_became_definitive=True))
        assert c is Class.REFIREABLE

    def test_refireable_beats_resumable(self) -> None:
        """CI-definitive refire is closer to unblocking than a nomination-based
        wake, so it wins the tie (see classifier docstring on ordering)."""
        c = classify(
            ClassifierInput(
                verdict_is_indefinite=True,
                ci_became_definitive=True,
                has_next_participant=True,
                external_prereqs_hold=True,
            )
        )
        assert c is Class.REFIREABLE

    def test_resumable_when_nomination_and_prereqs_hold(self) -> None:
        c = classify(ClassifierInput(has_next_participant=True, external_prereqs_hold=True))
        assert c is Class.RESUMABLE

    def test_unclassified_when_no_arm_fires(self) -> None:
        c = classify(ClassifierInput())
        assert c is Class.UNCLASSIFIED

    def test_verdict_indefinite_alone_is_unclassified(self) -> None:
        """§4: ``refireable`` requires BOTH the indefinite verdict AND CI having
        become definitive. Without the transition, the ledger has no signal that
        a refire could unblock the row."""
        c = classify(ClassifierInput(verdict_is_indefinite=True))
        assert c is Class.UNCLASSIFIED

    def test_next_participant_alone_is_unclassified(self) -> None:
        """§4 ``resumable`` also requires external prereqs to hold. A nomination
        with unmet prereqs is still stuck, but not ``resumable``."""
        c = classify(ClassifierInput(has_next_participant=True))
        assert c is Class.UNCLASSIFIED
