"""Async GitHub REST client — Stage 3 naysayer PR review (WIRING_ALLOWLIST_SPEC §A.3).

The PR-review naysayer driver
(:class:`~spirrow_mindwire.naysayer.pr_review.NaysayerPrReviewDriver`)
uses this to (1) fetch a PR's unified diff and (2) submit a PR review
(``APPROVE`` / ``REQUEST_CHANGES``) on the develop→main PR. Auth is a
**repo-scoped fine-grained token** (env spec §4: Contents R/W + PR R/W).
The implementer (author) token is resolved from ``MINDWIRE_GITHUB_TOKEN`` /
``GITHUB_TOKEN`` (:func:`github_token`) = ``takahito-spirrowgames``; the review
side (proposer + naysayer, shared) uses a *separate* identity
(``MINDWIRE_NAYSAYER_GITHUB_TOKEN``, :func:`naysayer_github_token`) =
``spirrowgames-ops`` so its review is author≠approver — GitHub rejects
approving your own PR (T22).

Error policy mirrors :mod:`spirrow_mindwire.lexora.client` / ``phanthand``:
clean typed exceptions, **fail-loud** — any non-2xx becomes a
:class:`GitHubHTTPError`, never a silent empty result. The adapter maps an
unreachable GitHub onto a fail-closed halt (ADR-07 §2.6).

:class:`GitHubClient` does real network I/O and is exercised only by the
``-m manual`` smoke test; the adapter logic is unit-tested against the
:class:`GitHubReviewClient` Protocol (httpx ``MockTransport``).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, Protocol

import httpx

_DEFAULT_API_BASE = "https://api.github.com"
_DEFAULT_TIMEOUT_SECONDS = 60.0

_PR_URL_RE = re.compile(r"github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/(\d+)")
# ``owner/repo#n`` in free text. Two details are load-bearing, and both are about the underscore.
#
# The ref ENDS at "not followed by an alphanumeric", not at ``\b``. ``\b`` is defined by ``\w``,
# and ``\w`` counts ``_`` as a word character, so ``acme/widgets#7_`` — a ref with a Markdown
# emphasis close against it — parsed as no ref at all, while ``acme/widgets#7*`` and
# ``acme/widgets#7.`` parsed fine. Every producer feeding this function is an LLM writing Markdown
# (chatroom messages, thread titles, ``NEXT:`` handoff lines), so a trailing ``_`` is punctuation
# here, never part of a ref. ``#7abc`` is still refused: letters and digits still continue the ref.
#
# The OWNER may not contain ``_``. A GitHub login is alphanumerics and hyphens, so an underscore
# there was never a real owner name — but the class accepted one, which meant an italicised ref
# ``_acme/widgets#7`` silently parsed with owner ``_acme`` and would have fired the gate at a
# repository that does not exist. It now does not match at all, which the callers already treat as
# "no ref" and fail safe on. (``repo`` keeps ``_``: repository names really do contain it.)
_PR_SHORT_RE = re.compile(r"\b([A-Za-z0-9.-]+)/([A-Za-z0-9._-]+)#(\d+)(?![^\W_])")

logger = logging.getLogger(__name__)


def github_token() -> str | None:
    """Resolve the implementer (author) GitHub token from env.

    ``MINDWIRE_GITHUB_TOKEN`` first, then ``GITHUB_TOKEN``. This is the
    implementer (author) identity (``takahito-spirrowgames`` = Heisenberg); it
    deliberately does **not** read the naysayer var, so the author and the
    review-side identities stay separate (T22).
    """
    return os.environ.get("MINDWIRE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or None


def naysayer_github_token() -> str | None:
    """Resolve the review-side GitHub token — a *separate identity* (T22).

    This is the review-side account (``spirrowgames-ops``), shared by the
    proposer (Bohr) and naysayer (Einstein). GitHub forbids approving your own
    PR, so a formal review must come from a different account than the
    implementer author (``takahito-spirrowgames`` = Heisenberg). Resolves
    ``MINDWIRE_NAYSAYER_GITHUB_TOKEN`` first; if that is not
    provisioned yet it falls back to the shared :func:`github_token` so the
    adapter still functions (the same-identity 422 is then handled by the
    naysayer adapter's COMMENT fallback) — once the distinct token is set, the
    formal APPROVE / REQUEST_CHANGES goes through. The token's scope is the
    caller's concern (env spec: repo-scoped, least privilege); this only resolves
    which identity to authenticate as.

    Logs a warning on fallback so a forgotten token placement is visible rather
    than silent: while falling back, the naysayer runs as the author identity
    (author == approver) and its formal review is blocked / downgraded to a
    COMMENT — "working" but not actually satisfying author≠approver (T22).
    """
    token = os.environ.get("MINDWIRE_NAYSAYER_GITHUB_TOKEN")
    if token:
        return token
    logger.warning(
        "MINDWIRE_NAYSAYER_GITHUB_TOKEN is unset; the naysayer is falling back to "
        "the shared author token and will run as author==approver (self-approval) "
        "until the distinct spirrowgames-ops token is provisioned (T22)."
    )
    return github_token()


class ReviewEvent(StrEnum):
    """A GitHub PR review event (the naysayer's verdict, §A.3)."""

    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    COMMENT = "COMMENT"


@dataclass(frozen=True)
class PrRef:
    """A pull-request reference (``owner/repo#number``)."""

    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


class CiState(StrEnum):
    """Aggregated CI state for a PR head SHA (ADR-2026-06-03-16 §D-4).

    ``UNKNOWN`` is the fail-closed value: any 403 / network / parse failure (or
    a head SHA with no CI runs) maps here, and the naysayer never treats it as
    green. Only ``SUCCESS`` is "CI green".
    """

    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CiStatus:
    """CI status for a PR head SHA (ADR-16 L1 / L4)."""

    state: CiState
    head_sha: str | None
    failing: list[str]  # names of failing workflow runs (for the REQUEST_CHANGES body)


@dataclass(frozen=True)
class ReviewInfo:
    """A submitted PR review (the subset the naysayer debounce reads)."""

    login: str  # the reviewer's GitHub login (``user.login``)
    state: str  # APPROVED / CHANGES_REQUESTED / COMMENTED / DISMISSED / PENDING
    commit_id: str | None  # the head SHA the review was submitted against
    submitted_at: str | None


# Run conclusions that count as "not a failure" (ADR-16 §D-4 state mapping).
_CI_OK_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


def _required_workflows_from_env() -> frozenset[str] | None:
    """The naysayer's CI gate scope, from ``MINDWIRE_NAYSAYER_REQUIRED_WORKFLOWS``.

    A comma-separated list of GitHub Actions workflow ``name``s that constitute
    the gate (e.g. ``voxel-gate``). When set, only those workflows decide the CI
    state — advisory / warning-only workflows (a drift job that queues forever on
    an unavailable self-hosted runner) can no longer hold the gate PENDING. When
    unset (the default), every workflow counts, preserving the prior behavior.
    Empty / whitespace-only → ``None`` (all workflows).
    """
    raw = os.environ.get("MINDWIRE_NAYSAYER_REQUIRED_WORKFLOWS", "").strip()
    if not raw:
        return None
    names = frozenset(s.strip() for s in raw.split(",") if s.strip())
    return names or None


def _derive_ci_state(
    workflow_runs: list[Any],
    head_sha: str,
    required_workflows: frozenset[str] | None = None,
) -> CiStatus:
    """Aggregate GitHub Actions ``workflow_runs`` into a :class:`CiStatus`.

    When ``required_workflows`` is non-empty, only runs whose ``name`` is in that
    set are considered (advisory / non-gating workflows are ignored).

    Then dedupe to the latest run per ``workflow_id`` (so a re-run / superseded
    older run doesn't false-fail): any non-``completed`` → PENDING; any completed
    run whose ``conclusion`` is not success/neutral/skipped → FAILURE; all
    success → SUCCESS.

    ``required_workflows`` is a *checklist*, not just an allowlist: SUCCESS also
    requires that every name in the set has produced a run for this SHA. A required
    workflow GitHub Actions has not created a run for yet (delayed scheduling, a
    matrix not yet expanded) holds the gate PENDING — otherwise the subset that did
    run could open the gate while a required workflow is still missing. With a
    checklist an absent required run is *always* a wait state (PENDING), whether
    NONE have been scheduled yet or only SOME — one reality, one state — never
    UNKNOWN. UNKNOWN is reserved for the no-checklist case where the SHA has no CI
    runs at all (plus read/parse failures upstream): the genuine "is there even CI
    for this SHA?" fail-closed value, never SUCCESS.
    """
    latest: dict[Any, dict[str, Any]] = {}
    for run in workflow_runs:
        if not isinstance(run, dict):
            continue
        if required_workflows and str(run.get("name") or "") not in required_workflows:
            continue  # advisory / non-gating workflow: does not decide the gate
        wid = run.get("workflow_id")
        prev = latest.get(wid)
        if prev is None or int(run.get("run_number") or 0) >= int(prev.get("run_number") or 0):
            latest[wid] = run
    runs = list(latest.values())
    if not runs:
        # No considered runs. With a checklist this is the same "a required run
        # hasn't been scheduled yet" wait as the partial-coverage case below →
        # PENDING, not UNKNOWN (UNKNOWN's gate message misreports a token/permissions
        # problem). Without a checklist, zero runs is the genuine "is there any CI
        # for this SHA?" fail-closed UNKNOWN. (naysayer PR #111 round 2: don't split
        # one reality across UNKNOWN/PENDING by Actions' scheduling timeline.)
        if required_workflows:
            return CiStatus(state=CiState.PENDING, head_sha=head_sha, failing=[])
        return CiStatus(state=CiState.UNKNOWN, head_sha=head_sha, failing=[])
    if any(run.get("status") != "completed" for run in runs):
        return CiStatus(state=CiState.PENDING, head_sha=head_sha, failing=[])
    failing = [
        str(run.get("name") or run.get("workflow_id"))
        for run in runs
        if run.get("conclusion") not in _CI_OK_CONCLUSIONS
    ]
    if failing:
        return CiStatus(state=CiState.FAILURE, head_sha=head_sha, failing=failing)
    # Coverage check: with an explicit required-workflows checklist, every required
    # workflow must have produced a run for this SHA before the gate can open. A
    # required run GitHub Actions hasn't created yet would otherwise let the subset
    # that did run open the gate — so a missing required workflow holds it PENDING
    # (fail-closed), never SUCCESS. (naysayer PR #111: "required" is a checklist,
    # not just an allowlist filter on the runs that happen to be present.)
    if required_workflows:
        present = {str(run.get("name") or "") for run in runs}
        if not required_workflows <= present:
            return CiStatus(state=CiState.PENDING, head_sha=head_sha, failing=[])
    return CiStatus(state=CiState.SUCCESS, head_sha=head_sha, failing=[])


def parse_pr_ref(text: str) -> PrRef | None:
    """Extract a :class:`PrRef` from free text (PR URL or ``owner/repo#n``).

    Returns ``None`` if no PR reference is present (the caller decides whether
    that is a no-op or an error).
    """
    m = _PR_URL_RE.search(text)
    if m is None:
        m = _PR_SHORT_RE.search(text)
    if m is None:
        return None
    return PrRef(owner=m.group(1), repo=m.group(2), number=int(m.group(3)))


class GitHubError(Exception):
    """Base class for all GitHub client errors."""


class GitHubHTTPError(GitHubError):
    """HTTP-layer failure: network error, non-2xx status, or malformed body.

    Fail-loud (ADR-07 §2.6): a failed fetch/submit surfaces here rather than
    degrading silently, so the naysayer adapter can fail-closed halt.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubReviewClient(Protocol):
    """Structural view of the GitHub methods the naysayer adapter drives."""

    async def fetch_pr_diff(self, pr: PrRef) -> str: ...

    async def fetch_ci_status(self, pr: PrRef) -> CiStatus: ...

    async def fetch_pr_reviews(self, pr: PrRef) -> list[ReviewInfo]: ...

    async def submit_review(
        self, pr: PrRef, *, event: ReviewEvent, body: str
    ) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class GitHubClient:
    """Thin async client over the GitHub REST API (diff fetch + review submit).

    A single instance is **shared** across naysayer sessions (one httpx pool)
    and closed once at adapter teardown via :meth:`aclose`.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = _DEFAULT_API_BASE,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token if token is not None else github_token()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers=headers,
            transport=transport,
        )

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_pr_diff(self, pr: PrRef) -> str:
        """``GET /repos/{owner}/{repo}/pulls/{n}`` as a unified diff."""
        path = f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}"
        try:
            resp = await self._client.get(
                path, headers={"Accept": "application/vnd.github.v3.diff"}
            )
        except httpx.RequestError as exc:
            raise GitHubHTTPError(f"GET {path} (diff): {exc}") from exc
        if resp.status_code >= 400:
            raise GitHubHTTPError(
                f"GET {path} (diff) returned {resp.status_code}: {_error_detail(resp)}",
                status_code=resp.status_code,
            )
        return resp.text

    async def fetch_ci_status(self, pr: PrRef) -> CiStatus:
        """Aggregate the PR head SHA's GitHub Actions CI state (ADR-16 L1 / §D-4).

        Two reads: ``GET /pulls/{n}`` for the head SHA, then
        ``GET /actions/runs?head_sha={sha}`` for the workflow runs. **Fail-closed**
        (ADR-16 D-1): any 403 / network / parse failure → :attr:`CiState.UNKNOWN`
        (the naysayer never treats UNKNOWN as green), rather than raising — a CI
        read failure must not crash the review, it must withhold APPROVE.

        Uses the **Actions API** (not check-runs / Combined Status): a
        fine-grained PAT cannot be granted ``Checks`` permission, and Combined
        Status is empty for Actions-based CI (false green) — ADR-16 §D-4. The
        review token needs ``Actions: Read-only``.
        """
        pr_path = f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}"
        try:
            resp = await self._client.get(pr_path)
        except httpx.RequestError as exc:
            logger.warning("fetch_ci_status: GET %s failed: %s (fail-closed UNKNOWN)", pr_path, exc)
            return CiStatus(state=CiState.UNKNOWN, head_sha=None, failing=[])
        if resp.status_code >= 400:
            logger.warning(
                "fetch_ci_status: GET %s -> %s (fail-closed UNKNOWN)", pr_path, resp.status_code
            )
            return CiStatus(state=CiState.UNKNOWN, head_sha=None, failing=[])
        try:
            head_sha = str(resp.json()["head"]["sha"])
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("fetch_ci_status: cannot parse head SHA: %s (fail-closed)", exc)
            return CiStatus(state=CiState.UNKNOWN, head_sha=None, failing=[])

        runs_path = f"/repos/{pr.owner}/{pr.repo}/actions/runs"
        try:
            resp = await self._client.get(runs_path, params={"head_sha": head_sha})
        except httpx.RequestError as exc:
            logger.warning("fetch_ci_status: GET %s failed: %s (fail-closed)", runs_path, exc)
            return CiStatus(state=CiState.UNKNOWN, head_sha=head_sha, failing=[])
        if resp.status_code >= 400:
            logger.warning(
                "fetch_ci_status: GET %s -> %s (fail-closed; check Actions:read on the token)",
                runs_path,
                resp.status_code,
            )
            return CiStatus(state=CiState.UNKNOWN, head_sha=head_sha, failing=[])
        try:
            body = resp.json()
            workflow_runs = body["workflow_runs"] if isinstance(body, dict) else []
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("fetch_ci_status: cannot parse runs: %s (fail-closed)", exc)
            return CiStatus(state=CiState.UNKNOWN, head_sha=head_sha, failing=[])
        return _derive_ci_state(
            workflow_runs if isinstance(workflow_runs, list) else [],
            head_sha,
            _required_workflows_from_env(),
        )

    async def fetch_pr_reviews(self, pr: PrRef) -> list[ReviewInfo]:
        """``GET /repos/{owner}/{repo}/pulls/{n}/reviews`` → submitted reviews (paginated).

        Fail-soft: any network / non-2xx / parse failure returns ``[]`` — a debounce that cannot
        read the prior reviews simply proceeds to a full review (the safe, costlier path), unlike
        the fail-loud diff fetch / review submit. Pages of 100 are followed until a short page (a PR
        has far fewer than 100 reviews in practice, so this is usually a single request).
        """
        path = f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews"
        out: list[ReviewInfo] = []
        page = 1
        while True:
            try:
                resp = await self._client.get(path, params={"per_page": 100, "page": page})
            except httpx.RequestError as exc:
                logger.warning("fetch_pr_reviews: GET %s failed: %s (fail-soft [])", path, exc)
                return []
            if resp.status_code >= 400:
                logger.warning(
                    "fetch_pr_reviews: GET %s -> %s (fail-soft [])", path, resp.status_code
                )
                return []
            try:
                rows = resp.json()
            except ValueError as exc:
                logger.warning("fetch_pr_reviews: malformed JSON: %s (fail-soft [])", exc)
                return []
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                user = row.get("user")
                login = str(user.get("login") or "") if isinstance(user, dict) else ""
                cid = row.get("commit_id")
                submitted = row.get("submitted_at")
                out.append(
                    ReviewInfo(
                        login=login,
                        state=str(row.get("state") or ""),
                        commit_id=str(cid) if cid else None,
                        submitted_at=str(submitted) if submitted else None,
                    )
                )
            if len(rows) < 100:
                break
            page += 1
        return out

    async def submit_review(self, pr: PrRef, *, event: ReviewEvent, body: str) -> dict[str, Any]:
        """``POST /repos/{owner}/{repo}/pulls/{n}/reviews`` with a verdict event."""
        path = f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews"
        try:
            resp = await self._client.post(path, json={"event": event.value, "body": body})
        except httpx.RequestError as exc:
            raise GitHubHTTPError(f"POST {path} (review): {exc}") from exc
        if resp.status_code >= 400:
            raise GitHubHTTPError(
                f"POST {path} (review) returned {resp.status_code}: {_error_detail(resp)}",
                status_code=resp.status_code,
            )
        try:
            body_json = resp.json()
        except ValueError as exc:
            raise GitHubHTTPError(f"POST {path} (review): malformed JSON: {exc}") from exc
        return body_json if isinstance(body_json, dict) else {"raw": body_json}


def _error_detail(resp: httpx.Response) -> str:
    """Best-effort extraction of a GitHub ``{"message": ...}`` error string."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict) and "message" in body:
        return str(body["message"])
    return str(body)[:500]


__all__ = [
    "CiState",
    "CiStatus",
    "GitHubClient",
    "GitHubError",
    "GitHubHTTPError",
    "GitHubReviewClient",
    "PrRef",
    "ReviewEvent",
    "ReviewInfo",
    "github_token",
    "naysayer_github_token",
    "parse_pr_ref",
]
