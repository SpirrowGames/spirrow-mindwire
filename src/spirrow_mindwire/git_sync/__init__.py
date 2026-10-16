"""git-sync hook around a dispatched turn (ADR-2026-10-09-23 unit 1, D7 / D8; design v1.2)."""

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
from .hook import GitSyncHook, PrHeadClient, parse_github_remote
from .resolve import MAX_GENERATION, resolve_generation

__all__ = [
    "EVENT_KIND_GIT_SYNC_PARK",
    "GIT_SYNC_DEFERRED_EXIT_CODE",
    "GIT_SYNC_DEFERRED_PAYLOAD_PREFIX",
    "MAX_GENERATION",
    "GitParkCode",
    "GitSyncDeferredError",
    "GitSyncHook",
    "GitSyncParkError",
    "GitSyncPhase",
    "PrHeadClient",
    "ProtectedBranchError",
    "ThreadBranch",
    "emit_git_sync_deferred_payload",
    "generation_name",
    "normalize_stem",
    "parse_github_remote",
    "resolve_generation",
]
