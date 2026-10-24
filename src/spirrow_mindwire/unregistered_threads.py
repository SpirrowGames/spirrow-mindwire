"""D-2 enumerator for "unregistered" chatroom threads.

Owns the pure predicate behind :ref:`T-sweep-intake-and-quarantine-stalls`'s
D-2 (Bohr msg-2529 §8 as refined by msg-2531 §1): count threads that would
be missed by the sweep because they are live in the chatroom yet not listed
in ``sweep.json``. The digest's "飢餓" (starvation) detector is pivoted on
the sweep's own candidate list (`run-conductor-scheduled.ps1` L1033), so
the one class of live thread it structurally cannot see is exactly the one
D-2 is intended to catch — the F-1 loop msg-1181 named.

"Unregistered" is defined by three conjunctive filters, applied in this
order:

1. ``status`` is in :data:`LIVE_STATUSES` — the same set
   :data:`spirrow_mindwire.gate_bootstrap._OPEN_ALERT_TARGET_STATUSES`
   uses (Bohr msg-2460 §4). ``resolved`` / ``superseded`` / ``parked`` are
   finished work and excluded by construction — carrying them here would
   reintroduce the status-blindness the gate-bootstrap thread already
   ruled against.
2. ``thread_id`` does NOT start with :data:`PR_REVIEW_PREFIX`. PR-review
   threads dispatch to the Tier B PR-gate and are excluded from the
   sweep by contract (``deploy/sweep.json.example`` ``_comment``);
   counting them as "unregistered" would create a permanent false
   positive equal to the number of open PRs.
3. ``(project, thread_id)`` is not present in :class:`RegisteredIndex`
   built from ``sweep.json``'s ``candidates`` array. Project scoping
   matters: thread ids are only unique WITHIN a project (see the state
   key in ``run-conductor-scheduled.ps1`` L2375).

**Wired into the sweep** (msg-5889 D-2 / msg-5893 D-2.1-D-2.4). The
earlier "log-only until P-1 ∧ P-2 hold" rule (msg-2531 §1) no longer
applies: Operator Board §F.1 / msg-4900 §2 dropped the plan to express
"cannot be swept" as a ``parked`` state. The sweep wrapper now adds
every thread this predicate reports to the current tick's candidates
(in memory; ``sweep.json`` is never written) when the discovery rule
below admits it (D-2.5), and lists the rest with their reason in the
daily digest. See ``scripts/unregistered_threads.py`` and
``deploy/lib/UnregisteredIntake.ps1`` for the split between an
auto-registered thread (an ordinary candidate, quarantinable) and a
refused one (never in any state file).

**Discovery by default** (T-sweep-intake-discovery-by-default, Bohr
msg-6776 as amended by msg-6778). The scanned projects are the union of
``sweep.json``'s ``clones`` keys, the candidates' projects and the
``projects`` key (穴 3: a project named nowhere used to be neither
scanned nor shown). Every unregistered thread now also carries how long
it has been idle, read from the listing's own ``last_activity_at`` — never
from a per-thread read (Einstein msg-6769: no N+1 calls per tick). The
timestamp is interpreted in exactly one place, :func:`classify_activity_at`,
which only the CLI's ``_list_live_threads`` calls. That boundary
CLASSIFIES (ok / missing / unparseable) and drops nothing: whether a
thread is admitted is decided later, by the wrapper's
``Resolve-UnregisteredIntake``, and only for threads ``sweep.json`` does
not list. An explicitly listed candidate never reaches this module's
report at all (:func:`is_unregistered_live` filter 3), so neither a
missing timestamp nor ``max_idle_days`` can touch it (msg-6778).

**The field is ``last_activity_at``** (T-sweep-intake-discovery-by-default
msg-7441 F-1, msg-7445). The listing this reads is magickit's
``chatroom_list_threads``, whose items carry ``last_activity_at`` and no
``updated_at`` (operator measurement, msg-7381 §4; the real item shape is
pinned in ``tests/data/magickit_chatroom_list_threads_page.json``). The
first version of this module read ``updated_at``, a field the listing
never had, so every thread was "time unknown" for eight days without a
word. Two things now stop that recurring: the name is the same from the
wire to the wrapper (input key, :func:`classify_activity_at`, the JSON
key, the PowerShell key), and the boundary counts the items that lack the
key outright (:attr:`ProjectReport.field_absent_count`, msg-7443 F-3c as
narrowed by msg-7445) so the wrapper can say so in the tick log and the
digest. There is no ``updated_at`` fallback (msg-7441 F-1: one field, not
two).

Fail direction: this module makes **no** MCP calls of its own — the CLI
around it does. What it does with an MCP failure is separated from what
it does with a well-shaped answer, on purpose (msg-2531 §2 invariants):

* the count field distinguishes "measured 0" from "did not measure"
  (``count is None`` vs ``count == 0``);
* the ``error`` string carries the reason so a downstream reader can log
  it verbatim rather than swallowing it into a bare `?`.

The wrapper is responsible for turning the outer bounds (subprocess
timeout / non-zero exit / JSON parse failure) into the operator-facing
`?` — this module only produces the per-project envelope from which
those decisions can be made.

**Third state (msg-2648 §3 / PR-gate #221 disposition):** items that
the MCP call returned but that were not shaped as JSON objects are
counted separately, in :attr:`ProjectReport.malformed_count`, and are
**never** mixed into ``unregistered_count``. "Not seen", "seen and
untracked", and "seen but unreadable" are three distinct states — the
first two feed the sweep-completeness signal, the third feeds a
data-integrity signal. Collapsing them into one number recreates the
fail-silent shape msg-1181 named as the core defect. A day where
``malformed_count > 0`` is a day where the count is not comparable to
another day: the P-2 before/after check msg-2531 §1 defines relies on
the assumption that the two measurements enumerated the same population,
and a garbled listing breaks that assumption without touching the
count.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

#: Chatroom statuses that count as "live work in flight" for D-2.
#: Kept string-value-identical to
#: :data:`spirrow_mindwire.gate_bootstrap._OPEN_ALERT_TARGET_STATUSES`
#: (Bohr msg-2460 §4). Deliberately excluded: ``resolved`` (done),
#: ``superseded`` (replaced), ``parked`` (intentionally set aside — a
#: parked thread is not live work, so it is never auto-registered).
LIVE_STATUSES: frozenset[str] = frozenset({"active", "awaiting_reply"})

#: The thread-id prefix used for PR-review threads. Any thread whose id
#: starts with this string is excluded from D-2 regardless of status.
#: The convention is documented in ``deploy/sweep.json.example``'s
#: ``_comment`` and in ``run-conductor-scheduled.ps1`` L2315-2317, and
#: also encoded (with a per-project tail) in
#: :data:`spirrow_mindwire.pr_review_sweep.config._THREAD_PREFIX_TEMPLATE`.
#: Here we match the class as a whole, so the un-templated prefix is what
#: we want.
PR_REVIEW_PREFIX: str = "T-pr-review-"

#: The listing field the idle time is read from (msg-7441 F-1). One name,
#: used for the input key, the JSON output key and the PowerShell key.
ACTIVITY_FIELD: str = "last_activity_at"

#: The three states of a listing item's ``last_activity_at`` (msg-6770 §1).
#: They are never mixed: "had a time", "had none" and "had something that
#: is not a time" are reported as such, so the wrapper can fail closed on
#: the last two without confusing them with each other. ``missing`` covers
#: both an absent key and a ``null`` / blank value; the absent-key case is
#: additionally COUNTED, not given a state of its own (msg-7445 F-3a).
TimeState = Literal["ok", "missing", "unparseable"]


@dataclass(frozen=True)
class ThreadTime:
    """The classified ``last_activity_at`` of one listing item.

    ``at`` is set (timezone-aware, UTC) only when ``state == "ok"``.
    """

    state: TimeState
    at: datetime | None = None


def classify_activity_at(raw: object) -> ThreadTime:
    """Classify a listing item's raw ``last_activity_at`` value. The ONE timestamp parser.

    Called only by the CLI's ``_list_live_threads`` (pinned by
    ``tests/test_unregistered_threads_cli.py``), so there is a single
    place where wire data becomes a time. Absent / ``None`` / blank is
    ``missing``; a non-string, a string ``datetime.fromisoformat`` rejects,
    or a time with no UTC offset (ambiguous, so not a time we can compare)
    is ``unparseable``.
    """
    if raw is None:
        return ThreadTime("missing")
    if not isinstance(raw, str):
        return ThreadTime("unparseable")
    text = raw.strip()
    if not text:
        return ThreadTime("missing")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ThreadTime("unparseable")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return ThreadTime("unparseable")
    return ThreadTime("ok", parsed.astimezone(UTC))


@dataclass(frozen=True)
class ListedThread:
    """One dict-shaped listing item plus its classified ``last_activity_at``."""

    thread: dict[str, Any]
    time: ThreadTime


@dataclass(frozen=True)
class UnregisteredThread:
    """One unregistered live thread as the wrapper sees it.

    ``idle_days`` is the time since ``last_activity_at`` in days (``None``
    unless ``time_state == "ok"``). The wrapper compares it against
    ``intake.max_idle_days`` and never parses a timestamp itself.
    """

    thread_id: str
    time_state: TimeState
    last_activity_at: str | None = None
    idle_days: float | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "time_state": self.time_state,
            ACTIVITY_FIELD: self.last_activity_at,
            "idle_days": self.idle_days,
        }


class UnregisteredThreadsError(ValueError):
    """The ``sweep.json`` payload is unusable for D-2 enumeration.

    Always fatal to the CLI: a D-2 report derived from a half-parsed
    ``sweep.json`` would produce false positives (missing "registered"
    entries) that look identical to real defects, and the count is the
    entire product of this script.
    """


@dataclass(frozen=True)
class RegisteredIndex:
    """The set of ``(project, thread_id)`` pairs listed in ``sweep.json``.

    Frozen because it is derived once per CLI run from the config file
    and passed to :func:`is_unregistered_live` for every candidate; a
    mutable version would let a downstream helper silently expand the
    registered set mid-enumeration.
    """

    #: Frozenset of ``(project, thread_id)`` tuples. Stored as a frozenset
    #: because membership is the only operation D-2 performs on this
    #: value; a list would force an O(N) scan per candidate.
    pairs: frozenset[tuple[str, str]]

    #: Ordered projects as they appeared in ``sweep.json``, with
    #: duplicates removed. The CLI iterates this to decide which
    #: projects to enumerate; preserving order keeps output stable
    #: across runs when the config has not been reordered.
    projects: tuple[str, ...]

    def contains(self, project: str, thread_id: str) -> bool:
        return (project, thread_id) in self.pairs


@dataclass(frozen=True)
class ProjectReport:
    """The D-2 verdict for one project.

    ``unregistered_count is None`` means "did not measure" — the MCP
    listing failed and the ``error`` field explains why. That is
    intentionally distinct from ``unregistered_count == 0``, which means
    "measured, and nothing is unregistered" (msg-2531 §2 invariant 2:
    "0 件" と "測れなかった" を同じ表示にしない).

    ``malformed_count`` is the third state msg-2648 §3 pins down:
    items the MCP call returned that were not shaped as JSON objects
    and therefore could not be evaluated. It is deliberately kept off
    ``unregistered_count`` — "unreadable" is not the same as "measured 0"
    or "measured N". ``malformed_count is None`` mirrors
    ``unregistered_count is None``: when the whole listing failed we do
    not know what we did not see, and the honest report is "unknown",
    not "zero".

    ``field_absent_count`` / ``listed_count`` are the schema-drift
    counter (msg-7443 F-3c as narrowed by msg-7445): of the
    ``listed_count`` dict-shaped items the listing returned for this
    project — ALL of them, counted before the registered filter, so a
    project whose every live thread is already in ``sweep.json`` is still
    checked — ``field_absent_count`` had no ``last_activity_at`` key at
    all. A key present with a ``null`` value is not counted. The wrapper
    reports ``N/M`` as a fact; nothing here infers what it means. Both are
    ``None`` when the listing failed, like ``malformed_count``.
    """

    project: str
    unregistered_count: int | None
    unregistered: tuple[str, ...] = ()
    error: str | None = None
    malformed_count: int | None = 0
    #: Per-thread detail for ``unregistered``, same order. Carries the
    #: time classification the wrapper's intake decision needs.
    threads: tuple[UnregisteredThread, ...] = ()
    field_absent_count: int | None = 0
    listed_count: int | None = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "unregistered_count": self.unregistered_count,
            "unregistered": list(self.unregistered),
            "threads": [t.as_json() for t in self.threads],
            "error": self.error,
            "malformed_count": self.malformed_count,
            "field_absent_count": self.field_absent_count,
            "listed_count": self.listed_count,
        }


@dataclass(frozen=True)
class EnumerateReport:
    """The whole-run D-2 report.

    ``unregistered_count_total`` is the sum of the per-project counts
    that WERE measured (Nones excluded). ``unmeasured_projects`` names
    the projects whose counts were not measured — this is the signal the
    wrapper uses to decide between rendering a number and rendering `?`
    (msg-2531 §2 invariant 2).

    ``malformed_count_total`` is the same sum for the third-state field
    (msg-2648 §3): the aggregate number of items the MCP calls returned
    but that could not be shaped as objects. When this is non-zero the
    matching-day count is **not** comparable to another day's count —
    the P-2 before/after check documented in msg-2531 §1 requires the
    two measurements to have enumerated the same population, and a
    garbled listing breaks that assumption without touching the count.
    """

    projects: tuple[ProjectReport, ...] = field(default_factory=tuple)

    @property
    def unregistered_count_total(self) -> int:
        return sum(p.unregistered_count for p in self.projects if p.unregistered_count is not None)

    @property
    def unmeasured_projects(self) -> tuple[str, ...]:
        return tuple(p.project for p in self.projects if p.unregistered_count is None)

    @property
    def any_unmeasured(self) -> bool:
        return any(p.unregistered_count is None for p in self.projects)

    @property
    def malformed_count_total(self) -> int:
        return sum(p.malformed_count for p in self.projects if p.malformed_count is not None)

    @property
    def any_malformed(self) -> bool:
        return any(p.malformed_count is not None and p.malformed_count > 0 for p in self.projects)

    def as_json(self) -> dict[str, Any]:
        return {
            "projects": [p.as_json() for p in self.projects],
            # The field name itself, so the PowerShell side words its WARN,
            # header and 時刻不明 reason from this one constant instead of
            # hardcoding it a second time (PR #497 gate advisory).
            "activity_field": ACTIVITY_FIELD,
            "unregistered_count_total": self.unregistered_count_total,
            "unmeasured_projects": list(self.unmeasured_projects),
            "any_unmeasured": self.any_unmeasured,
            "malformed_count_total": self.malformed_count_total,
            "any_malformed": self.any_malformed,
        }


def parse_registered(payload: Any) -> RegisteredIndex:
    """Parse ``sweep.json`` into a :class:`RegisteredIndex`.

    ``pairs`` comes from the ``candidates`` array. Only the two fields D-2
    needs (``project``, ``thread_id``) are validated here — ``repo_dir``
    is not, because a missing repo_dir is a real defect but not one this
    script exists to catch (the sweep wrapper already refuses to run on
    it, ``Get-SweepCandidates``). A candidate with a missing or blank
    ``project`` / ``thread_id`` is rejected at load time rather than
    silently dropped: a candidate that vanished from the registered set
    would be indistinguishable from an unregistered thread and would
    inflate the D-2 count. A missing ``candidates`` key is an empty list;
    a present non-array one is rejected.

    ``projects`` (the scan range) is the union, in this order, of the
    candidates' projects, the ``clones`` keys and the ``projects`` key
    (msg-6776 走査と受け付け). ``clones`` is validated for shape and
    rejected loudly (an object of non-blank project -> non-blank string),
    matching the wrapper, which throws on the same error. A malformed
    ``projects`` key widens nothing and is not an error here: the
    park-wake tick already names it in the digest as
    ``sweep.json:projects`` (``Get-SweepDeclaredProjects``).
    """
    if not isinstance(payload, dict):
        raise UnregisteredThreadsError("sweep.json root must be a JSON object")

    raw_candidates = payload.get("candidates", [])
    if not isinstance(raw_candidates, list):
        raise UnregisteredThreadsError("sweep.json 'candidates' must be a JSON array")

    pairs: set[tuple[str, str]] = set()
    projects: list[str] = []
    seen_projects: set[str] = set()

    def _add_project(name: str) -> None:
        if name not in seen_projects:
            seen_projects.add(name)
            projects.append(name)

    for index, raw in enumerate(raw_candidates):
        where = f"candidates[{index}]"
        if not isinstance(raw, dict):
            raise UnregisteredThreadsError(f"{where}: must be a JSON object")
        project = raw.get("project")
        thread_id = raw.get("thread_id")
        if not isinstance(project, str) or not project.strip():
            raise UnregisteredThreadsError(
                f"{where}: 'project' is required and must be a non-empty string"
            )
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise UnregisteredThreadsError(
                f"{where}: 'thread_id' is required and must be a non-empty string"
            )
        project_s = project.strip()
        thread_id_s = thread_id.strip()
        pairs.add((project_s, thread_id_s))
        _add_project(project_s)

    if "clones" in payload:
        clones = payload["clones"]
        if not isinstance(clones, dict):
            raise UnregisteredThreadsError(
                "sweep.json 'clones' must be a JSON object (project -> clone path)"
            )
        for key, value in clones.items():
            if not str(key).strip() or not isinstance(value, str) or not value.strip():
                raise UnregisteredThreadsError(
                    f"sweep.json 'clones' has a blank key or value: {key!r} => {value!r}"
                )
            _add_project(str(key).strip())

    declared = payload.get("projects")
    if isinstance(declared, list) and all(isinstance(p, str) and p.strip() for p in declared):
        for p in declared:
            _add_project(p.strip())

    return RegisteredIndex(pairs=frozenset(pairs), projects=tuple(projects))


def load_registered(path: Path) -> RegisteredIndex:
    """Read and parse the ``sweep.json`` at ``path``."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UnregisteredThreadsError(f"cannot read {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UnregisteredThreadsError(f"{path} is not valid JSON: {exc}") from exc
    return parse_registered(payload)


def is_unregistered_live(project: str, thread: dict[str, Any], registered: RegisteredIndex) -> bool:
    """Return ``True`` iff the thread is live, non-PR-review, and unregistered.

    Extracted as its own predicate so the ordering of the three filters
    is machine-checkable: any future edit that reshuffles them changes
    the answer for the same input and will fail a unit test rather than
    a production log at 03:00.
    """
    status = str(thread.get("status") or "").strip()
    if status not in LIVE_STATUSES:
        return False
    thread_id = str(thread.get("thread_id") or "").strip()
    if not thread_id:
        return False
    if thread_id.startswith(PR_REVIEW_PREFIX):
        return False
    return not registered.contains(project, thread_id)


def enumerate_project(
    project: str,
    threads: Iterable[ListedThread],
    registered: RegisteredIndex,
    *,
    malformed_count: int = 0,
    field_absent_count: int = 0,
    now: datetime | None = None,
) -> ProjectReport:
    """Apply :func:`is_unregistered_live` over ``threads`` and produce a report.

    ``threads`` is a well-shaped iterable of :class:`ListedThread` — a
    dict plus its already-classified ``last_activity_at``. Callers are
    responsible for filtering out non-object items and for classifying
    the time *before* handing the list here; the CLI's
    ``_list_live_threads`` does both at the tool boundary and passes the
    count of what it dropped through ``malformed_count``. This function
    neither re-filters nor parses a timestamp: the shape check and the
    time parse live at the one boundary that actually meets untrusted
    input (msg-3225 PR-gate ADVISORY on PR #282 — YAGNI / dual-management;
    msg-6770 §1 — one place interprets the time).

    A thread whose ``thread_id`` is missing, empty, or a falsy non-string
    value (e.g., ``0``, ``[]``, ``{}``) is ignored — the coercion below
    yields ``""``, and an empty id has nothing to compare against
    sweep.json. Truthy non-string values (e.g., ``123``, ``["x"]``) are
    coerced via :func:`str` and surface as their coerced form (``"123"``,
    ``"['x']"``) under ``unregistered``; the upstream tool contract
    expects ``thread_id`` to be a string, so any such coercion reflects a
    schema violation upstream.

    ``now`` (default: the current UTC time) is what ``idle_days`` is
    measured against. A thread whose time is not ``ok`` is still listed,
    with ``idle_days = None``: classifying is not dropping (msg-6778).

    ``malformed_count`` is the count of non-object items the caller
    already dropped upstream (msg-2648 §3 — the CLI's ``_list_live_threads``
    filters non-dicts before handing the accumulated list here). It flows
    through unchanged onto :attr:`ProjectReport.malformed_count` and is
    **never** merged into ``unregistered_count``.

    ``field_absent_count`` is the boundary's count of items with no
    ``last_activity_at`` key (msg-7445), passed through the same way; the
    ``listed_count`` beside it is the number of items in ``threads``, all
    of them, before any filter (the ``M`` of ``N/M``).
    """
    reference = now if now is not None else datetime.now(UTC)
    unregistered: list[str] = []
    details: list[UnregisteredThread] = []
    listed_count = 0
    for listed in threads:
        listed_count += 1
        thread = listed.thread
        if not is_unregistered_live(project, thread, registered):
            continue
        thread_id = str(thread.get("thread_id") or "").strip()
        if not thread_id:
            continue
        unregistered.append(thread_id)
        at = listed.time.at
        if listed.time.state == "ok" and at is not None:
            idle = max(0.0, (reference - at).total_seconds() / 86400.0)
            details.append(
                UnregisteredThread(
                    thread_id=thread_id,
                    time_state="ok",
                    last_activity_at=at.isoformat(),
                    idle_days=round(idle, 3),
                )
            )
        else:
            details.append(UnregisteredThread(thread_id=thread_id, time_state=listed.time.state))
    return ProjectReport(
        project=project,
        unregistered_count=len(unregistered),
        unregistered=tuple(unregistered),
        error=None,
        malformed_count=malformed_count,
        threads=tuple(details),
        field_absent_count=field_absent_count,
        listed_count=listed_count,
    )


def project_error_report(project: str, error: str) -> ProjectReport:
    """A :class:`ProjectReport` for a project whose enumeration failed.

    Distinct from ``enumerate_project(...)`` returning an empty list:
    this constructor forces ``unregistered_count = None`` and
    ``malformed_count = None`` and carries the reason. Callers
    converting an :class:`~spirrow_mindwire.magickit.client.MagickitMcpError`
    (or any transport failure) go through this helper so the "did not
    measure" signal is uniform. ``malformed_count`` is ``None`` here for
    the same reason ``unregistered_count`` is (and so are
    ``field_absent_count`` / ``listed_count``): when the whole listing
    call failed we do not know how many items came back garbled, and a
    zero would silently claim the population was cleanly enumerated.
    """
    return ProjectReport(
        project=project,
        unregistered_count=None,
        unregistered=(),
        error=error,
        malformed_count=None,
        field_absent_count=None,
        listed_count=None,
    )


__all__ = [
    "ACTIVITY_FIELD",
    "LIVE_STATUSES",
    "PR_REVIEW_PREFIX",
    "EnumerateReport",
    "ListedThread",
    "ProjectReport",
    "RegisteredIndex",
    "ThreadTime",
    "TimeState",
    "UnregisteredThread",
    "UnregisteredThreadsError",
    "classify_activity_at",
    "enumerate_project",
    "is_unregistered_live",
    "load_registered",
    "parse_registered",
    "project_error_report",
]
