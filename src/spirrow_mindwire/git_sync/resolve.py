"""Generation search for a thread's branch name (ADR-2026-10-09-23 D8, design v1.2 B-2).

For n = 1, 2, … the candidate is :func:`~.branch.generation_name` ``(stem, n)``. Each candidate is
asked about with ONE exact-match query (``GET /pulls?state=all&head={owner}:{candidate}``; the
``head`` filter takes no prefix, and the Search API's 30/min limit rules it out for a per-turn
path — Einstein msg-6763). A candidate with a merged PR is used up, and the search moves on; an
empty answer, or only open / closed-unmerged PRs, settles the name (D8 renames only on "merged").
Usually one query; two after one merged generation.

Errors are classified per query, by :func:`~spirrow_mindwire.github.classify_http_error`:

- retryable (5xx, timeout / connection, 429, rate-limited 403): up to :data:`RETRIES` more
  attempts, :data:`RETRY_DELAY_S` apart (or ``Retry-After``, capped at
  :data:`RETRY_DELAY_MAX_S`); still failing → :class:`~.errors.GitSyncDeferredError`.
- terminal (401, plain 403, 404, other 4xx) or a body of the wrong shape → park
  ``git.branch_resolve_failed`` with the HTTP status in the summary.

Past :data:`MAX_GENERATION` the search parks (``generation limit exceeded``): a backstop against
corrupt data or a bug, unreachable in normal operation. Nothing is stored; a deferred search
restarts at n = 1 next tick.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ..github.client import (
    GitHubHTTPError,
    PrHeadListingShapeError,
    PrHeadRow,
    Retryability,
    classify_http_error,
)
from .branch import generation_name
from .errors import GitParkCode, GitSyncDeferredError, GitSyncParkError, GitSyncPhase

MAX_GENERATION = 20
RETRIES = 2
RETRY_DELAY_S = 2.0
RETRY_DELAY_MAX_S = 10.0

PrHeadLister = Callable[[str], Awaitable[list[PrHeadRow]]]
"""``candidate branch name -> PRs whose head is exactly that name`` (owner / repo bound)."""

Sleep = Callable[[float], Awaitable[None]]


def _park(thread_id: str, candidate: str, summary: str) -> GitSyncParkError:
    return GitSyncParkError(
        GitParkCode.BRANCH_RESOLVE_FAILED,
        thread_id=thread_id,
        branch=candidate,
        summary=summary,
        phase=GitSyncPhase.PRE_TURN,
    )


async def _query(
    lister: PrHeadLister, *, thread_id: str, candidate: str, sleep: Sleep
) -> list[PrHeadRow]:
    attempt = 0
    while True:
        try:
            return await lister(candidate)
        except PrHeadListingShapeError as exc:
            raise _park(thread_id, candidate, f"response shape mismatch: {exc}") from exc
        except GitHubHTTPError as exc:
            if classify_http_error(exc) is Retryability.TERMINAL:
                raise _park(
                    thread_id, candidate, f"GitHub API HTTP {exc.status_code}: {exc}"
                ) from exc
            if attempt >= RETRIES:
                raise GitSyncDeferredError(
                    thread_id=thread_id,
                    candidate=candidate,
                    detail=(
                        f"GitHub API transient failure after {RETRIES + 1} attempts "
                        f"(HTTP {exc.status_code if exc.status_code is not None else 'n/a'}): "
                        f"{exc}"
                    ),
                ) from exc
            attempt += 1
            delay = exc.retry_after if exc.retry_after is not None else RETRY_DELAY_S
            await sleep(max(0.0, min(delay, RETRY_DELAY_MAX_S)))


async def resolve_generation(
    thread_id: str,
    stem: str,
    lister: PrHeadLister,
    *,
    sleep: Sleep = asyncio.sleep,
) -> str:
    """The first generation name of ``stem`` with no merged PR. See the module docstring."""
    for n in range(1, MAX_GENERATION + 1):
        candidate = generation_name(stem, n)
        rows = await _query(lister, thread_id=thread_id, candidate=candidate, sleep=sleep)
        if not any(row.merged_at is not None for row in rows):
            return candidate
    raise _park(
        thread_id,
        generation_name(stem, 1),
        f"generation limit exceeded (> {MAX_GENERATION} merged generations)",
    )


__all__ = [
    "MAX_GENERATION",
    "RETRIES",
    "RETRY_DELAY_MAX_S",
    "RETRY_DELAY_S",
    "PrHeadLister",
    "resolve_generation",
]
