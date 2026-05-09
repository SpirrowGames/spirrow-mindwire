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

Scope of this layer:
- This is a state-pure transform: ``(meta, messages)`` → ``str``. No
  I/O, no clock reads, no decisions about *which* messages to include.
- The watcher chooses the message snapshot to render; this builder
  treats whatever it's given as the source of truth.

Format conventions:
- Attribute order is fixed for diff stability and prompt-cache friendliness.
- Timestamps use second precision with the ``Z`` suffix (architecture.md
  §3); sub-second precision is intentionally dropped.
- Thread-level timestamps (``meta.created_at`` / ``updated_at``) are
  intentionally **not** rendered — the per-message ``created_at`` series
  carries the same information at the granularity Claude actually needs.
- ``meta.participants`` is rendered as a comma-separated attribute. The
  ``Participant`` literal type guarantees the values cannot contain
  commas; a future schema widening would need to revisit this format.
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

    Of the messages handed in, the one with the highest ``seq`` is
    tagged ``is_latest="true"``. The semantics are:

        "the message Claude should reply to" == "highest seq in this
        snapshot"

    Choosing *which* message Claude should reply to is the watcher's
    job — by deciding which messages to include in *messages*. This
    builder is state-pure and does not look outside the input.

    Raises ``ValueError`` on three malformed inputs (fail-loudly
    philosophy, same as the empty-list check):
    - empty *messages* list
    - duplicate ``seq`` values (would emit multiple ``is_latest="true"``)
    - any ``msg_id`` whose thread_id prefix doesn't match
      ``meta.thread_id`` (mixing threads in one prompt is not allowed)
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
