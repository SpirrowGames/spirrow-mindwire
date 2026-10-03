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
order), msg-4196 DECIDED 1 (``gate_result is None`` → no call). The call order, verbatim in
effect:

0. ``gate_result is None`` → **no call**; :meth:`DeciderLexoraAdapter.evaluate` returns ``None``
   (msg-4196 DECIDED 1, design §3.5 item 5). ``None`` means "the admission gate did not run",
   not "outside the grey zone": a call would bill Jev for an answer with no scope label.
1. otherwise ``/v1/decide`` is **always** called, whatever the gate verdict.
2. transport failure / timeout / non-200 / unusable envelope → ``TRANSPORT_ERROR``.
3. ``provider == "null"`` → ``NO_VERDICT_NULL`` (never synthesised:
   three 0.5s sum to 1.5 and read CONFIRMED).
4. any of the 6 answers missing / wrong type / out of [0, 1] → ``NO_VERDICT_MALFORMED``.
5. only now branch on the gate: grey zone → ``evaluate_tierc`` (IN_GATE); any other gate
   result → ``build_out_of_gate_verdict`` (OUT_OF_GATE). Both are ``EVALUATED`` with a
   ``decision_id``. (msg-4186's "``None`` → OUT_OF_GATE" fallback is withdrawn by msg-4196.)

Once a gate result exists, the gate decides how answers are combined, never whether Lexora is
called.

**tierc-v2 (T-decider-tierc-v2-all-escalations; Bohr msg-4380 / 4382 / 4384, Einstein-approved).**
When the adapter is built with a rules file (``[decider.tierc].questions = "tierc-v2"``, the
default) the order above changes in two places, and only there:

* step 0 is gone: a ``gate_result is None`` turn is sent too (msg-4380 Δ2, overriding msg-4196
  DECIDED 1 — its reason, "no scope label", went away with D18) and goes out as
  ``gate_result: null``;
* step 5 has no gate branch (D18 grey-zone gating is withdrawn, msg-4360): every turn is
  ``evaluate_tierc_v2(should_ask_human)`` with scope ``IN_GATE``. ``gate_kind`` /
  ``gate_is_grey_zone`` stay as feature + log columns.

Step 4 checks ``should_ask_human`` only. ``matched_rule`` (a ``choice`` question) is validated
separately; a bad one never makes the record MALFORMED — it leaves ``matched_rule=None`` plus
``matched_rule_error`` (display-only, msg-4380 Δ3). The v1 path is kept unchanged for the v1
replay comparison.

Enablement (msg-4180 §4): backend ``lexora`` — from env ``MINDWIRE_DECIDER_BACKEND`` when set,
else ``[decider].backend`` — **and** ``MINDWIRE_LEXORA_URL`` set. :func:`build_decider` returns
``None`` when the Decider is off, and refuses (``ValueError``) a half-configured enablement rather
than silently pointing at the loopback default.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from ..decider.questions import (
    MATCHED_RULE_KEY,
    SHOULD_ASK_HUMAN_KEY,
    TIERC_QUESTIONS_VERSION,
    TIERC_V2_PROCEED_QUESTIONS_VERSION,
    TIERC_V2_QUESTIONS_VERSION,
    TierCRules,
    load_tierc_rules,
)
from ..decider.result import DecisionOutcome, DecisionResult
from ..decider.state import DecisionState
from ..decider.verdict import (
    TIER_C_GENUINE_KEYS,
    TIER_C_SPURIOUS_KEYS,
    TierCThresholds,
    TierCV2Thresholds,
    build_out_of_gate_verdict,
    evaluate_tierc,
    evaluate_tierc_v2,
)
from ..decider.wire import POLICY_LIVE_PROCEED, POLICY_LIVE_TIERC, build_decide_request
from ..lexora.client import LexoraClient, LexoraError

logger = logging.getLogger(__name__)

DECIDER_TIMEOUT_SECONDS = 5.0
"""Client timeout for ``/v1/decide`` (msg-4180 §2-4): Lexora's own upstream timeout is 2000 ms,
plus headroom so Lexora's fallback-to-Null answer arrives before we cut the connection."""

HUMAN_NEXT = "human"
_BACKEND_ENV = "MINDWIRE_DECIDER_BACKEND"
_URL_ENV = "MINDWIRE_LEXORA_URL"
_ALL_TIERC_KEYS: tuple[str, ...] = TIER_C_GENUINE_KEYS + TIER_C_SPURIOUS_KEYS

MATCHED_RULE_SOURCE_CHOICE = "choice"
"""``matched_rule_source`` of a ``choice`` answer. Lexora ``/v1/decide`` accepts
``type: "choice"`` (spirrow-lexora main 344d467, ``decide/contract.py``), so this is msg-4382's
path (a) and the only one built; the noul-argmax fallback (b) is not needed."""

QuestionSet = Literal["tierc-v1", "tierc-v2"]


class DecideClient(Protocol):
    """The one Lexora method the Decider drives (satisfied by :class:`LexoraClient`)."""

    async def decide(self, body: dict[str, Any]) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


def _extract_noul(
    answers: Any, keys: tuple[str, ...] = _ALL_TIERC_KEYS
) -> tuple[dict[str, float] | None, str | None]:
    """``{"k": {"noul": p}}`` → ``{"k": p}`` for every key in ``keys`` (default: the 6 v1 keys),
    or ``(None, reason)``.

    No partial synthesis (§2-3): one bad answer and the whole record is MALFORMED.
    """
    if not isinstance(answers, Mapping):
        return None, f"answers is {type(answers).__name__}, expected an object"
    scores: dict[str, float] = {}
    for key in keys:
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


def _extract_matched_rule(answers: Any, rules: TierCRules) -> tuple[str | None, str | None]:
    """``{"matched_rule": {"choice": "rule_k", ...}}`` → ``("rule_k", None)``, or
    ``(None, reason)``. The choice must be one of the options that were sent."""
    entry = answers.get(MATCHED_RULE_KEY) if isinstance(answers, Mapping) else None
    if not isinstance(entry, Mapping) or "choice" not in entry:
        return None, f"answer {MATCHED_RULE_KEY!r} missing or has no 'choice'"
    choice = entry["choice"]
    if not isinstance(choice, str) or choice not in rules.matched_rule_options:
        return None, (
            f"answer {MATCHED_RULE_KEY!r} choice={choice!r} is not one of "
            f"{list(rules.matched_rule_options)}"
        )
    return choice, None


async def decide_once(
    state: DecisionState,
    *,
    client: DecideClient,
    policy: str,
    thresholds: TierCThresholds | None = None,
    rules: TierCRules | None = None,
    v2_thresholds: TierCV2Thresholds | None = None,
    proceed: bool = False,
) -> DecisionResult:
    """One ``/v1/decide`` round-trip → :class:`DecisionResult`, following msg-4188's order.

    Shared by the live adapter and ``scripts/decider_replay.py --endpoint`` so both classify a
    response identically. Never raises for a Lexora-side problem.

    ``rules=None`` is the v1 set, and raises ``ValueError`` (before any HTTP) when
    ``state.gate_result is None`` (msg-4196 DECIDED 1). ``rules`` given is tierc-v2: every state
    is sent, gate result or not (msg-4380 Δ2); ``thresholds`` is then unused and
    ``v2_thresholds`` applies.

    ``proceed=True`` (D-4' G3) sends the ``tierc-v2-proceed`` set — the v2 keys asked of a
    naysayer's proceed handoff — and is classified exactly like tierc-v2. It requires ``rules``
    (``ValueError`` before any HTTP otherwise).
    """
    if proceed and rules is None:
        raise ValueError("decide_once(proceed=True) requires the tierc-v2 rules")
    if rules is None and state.gate_result is None:
        raise ValueError("decide_once requires a gate_result (msg-4196 DECIDED 1)")
    body = build_decide_request(state, policy=policy, rules=rules, proceed=proceed)
    v2_version = TIERC_V2_PROCEED_QUESTIONS_VERSION if proceed else TIERC_V2_QUESTIONS_VERSION
    version_fields: dict[str, Any] = (
        {"questions_version": TIERC_QUESTIONS_VERSION}
        if rules is None
        else {
            "questions_version": v2_version,
            "rules_sha256": rules.sha256,
            # No answer was obtained on these paths, so ``matched_rule`` is ``None`` and so is its
            # source (Bohr msg-4629 §2): ``"choice"`` is only ever paired with a value.
        }
    )

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
            **version_fields,
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
            **version_fields,
        )

    # 3. NullProvider: record, never synthesise (its choice answer is options[0] — meaningless,
    #    so matched_rule stays None too).
    if provider == "null":
        return _no_verdict(DecisionOutcome.NO_VERDICT_NULL, None)

    if rules is not None:
        return _v2_result(
            answers=answers,
            rules=rules,
            thresholds=v2_thresholds,
            decision_id=str(decision_id),
            provider=provider,
            raw_answers=raw_answers,
            policy=policy,
            latency_ms=latency_ms,
            no_verdict=_no_verdict,
            questions_version=v2_version,
        )

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

    # 5. only now branch on the gate (v1 only; non-None was checked on entry).
    gate = state.gate_result
    assert gate is not None
    if gate.is_grey_zone:
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


def _v2_result(
    *,
    answers: Any,
    rules: TierCRules,
    thresholds: TierCV2Thresholds | None,
    decision_id: str,
    provider: str,
    raw_answers: Mapping[str, Any] | None,
    policy: str,
    latency_ms: int | None,
    no_verdict: Callable[[DecisionOutcome, str | None], DecisionResult],
    questions_version: str = TIERC_V2_QUESTIONS_VERSION,
) -> DecisionResult:
    """tierc-v2 steps 4-5: ``should_ask_human`` alone decides MALFORMED and the verdict;
    ``matched_rule`` is validated on its own and never changes the outcome (msg-4380 Δ3)."""
    scores, reason = _extract_noul(answers, (SHOULD_ASK_HUMAN_KEY,))
    if scores is None:
        logger.warning(
            "decider /v1/decide malformed answers (decision_id=%s provider=%s): %s",
            decision_id,
            provider,
            reason,
        )
        return no_verdict(DecisionOutcome.NO_VERDICT_MALFORMED, reason)
    matched_rule, rule_error = _extract_matched_rule(answers, rules)
    if rule_error is not None:
        logger.warning(
            "decider /v1/decide matched_rule unusable (decision_id=%s provider=%s): %s",
            decision_id,
            provider,
            rule_error,
        )
    return DecisionResult(
        outcome=DecisionOutcome.EVALUATED,
        decision_id=decision_id,
        provider=provider,
        raw_answers=raw_answers,
        verdict=evaluate_tierc_v2(scores[SHOULD_ASK_HUMAN_KEY], thresholds),
        policy=policy,
        questions_version=questions_version,
        latency_ms=latency_ms,
        matched_rule=matched_rule,
        # ``None`` with ``None`` (msg-4629 §2): a broken ``choice`` answer has no source either —
        # ``matched_rule_error`` says why — so a reader never needs ``outcome`` to tell them apart.
        matched_rule_source=MATCHED_RULE_SOURCE_CHOICE if matched_rule is not None else None,
        matched_rule_error=rule_error,
        rules_sha256=rules.sha256,
    )


def _default_client_factory(url: str) -> Callable[[], DecideClient]:
    def factory() -> DecideClient:
        return LexoraClient(url, timeout_seconds=DECIDER_TIMEOUT_SECONDS)

    return factory


class DeciderLexoraAdapter:
    """The live Decider: gates on ``parsed_next`` / mode, then :func:`decide_once`.

    A fresh client per call (closed in ``finally``): the hook fires only on ``NEXT: human``
    turns, so pooling buys nothing and a per-call client leaves no teardown for the Conductor.
    ``rules`` set = tierc-v2 (the rules file read once at build time); ``None`` = tierc-v1.
    """

    def __init__(
        self,
        *,
        tierc_mode: str,
        client_factory: Callable[[], DecideClient],
        thresholds: TierCThresholds | None = None,
        policy: str = POLICY_LIVE_TIERC,
        rules: TierCRules | None = None,
        v2_thresholds: TierCV2Thresholds | None = None,
    ) -> None:
        self._tierc_mode = tierc_mode
        self._client_factory = client_factory
        self._thresholds = thresholds
        self._policy = policy
        self._rules = rules
        self._v2_thresholds = v2_thresholds

    @property
    def tierc_mode(self) -> str:
        return self._tierc_mode

    @property
    def rules(self) -> TierCRules | None:
        return self._rules

    def is_target(self, state: DecisionState) -> bool:
        """Whether this Decider evaluates ``state`` at all, gate aside (PR-gate advisory on #345).

        The single owner of the "is this turn a Tier-C target" rule: mode not ``off`` and head
        ``NEXT: human``. :meth:`evaluate` and the Conductor hook both ask this method rather than
        each restating the condition.
        """
        return self._tierc_mode != "off" and state.parsed_next == HUMAN_NEXT

    async def evaluate(self, state: DecisionState) -> DecisionResult | None:
        """``None`` iff the Decider was not called (msg-4182); otherwise always a result.

        Not called when :meth:`is_target` is false. Under v1 (no rules) also not called when the
        admission gate did not run (``gate_result is None`` — msg-4196 DECIDED 1); under v2 a
        targeted turn is always sent (msg-4380 Δ2).
        """
        if not self.is_target(state):
            return None
        if self._rules is None and state.gate_result is None:
            return None
        client = self._client_factory()
        try:
            return await decide_once(
                state,
                client=client,
                policy=self._policy,
                thresholds=self._thresholds,
                rules=self._rules,
                v2_thresholds=self._v2_thresholds,
            )
        finally:
            try:
                await client.aclose()
            except Exception:
                logger.warning("decider client close failed", exc_info=True)

    async def clear_proceed(self, state: DecisionState) -> DecisionResult | None:
        """D-4' G3: evaluate a naysayer's proceed handoff (``NEXT: <implementer>``).

        ``None`` iff the Decider was not called: mode ``off``, or no tierc-v2 rules (v1 has no
        proceed variant). G3 is a veto (msg-5219): the caller treats ``None`` — like every result
        that is not an actionable tierc-v2 ``CONFIRMED`` — as "no veto", and G1 / G2 decide.
        Unlike :meth:`evaluate` this does NOT require ``parsed_next == human``: the proceed turn
        is exactly the one that isn't.
        """
        if self._tierc_mode == "off" or self._rules is None:
            return None
        client = self._client_factory()
        try:
            return await decide_once(
                state,
                client=client,
                policy=POLICY_LIVE_PROCEED,
                rules=self._rules,
                v2_thresholds=self._v2_thresholds,
                proceed=True,
            )
        finally:
            try:
                await client.aclose()
            except Exception:
                logger.warning("decider client close failed", exc_info=True)


def _corrupt_evidence_path(dest: Path, now: datetime) -> Path:
    """``<hash>.toml.corrupt-<UTC %Y%m%dT%H%M%SZ>`` (msg-5130 step 2); a ``-<n>`` suffix only if
    a second corruption lands inside the same second, so earlier evidence is never overwritten."""
    base = f"{dest.name}.corrupt-{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    candidate = dest.with_name(base)
    n = 1
    while candidate.exists():
        candidate = dest.with_name(f"{base}-{n}")
        n += 1
    return candidate


def _utc_now() -> datetime:
    return datetime.now(UTC)


def snapshot_tierc_rules(
    rules: TierCRules,
    snapshot_dir: Path,
    *,
    now: Callable[[], datetime] = _utc_now,
) -> bool:
    """Save the bytes the live Decider hashed to ``<snapshot_dir>/<rules_sha256>.toml``.

    Design: T-decider-tierc-v2-all-escalations, Bohr msg-4631 / 4633 / 5130 (Einstein msg-5131
    approved). The canonical rules file lives outside git and is edited in place, so the text
    behind a shadow row's ``rules_sha256`` is only recoverable if it is saved when the hash is
    computed — here, at build time — from :attr:`TierCRules.raw` (never a re-read of the file).

    * A same-named snapshot whose bytes hash to its name → nothing is written (idempotent).
    * Absent → write ``<hash>.toml.tmp`` (fsync), ``os.replace`` it onto ``<hash>.toml``.
    * Present but its bytes do not hash to its name (corrupt / tampered) → write the tmp file,
      move the bad file aside to ``<hash>.toml.corrupt-<UTC>`` (evidence kept), ``os.replace``
      the tmp file in, and log ERROR with the bad file's real sha256. If the move aside fails
      the bad file is left untouched — evidence is never destroyed to make room.
    * The tmp name is fixed per hash and is unlinked in ``finally`` unless the replace
      completed, so repeated failing starts (disk full) never pile up files (msg-5130 Obj. 2).

    **Never raises** (msg-4633 Objection 1 / D20): a snapshot is telemetry for the offline
    report, and failing to write one must not stop the conductor. Every failure is one
    ``logger.error`` line naming the reason, the hash and the path. Returns ``True`` iff
    ``<hash>.toml`` holds ``raw`` afterwards.
    """
    sha = rules.sha256
    dest = snapshot_dir / f"{sha}.toml"
    tmp = snapshot_dir / f"{sha}.toml.tmp"
    try:
        if hashlib.sha256(rules.raw).hexdigest() != sha:
            logger.error(
                "decider tierc rules snapshot skipped: loaded bytes do not hash to "
                "rules_sha256=%s (path=%s)",
                sha,
                dest,
            )
            return False
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        existing: bytes | None
        try:
            existing = dest.read_bytes()
        except FileNotFoundError:
            existing = None
        existing_sha = hashlib.sha256(existing).hexdigest() if existing is not None else None
        if existing_sha == sha:
            return True
        evidence: Path | None = None
        replaced = False
        try:
            with open(tmp, "wb") as fh:
                fh.write(rules.raw)
                fh.flush()
                os.fsync(fh.fileno())
            if existing is not None:
                evidence = _corrupt_evidence_path(dest, now())
                try:
                    os.replace(dest, evidence)
                except OSError as exc:
                    logger.error(
                        "decider tierc rules snapshot is corrupt and could not be moved aside "
                        "(rules_sha256=%s actual_sha256=%s path=%s): %s — left untouched",
                        sha,
                        existing_sha,
                        dest,
                        exc,
                    )
                    return False
            os.replace(tmp, dest)
            replaced = True
        finally:
            if not replaced:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(
                        "decider tierc rules snapshot tmp file %s not removed: %s", tmp, exc
                    )
        if evidence is not None:
            logger.error(
                "decider tierc rules snapshot was corrupt (rules_sha256=%s actual_sha256=%s); "
                "moved aside to %s and restored %s from the loaded bytes",
                sha,
                existing_sha,
                evidence,
                dest,
            )
        else:
            logger.info("decider tierc rules snapshot saved: %s", dest)
        return True
    except Exception as exc:  # telemetry must never stop the conductor (D20, msg-4633)
        logger.error(
            "decider tierc rules snapshot failed (rules_sha256=%s path=%s): %s: %s",
            sha,
            dest,
            type(exc).__name__,
            exc,
        )
        return False


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
    questions: QuestionSet = "tierc-v2",
    rules_path: Path | None = None,
    v2_thresholds: TierCV2Thresholds | None = None,
    snapshot_dir: Path | None = None,
) -> DeciderLexoraAdapter | None:
    """Composition-root factory. ``None`` = Decider off (no HTTP will ever be made).

    Raises ``ValueError`` for configurations that would be silently wrong: an unknown backend,
    ``lexora`` without ``MINDWIRE_LEXORA_URL``, or a Tier-C mode whose acting half is not built
    yet (``annotate`` — accepting it would log as if annotating while annotating nothing).
    ``bounce`` is acted on by the Conductor under ``[tierc_gate] mode = "enforce"``
    (:meth:`~spirrow_mindwire.conductor.core.Conductor._enforce_tierc_gate`).

    ``questions="tierc-v2"`` reads the rules file at ``rules_path`` **once, here** (msg-4384: no
    hot-reload; edit the file, then restart). A missing, unreadable or malformed file raises
    :class:`~spirrow_mindwire.decider.questions.TierCRulesError` (a ``ValueError``) — the same
    refuse-to-start policy as a missing ``MINDWIRE_LEXORA_URL``. The packaged template is never
    read as a fallback (msg-4382: one canonical copy).

    ``snapshot_dir`` given → the loaded bytes are saved to ``<snapshot_dir>/<rules_sha256>.toml``
    right after the read (:func:`snapshot_tierc_rules`, msg-4631 / 4633 / 5130). That step never
    raises: a snapshot failure is an ERROR log line and the Decider is built as usual. Only a
    missing / malformed rules file — the evaluation's own input — refuses startup.
    """
    backend = resolve_backend(config_backend)
    if backend == "off" or tierc_mode == "off":
        return None
    if backend != "lexora":
        raise ValueError(f"unknown decider backend {backend!r} (expected 'off' or 'lexora')")
    if tierc_mode not in ("shadow", "bounce"):
        raise ValueError(
            f"[decider.tierc].mode={tierc_mode!r} is not implemented yet: 'shadow' and "
            "'bounce' are wired ('annotate' lands in a later step)"
        )
    if questions not in ("tierc-v1", "tierc-v2"):
        raise ValueError(f"unknown [decider.tierc].questions {questions!r}")
    if client_factory is None:
        url = os.environ.get(_URL_ENV, "").strip()
        if not url:
            raise ValueError(
                f"decider backend 'lexora' requires {_URL_ENV} to be set (msg-4180 §4)"
            )
        client_factory = _default_client_factory(url)
    rules: TierCRules | None = None
    if questions == "tierc-v2":
        if rules_path is None:
            raise ValueError("[decider.tierc].questions='tierc-v2' requires a rules file path")
        rules = load_tierc_rules(rules_path)
        logger.info(
            "decider tierc-v2 rules loaded from %s (rules_sha256=%s, %d rules)",
            rules.source,
            rules.sha256,
            len(rules.rules),
        )
        if snapshot_dir is not None:
            snapshot_tierc_rules(rules, snapshot_dir)
    return DeciderLexoraAdapter(
        tierc_mode=tierc_mode,
        client_factory=client_factory,
        thresholds=thresholds,
        rules=rules,
        v2_thresholds=v2_thresholds,
    )


__all__ = [
    "DECIDER_TIMEOUT_SECONDS",
    "MATCHED_RULE_SOURCE_CHOICE",
    "DecideClient",
    "DeciderLexoraAdapter",
    "QuestionSet",
    "build_decider",
    "decide_once",
    "resolve_backend",
    "snapshot_tierc_rules",
]
