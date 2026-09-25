"""``DecisionResult`` — what one Decider call hands back to the Conductor hook.

Spec: Bohr msg-4182 (composite result, after Einstein msg-4181), msg-4184 (``actionable_verdict``
+ invariants, after msg-4183), msg-4186 (scope lives only on ``TierCVerdict.scope``; outcome is
4-valued, after msg-4185), msg-4188 (``/v1/decide`` is always called, so ``decision_id`` is
present on every outcome but ``TRANSPORT_ERROR``).

Why a composite and not ``TierCVerdict | None``: when the provider was Null, or the answers were
malformed, there is no verdict — but Lexora still wrote a decision row, and the ``decision_id`` is
the §6 join key (thread_id + round + decision_id). Returning bare ``None`` would drop it in
transit (msg-4181). So ``None`` from ``Decider.evaluate`` means only "the Decider was not called"
(backend off / tierc mode off / ``parsed_next != "human"``); every call that reached for Lexora
returns a :class:`DecisionResult`, success or failure.

**Who may read what (msg-4184).** Anything that *acts* — stop decisions, annotate, a future
bounce, ``skip_naysayer_when_confirmed`` — reads :attr:`DecisionResult.actionable_verdict` and
nothing else. The raw :attr:`DecisionResult.verdict` field is for ``log_decision`` and the replay
record only (the §6.3 scope-split tally needs the out-of-gate records). A grep-level test pins
that the hook does not read ``.verdict`` directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from spirrow_mindwire.decider.questions import TIERC_QUESTIONS_VERSION
from spirrow_mindwire.decider.verdict import TierCScope, TierCVerdict


class DecisionOutcome(StrEnum):
    """How far one ``/v1/decide`` call got (msg-4186: 4 values, no scope in here)."""

    EVALUATED = "evaluated"
    """A non-null provider answered all 6 questions in range; a verdict was synthesised
    (``evaluate_tierc`` in the grey zone, ``build_out_of_gate_verdict`` outside it)."""

    NO_VERDICT_NULL = "no_verdict_null"
    """``provider == "null"`` — NullProvider's flat 0.5s would sum to 1.5 genuine and read as
    CONFIRMED every time, so no verdict is built (msg-4180 §2-2)."""

    NO_VERDICT_MALFORMED = "no_verdict_malformed"
    """An answer was missing, the wrong type, or outside [0, 1] — no partial synthesis (§2-3)."""

    TRANSPORT_ERROR = "transport_error"
    """Connection failure, timeout, non-200, or an envelope with no usable ``decision_id`` /
    ``provider`` (§2-4). The only outcome allowed to carry ``decision_id=None``."""


@dataclass(frozen=True)
class DecisionResult:
    """One Decider call's result, carried intact to ``log_decision`` (msg-4182)."""

    outcome: DecisionOutcome
    decision_id: str | None
    provider: str | None
    raw_answers: Mapping[str, Any] | None
    verdict: TierCVerdict | None
    policy: str
    questions_version: str = TIERC_QUESTIONS_VERSION
    latency_ms: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        # msg-4186 invariants (msg-4184 §4 as amended): verdict present iff EVALUATED; a missing
        # decision_id only on TRANSPORT_ERROR (msg-4188: Lexora is always called, so every other
        # outcome has one).
        if self.outcome is DecisionOutcome.EVALUATED:
            if self.verdict is None:
                raise ValueError("DecisionResult: outcome=EVALUATED requires a verdict")
        elif self.verdict is not None:
            raise ValueError(
                f"DecisionResult: outcome={self.outcome.value} must not carry a verdict"
            )
        if self.decision_id is None and self.outcome is not DecisionOutcome.TRANSPORT_ERROR:
            raise ValueError(
                f"DecisionResult: decision_id=None is only allowed on TRANSPORT_ERROR "
                f"(got outcome={self.outcome.value})"
            )

    @property
    def actionable_verdict(self) -> TierCVerdict | None:
        """The only verdict anything may act on (msg-4184 §1, as amended by msg-4186).

        ``EVALUATED`` **and** ``scope is IN_GATE``. An out-of-gate record (grey-zone outside,
        or ``gate_result is None``) is EVALUATED too, and returns ``None`` here — it exists to
        be counted, never to change a stop.
        """
        if (
            self.outcome is DecisionOutcome.EVALUATED
            and self.verdict is not None
            and self.verdict.scope is TierCScope.IN_GATE
        ):
            return self.verdict
        return None


def decision_result_to_dict(dr: DecisionResult) -> dict[str, Any]:
    """Flat JSON-ready view shared by ``log_decision`` and the replay record (msg-4182)."""
    v = dr.verdict
    return {
        "outcome": dr.outcome.value,
        "decision_id": dr.decision_id,
        "provider": dr.provider,
        "raw_answers": dict(dr.raw_answers) if dr.raw_answers is not None else None,
        "verdict": (
            None
            if v is None
            else {
                "kind": v.kind.value,
                "scope": v.scope.value,
                "genuine_score": v.genuine_score,
                "spurious_score": v.spurious_score,
                "fired_reason": v.fired_reason,
            }
        ),
        "policy": dr.policy,
        "questions_version": dr.questions_version,
        "latency_ms": dr.latency_ms,
        "error": dr.error,
    }


__all__ = [
    "DecisionOutcome",
    "DecisionResult",
    "decision_result_to_dict",
]
