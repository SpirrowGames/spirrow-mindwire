"""Park / defer outcomes of the git-sync hook (ADR-2026-10-09-23 unit 1, design v1.2 msg-6766).

Two ways the hook can stop a turn, and they are deliberately different:

- **park** (:class:`GitSyncParkError`) — a fact about this thread's branch or about the clone
  this thread's turn left behind, which re-running will not change: the remote diverged, the
  turn left uncommitted work, a push was refused, the GitHub API refused the request (401 /
  non-rate-limit 403 / 404 / a body it cannot read). The conductor posts a stop notice into the
  thread (``conductor-relay``, ``NEXT: human``) whose first line is
  ``git.<code>: <thread_id> <branch> <summary>`` (B-6), so the stop is never silent
  (T-silent-stops-need-a-generic-watchdog-and-a-loud-stand-down).
- **defer** (:class:`GitSyncDeferredError`) — the GitHub API was transiently unavailable (5xx,
  timeout, connection error, rate limit) and stayed so through the retries. Not this thread's
  fault and not the clone's: nothing is posted, nobody is quarantined or parked, and the next
  tick re-derives everything from n = 1 (the generation search keeps no state). ``loop_runner.main``
  exits :data:`GIT_SYNC_DEFERRED_EXIT_CODE`; the sweep wrapper reverts the launch (it does not
  count toward backoff) and notifies on the same channel the dirty-clone exit uses.
"""

from __future__ import annotations

import json
import sys
from enum import StrEnum

# The next free daemon exit code: 1 crash, 2 environment terminal, 3 stand-down, 4 stall, 5/6/7
# wall-clock budget (conductor/run_budget.py), 8 dirty clone (clone_guard.py). The wrapper
# mirrors it as $GitSyncDeferredExitCode (a pytest pins the two).
GIT_SYNC_DEFERRED_EXIT_CODE = 9

# stdout sentinel for the one JSON row ``loop_runner.main`` prints before exiting 9. As with the
# dirty-clone row, the wrapper reads it only to enrich the notification; the
# do-not-quarantine decision rides on the exit code alone.
GIT_SYNC_DEFERRED_PAYLOAD_PREFIX = "MINDWIRE_GIT_SYNC_DEFERRED_PAYLOAD "

EVENT_KIND_GIT_SYNC_PARK = "git_sync.park"


class GitParkCode(StrEnum):
    """The ``<code>`` of ``git.<code>`` (design v1.2 B-6). Closed vocabulary."""

    FETCH_FAILED = "fetch_failed"
    BRANCH_RESOLVE_FAILED = "branch_resolve_failed"
    LOCAL_AHEAD = "local_ahead"
    DIVERGED = "diverged"
    # A ``git switch`` / ``merge --ff-only`` that the preceding checks said would succeed and did
    # not (a git fault, not a branch state). Not named in design v1.2; added so such a failure
    # parks under its own name instead of borrowing another code's meaning.
    CHECKOUT_FAILED = "checkout_failed"
    LFS_FAILED = "lfs_failed"
    DIRTY_AFTER_TURN = "dirty_after_turn"
    HEAD_MOVED = "head_moved"
    PUSH_FAILED = "push_failed"


class GitSyncPhase(StrEnum):
    PRE_TURN = "pre_turn"
    POST_TURN = "post_turn"


class GitSyncParkError(Exception):
    """The hook parked the thread. Carries what the conductor's park notice prints."""

    def __init__(
        self,
        code: GitParkCode,
        *,
        thread_id: str,
        branch: str | None,
        summary: str,
        phase: GitSyncPhase,
    ) -> None:
        self.code = code
        self.thread_id = thread_id
        self.branch = branch
        self.summary = summary
        self.phase = phase
        super().__init__(self.reason_line)

    @property
    def reason_line(self) -> str:
        """``git.<code>: <thread_id> <branch> <要約>`` — design v1.2 B-6, verbatim shape."""
        return f"git.{self.code.value}: {self.thread_id} {self.branch or '-'} {self.summary}"


class GitSyncDeferredError(Exception):
    """A transient GitHub API failure outlasted the retries; this turn is skipped, not parked."""

    def __init__(self, *, thread_id: str, candidate: str, detail: str) -> None:
        self.thread_id = thread_id
        self.candidate = candidate
        self.detail = detail
        super().__init__(f"git sync deferred: {thread_id} {candidate} {detail}")

    def payload(self) -> dict[str, object]:
        return {"thread_id": self.thread_id, "candidate": self.candidate, "detail": self.detail}


def emit_git_sync_deferred_payload(exc: GitSyncDeferredError) -> None:
    """Print the one ``MINDWIRE_GIT_SYNC_DEFERRED_PAYLOAD <json>`` row the sweep wrapper parses."""
    sys.stdout.write(f"{GIT_SYNC_DEFERRED_PAYLOAD_PREFIX}{json.dumps(exc.payload())}\n")
    sys.stdout.flush()


__all__ = [
    "EVENT_KIND_GIT_SYNC_PARK",
    "GIT_SYNC_DEFERRED_EXIT_CODE",
    "GIT_SYNC_DEFERRED_PAYLOAD_PREFIX",
    "GitParkCode",
    "GitSyncDeferredError",
    "GitSyncParkError",
    "GitSyncPhase",
    "emit_git_sync_deferred_payload",
]
