"""Decider adapter over Lexora ``POST /v1/decide`` (T-decider-conductor-hook step 2).

Same shape as :mod:`.naysayer_lexora` — stateless HTTP through the shared
:class:`~spirrow_mindwire.lexora.client.LexoraClient`, with the client injected through a
``client_factory`` so the logic is unit-tested against a fake — but the failure policy is the
opposite one. The naysayer is fail-loud because its reply *is* the product. The Decider is an
observer bolted onto a stop decision that already exists, so **nothing it does may change that
decision when it fails** (D20 / monotonicity): every failure becomes a
:class:`~spirrow_mindwire.decider.result.DecisionResult` with a failure outcome and a warning log,
never an exception into the Conductor.

Spec (Bohr, reviewed by Einstein): msg-4180 (wire, Null rule, extraction, 5 s timeout, policy
tags), msg-4182 / 4184 / 4186 (result type, ``actionable_verdict``, invariants), msg-4188 (call
order). The call order, verbatim in effect:

1. ``/v1/decide`` is **always** called — whatever ``gate_result`` is, including ``None``.
2. transport failure / timeout / non-200 / unusable envelope → ``TRANSPORT_ERROR``.
3. ``provider == "null"`` → ``NO_VERDICT_NULL`` (never synthesised:
   three 0.5s sum to 1.5 and read CONFIRMED).
4. any of the 6 answers missing / wrong type / out of [0, 1] → ``NO_VERDICT_MALFORMED``.
5. only now branch on the gate: grey zone → ``evaluate_tierc`` (IN_GATE); anything else,
   ``gate_result is None`` included → ``build_out_of_gate_verdict`` (OUT_OF_GATE). Both are
   ``EVALUATED`` with a ``decision_id``.

The gate decides how answers are combined, never whether Lexora is called.

Enablement (msg-4180 §4): backend ``lexora`` — from env ``MINDWIRE_DECIDER_BACKEND`` when set,
else ``[decider].backend`` — **and** ``MINDWIRE_LEXORA_URL`` set. :func:`build_decider` returns
``None`` when the Decider is off, and refuses (``ValueError``) a half-configured enablement rather
than silently pointing at the loopback default.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from ..decider.result import DecisionOutcome, DecisionResult
from ..decider.state import DecisionState
from ..decider.verdict import (
    TIER_C_GENUINE_KEYS,
    TIER_C_SPURIOUS_KEYS,
    TierCThresholds,
    build_out_of_gate_verdict,
    evaluate_tierc,
)
from ..decider.wire import POLICY_LIVE_TIERC, build_decide_request
from ..lexora.client import LexoraClient, LexoraError

logger = logging.getLogger(__name__)

DECIDER_TIMEOUT_SECONDS = 5.0
"""Client timeout for ``/v1/decide`` (msg-4180 §2-4): Lexora's own upstream timeout is 2000 ms,
plus headroom so Lexora's fallback-to-Null answer arrives before we cut the connection."""

HUMAN_NEXT = "human"
_BACKEND_ENV = "MINDWIRE_DECIDER_BACKEND"
_URL_ENV = "MINDWIRE_LEXORA_URL"
_ALL_TIERC_KEYS: tuple[str, ...] = TIER_C_GENUINE_KEYS + TIER_C_SPURIOUS_KEYS


class DecideClient(Protocol):
    """The one Lexora method the Decider drives (satisfied by :class:`LexoraClient`)."""

    async def decide(self, body: dict[str, Any]) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


def _extract_noul(answers: Any) -> tuple[dict[str, float] | None, str | None]:
    """``{"k": {"noul": p}}`` → ``{"k": p}`` for all 6 Tier-C keys, or ``(None, reason)``.

    No partial synthesis (§2-3): one bad answer and the whole record is MALFORMED.
    """
    if not isinstance(answers, Mapping):
        return None, f"answers is {type(answers).__name__}, expected an object"
    scores: dict[str, float] = {}
    for key in _ALL_TIERC_KEYS:
        entry = answers.get(key)
        if not isinstance(entry, Mapping) or "noul" not in entry:
            return None, f"answer {key!r} missing or has no 'noul'"
        value = entry["noul"]
        # bool is an int subclass — a True/False here is a schema error, not 1.0/0.0.
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None, f"answer {key!r} noul is {type(value).__name__}, expected a number"
        f = float(value)
        if math.isnan(f) or not 0.0 <= f <= 1.0:
            return None, f"answer {key!r} noul={value!r} outside [0, 1]"
        scores[key] = f
    return scores, None


async def decide_once(
    state: DecisionState,
    *,
    client: DecideClient,
    policy: str,
    thresholds: TierCThresholds | None = None,
) -> DecisionResult:
    """One ``/v1/decide`` round-trip → :class:`DecisionResult`, following msg-4188's order.

    Shared by the live adapter and ``scripts/decider_replay.py --endpoint`` so both classify a
    response identically. Never raises for a Lexora-side problem.
    """
    body = build_decide_request(state, policy=policy)

    def _transport_error(reason: str) -> DecisionResult:
        logger.warning("decider /v1/decide transport error (policy=%s): %s", policy, reason)
        return DecisionResult(
            outcome=DecisionOutcome.TRANSPORT_ERROR,
            decision_id=None,
            provider=None,
            raw_answers=None,
            verdict=None,
            policy=policy,
            error=reason,
        )

    # 1-2. always call; any transport-level failure is TRANSPORT_ERROR.
    try:
        payload = await client.decide(body)
    except LexoraError as exc:
        return _transport_error(f"{type(exc).__name__}: {exc}")

    decision_id = payload.get("decision_id")
    provider = payload.get("provider")
    if isinstance(decision_id, bool) or not isinstance(decision_id, str | int) or decision_id == "":
        return _transport_error(f"response has no usable decision_id: {decision_id!r}")
    if not isinstance(provider, str) or not provider:
        return _transport_error(f"response has no usable provider: {provider!r}")
    latency_raw = payload.get("latency_ms")
    latency_ms = (
        int(latency_raw)
        if isinstance(latency_raw, int | float) and not isinstance(latency_raw, bool)
        else None
    )
    answers = payload.get("answers")
    raw_answers = dict(answers) if isinstance(answers, Mapping) else None

    def _no_verdict(outcome: DecisionOutcome, error: str | None) -> DecisionResult:
        return DecisionResult(
            outcome=outcome,
            decision_id=str(decision_id),
            provider=provider,
            raw_answers=raw_answers,
            verdict=None,
            policy=policy,
            latency_ms=latency_ms,
            error=error,
        )

    # 3. NullProvider: record, never synthesise.
    if provider == "null":
        return _no_verdict(DecisionOutcome.NO_VERDICT_NULL, None)

    # 4. malformed answers.
    scores, reason = _extract_noul(answers)
    if scores is None:
        logger.warning(
            "decider /v1/decide malformed answers (decision_id=%s provider=%s): %s",
            decision_id,
            provider,
            reason,
        )
        return _no_verdict(DecisionOutcome.NO_VERDICT_MALFORMED, reason)

    # 5. only now does the gate matter.
    gate = state.gate_result
    if gate is not None and gate.is_grey_zone:
        verdict = evaluate_tierc(scores, thresholds)
    else:
        verdict = build_out_of_gate_verdict(scores)
    return DecisionResult(
        outcome=DecisionOutcome.EVALUATED,
        decision_id=str(decision_id),
        provider=provider,
        raw_answers=raw_answers,
        verdict=verdict,
        policy=policy,
        latency_ms=latency_ms,
    )


def _default_client_factory(url: str) -> Callable[[], DecideClient]:
    def factory() -> DecideClient:
        return LexoraClient(url, timeout_seconds=DECIDER_TIMEOUT_SECONDS)

    return factory


class DeciderLexoraAdapter:
    """The live Decider: gates on ``parsed_next`` / mode, then :func:`decide_once`.

    A fresh client per call (closed in ``finally``): the hook fires only on ``NEXT: human``
    turns, so pooling buys nothing and a per-call client leaves no teardown for the Conductor.
    """

    def __init__(
        self,
        *,
        tierc_mode: str,
        client_factory: Callable[[], DecideClient],
        thresholds: TierCThresholds | None = None,
        policy: str = POLICY_LIVE_TIERC,
    ) -> None:
        self._tierc_mode = tierc_mode
        self._client_factory = client_factory
        self._thresholds = thresholds
        self._policy = policy

    @property
    def tierc_mode(self) -> str:
        return self._tierc_mode

    async def evaluate(self, state: DecisionState) -> DecisionResult | None:
        """``None`` iff the Decider was not called (msg-4182); otherwise always a result."""
        if self._tierc_mode == "off" or state.parsed_next != HUMAN_NEXT:
            return None
        client = self._client_factory()
        try:
            return await decide_once(
                state, client=client, policy=self._policy, thresholds=self._thresholds
            )
        finally:
            try:
                await client.aclose()
            except Exception:
                logger.warning("decider client close failed", exc_info=True)


def resolve_backend(config_backend: str) -> str:
    """``MINDWIRE_DECIDER_BACKEND`` when set (msg-4180 §4 / D4), else ``[decider].backend``."""
    env = os.environ.get(_BACKEND_ENV, "").strip()
    return env or config_backend


def build_decider(
    *,
    config_backend: str,
    tierc_mode: str,
    thresholds: TierCThresholds | None = None,
    client_factory: Callable[[], DecideClient] | None = None,
) -> DeciderLexoraAdapter | None:
    """Composition-root factory. ``None`` = Decider off (no HTTP will ever be made).

    Raises ``ValueError`` for configurations that would be silently wrong: an unknown backend,
    ``lexora`` without ``MINDWIRE_LEXORA_URL``, or a Tier-C mode whose acting half is not built
    yet (``annotate`` / ``bounce`` — step 2 ships shadow only; accepting them would log as if
    annotating while annotating nothing).
    """
    backend = resolve_backend(config_backend)
    if backend == "off" or tierc_mode == "off":
        return None
    if backend != "lexora":
        raise ValueError(f"unknown decider backend {backend!r} (expected 'off' or 'lexora')")
    if tierc_mode != "shadow":
        raise ValueError(
            f"[decider.tierc].mode={tierc_mode!r} is not implemented yet: step 2 wires "
            "'shadow' only (annotate / bounce land in later steps)"
        )
    if client_factory is None:
        url = os.environ.get(_URL_ENV, "").strip()
        if not url:
            raise ValueError(
                f"decider backend 'lexora' requires {_URL_ENV} to be set (msg-4180 §4)"
            )
        client_factory = _default_client_factory(url)
    return DeciderLexoraAdapter(
        tierc_mode=tierc_mode, client_factory=client_factory, thresholds=thresholds
    )


__all__ = [
    "DECIDER_TIMEOUT_SECONDS",
    "DecideClient",
    "DeciderLexoraAdapter",
    "build_decider",
    "decide_once",
    "resolve_backend",
]
