"""Read a thread's persisted state (meta.yaml + messages) from disk.

architecture.md §3 fixes the layout. The loader is read-only: it
parses YAML / Markdown and returns validated pydantic models. Atomic-
write guarantees from ``filesystem.atomic_write_text`` mean we never
observe a partially-written file (the watcher patterns ignore
``*.tmp`` anyway).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.schema import Message, ThreadMeta

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_MESSAGE_FILENAME_RE = re.compile(r"^(\d{3,})-from-(cai|cc)\.md$")


def load_thread_meta(layout: ThreadDirLayout) -> ThreadMeta:
    """Parse and validate ``meta.yaml`` for the given thread."""
    raw = layout.meta_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    return ThreadMeta.model_validate(data)


def load_messages(layout: ThreadDirLayout) -> list[Message]:
    """Load every ``NNN-from-{cai|cc}.md`` under the thread, sorted by seq.

    Files that don't match the canonical filename pattern are skipped
    silently — the watcher's pattern filter is supposed to keep them
    out, but we belt-and-suspender here so a stray file in the dir
    doesn't crash the loader.
    """
    if not layout.messages_dir.is_dir():
        return []
    msgs: list[Message] = []
    for path in sorted(layout.messages_dir.iterdir()):
        if not path.is_file():
            continue
        if not _MESSAGE_FILENAME_RE.match(path.name):
            continue
        msgs.append(_parse_message_file(path))
    msgs.sort(key=lambda m: m.seq)
    return msgs


def _parse_message_file(path: Path) -> Message:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise ValueError(f"missing YAML frontmatter in {path}")
    fm_yaml, body = match.groups()
    fm = yaml.safe_load(fm_yaml) or {}
    fm["body"] = body.lstrip("\n").rstrip()
    return Message.model_validate(fm)


__all__ = ["load_messages", "load_thread_meta"]
