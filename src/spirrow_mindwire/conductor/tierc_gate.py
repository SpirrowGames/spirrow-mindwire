"""Tier-C admission gate, enforced (T-decider-conductor-hook DECIDED 2e-1b, msg-5141 / msg-5143).

Until 2e-1b the label gate of T-tier-c-admission-gate (:func:`~spirrow_mindwire.
tier_c_admission_gate.decide_admission`, Einstein APPROVE msg-3711) ran **compute-only** inside the
Decider hook: it classified every ``NEXT: human`` and changed nothing. This module is the acting
half, behind ``[tierc_gate] mode = "enforce"`` (default ``off``):

* a role-authored (proposer / implementer / naysayer) ``NEXT: human`` that ``_route`` stopped at
  the human and that the gate answers ``BOUNCE`` for goes back to its **author**, not the human;
* the gate's log entries (``BOUNCED`` / ``RETRY_ADMIT`` / ``LABEL_MIGRATION`` / ``ADMIT_UNSURE``)
  are appended to ``<data_dir>/state/tier_c_decisions_log.jsonl`` — the existing single-file log;
* the RETRY state is read back from that same log by
  :func:`~spirrow_mindwire.tier_c_decisions_log.build_retry_lookup` — never ``never_retry``,
  which would refuse every ``RETRY:`` and bounce forever (msg-5143).

The three conditions of the monotonicity exception (msg-5141, endorsed msg-5142 / msg-5144):

1. **One bounce per RETRY token.** A ``RETRY: <uuid>`` naming this author's unresolved bounce is
   admitted to the human unconditionally (gate step 0); the ``RETRY_ADMIT`` row then resolves the
   uuid, so it opens the door exactly once. A *new* unlabelled ``NEXT: human`` without a
   ``RETRY:`` line is bounced again — there is no implicit "second time from this author" pass
   (Einstein msg-5142, Bohr msg-5143 T1-T4).
2. **The gate failing opens the door.** Any exception from the gate or the log write is caught by
   the caller and the turn stops at the human exactly as it would with the gate off.
3. **Only role authors.** ``pr-gate-relay``, ``conductor-relay``, operator and the human are never
   gated (:func:`~spirrow_mindwire.decider.hook.is_tierc_entry`, roster roles only).

Producer declaration (OBL-CHATROOM-PRODUCER-READER-SURFACE) for the bounce notice
:func:`render_bounce_body` builds. **Intended reader**: the author whose ``NEXT: human`` was
bounced — the notice names them on its ``NEXT:`` line and the conductor dispatches them on it.
**When the thread is gone**: disposition (3), fail loudly — ``_post_as_conductor_relay`` logs the
:class:`~spirrow_mindwire.magickit.client.ThreadResolvedError` at WARNING and returns an empty
``msg_id``; the conductor then stops at the human on the original head instead of bouncing (the
fail-open direction of condition 2). The ``BOUNCED`` row is already written by then; nobody holds
its uuid, so it can never be redeemed and is harmless.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..tier_c_admission_gate import (
    ADMIT_LABELS,
    AdmissionDecision,
    AdmissionVerdict,
    LogKind,
    decide_admission,
)
from ..tier_c_decisions_log import append_log_entries, build_retry_lookup

TIERC_BOUNCE_HEADER = "Conductor bounce — Tier-C admission gate"
"""First line of every bounce notice. :func:`is_bounce_notice` keys on it so the conductor can tell
a bounce write-back from a guard-(i) redirect write-back (both are ``conductor-relay`` posts)."""


def _new_uuid() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class TierCGate:
    """The enforced admission gate: ``decide_admission`` + the decisions log it reads and writes.

    ``uuid_factory`` mints the bounce token (``uuid4`` in production — one fresh token per bounce,
    msg-5143); tests inject a fixed one so a scripted author can quote it back.
    """

    log_path: Path
    uuid_factory: Callable[[], str] = field(default=_new_uuid)

    def retry_lookup(self, uuid_: str, author: str) -> bool:
        """The live RETRY store — the same lookup :meth:`admit` uses, for the Decider hook's
        compute-only gate to agree with the enforced one (msg-5143: not ``never_retry``)."""
        return build_retry_lookup(self.log_path)(uuid_, author)

    def admit(
        self,
        *,
        body: str,
        author: str,
        thread: str,
        msg_id: str,
        now: datetime,
    ) -> AdmissionDecision:
        """Decide one ``NEXT: human`` and append the decision's log entries (thread + msg_id).

        The rows are written **before** the caller posts any bounce: a bounce whose ``BOUNCED`` row
        failed to land would hand the author a uuid the RETRY lookup has never heard of, and every
        retry would bounce again. Exceptions propagate; the caller fails open (condition 2).
        """
        decision = decide_admission(
            body=body,
            author=author,
            retry_lookup=build_retry_lookup(self.log_path),
            now=now,
            bounce_uuid=self.uuid_factory(),
        )
        append_log_entries(self.log_path, decision.log_entries, thread=thread, msg_id=msg_id)
        return decision


def bounce_retry_uuid(decision: AdmissionDecision) -> str:
    """The ``retry_uuid`` of the decision's ``BOUNCED`` entry. Raises if it is not a bounce."""
    if decision.verdict is not AdmissionVerdict.BOUNCE:
        raise ValueError(f"not a bounce: {decision.rule}")
    for entry in decision.log_entries:
        if entry.kind is LogKind.BOUNCED:
            return str(entry.payload["retry_uuid"])
    raise ValueError(f"bounce without a BOUNCED entry: {decision.rule}")


def is_bounce_notice(content: str) -> bool:
    """Is ``content`` a bounce notice written by :func:`render_bounce_body`?"""
    return content.lstrip().startswith(TIERC_BOUNCE_HEADER)


def render_bounce_body(*, author: str, decision: AdmissionDecision) -> str:
    """The bounce notice posted under ``conductor-relay``, ending ``NEXT: <author>``.

    The final line is the only line-start ``NEXT:``; every other mention (``NEXT: human``,
    ``TIER-C: <label>``) is inline inside backticks so neither the handoff parser (last line-start
    ``NEXT:``) nor the G1 latch (a whole-line ``TIER-C:``) can read it. The ``RETRY:`` line is the
    one line meant to be copied verbatim, so it stands alone; this post's author is not a roster
    role, so the gate never reads it here.
    """
    retry_uuid = bounce_retry_uuid(decision)
    reason = decision.bounce_reason.value if decision.bounce_reason is not None else "unknown"
    labels = " / ".join(sorted(ADMIT_LABELS))
    hint_line = f"ヒント: {decision.bounce_hint}\n\n" if decision.bounce_hint else ""
    return (
        f"{TIERC_BOUNCE_HEADER}\n\n"
        f"{author} の `NEXT: human` は人に届けず、書いた本人に差し戻しました"
        f" (理由: `{reason}`)。\n\n"
        f"{hint_line}"
        f"人に上げてよいのは Tier-C の4種類 ({labels})だけです。次のどれかで続けてください。\n"
        "- 4種類のどれかに当たるなら、`NEXT: human` のすぐ上の行に `TIER-C: <label>` を書いて"
        "出し直してください。\n"
        "- どれにも当たらないなら、人に上げずに自分で決めて進めてください。\n"
        "- それでも人の判断が要ると考えるなら、返信に次の1行をそのまま単独で書いてください。"
        "この差し戻しに対して1回だけ、ラベルに関係なく人へ通します。\n\n"
        f"RETRY: {retry_uuid}\n\n"
        "この post は conductor の書き戻しです (author: `conductor-relay`、role: なし。"
        "T-decider-conductor-hook DECIDED 2e-1b)。\n\n"
        f"NEXT: {author}"
    )


__all__ = [
    "TIERC_BOUNCE_HEADER",
    "TierCGate",
    "bounce_retry_uuid",
    "is_bounce_notice",
    "render_bounce_body",
]
