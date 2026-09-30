"""Small, dependency-free helpers about magickit chatroom threads.

Kept free of I/O and of any import from the conductor or the sweep scripts so that both the
per-tick head-skip CLI and the batch PR-review intake can depend on it without depending on
each other (T-sweep-admission-ignores-thread-status, Einstein advisory 1).
"""

from spirrow_mindwire.chatroom.status import FINISHED_STATUSES, is_terminal_status

__all__ = ["FINISHED_STATUSES", "is_terminal_status"]
