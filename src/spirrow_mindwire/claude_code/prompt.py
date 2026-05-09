"""Render a thread's state as the XML prompt the watcher injects.

See ``docs/architecture.md`` §6.4. The watcher serializes ``meta.yaml``
+ all ``messages/NNN-from-{cai|cc}.md`` into a single ``<mw_thread>``
element and pushes it to the Claude Agent SDK as the user-turn prompt
("full eager push" — every visible turn is replayed each invocation).

Why XML over JSON / YAML:
- Claude consumes XML natively for nested structured input.
- Prefix ``mw_`` keeps user-authored XML in message bodies from
  colliding with the framing tags.
- Message bodies are XML-escaped (``<`` / ``>`` / ``&``) so arbitrary
  markdown / code can ride through without re-parsing.

Attribute order is fixed for diff stability across watcher runs.
"""

from __future__ import annotations

from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

from spirrow_mindwire.schema import Message, ThreadMeta


def _iso_z(dt: datetime) -> str:
    """Render a UTC datetime as ``YYYY-MM-DDTHH:MM:SSZ`` (architecture.md §3)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_thread_prompt(meta: ThreadMeta, messages: list[Message]) -> str:
    """Render *meta* + *messages* as a ``<mw_thread>`` XML payload.

    The most recent message (highest ``seq``) carries
    ``is_latest="true"`` so the system prompt can point at the message
    Claude is supposed to reply to.

    Raises ``ValueError`` on an empty *messages* list (a thread with no
    messages has nothing to prompt about; the watcher should never emit
    one and we want the bug to surface loudly).
    """

    if not messages:
        raise ValueError("messages must not be empty")

    seqs = [m.seq for m in messages]
    if len(seqs) != len(set(seqs)):
        raise ValueError(
            f"messages contain duplicate seq values: {sorted(seqs)} "
            "(architecture.md §6.4 requires exactly one is_latest message)"
        )

    for m in messages:
        prefix = m.msg_id.split("/", 1)[0]
        if prefix != meta.thread_id:
            raise ValueError(
                f"message {m.msg_id!r} does not belong to thread "
                f"{meta.thread_id!r} (mixing threads in one prompt is "
                "not allowed)"
            )

    sorted_msgs = sorted(messages, key=lambda m: m.seq)
    last_seq = sorted_msgs[-1].seq

    participants_csv = ",".join(meta.participants)
    lines = [
        "<mw_thread"
        f" thread_id={quoteattr(meta.thread_id)}"
        f" status={quoteattr(meta.status)}"
        f" participants={quoteattr(participants_csv)}>"
    ]

    for msg in sorted_msgs:
        is_latest_attr = ' is_latest="true"' if msg.seq == last_seq else ""
        lines.append(
            "  <mw_message"
            f' seq="{msg.seq}"'
            f" from={quoteattr(msg.from_)}"
            f" to={quoteattr(msg.to)}"
            f' created_at="{_iso_z(msg.created_at)}"'
            f"{is_latest_attr}>"
        )
        lines.append(escape(msg.body))
        lines.append("  </mw_message>")

    lines.append("</mw_thread>")
    return "\n".join(lines)


__all__ = ["build_thread_prompt"]
