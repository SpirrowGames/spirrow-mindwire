"""Decider — Conductor 停止判定への判断フック(step 1 + step 2)。

step 2 (Bohr msg-4180/4182/4184/4186/4188) で ``result`` (``DecisionResult``) と
``wire`` (``/v1/decide`` request builder) を追加。 adapter は
``spirrow_mindwire.adapters.decider_lexora``、Conductor 側フックは ``decider.hook``。
以下は step 1 時点の説明:

Fermi msg-4066 の DECIDED に基づく step 1 実装。 `spirrow-lexora/T-decide-jev-provider`
が main に着地するまで adapter と Conductor フック配線は着手しない。ここに入るのは:

* :class:`DecisionState` と ``state_builder``(§3.2 / §3.5、in-memory ``gate_result`` 契約)
* Tier-C 3 + 3 の問いセット v1(``"tierc-v1"``、Track B は別 set として後で追加)
* verdict 合成(genuine=和 / spurious=max)の純関数
* ``scripts/decider_replay.py --track=tierc --dry-run`` の driver 補助

adapter (``adapters/decider_lexora.py``) と Conductor フック (§3.3.a / §3.3.b) は
step 2 で着地する。本 module は adapter を import しない ∴ backend=off の
既定設定下で import 副作用は無い。

設計 SOT: ``docs/decider-conductor-hook-design.md`` v3.4、msg-4066 / msg-4067。
"""

from __future__ import annotations

from spirrow_mindwire.decider.questions import (
    TIERC_QUESTIONS_V1,
    TIERC_QUESTIONS_VERSION,
    TierCQuestion,
    TierCQuestionKind,
)
from spirrow_mindwire.decider.result import (
    DecisionOutcome,
    DecisionResult,
    decision_result_to_dict,
)
from spirrow_mindwire.decider.state import (
    AdmissionGateResult,
    DecisionState,
    DiffStat,
    EventSummary,
    Turn,
    state_builder,
)
from spirrow_mindwire.decider.verdict import (
    TIER_C_GENUINE_KEYS,
    TIER_C_SPURIOUS_KEYS,
    TierCScope,
    TierCVerdict,
    TierCVerdictKind,
    build_out_of_gate_verdict,
    evaluate_tierc,
)
from spirrow_mindwire.decider.wire import (
    POLICY_LIVE_TIERC,
    POLICY_REPLAY_TIERC,
    build_decide_request,
    questions_to_wire,
    state_to_wire,
)

__all__ = [
    "POLICY_LIVE_TIERC",
    "POLICY_REPLAY_TIERC",
    "TIERC_QUESTIONS_V1",
    "TIERC_QUESTIONS_VERSION",
    "TIER_C_GENUINE_KEYS",
    "TIER_C_SPURIOUS_KEYS",
    "AdmissionGateResult",
    "DecisionOutcome",
    "DecisionResult",
    "DecisionState",
    "DiffStat",
    "EventSummary",
    "TierCQuestion",
    "TierCQuestionKind",
    "TierCScope",
    "TierCVerdict",
    "TierCVerdictKind",
    "Turn",
    "build_decide_request",
    "build_out_of_gate_verdict",
    "decision_result_to_dict",
    "evaluate_tierc",
    "questions_to_wire",
    "state_builder",
    "state_to_wire",
]
