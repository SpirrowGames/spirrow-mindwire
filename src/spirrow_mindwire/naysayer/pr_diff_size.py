"""``mindwire pr-diff-size`` — measure a PR's diff exactly as the naysayer PR gate does.

T-fix-now-vs-followup-is-mechanical (DECIDED msg-5233, design Bohr msg-5241 §4). The rule the
loop follows: a review finding is fixed in the current PR (fix-now) unless, measured AFTER the
fix is pushed, the PR's gate-read diff exceeds the gate's warn threshold — then the fix moves to
a follow-up PR (split). The question is mechanical, so it is never handed to the human.

Fidelity choices, each made on purpose:

- **Same artifact as the gate.** The diff is GitHub's three-dot ``compare`` text from
  :meth:`GitHubClient.fetch_compare_diff` — the function the gate's ``fetch_pr_diff`` itself
  calls — and the size is ``len()`` of it, which is the gate's ``DiffView.original_chars``.
  No local ``git diff`` mode: it is a different diff engine and not byte-identical (msg-5241
  dropped it).
- **Explicit head.** Only ``base.ref`` comes from the PR metadata. The head is the SHA the
  caller names (default: local ``git rev-parse HEAD``), because right after a push the PR
  metadata's ``head.sha`` can lag and would measure the pre-fix diff (msg-5239 §1). A SHA is
  immutable, so no cache staleness can enter.
- **Constants are imported, never copied** — :data:`pr_review._DIFF_WARN_THRESHOLD` and
  :data:`pr_review._MAX_DIFF_CHARS` — so a change to the gate's limit keeps this rule's meaning.
- **Boundary is ``>``** (DECIDED wording: "超える"). Exactly at the threshold is fix-now even
  though the gate's ``DiffView.in_headroom`` (``>=``) already warns there; the mismatch is
  intended (msg-5234 §4).
- **Fail-loud.** A fetch failure (unpushed SHA → compare 404, auth, network) exits non-zero
  WITHOUT printing a ``decision=`` line, so a script cannot read a failure as fix-now.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal, Protocol

from ..github.client import GitHubHTTPError, PrRef
from . import pr_review

Decision = Literal["fix-now", "split"]

EXIT_FIX_NOW = 0
EXIT_SPLIT = 3
EXIT_ERROR = 1


class _DiffSource(Protocol):
    async def fetch_pr_base_and_head(self, pr: PrRef) -> tuple[str, str]: ...

    async def fetch_compare_diff(
        self, owner: str, repo: str, base_ref: str, head_sha: str
    ) -> str: ...


@dataclass(frozen=True)
class DiffSize:
    """One measurement: the gate's numbers for the PR at ``head_sha``."""

    base_ref: str
    head_sha: str
    original_chars: int
    warn_threshold: int
    limit: int

    @property
    def decision(self) -> Decision:
        return decide(self.original_chars, warn_threshold=self.warn_threshold)

    def render(self) -> str:
        return (
            f"base={self.base_ref} head={self.head_sha} "
            f"original_chars={self.original_chars} warn_threshold={self.warn_threshold} "
            f"limit={self.limit} decision={self.decision}"
        )


def decide(original_chars: int, *, warn_threshold: int | None = None) -> Decision:
    """``split`` iff the measured diff strictly exceeds the gate's warn threshold."""
    threshold = pr_review._DIFF_WARN_THRESHOLD if warn_threshold is None else warn_threshold
    return "split" if original_chars > threshold else "fix-now"


async def measure(client: _DiffSource, pr: PrRef, head_sha: str) -> DiffSize:
    """Measure ``pr``'s gate diff at ``head_sha``. Raises :class:`GitHubHTTPError` on failure."""
    base_ref, _stale_head = await client.fetch_pr_base_and_head(pr)
    diff = await client.fetch_compare_diff(pr.owner, pr.repo, base_ref, head_sha)
    return DiffSize(
        base_ref=base_ref,
        head_sha=head_sha,
        original_chars=len(diff),
        warn_threshold=pr_review._DIFF_WARN_THRESHOLD,
        limit=pr_review._MAX_DIFF_CHARS,
    )


def local_head_sha() -> str:
    """``git rev-parse HEAD`` of the current directory; raises on failure (no empty default)."""
    proc = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    if proc.returncode != 0:
        # Carry git's own stderr (e.g. "fatal: not a git repository") so the
        # caller sees why it failed, not only the exit status.
        detail = (proc.stderr or "").strip() or f"exit status {proc.returncode}"
        raise RuntimeError(f"git rev-parse HEAD failed: {detail}")
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError("git rev-parse HEAD returned nothing")
    return out


def parse_repo(value: str) -> tuple[str, str]:
    owner, sep, repo = value.partition("/")
    if not sep or not owner or not repo or "/" in repo:
        raise ValueError(f"--repo must be owner/repo (got {value!r})")
    return owner, repo


def run(repo: str, number: int, head: str | None, *, client: _DiffSource | None = None) -> int:
    """CLI body. Prints one result line on stdout and returns the exit code."""
    try:
        owner, name = parse_repo(repo)
        head_sha = head if head else local_head_sha()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"pr-diff-size: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    pr = PrRef(owner=owner, repo=name, number=number)
    try:
        if client is not None:
            size = asyncio.run(measure(client, pr, head_sha))
        else:
            size = asyncio.run(_measure_with_default_client(pr, head_sha))
    except GitHubHTTPError as exc:
        # No `decision=` on this path: an unmeasured diff must not read as fix-now.
        print(
            f"pr-diff-size: error: could not fetch the gate diff for {pr.slug} at "
            f"{head_sha} (is the commit pushed?): {exc}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(size.render())
    return EXIT_SPLIT if size.decision == "split" else EXIT_FIX_NOW


async def _measure_with_default_client(pr: PrRef, head_sha: str) -> DiffSize:
    from ..github.client import GitHubClient

    async with GitHubClient() as client:
        return await measure(client, pr, head_sha)
