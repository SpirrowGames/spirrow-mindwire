"""Shared on-disk fixture builders for spirrow-mindwire tests.

Four test modules used to carry near-identical ``_seed_thread`` /
``_write_message[_atomically]`` helpers that drifted in small ways
(some seeded an initial message, some didn't; some wrote via the
``.tmp`` → ``rename`` staging dance, some wrote in place; some
accepted frontmatter overrides, some didn't). Centralizing them keeps
the canonical defaults in one place and forces deliberate divergence
when a test really needs different behavior.
"""

from __future__ import annotations

from typing import Any

import yaml

from spirrow_mindwire._seq import zero_padded_seq
from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.schema import Participant

DEFAULT_TS = "2026-05-07T08:43:07Z"
"""Canonical UTC timestamp used in fixture frontmatter / meta defaults.

Tests that need a different time should pass ``created_at=...`` /
``updated_at=...`` overrides, not redefine the constant.
"""


def seed_thread_meta(layout: ThreadDirLayout, **overrides: Any) -> None:
    """Write ``meta.yaml`` for *layout* using the canonical Phase 0 defaults.

    Defaults: ``status='awaiting-cc'``, two participants, empty title /
    tags, both timestamps at :data:`DEFAULT_TS`. ``overrides`` shallow-
    merge into the payload before serialization, so tests can change
    one field (``status='active'``) without restating the rest.
    """

    payload: dict[str, Any] = {
        "schema_version": 1,
        "thread_id": layout.thread_id,
        "title": "",
        "status": "awaiting-cc",
        "participants": ["claude.ai", "claude-code"],
        "created_at": DEFAULT_TS,
        "updated_at": DEFAULT_TS,
        "tags": [],
    }
    payload.update(overrides)
    layout.thread_dir.mkdir(parents=True, exist_ok=True)
    layout.meta_path.write_text(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def write_message_file(
    layout: ThreadDirLayout,
    seq: int,
    sender: Participant,
    body: str = "hello",
    *,
    atomic: bool = True,
    **frontmatter_overrides: Any,
) -> None:
    """Write a fully-formed message file (frontmatter + body) at *seq*.

    ``atomic=True`` (default) goes through the ``.tmp`` → ``os.replace``
    staging dance, so the watcher observer fires exactly one event for
    the move — appropriate for tests that exercise the observer.
    ``atomic=False`` writes in place, appropriate for pre-seeding state
    outside any observer's life cycle (dispatcher unit tests, loader
    fixtures).

    ``sender`` is typed as :data:`Participant` so the ``to`` derivation
    below is exhaustive under mypy and the helper cannot accidentally
    generate filenames for unknown participants.
    """

    layout.messages_dir.mkdir(parents=True, exist_ok=True)
    fm: dict[str, Any] = {
        "schema_version": 1,
        "msg_id": f"{layout.thread_id}/{zero_padded_seq(seq)}",
        "seq": seq,
        "from": sender,
        "to": "claude-code" if sender == "claude.ai" else "claude.ai",
        "created_at": DEFAULT_TS,
    }
    fm.update(frontmatter_overrides)
    yaml_block = yaml.safe_dump(fm, default_flow_style=False, sort_keys=False)
    content = f"---\n{yaml_block}---\n\n{body}\n"
    target = layout.message_path(seq, sender)
    if atomic:
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
    else:
        target.write_text(content, encoding="utf-8")


__all__ = ["DEFAULT_TS", "seed_thread_meta", "write_message_file"]
