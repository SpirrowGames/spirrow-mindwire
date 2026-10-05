"""D-16ab source adapters, motion events, and the remedy-marker codec.

Spec: T-stalled-pr-has-no-detector msg-4685 §3 / §5, with msg-4681 §3 and msg-4683 §2-§3.

Three adapters, one per source, each producing a :class:`SourceResult` whose
:class:`~spirrow_mindwire.stall_ledger.heartbeat.SourceReport` satisfies
``recognized + unrecognized == examined`` (enforced by the report's constructor):

* :class:`GitHubOpenPrAdapter`  -- open PRs of one repository.
* :class:`ChatroomThreadAdapter` -- threads of one chatroom project, through
  :class:`~spirrow_mindwire.magickit.read_only.ReadOnlyMcp`.
* :class:`QuarantineFileAdapter` -- ``<DataDir>/state/quarantine.json``.

None of them reads ``sweep.json`` (msg-4399 §1), and none writes anything anywhere.
No new API client lives in this package (msg-4413 §4): GitHub goes through
:class:`~spirrow_mindwire.github.client.GitHubClient`, the chatroom through the
magickit client.

**The O(1) body-fetch guarantee is structural** (msg-4683 §3). :class:`MotionEvent`
carries ``type``, ``id`` and ``at`` and nothing else, so a listing has nowhere to put
a decoded marker and nothing that would tempt it to decode every event. The only path
that fetches a body is :meth:`MarkerChecker.check_marker`, which the driver calls at
most once per record it opens without a trustworthy stored record.

How each adapter maps its source onto the ledger's inputs is written on the adapter.
Those mappings are the implementer's reading of the existing predicate types
(``PrState`` / ``ThreadState`` / ``QuarantineState`` and ``ClassifierInput``); the
spec names the sources and the invariants, not the field-by-field mapping. Where the
source cannot tell, the choice leans to the loud side (a unit reads as needing an
actor, or as unclassified) -- with ONE stated exception: :class:`ChatroomThreadAdapter`
uses the 72h ``other_thread`` threshold for every thread, which is the QUIET side for a
thread that was actually nominated (6h). The adapter's docstring says why.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

from spirrow_mindwire.github.client import (
    GitHubClient,
    GitHubError,
    OpenPr,
    OpenPrListingError,
    PrRef,
    PrResolution,
    ReviewInfo,
)
from spirrow_mindwire.magickit.read_only import ReadOnlyMcp, ToolCaller
from spirrow_mindwire.stall_ledger.classifier import ClassifierInput
from spirrow_mindwire.stall_ledger.heartbeat import FetchOutcome, SourceReport
from spirrow_mindwire.stall_ledger.model import Unit, UnitKind
from spirrow_mindwire.stall_ledger.predicates import (
    N_THRESHOLDS,
    PrState,
    QuarantineState,
    ThreadState,
    needs_actor_pr,
    needs_actor_quarantine,
    needs_actor_thread,
)
from spirrow_mindwire.stall_ledger.timing import FETCH_TIMEOUT

#: The format version every adapter in this module parses. One string, compared by
#: :meth:`SourceReport.is_failure` against the driver's expected version.
ADAPTER_FORMAT_VERSION = "stall-ledger-adapter/1"

#: Default per-fetch timeout (msg-4701 §3). Set by D-16c in :mod:`.timing`.
DEFAULT_FETCH_TIMEOUT = FETCH_TIMEOUT


# ─── Motion events ─────────────────────────────────────────────────────────────────────


class MotionType(StrEnum):
    """The motion types the ledger knows (msg-4677 §1: E is a push, a review or a msg)."""

    HEAD_PUSH = "head_push"
    REVIEW = "review"
    CHATROOM_MSG = "chatroom_msg"


@dataclass(frozen=True)
class MotionEvent:
    """One motion event from a listing (msg-4685 §3, msg-4683 §3-1).

    ``type`` is a plain ``str`` rather than :class:`MotionType` on purpose: the driver's
    E table has an "anything else" row (``unknown_motion_type``), and a value outside the
    enum must be representable to reach it.

    **No field for marker information**, by design.
    """

    type: str
    id: str
    at: datetime


# ─── MarkerCheck: three explicit states (msg-4683 §2) ──────────────────────────────────

UnverifiableReason = Literal["fetch_failed", "deleted", "malformed_marker"]
UNVERIFIABLE_REASONS: tuple[UnverifiableReason, ...] = (
    "fetch_failed",
    "deleted",
    "malformed_marker",
)


@dataclass(frozen=True)
class Marked:
    attempt_id: str


@dataclass(frozen=True)
class Unmarked:
    pass


@dataclass(frozen=True)
class Unverifiable:
    reason: UnverifiableReason


MarkerCheck = Marked | Unmarked | Unverifiable

#: Every variant of :data:`MarkerCheck`, for the exhaustiveness test (msg-4685 §7-3).
MARKER_CHECK_VARIANTS: tuple[type, ...] = (Marked, Unmarked, Unverifiable)


# ─── RemedyMarkerCodec (msg-4685 §5) ───────────────────────────────────────────────────


class RemedyMarkerCodec:
    """Encode / decode the v1 remedy marker.

    Format: the FIRST line of the body, starting at column 0, is exactly
    ``<!-- stall-ledger-remedy:v1 attempt=<id> -->``. GitHub and the chatroom are both
    markdown, so both adapters share this one implementation (msg-4681 §3-3).

    D-16ab only calls :meth:`decode`. :meth:`encode` exists for the refire unit.

    ``decode`` is anchored: a marker behind a ``> `` quote, after leading whitespace,
    inside a code fence, or on line 2 or later is NOT a marker (it is ``Unmarked``). A
    first line that *starts* like a marker but does not parse as v1 is
    ``Unverifiable(malformed_marker)`` -- fail-closed (msg-4681 §3-5).
    """

    PREFIX = "<!-- stall-ledger-remedy"
    _V1 = re.compile(r"\A<!-- stall-ledger-remedy:v1 attempt=(?P<id>[A-Za-z0-9._:-]+) -->\Z")
    _ID = re.compile(r"\A[A-Za-z0-9._:-]+\Z")

    @classmethod
    def encode(cls, attempt_id: str) -> str:
        if not cls._ID.match(attempt_id):
            raise ValueError(f"attempt_id {attempt_id!r} is not encodable in the v1 marker")
        return f"<!-- stall-ledger-remedy:v1 attempt={attempt_id} -->"

    @classmethod
    def decode(cls, body: str) -> MarkerCheck:
        first = body.split("\n", 1)[0]
        if first.endswith("\r"):
            first = first[:-1]
        if not first.startswith(cls.PREFIX):
            return Unmarked()
        m = cls._V1.match(first)
        if m is None:
            return Unverifiable("malformed_marker")
        return Marked(m.group("id"))


# ─── Adapter contract ──────────────────────────────────────────────────────────────────


class MarkerChecker(Protocol):
    async def check_marker(self, unit: Unit, event: MotionEvent) -> MarkerCheck: ...


@dataclass(frozen=True)
class UnitObservation:
    """Everything the driver needs about one unit on one heartbeat.

    ``baseline_at`` is the unit's own start (PR ``created_at``, thread ``created_at``,
    quarantine ``first_failure_at``). It is the floor of ``last_participant_motion_at``
    for a unit with no participant motion event, and it is never a candidate for E: no
    remedy can create a unit, so there is nothing for the marker check to look at.
    """

    unit: Unit
    needs_actor: bool
    n: timedelta
    classifier_input: ClassifierInput
    motion: tuple[MotionEvent, ...]
    baseline_at: datetime


@dataclass(frozen=True)
class SourceResult:
    """One source's contribution to a heartbeat.

    ``complete`` is True only when the listing itself was read in full. Only then may
    the driver read "an open record of this source's scope is absent from
    ``observations``" as ``needs_actor = false`` (close rule (a)). ``unobservable``
    names units the listing returned but whose per-unit reads failed: their records are
    left exactly as they are.

    The same holds for a listed row whose payload is malformed (PR-gate #361 @ e426e69):
    if its unit key can still be read, the key goes into ``unobservable``; if even the
    key cannot be read, the row could be ANY tracked unit, so the adapter reports
    ``complete = False`` and close rule (a) is suspended for that source this tick. A
    malformed row never reads as "this unit is gone".
    """

    report: SourceReport
    observations: tuple[UnitObservation, ...]
    unobservable: frozenset[str]
    complete: bool
    scope: Callable[[Unit], bool]
    checker: MarkerChecker


T = TypeVar("T")


async def _bounded(coro: Awaitable[T], timeout: timedelta) -> T:
    """Every network call gets an explicit timeout (msg-4701 §3)."""
    return await asyncio.wait_for(coro, timeout=timeout.total_seconds())


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def _failed(
    name: str, outcome: FetchOutcome, scope: Callable[[Unit], bool], checker: MarkerChecker
) -> SourceResult:
    return SourceResult(
        report=SourceReport(
            name=name,
            fetch_outcome=outcome,
            examined=0,
            recognized=0,
            unrecognized=0,
            observed_format_version=ADAPTER_FORMAT_VERSION,
        ),
        observations=(),
        unobservable=frozenset(),
        complete=False,
        scope=scope,
        checker=checker,
    )


def github_credential_present(token: str | None) -> bool:
    """True when ``token`` can authenticate a request: not ``None``, not blank.

    Truthiness after ``strip()`` rather than ``is None`` (Einstein's advisory on msg-6415): a
    wrapper that copies an unset variable can hand over ``""``, and a blank token must
    fail as ``auth_missing`` too, not slip through to an unauthenticated request.
    """
    return token is not None and bool(token.strip())


# ─── GitHub ────────────────────────────────────────────────────────────────────────────

#: ``mergeable_state`` values under which the merge is executable. Every other
#: OBSERVED value -- ``dirty``, ``blocked``, ``behind``, ``unstable``, ``draft`` --
#: reads as not executable, which is the loud side of ``needs_actor_pr``.
_EXECUTABLE_MERGE_STATES = frozenset({"clean", "has_hooks"})

#: ``mergeable_state`` values that mean GitHub has not computed the merge state yet
#: (it is computed lazily; the first read often answers ``unknown``). ``None`` -- the
#: field absent or JSON null -- is treated the same way. Bohr msg-5157 §3: such a PR is
#: UNOBSERVABLE this tick -- neither ``dirty`` (a false external block) nor "not
#: blocked" -- so it goes into ``unobservable`` and close rule (a) does not touch it.
_UNCOMPUTED_MERGE_STATES = frozenset({"unknown"})


def mergeable_state_is_observed(mergeable_state: str | None) -> bool:
    """False for ``None`` / ``unknown``: the merge state has not been computed yet."""
    return mergeable_state is not None and mergeable_state not in _UNCOMPUTED_MERGE_STATES


#: Review states that count as a verdict on the head they were submitted against.
_VERDICT_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "COMMENTED"})


class GitHubOpenPrAdapter:
    """Open PRs of one repository.

    Per heartbeat: one paginated listing (``list_open_prs``, the request pinned by
    ``build_open_pr_query``), then per open PR three existing reads -- reviews
    (strict), the PR object (for ``mergeable_state``) and the check rollup (for the
    head commit's clock and the CI rows). A PR whose per-PR reads fail -- or whose
    ``mergeable_state`` GitHub has not computed yet (``unknown`` / null, msg-5157 §3) --
    is counted as ``unrecognized`` and reported ``unobservable``: its record is neither
    opened nor closed on a heartbeat that could not see it.

    Mapping onto ``PrState`` / ``ClassifierInput`` (implementer's reading, see module
    docstring):

    * verdict = the newest review on the CURRENT head whose state is APPROVED,
      CHANGES_REQUESTED or COMMENTED. A head with no review at all, or one that moved
      past every verdict, has none -- "no verdict", the loud side -- so the review
      actor owes a call.
    * ``verdict_recorded_as_indefinite_input`` = that verdict is COMMENTED (the gate
      posts COMMENT for ``ci=pending`` / ``ci=unknown``, msg-2354 M-3/M-4).
    * ``merge_state_is_executable`` = ``mergeable_state`` in {clean, has_hooks}.
    * ``is_approve_awaiting_human_merge`` = APPROVED and executable.
    * ``is_externally_blocked`` = ``mergeable_state == "dirty"`` (M-3: needs a rebase).
      ``blocked`` / ``behind`` / ``unstable`` are NOT external blocks: ``blocked`` is
      the normal state of a PR carrying an RC, CI runs on ``behind``, and ``unstable``
      is failing CI -- in each the PR's actor is still expected to move (msg-5157 §3).
    * ``mergeable_state`` ``unknown`` / null -> the PR is unobservable this tick (see
      above), never mapped to blocked or not-blocked.
    * ``ci_became_definitive`` = the rollup has rows and every row is completed.
    * motion: the head as ``head_push`` at the head commit's ``committedDate``, and every
      submitted review as ``review`` at ``submitted_at``. ``committedDate`` is used
      rather than the rollup's ``head_pushed_at`` because the latter falls back to the
      PR's ``updatedAt``, which any label or comment moves -- that would make motion look
      newer than it is (the quiet side). ``committedDate`` can only be earlier than the
      push (the loud side).

      Known residual (accepted, Bohr msg-5157 §3): a commit authored locally long ago
      and pushed later carries its old ``committedDate``, so that push reads as older
      motion than it is and the stall can open EARLY (by up to the commit-to-push gap).
      This errs to the loud side and is not corrected here.

    **No credential, no request** (T-stalled-pr-has-no-detector msg-6415 Q2). The caller
    says whether a GitHub token is configured (``credential_present``; the tick decides it
    at its entry point, :func:`github_credential_present`). Without one, :meth:`fetch`
    returns ``auth_missing`` and :meth:`check_marker` answers unverifiable, both without
    touching the client. :class:`GitHubClient` itself still sends an unauthenticated
    request when it has no token -- that is the conductor's contract and is left alone --
    but for the ledger it was measured (msg-6414) to exhaust the 60/h per-IP limit and
    surface only as ``unrecognized == examined``, a silent failure. Failing loudly here
    puts the cause on the heartbeat's source line instead.
    """

    def __init__(
        self,
        client: GitHubClient,
        owner: str,
        repo: str,
        *,
        fetch_timeout: timedelta = DEFAULT_FETCH_TIMEOUT,
        credential_present: bool = True,
    ) -> None:
        self._client = client
        self._owner = owner
        self._repo = repo
        self._timeout = fetch_timeout
        self._credential_present = credential_present
        self.name = f"github:{owner}/{repo}"

    def scope(self, unit: Unit) -> bool:
        return unit.kind == UnitKind.PR and unit.identifier.startswith(
            f"{self._owner}/{self._repo}#"
        )

    @staticmethod
    def unit_for(ref: PrRef) -> Unit:
        return Unit(UnitKind.PR, ref.slug)

    async def fetch(self) -> SourceResult:
        if not self._credential_present:
            return _failed(self.name, FetchOutcome.AUTH_MISSING, self.scope, self)
        try:
            listing = await _bounded(
                self._client.list_open_prs(self._owner, self._repo), self._timeout
            )
        except TimeoutError:
            return _failed(self.name, FetchOutcome.TIMEOUT, self.scope, self)
        except OpenPrListingError as exc:
            return _failed(self.name, FetchOutcome(exc.outcome), self.scope, self)

        observations: list[UnitObservation] = []
        unobservable: set[str] = set()
        for pr in listing.prs:
            obs = await self._observe(pr)
            if obs is None:
                unobservable.add(self.unit_for(pr.ref).key)
            else:
                observations.append(obs)
        report = SourceReport(
            name=self.name,
            fetch_outcome=FetchOutcome.OK,
            examined=listing.examined,
            recognized=len(observations),
            unrecognized=listing.unrecognized + len(unobservable),
            observed_format_version=ADAPTER_FORMAT_VERSION,
        )
        return SourceResult(
            report=report,
            observations=tuple(observations),
            unobservable=frozenset(unobservable),
            # A listing row the client could not parse has no PR number we can trust, so
            # it could be any tracked PR: rule (a) waits for a clean listing.
            complete=listing.unrecognized == 0,
            scope=self.scope,
            checker=self,
        )

    async def _observe(self, pr: OpenPr) -> UnitObservation | None:
        try:
            reviews = await _bounded(self._client.fetch_pr_reviews_strict(pr.ref), self._timeout)
            state = await _bounded(self._client.fetch_pr_state(pr.ref), self._timeout)
            rollup = await _bounded(self._client.fetch_check_rollup(pr.ref), self._timeout)
        except (TimeoutError, GitHubError):
            return None
        if state.resolution != PrResolution.OPEN or rollup is None:
            return None
        if not mergeable_state_is_observed(state.mergeable_state):
            return None
        head_sha = state.head_sha or pr.head_sha
        return build_pr_observation(
            pr=pr,
            head_sha=head_sha,
            reviews=reviews,
            mergeable_state=state.mergeable_state,
            head_committed_at=rollup.head_committed_date,
            ci_rows_completed=[row.status == "completed" for row in rollup.rows],
        )

    async def check_marker(self, unit: Unit, event: MotionEvent) -> MarkerCheck:
        if not self._credential_present:
            return Unverifiable("fetch_failed")
        if event.type != MotionType.REVIEW:
            # The driver only asks about reviews / msgs; a msg on a PR unit is not ours.
            return Unverifiable("fetch_failed")
        ref = _pr_ref_from_identifier(unit.identifier)
        if ref is None:
            return Unverifiable("fetch_failed")
        try:
            body = await _bounded(self._client.fetch_review_body(ref, event.id), self._timeout)
        except (TimeoutError, GitHubError):
            return Unverifiable("fetch_failed")
        if body is None:
            return Unverifiable("deleted")
        return RemedyMarkerCodec.decode(body)


def _pr_ref_from_identifier(identifier: str) -> PrRef | None:
    m = re.fullmatch(r"(?P<owner>[^/]+)/(?P<repo>[^#]+)#(?P<n>\d+)", identifier)
    if m is None:
        return None
    return PrRef(owner=m.group("owner"), repo=m.group("repo"), number=int(m.group("n")))


def build_pr_observation(
    *,
    pr: OpenPr,
    head_sha: str,
    reviews: Iterable[ReviewInfo],
    mergeable_state: str | None,
    head_committed_at: datetime,
    ci_rows_completed: list[bool],
) -> UnitObservation:
    """Pure mapping from the GitHub reads to a :class:`UnitObservation` (see adapter doc)."""
    submitted: list[tuple[datetime, ReviewInfo]] = []
    for review in reviews:
        at = _parse_ts(review.submitted_at)
        if at is not None:
            submitted.append((at, review))
    on_head = [
        (at, r) for at, r in submitted if r.commit_id == head_sha and r.state in _VERDICT_STATES
    ]
    verdict = max(on_head, key=lambda pair: pair[0])[1] if on_head else None
    executable = mergeable_state in _EXECUTABLE_MERGE_STATES
    verdict_state = verdict.state if verdict is not None else None
    pr_state = PrState(
        is_open=True,
        is_draft=pr.draft,
        has_verdict=verdict is not None,
        verdict_is_request_changes=verdict_state == "CHANGES_REQUESTED",
        verdict_recorded_as_indefinite_input=verdict_state == "COMMENTED",
        merge_state_is_executable=executable,
        is_approve_awaiting_human_merge=verdict_state == "APPROVED" and executable,
    )
    motion: list[MotionEvent] = [MotionEvent(MotionType.HEAD_PUSH, head_sha, head_committed_at)]
    for at, review in submitted:
        if review.review_id:
            motion.append(MotionEvent(MotionType.REVIEW, review.review_id, at))
    classifier_input = ClassifierInput(
        is_externally_blocked=mergeable_state == "dirty",
        verdict_is_indefinite=verdict_state == "COMMENTED",
        ci_became_definitive=bool(ci_rows_completed) and all(ci_rows_completed),
    )
    return UnitObservation(
        unit=GitHubOpenPrAdapter.unit_for(pr.ref),
        needs_actor=needs_actor_pr(pr_state),
        n=N_THRESHOLDS["pr_with_verdict"],
        classifier_input=classifier_input,
        motion=tuple(motion),
        baseline_at=pr.created_at,
    )


# ─── Chatroom ──────────────────────────────────────────────────────────────────────────

#: The chatroom tools this adapter may call. Both are reads.
CHATROOM_READ_TOOLS = frozenset({"chatroom_list_threads", "chatroom_get_thread"})

_THREAD_PAGE = 200


class ChatroomThreadAdapter:
    """Threads of one chatroom project (every status, paged).

    Mapping (implementer's reading):

    * ``ThreadState.status`` = the listing's ``status``; ``dormant_until_expired`` is
      always True, because the listing carries no dormant-until pin -- reading every
      thread as un-pinned is the loud side.
    * ``N`` = ``other_thread`` (72h). **This is the quiet side** for a thread that was
      actually nominated (``thread_with_nomination`` = 6h): such a stall opens up to
      66h late. Telling the two apart needs the last message's ``next_participant``,
      i.e. one body fetch per thread per heartbeat, which the listing-only contract
      (msg-4683 §3-1) does not allow. Recorded as a known quiet-side residual.
    * motion: the thread head as one ``chatroom_msg`` event (``last_msg_id`` at
      ``last_activity_at``). That is the newest motion, which is all E needs.
    * ``ClassifierInput`` is all-False -> ``unclassified``.
    """

    def __init__(
        self,
        mcp: ToolCaller,
        project: str,
        *,
        fetch_timeout: timedelta = DEFAULT_FETCH_TIMEOUT,
    ) -> None:
        self._mcp = ReadOnlyMcp(mcp, CHATROOM_READ_TOOLS, label="stall ledger")
        self._project = project
        self._timeout = fetch_timeout
        self.name = f"chatroom:{project}"

    def scope(self, unit: Unit) -> bool:
        return unit.kind == UnitKind.THREAD and unit.identifier.startswith(f"{self._project}/")

    async def fetch(self) -> SourceResult:
        items: list[Any] = []
        offset = 0
        try:
            while True:
                page = await _bounded(
                    self._mcp.call_tool(
                        "chatroom_list_threads",
                        {"project": self._project, "limit": _THREAD_PAGE, "offset": offset},
                    ),
                    self._timeout,
                )
                if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                    return _failed(self.name, FetchOutcome.PARSE_ERROR, self.scope, self)
                batch = page["items"]
                if not batch:
                    break
                items.extend(batch)
                offset += len(batch)
                total = page.get("total")
                if not isinstance(total, int) or offset >= total:
                    break
        except TimeoutError:
            return _failed(self.name, FetchOutcome.TIMEOUT, self.scope, self)
        except Exception:
            return _failed(self.name, FetchOutcome.HTTP_ERROR, self.scope, self)

        observations: list[UnitObservation] = []
        unobservable: set[str] = set()
        unkeyed = 0
        for item in items:
            obs = self._thread_row(item)
            if obs is not None:
                observations.append(obs)
                continue
            # Malformed row: never "absent" (close rule (a)). Keyed -> unobservable;
            # unkeyed -> the whole listing is not complete (see SourceResult).
            key = self._row_key(item)
            if key is None:
                unkeyed += 1
            else:
                unobservable.add(key)
        report = SourceReport(
            name=self.name,
            fetch_outcome=FetchOutcome.OK,
            examined=len(items),
            recognized=len(observations),
            unrecognized=len(items) - len(observations),
            observed_format_version=ADAPTER_FORMAT_VERSION,
        )
        return SourceResult(
            report=report,
            observations=tuple(observations),
            unobservable=frozenset(unobservable),
            complete=unkeyed == 0,
            scope=self.scope,
            checker=self,
        )

    def _row_key(self, item: object) -> str | None:
        """The unit key of a listing row, or ``None`` if the row carries no usable id."""
        if not isinstance(item, dict):
            return None
        thread_id = item.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            return None
        return Unit(UnitKind.THREAD, f"{self._project}/{thread_id}").key

    def _thread_row(self, item: object) -> UnitObservation | None:
        if not isinstance(item, dict):
            return None
        thread_id = item.get("thread_id")
        status = item.get("status")
        created = _parse_ts(item.get("created_at"))
        last_at = _parse_ts(item.get("last_activity_at"))
        last_id = item.get("last_msg_id")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(status, str):
            return None
        if created is None or last_at is None or not isinstance(last_id, str) or not last_id:
            return None
        state = ThreadState(status=status, dormant_until_expired=True)
        return UnitObservation(
            unit=Unit(UnitKind.THREAD, f"{self._project}/{thread_id}"),
            needs_actor=needs_actor_thread(state),
            n=N_THRESHOLDS["other_thread"],
            classifier_input=ClassifierInput(),
            motion=(MotionEvent(MotionType.CHATROOM_MSG, last_id, last_at),),
            baseline_at=created,
        )

    async def check_marker(self, unit: Unit, event: MotionEvent) -> MarkerCheck:
        if event.type != MotionType.CHATROOM_MSG:
            return Unverifiable("fetch_failed")
        project, _, thread_id = unit.identifier.partition("/")
        try:
            result = await _bounded(
                self._mcp.call_tool(
                    "chatroom_get_thread",
                    {"project": project, "thread_id": thread_id, "mode": "full"},
                ),
                self._timeout,
            )
        except Exception:
            return Unverifiable("fetch_failed")
        messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(messages, list):
            return Unverifiable("fetch_failed")
        for msg in messages:
            if isinstance(msg, dict) and msg.get("msg_id") == event.id:
                content = msg.get("content")
                if not isinstance(content, str):
                    return Unverifiable("fetch_failed")
                return RemedyMarkerCodec.decode(content)
        return Unverifiable("deleted")


# ─── quarantine.json ───────────────────────────────────────────────────────────────────


@dataclass
class QuarantineFileAdapter:
    """``<DataDir>/state/quarantine.json``: every entry is a unit, and presence IS the
    predicate (``N = 0``, ``predicates.py``).

    A missing file is ``file_missing`` and an unparseable one is ``parse_error`` -- both
    failures, per :class:`FetchOutcome`'s own definitions. An entry whose value is not an
    object or has no parseable ``first_failure_at`` is ``unrecognized`` and reported
    ``unobservable`` (its record is left as it is, never closed). Quarantine units
    have no motion events; ``first_failure_at`` is the baseline.

    Reads the file; never writes it (msg-4685 §8, msg-4697 §2-3).
    """

    path: Path
    name: str = field(default="quarantine")

    def scope(self, unit: Unit) -> bool:
        return unit.kind == UnitKind.QUARANTINE

    async def fetch(self) -> SourceResult:
        try:
            raw = self.path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return _failed(self.name, FetchOutcome.FILE_MISSING, self.scope, self)
        except OSError:
            return _failed(self.name, FetchOutcome.FILE_MISSING, self.scope, self)
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return _failed(self.name, FetchOutcome.PARSE_ERROR, self.scope, self)
        if not isinstance(data, dict):
            return _failed(self.name, FetchOutcome.PARSE_ERROR, self.scope, self)
        observations: list[UnitObservation] = []
        unobservable: set[str] = set()
        for key, value in data.items():
            first = _parse_ts(value.get("first_failure_at")) if isinstance(value, dict) else None
            if first is None:
                # The entry is present -- the predicate for this source -- but its payload
                # is unreadable: unobservable, never "cleared" (close rule (a)).
                unobservable.add(Unit(UnitKind.QUARANTINE, str(key)).key)
                continue
            observations.append(
                UnitObservation(
                    unit=Unit(UnitKind.QUARANTINE, str(key)),
                    needs_actor=needs_actor_quarantine(QuarantineState(is_present=True)),
                    n=N_THRESHOLDS["quarantine"],
                    classifier_input=ClassifierInput(is_quarantined=True),
                    motion=(),
                    baseline_at=first,
                )
            )
        report = SourceReport(
            name=self.name,
            fetch_outcome=FetchOutcome.OK,
            examined=len(data),
            recognized=len(observations),
            unrecognized=len(data) - len(observations),
            observed_format_version=ADAPTER_FORMAT_VERSION,
        )
        return SourceResult(
            report=report,
            observations=tuple(observations),
            unobservable=frozenset(unobservable),
            complete=True,
            scope=self.scope,
            checker=self,
        )

    async def check_marker(self, unit: Unit, event: MotionEvent) -> MarkerCheck:
        # Quarantine units carry no motion, so the driver never asks. If it ever does,
        # answer on the loud side.
        del unit, event
        return Unverifiable("fetch_failed")


class Adapter(Protocol):
    name: str

    async def fetch(self) -> SourceResult: ...
