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

import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..tier_c_admission_gate import (
    ADMIT_LABELS,
    SECURITY_LABEL,
    AdmissionDecision,
    AdmissionVerdict,
    BounceReason,
    LogEntry,
    LogKind,
    check_security_basis,
    decide_admission,
    normalized_body_sha256,
)
from ..tier_c_decisions_log import append_log_entries, build_retry_lookup
from ..tier_c_security_holds import SecurityHold, open_holds

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
        released_holds: Iterable[str] = (),
    ) -> AdmissionDecision:
        """Decide one ``NEXT: human`` and append the decision's log entries (thread + msg_id).

        The security holds of ``(thread, author)`` are read back from the same log
        (:func:`~spirrow_mindwire.tier_c_security_holds.open_holds`); ``released_holds`` are the
        hold ids a human released with ``HOLD-RELEASE:`` in this thread.

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
            security_hold=bool(
                self.holds(thread=thread, author=author, released_holds=released_holds)
            ),
        )
        append_log_entries(self.log_path, decision.log_entries, thread=thread, msg_id=msg_id)
        return decision

    def holds(
        self, *, thread: str, author: str, released_holds: Iterable[str] = ()
    ) -> list[SecurityHold]:
        """The open security holds of ``(thread, author)``, read back from the log."""
        return open_holds(self.log_path, thread=thread, author=author, released=released_holds)

    def escalated(
        self,
        *,
        author: str,
        decision: AdmissionDecision,
        thread: str,
        msg_id: str,
        now: datetime,
        released_holds: Iterable[str] = (),
    ) -> None:
        """Record that the head was delivered to the human (``ESCALATED``, msg-9650 §4).

        Written only once nothing after the gate bounced it. ``released_holds`` are the holds this
        delivery releases (msg-9661 §1.1⁵ 3.: after delivery, the supplemented ones only).
        """
        retry_uuid = None
        for logged in decision.log_entries:
            if logged.kind is LogKind.RETRY_ADMIT:
                retry_uuid = logged.payload.get("retry_uuid")
        entry = LogEntry(
            kind=LogKind.ESCALATED,
            payload={
                "ts": now.isoformat(),
                "author": author,
                "label": decision.normalized_label,
                "rule": decision.rule,
                "retry_uuid": retry_uuid,
                "released_holds": sorted(released_holds),
            },
        )
        append_log_entries(self.log_path, [entry], thread=thread, msg_id=msg_id)

    def hold_bounce(
        self,
        *,
        author: str,
        label: str | None,
        reason: BounceReason,
        hint: str,
        thread: str,
        msg_id: str,
        now: datetime,
        body: str,
        history: list[dict[str, object]],
        open_new: bool,
    ) -> AdmissionDecision:
        """Record the bounce of an ``RH-recheck`` head that may not pass its holds (msg-9661
        §1.1⁵, msg-9663 §1.1⁶) and return it as a bounce decision for the notice.

        ``history`` appends to the holds this head touched (§1.1⁶ (a)); ``open_new`` opens a new
        semantic hold on this message (§1.1⁶ (b), ``unheld_unbased = yes``). Nothing is released.
        """
        retry_uuid = self.uuid_factory()
        payload: dict[str, object] = {
            "ts": now.isoformat(),
            "author": author,
            "reason": reason.value,
            "label": label,
            "hint": hint,
            "retry_uuid": retry_uuid,
            "hold_history": history,
        }
        if open_new:
            payload |= {
                "security_hold": True,
                "hold_kind": "semantic",
                "held_basis": check_security_basis(body).basis,
                "body_sha256": normalized_body_sha256(body),
            }
        entry = LogEntry(kind=LogKind.BOUNCED, payload=payload)
        append_log_entries(self.log_path, [entry], thread=thread, msg_id=msg_id)
        return AdmissionDecision(
            verdict=AdmissionVerdict.BOUNCE,
            rule="RH-relation",
            bounce_reason=reason,
            bounce_hint=hint,
            normalized_label=label,
            log_entries=[entry],
        )

    def jev_bounce(
        self,
        *,
        author: str,
        label: str | None,
        ask_score: float,
        thread: str,
        msg_id: str,
        now: datetime,
        body: str | None = None,
    ) -> str:
        """Record a Jev bounce of a label-admitted ``NEXT: human`` and return its RETRY uuid.

        The row is an ordinary ``BOUNCED`` entry (reason ``jev-likely-not``), so
        :func:`build_retry_lookup` redeems a ``RETRY: <uuid>`` for it exactly as for a label
        bounce: one retry always reaches the human. As in :meth:`admit`, the row is written before
        the caller posts the notice; exceptions propagate and the caller fails open.

        A ``security`` head Jev judged ``LIKELY_NOT`` also opens a *semantic* security hold,
        whatever its basis kind (msg-9655 §1.1''), so its RETRY is re-checked, not force-admitted.
        ``body`` supplies the basis line and the dedupe hash.
        """
        retry_uuid = self.uuid_factory()
        payload: dict[str, object] = {
            "ts": now.isoformat(),
            "author": author,
            "reason": BounceReason.JEV_LIKELY_NOT.value,
            "label": label,
            "retry_uuid": retry_uuid,
            "ask_score": ask_score,
        }
        if label == SECURITY_LABEL and body is not None:
            payload |= {
                "security_hold": True,
                "hold_kind": "semantic",
                "held_basis": check_security_basis(body).basis,
                "body_sha256": normalized_body_sha256(body),
            }
        entry = LogEntry(kind=LogKind.BOUNCED, payload=payload)
        append_log_entries(self.log_path, [entry], thread=thread, msg_id=msg_id)
        return retry_uuid


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


_BOUNCED_LINE_RE: re.Pattern[str] = re.compile(r"^bounced: `(?P<msg_id>[^`\s]+)`\r?$", re.MULTILINE)


def bounced_msg_id(content: str) -> str | None:
    """The msg_id a bounce notice names on its ``bounced:`` line, else ``None``.

    The notice names the message it bounced instead of relying on being posted right after it:
    another post can land between the ``NEXT: human`` and the notice (PR-gate advisory on #398),
    so adjacency is not a reliable link. ``None`` for anything that is not a bounce notice.
    """
    if not is_bounce_notice(content):
        return None
    match = _BOUNCED_LINE_RE.search(content)
    return match.group("msg_id") if match is not None else None


def render_bounce_body(*, author: str, decision: AdmissionDecision, bounced_msg_id: str) -> str:
    """The bounce notice posted under ``conductor-relay``, ending ``NEXT: <author>``.

    The final line is the only line-start ``NEXT:``; every other mention (``NEXT: human``,
    ``TIER-C: <label>``) is inline inside backticks so neither the handoff parser (last line-start
    ``NEXT:``) nor the G1 latch (a whole-line ``TIER-C:``) can read it. The ``RETRY:`` line is the
    one line meant to be copied verbatim, so it stands alone; this post's author is not a roster
    role, so the gate never reads it here. ``bounced_msg_id`` is written on its own
    ``bounced: `<msg_id>``` line for :func:`bounced_msg_id` to read back.
    """
    retry_uuid = bounce_retry_uuid(decision)
    reason = decision.bounce_reason.value if decision.bounce_reason is not None else "unknown"
    labels = " / ".join(sorted(ADMIT_LABELS))
    hint_line = f"ヒント: {decision.bounce_hint}\n\n" if decision.bounce_hint else ""
    return (
        f"{TIERC_BOUNCE_HEADER}\n\n"
        f"bounced: `{bounced_msg_id}`\n\n"
        f"{author} の `NEXT: human` は人に届けず、書いた本人に差し戻しました"
        f" (理由: `{reason}`)。\n\n"
        f"{hint_line}"
        f"人に上げてよいのは Tier-C の{len(ADMIT_LABELS)}種類 ({labels})だけです"
        "(人にしかできない作業は `HUMAN-TASK:` の形で頼みます)。次のどれかで続けてください。\n"
        "- どれかの種類に当たるなら、`NEXT: human` のすぐ上の行に `TIER-C: <label>` を書いて"
        "出し直してください。\n"
        "- どれにも当たらないなら、人に上げずに自分で決めて進めてください。\n"
        f"{_retry_bullet(decision.bounce_reason)}\n\n"
        f"RETRY: {retry_uuid}\n\n"
        "この post は conductor の書き戻しです (author: `conductor-relay`、role: なし。"
        "T-decider-conductor-hook DECIDED 2e-1b)。\n\n"
        f"NEXT: {author}"
    )


#: Bounces a RETRY does not force through: a security request without its basis, or one that
#: meets an open security hold (msg-9650 §1.1, msg-9653 §1.1'). The notice must not promise
#: otherwise.
_HOLDING_REASONS: frozenset[BounceReason] = frozenset(
    {
        BounceReason.SECURITY_BASIS_MISSING,
        BounceReason.SECURITY_BASIS_MALFORMED,
        BounceReason.SECURITY_HOLD,
        BounceReason.SECURITY_HOLD_NO_VERDICT,
    }
)


def _retry_bullet(reason: BounceReason | None) -> str:
    if reason in _HOLDING_REASONS:
        return (
            "- セキュリティの申請は、根拠 (場所、または何をどこまで広げるか) を補わない限り、"
            "下の `RETRY:` 行を付けても人へは通りません。根拠を補って出し直すか、"
            "人に上げずに `DECIDED:` / `DEFERRED:` で残してください。"
        )
    return (
        "- それでも人の判断が要ると考えるなら、返信に次の1行をそのまま単独で書いてください。"
        "この差し戻しに対して1回だけ、ラベルに関係なく人へ通します。"
    )


def render_jev_bounce_body(
    *, author: str, bounced_msg_id: str, retry_uuid: str, label: str | None, ask_score: float
) -> str:
    """The notice for a Jev bounce: same header, ``bounced:`` line and ``RETRY:`` line as
    :func:`render_bounce_body`, so :func:`is_bounce_notice` / :func:`bounced_msg_id` read it and
    the conductor treats it like a label bounce. Ends ``NEXT: <author>``; every other ``NEXT:`` /
    ``TIER-C:`` mention is inline inside backticks."""
    shown_label = f"`TIER-C: {label}`" if label else "ラベル"
    return (
        f"{TIERC_BOUNCE_HEADER}\n\n"
        f"bounced: `{bounced_msg_id}`\n\n"
        f"{author} の `NEXT: human` は {shown_label} 付きでしたが、Tier-C 判定 (Jev) が"
        f"「人に聞く必要は低い」と判定したため、人に届けず書いた本人に差し戻しました"
        f" (理由: `{BounceReason.JEV_LIKELY_NOT.value}`、should_ask_human={ask_score:.2f})。\n\n"
        "次のどれかで続けてください。\n"
        "- 人に聞かずに決められるなら、自分で決めて進めてください。"
        "決めた内容は `DECIDED:` 行で残してください。\n"
        "- それでも人の判断が要ると考えるなら、返信に次の1行をそのまま単独で書いてください。"
        "この差し戻しに対して1回だけ、判定に関係なく人へ通します。\n\n"
        f"RETRY: {retry_uuid}\n\n"
        "この post は conductor の書き戻しです (author: `conductor-relay`、role: なし。"
        '`[decider.tierc] mode = "bounce"`)。\n\n'
        f"NEXT: {author}"
    )


__all__ = [
    "TIERC_BOUNCE_HEADER",
    "TierCGate",
    "bounce_retry_uuid",
    "bounced_msg_id",
    "is_bounce_notice",
    "render_bounce_body",
    "render_jev_bounce_body",
]
