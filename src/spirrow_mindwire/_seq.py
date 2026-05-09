"""Sequence-number formatting helper shared across the codebase.

architecture.md §3.2 fixes the on-disk filename / ``msg_id`` rule: the
``seq`` portion is zero-padded to 3 digits, and overflows past 999
expand to ``len(str(seq))`` digits. The same padding has to appear in
multiple places (the schema validator that checks ``msg_id`` against
``seq``, the ``write_reply`` MCP tool that emits new files), so the
helper lives in this neutral top-level module instead of either of
those packages — that keeps the dependency direction one-way and avoids
a schema ↔ claude_code import cycle.
"""

from __future__ import annotations


def zero_padded_seq(seq: int) -> str:
    """Render *seq* with the §3.2 padding rule (3 digits, 4+ on overflow).

    Kept symmetric with ``filesystem.thread_dir._message_filename`` so
    ``msg_id`` and the on-disk filename always agree.
    """

    width = 3 if seq < 1000 else len(str(seq))
    return f"{seq:0{width}d}"


__all__ = ["zero_padded_seq"]
