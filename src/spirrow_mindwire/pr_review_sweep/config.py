"""Loader for ``pr_review_sweep.json`` — the sweep's two operator-supplied tables.

The file's own ``_comment`` block is the human-facing specification; see
``deploy/pr_review_sweep.json.example``. This module is the machine half of the same
contract, and the two constraints the example promises are enforced HERE, at load
time, because promising them in prose and checking them nowhere is precisely the
defect the gate caught on head ``0d9b205``:

1. ``justification`` is required and must not be a placeholder. An operator who
   cannot cite evidence for the gate-activation date must OMIT the whole
   ``gate_active_since`` key, which the sweep reads as UNDETERMINED. Writing ``""``
   to get past the check is refused rather than silently accepted, so the audit
   trail cannot be laundered into existence.
2. ``date`` is ``YYYY-MM-DD`` exactly. Not ``YYYY-MM``, not an ISO timestamp — a
   day-precision calendar date, because the comparison it feeds (``terminal_at >=
   date``) needs a well-defined boundary. The operator's *knowledge* is usually
   only month-grained (msg-2174 R-39); that is a reason to document the chosen day
   in ``justification``, not a reason to widen the format (msg-2177 R-42(1)).

**The placeholder vocabulary is the documented one, not a heuristic.** It rejects
blank/whitespace plus the exact tokens the example file names. It deliberately does
NOT try to judge whether a non-placeholder justification is *substantive* — "cites a
PR number, commit SHA, or msg-id" is a property no string check can decide, and a
regex approximating it would reject valid prose while passing ``"see the PR"``. The
residual is stated rather than papered over: a determined operator can still write
``"x"``. What the check buys is that the *default* failure mode — leaving the field
empty, or pasting the example's own empty shape — is refused.

Note what is NOT a schema field: ``thread_prefix``. It is derived from ``project``
via :func:`thread_prefix_for`, because the prefix is a chatroom-internal naming
convention, whereas ``owner``/``repo`` name a GitHub tuple the sweep genuinely
cannot infer (msg-2162 field ③ forbids guessing that one).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

#: The only schema version this loader understands. A file declaring anything else is
#: refused rather than read on a best-effort basis: the fields carry operator-audited
#: facts, and reading a future shape "as far as we can" would silently drop whichever
#: field the new version added.
SUPPORTED_SCHEMA_VERSION = 1

#: ``T-pr-review-<project>-<n>`` (msg-2162). The trailing hyphen is part of the prefix
#: so ``T-pr-review-spirrow-mindwire-`` cannot also match a project named
#: ``spirrow-mindwire-legacy``.
_THREAD_PREFIX_TEMPLATE = "T-pr-review-{project}-"

#: Rejected verbatim (case-insensitive, after stripping) as ``justification``. This is
#: the list the example file publishes, not an invented superset — see the module
#: docstring on why no cleverer test is attempted.
_PLACEHOLDER_JUSTIFICATIONS = frozenset({"", "unknown", "推定"})

#: ``YYYY-MM-DD`` and nothing else. ``date.fromisoformat`` alone is too permissive on
#: Python 3.11+ (it accepts ``YYYYMMDD`` and full ISO 8601 datetimes), and accepting
#: those would let a config drift away from the format the example documents.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SweepConfigError(ValueError):
    """The config file is unusable.

    Always fatal to the run. A sweep that proceeds on a half-understood config would
    report a go/no-go number derived from a mapping nobody validated, and the number
    is the entire product of Phase 0.
    """


def thread_prefix_for(project: str) -> str:
    """The chatroom thread-id prefix for a project's PR-review threads."""
    return _THREAD_PREFIX_TEMPLATE.format(project=project)


@dataclass(frozen=True)
class GateActiveSince:
    """When the PR gate first ran against merges in a repo, plus the evidence for it.

    Absent (``None`` on :class:`ProjectEntry`) means UNDETERMINED, which is a legal
    and expected state — not a defect to be filled in with a guess.
    """

    date: date
    justification: str


@dataclass(frozen=True)
class ProjectEntry:
    """One project's row: the chatroom slug, the GitHub tuple, and Table 2."""

    project: str
    owner: str
    repo: str
    gate_active_since: GateActiveSince | None = None

    @property
    def thread_prefix(self) -> str:
        return thread_prefix_for(self.project)

    def pr_number_for_thread(self, thread_id: str) -> int | None:
        """``T-pr-review-<project>-42`` -> ``42``, or ``None`` when it is not one.

        Returning ``None`` (rather than raising) is what lets the caller treat a
        non-matching thread as "not a PR-review thread" and a matching-but-malformed
        one as a skip. That is the S0 fail-open direction: never fabricate a PR
        number, never abort the whole sweep over one odd thread id.
        """
        prefix = self.thread_prefix
        if not thread_id.startswith(prefix):
            return None
        tail = thread_id[len(prefix) :]
        # ``str.isdigit`` is True for non-ASCII digits (fullwidth forms), which ``int()``
        # then happily parses into a number that is not what the thread id says.
        if not tail or not tail.isascii() or not tail.isdigit():
            return None
        return int(tail)


@dataclass(frozen=True)
class SweepConfig:
    """The parsed file. Ordered as written so the sweep's output order is stable."""

    entries: tuple[ProjectEntry, ...]

    def entry_for_project(self, project: str) -> ProjectEntry | None:
        for entry in self.entries:
            if entry.project == project:
                return entry
        return None


def _require_str(raw: Any, field: str, where: str) -> str:
    value = raw.get(field) if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise SweepConfigError(f"{where}: '{field}' is required and must be a non-empty string")
    return value.strip()


def _parse_gate_active_since(raw: Any, where: str) -> GateActiveSince:
    if not isinstance(raw, dict):
        raise SweepConfigError(f"{where}: 'gate_active_since' must be an object (or be omitted)")

    raw_date = raw.get("date")
    if not isinstance(raw_date, str) or not _DATE_RE.match(raw_date.strip()):
        raise SweepConfigError(
            f"{where}: 'gate_active_since.date' must be YYYY-MM-DD, got {raw_date!r}"
        )
    try:
        parsed = date.fromisoformat(raw_date.strip())
    except ValueError as exc:  # e.g. 2025-02-30 — well-formed but not a real day
        raise SweepConfigError(
            f"{where}: 'gate_active_since.date' is not a real date: {exc}"
        ) from exc

    raw_justification = raw.get("justification")
    justification = raw_justification.strip() if isinstance(raw_justification, str) else ""
    if justification.casefold() in _PLACEHOLDER_JUSTIFICATIONS:
        raise SweepConfigError(
            f"{where}: 'gate_active_since.justification' is a placeholder "
            f"({raw_justification!r}). Cite the evidence, or OMIT the whole "
            f"'gate_active_since' key to declare UNDETERMINED."
        )
    return GateActiveSince(date=parsed, justification=justification)


def parse_sweep_config(payload: Any) -> SweepConfig:
    """Validate an already-decoded JSON payload."""
    if not isinstance(payload, dict):
        raise SweepConfigError("config root must be a JSON object")

    version = payload.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise SweepConfigError(
            f"schema_version must be {SUPPORTED_SCHEMA_VERSION}, got {version!r}"
        )

    raw_projects = payload.get("projects")
    if not isinstance(raw_projects, list) or not raw_projects:
        raise SweepConfigError("'projects' must be a non-empty JSON array")

    entries: list[ProjectEntry] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_projects):
        where = f"projects[{index}]"
        if not isinstance(raw, dict):
            raise SweepConfigError(f"{where}: must be a JSON object")
        project = _require_str(raw, "project", where)
        # The gate's advisory on head d718ebc: an array cannot enforce key uniqueness
        # the way an object would, so a duplicate slug would leave the loader to pick
        # one arbitrarily. It does not pick — it refuses.
        if project in seen:
            raise SweepConfigError(f"{where}: duplicate project {project!r}")
        seen.add(project)
        gate_raw = raw.get("gate_active_since")
        entries.append(
            ProjectEntry(
                project=project,
                owner=_require_str(raw, "owner", where),
                repo=_require_str(raw, "repo", where),
                gate_active_since=(
                    None if gate_raw is None else _parse_gate_active_since(gate_raw, where)
                ),
            )
        )
    return SweepConfig(entries=tuple(entries))


def load_sweep_config(path: Path) -> SweepConfig:
    """Read and validate the config file at ``path``."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SweepConfigError(f"cannot read {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SweepConfigError(f"{path} is not valid JSON: {exc}") from exc
    return parse_sweep_config(payload)


__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "GateActiveSince",
    "ProjectEntry",
    "SweepConfig",
    "SweepConfigError",
    "load_sweep_config",
    "parse_sweep_config",
    "thread_prefix_for",
]
