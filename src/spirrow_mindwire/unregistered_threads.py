"""D-2 log-only enumerator for "unregistered" chatroom threads.

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

**Log-only until P-1 ∧ P-2 hold** (msg-2531 §1). This module and its CLI
produce a JSON report; wiring the count into the daily digest is BLOCKED
until magickit surfaces a ``parked`` transition (P-1) and the eight
intentionally-parked threads Bohr enumerated in msg-2529 §3 have been
moved to ``status=parked`` (P-2). Configuring the digest to render this
count today would ship an alarm with a known-fixed 31 % false-positive
rate (msg-2530 point 2), and an alarm ignored on day one becomes an
alarm ignored on every subsequent day — exactly the failure the
msg-1181 audit named "silent stop, then silent noise".

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
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Chatroom statuses that count as "live work in flight" for D-2.
#: Kept string-value-identical to
#: :data:`spirrow_mindwire.gate_bootstrap._OPEN_ALERT_TARGET_STATUSES`
#: (Bohr msg-2460 §4). Deliberately excluded: ``resolved`` (done),
#: ``superseded`` (replaced), ``parked`` (intentionally set aside — the
#: whole reason D-2 depends on P-1 landing first).
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
    """

    project: str
    unregistered_count: int | None
    unregistered: tuple[str, ...] = ()
    error: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "unregistered_count": self.unregistered_count,
            "unregistered": list(self.unregistered),
            "error": self.error,
        }


@dataclass(frozen=True)
class EnumerateReport:
    """The whole-run D-2 report.

    ``unregistered_count_total`` is the sum of the per-project counts
    that WERE measured (Nones excluded). ``unmeasured_projects`` names
    the projects whose counts were not measured — this is the signal the
    wrapper uses to decide between rendering a number and rendering `?`
    (msg-2531 §2 invariant 2).
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

    def as_json(self) -> dict[str, Any]:
        return {
            "projects": [p.as_json() for p in self.projects],
            "unregistered_count_total": self.unregistered_count_total,
            "unmeasured_projects": list(self.unmeasured_projects),
            "any_unmeasured": self.any_unmeasured,
        }


def parse_registered(payload: Any) -> RegisteredIndex:
    """Parse the ``candidates`` array of ``sweep.json`` into a :class:`RegisteredIndex`.

    Only the two fields D-2 needs (``project``, ``thread_id``) are
    validated here — ``repo_dir`` is not, because a missing repo_dir is
    a real defect but not one this script exists to catch (the sweep
    wrapper already refuses to run on it, ``run-conductor-scheduled.ps1``
    L2341-2345). A candidate with a missing or blank ``project`` /
    ``thread_id`` is rejected at load time rather than silently dropped:
    a candidate that vanished from the registered set would be
    indistinguishable from an unregistered thread and would inflate the
    D-2 count.
    """
    if not isinstance(payload, dict):
        raise UnregisteredThreadsError("sweep.json root must be a JSON object")

    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise UnregisteredThreadsError("sweep.json 'candidates' must be a JSON array")

    pairs: set[tuple[str, str]] = set()
    projects: list[str] = []
    seen_projects: set[str] = set()
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
        if project_s not in seen_projects:
            seen_projects.add(project_s)
            projects.append(project_s)

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
    project: str, threads: Iterable[dict[str, Any]], registered: RegisteredIndex
) -> ProjectReport:
    """Apply :func:`is_unregistered_live` over ``threads`` and produce a report.

    ``threads`` is the raw ``items`` array from ``chatroom_list_threads``.
    A thread whose ``thread_id`` is missing or non-string is ignored (it
    cannot be a "known thread" the sweep is missing — no id to compare
    against), matching the shape-tolerance :mod:`parked_humans` applies
    to head-cross-check candidates.
    """
    unregistered: list[str] = []
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        if not is_unregistered_live(project, thread, registered):
            continue
        thread_id = str(thread.get("thread_id") or "").strip()
        if thread_id:
            unregistered.append(thread_id)
    return ProjectReport(
        project=project,
        unregistered_count=len(unregistered),
        unregistered=tuple(unregistered),
        error=None,
    )


def project_error_report(project: str, error: str) -> ProjectReport:
    """A :class:`ProjectReport` for a project whose enumeration failed.

    Distinct from ``enumerate_project(...)`` returning an empty list:
    this constructor forces ``unregistered_count = None`` and carries
    the reason. Callers converting an :class:`~spirrow_mindwire.magickit
    .client.MagickitMcpError` (or any transport failure) go through this
    helper so the "did not measure" signal is uniform.
    """
    return ProjectReport(
        project=project,
        unregistered_count=None,
        unregistered=(),
        error=error,
    )


__all__ = [
    "LIVE_STATUSES",
    "PR_REVIEW_PREFIX",
    "EnumerateReport",
    "ProjectReport",
    "RegisteredIndex",
    "UnregisteredThreadsError",
    "enumerate_project",
    "is_unregistered_live",
    "load_registered",
    "parse_registered",
    "project_error_report",
]
