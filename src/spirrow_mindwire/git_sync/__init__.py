"""git-sync hook around a dispatched turn (ADR-2026-10-09-23 unit 1, D7 / D8; design v1.2).

This first slice carries the pure naming layer only: the mechanical branch name and the
push-target guard (:mod:`.branch`), the generation search over merged PRs (:mod:`.resolve`) and
the park / defer outcomes (:mod:`.errors`). The hook itself and its wiring into the dispatcher
come in the next slice (PR #473); until then nothing imports this package at runtime.
"""

from __future__ import annotations

from .branch import ProtectedBranchError, ThreadBranch, generation_name, normalize_stem
from .errors import (
    EVENT_KIND_GIT_SYNC_PARK,
    GIT_SYNC_DEFERRED_EXIT_CODE,
    GIT_SYNC_DEFERRED_PAYLOAD_PREFIX,
    GitParkCode,
    GitSyncDeferredError,
    GitSyncParkError,
    GitSyncPhase,
    emit_git_sync_deferred_payload,
)
from .resolve import MAX_GENERATION, resolve_generation

__all__ = [
    "EVENT_KIND_GIT_SYNC_PARK",
    "GIT_SYNC_DEFERRED_EXIT_CODE",
    "GIT_SYNC_DEFERRED_PAYLOAD_PREFIX",
    "MAX_GENERATION",
    "GitParkCode",
    "GitSyncDeferredError",
    "GitSyncParkError",
    "GitSyncPhase",
    "ProtectedBranchError",
    "ThreadBranch",
    "emit_git_sync_deferred_payload",
    "generation_name",
    "normalize_stem",
    "resolve_generation",
]
