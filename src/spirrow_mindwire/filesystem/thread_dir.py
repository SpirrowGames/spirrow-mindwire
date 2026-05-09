"""Thread directory layout helpers.

Owns *path computation only* — never reads or writes files. The on-disk
shape is fixed by ``docs/architecture.md`` §3.2 / §3.3:

    threads/<ULID>/meta.yaml
    threads/<ULID>/messages/NNN-from-{cai|cc}.md
    logs/threads/<ULID>.jsonl

Staging support implements the T06 ULID-atomicity decision (Q5): a
thread is built inside ``threads/.staging-<ULID>/`` and ``os.rename``-d
into place once ``meta.yaml`` and the first message are persisted, so
the watcher never observes a half-built thread directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ulid import ULID

from spirrow_mindwire.schema import Participant

_SENDER_SHORT: dict[Participant, str] = {
    "claude.ai": "cai",
    "claude-code": "cc",
}


def _message_filename(seq: int, sender: Participant) -> str:
    if seq < 1:
        raise ValueError(f"seq must be >= 1, got {seq}")
    short = _SENDER_SHORT.get(sender)
    if short is None:
        raise ValueError(f"sender must be one of {sorted(_SENDER_SHORT)}, got {sender!r}")
    width = 3 if seq < 1000 else len(str(seq))
    return f"{seq:0{width}d}-from-{short}.md"


@dataclass(frozen=True)
class ThreadDirLayout:
    """Path computations rooted at ``base_dir`` for a single thread.

    ``thread_id`` is validated as a ULID in ``__post_init__`` so values
    containing path separators (e.g. ``../etc``) cannot escape the data
    root through ``thread_dir`` / ``staging_dir`` / ``event_log_path``.
    """

    base_dir: Path
    thread_id: str

    def __post_init__(self) -> None:
        try:
            ULID.from_str(self.thread_id)
        except ValueError as e:
            raise ValueError(f"thread_id must be a ULID, got {self.thread_id!r}") from e

    @property
    def threads_root(self) -> Path:
        return self.base_dir / "threads"

    @property
    def thread_dir(self) -> Path:
        return self.threads_root / self.thread_id

    @property
    def staging_dir(self) -> Path:
        return self.threads_root / f".staging-{self.thread_id}"

    @property
    def meta_path(self) -> Path:
        return self.thread_dir / "meta.yaml"

    @property
    def messages_dir(self) -> Path:
        return self.thread_dir / "messages"

    @property
    def staging_meta_path(self) -> Path:
        return self.staging_dir / "meta.yaml"

    @property
    def staging_messages_dir(self) -> Path:
        return self.staging_dir / "messages"

    @property
    def event_log_path(self) -> Path:
        return self.base_dir / "logs" / "threads" / f"{self.thread_id}.jsonl"

    def message_path(self, seq: int, sender: Participant) -> Path:
        return self.messages_dir / _message_filename(seq, sender)

    def staging_message_path(self, seq: int, sender: Participant) -> Path:
        return self.staging_messages_dir / _message_filename(seq, sender)


__all__ = ["ThreadDirLayout"]
