"""The clone-retreat record: where a launch's WIP went, carried to that thread's next implementer.

Thread T-dirty-clone-wip-retreat-and-silent-stall-alerts, PR-1b (Bohr msg-8043 v1.6, revised by
msg-8045 v1.7; naysayer go from Einstein on the reply to msg-8045). PR-1a
(:mod:`spirrow_mindwire.clone_retreat`) retreats a dirty shared clone after a launch, but it tells
nobody but the tick log. This module closes msg-7637 D-1's second and third bullets: the next
implementer turn on the same thread can see the branch (or the patch) its predecessor's work is in.

**Option (b), no thread post (v1.7).** Nothing is posted into the chatroom thread. A post with no
``NEXT:`` at the head of the thread could change which persona the conductor routes to next, or
stall the thread (msg-8043 requirement 2); a carry-forward of the previous ``NEXT:`` risks the
relaunch loop of msg-2537 §4. Instead:

* ``mindwire clone-retreat`` appends one ``retreat`` row to
  ``<data_dir>/state/clone-retreats.jsonl`` per result that is not a no-op (``clean`` and
  ``not_eligible`` write nothing). The row is built from the same
  :class:`~spirrow_mindwire.clone_retreat.RetreatResult` the command prints to the wrapper; no git
  command runs a second time to fill it.
* The conductor, on the first implementer dispatch of a run, reads the thread's latest ``retreat``
  row that has no ``delivered`` row yet and attaches :func:`render_notice` to that dispatch's event
  (:attr:`~spirrow_mindwire.value_objects.ChatroomEvent.clone_retreat_notice`), which
  :func:`~spirrow_mindwire.thread_context.build_turn_prompt` renders as its own prompt block. Once
  the dispatch returns, a ``delivered`` row is appended, so the record reaches the *next*
  implementer turn and not every later one. A dispatch that raised leaves it pending, so a retry
  gets it again.

Reader and fallback surface (OBL-CHATROOM-PRODUCER-READER-SURFACE): this module posts into no
chatroom thread. The reader is the thread's next implementer session, reached through its prompt;
the durable surface is the JSONL file itself (and PR-1a's tick-log row), so a resolved thread has
nothing to fall back from.

Writing and reading are observation, never gating: a failure is a WARNING and the conductor routes
exactly as it would without the file. The routing reads nothing from here (a test pins it).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import CLONE_RETREAT_LOG_FILENAME

if TYPE_CHECKING:
    from .clone_retreat import RetreatResult

logger = logging.getLogger(__name__)

RETREAT_KIND = "retreat"
DELIVERED_KIND = "delivered"
# clone_retreat.RetreatOutcome values that are a no-op: the clone was left alone, nothing to tell.
NO_OP_OUTCOMES = frozenset({"clean", "not_eligible"})
NOTICE_TAG = "[退避の記録 / clone-retreat record]"


def _now_iso(at: datetime | None) -> str:
    return (at or datetime.now(UTC)).astimezone(UTC).isoformat()


def _append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    prefix = "" if _ends_with_newline_or_empty(path) else "\n"
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(prefix + line + "\n")


def _ends_with_newline_or_empty(path: Path) -> bool:
    # A torn earlier write must stay one malformed line, not swallow the next row.
    if not path.exists():
        return True
    with path.open("rb") as fh:
        fh.seek(0, 2)
        if fh.tell() == 0:
            return True
        fh.seek(-1, 2)
        return fh.read(1) == b"\n"


def build_record(
    result: RetreatResult,
    *,
    repo_dir: Path,
    head_msg: str,
    stop_reason: str,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """The ``retreat`` row for ``result``, or ``None`` for a no-op outcome."""
    outcome = result.outcome.value
    if outcome in NO_OP_OUTCOMES:
        return None
    stamp = _now_iso(at)
    return {
        "kind": RETREAT_KIND,
        # The uuid suffix keeps two records of one thread distinct when the clock gives them the
        # same timestamp (Windows' coarse clock does, within one tick), so a newer record is
        # never mistaken for an already-delivered one.
        "record_id": f"{result.thread_id}@{stamp}#{uuid.uuid4().hex[:12]}",
        "at": stamp,
        "thread_id": result.thread_id,
        "repo_dir": str(repo_dir),
        "head_msg": head_msg or "-",
        "stop_reason": stop_reason or "-",
        "outcome": outcome,
        "pushed": result.pushed,
        "branch": result.branch,
        "files": result.files,
        "insertions": result.insertions,
        "deletions": result.deletions,
        "patch": result.patch,
        "untracked": result.untracked,
        "marker": result.marker,
        "detail": result.detail,
    }


def record_result(
    path: Path,
    result: RetreatResult,
    *,
    repo_dir: Path,
    head_msg: str,
    stop_reason: str,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """Append the ``retreat`` row for ``result``; return it, or ``None`` when it was a no-op."""
    row = build_record(result, repo_dir=repo_dir, head_msg=head_msg, stop_reason=stop_reason, at=at)
    if row is not None:
        _append(path, row)
    return row


PRESERVED_OUTCOME = "preserved"


def build_preserved_record(
    *,
    thread_id: str,
    repo_dir: Path,
    branch: str | None,
    preserved_ref: str,
    preserved_sha: str,
    recovered: bool,
    detail: str = "",
    at: datetime | None = None,
) -> dict[str, Any]:
    """The ``retreat`` row for ``mindwire clone-recover --preserve`` (T-role-exit-leaves-clone-off-
    default-branch PR-A, msg-8698 §3): a clean clone off the default branch whose unpushed commits
    were kept on the local ref ``preserved_ref``.

    ``outcome`` is ``preserved`` when the clone is back on the default branch, and ``failed`` when
    the ref was made but the switch back failed. In that case :func:`_opening_state` sends the
    implementer to inspect the clone first. The row has the PR-1b keys, so the wrapper's readers
    (parking alert line, discard alerts that select ``discarded`` / ``discard_incomplete`` only)
    read it unchanged."""
    stamp = _now_iso(at)
    return {
        "kind": RETREAT_KIND,
        "record_id": f"{thread_id}@{stamp}#{uuid.uuid4().hex[:12]}",
        "at": stamp,
        "thread_id": thread_id,
        "repo_dir": str(repo_dir),
        "head_msg": "-",
        "stop_reason": "-",
        "outcome": PRESERVED_OUTCOME if recovered else "failed",
        "pushed": False,
        "branch": branch,
        "files": None,
        "insertions": None,
        "deletions": None,
        "patch": None,
        "untracked": None,
        "marker": None,
        "detail": detail,
        "preserved_ref": preserved_ref,
        "preserved_sha": preserved_sha,
    }


def record_preserved(path: Path, **kw: Any) -> dict[str, Any]:
    """Append :func:`build_preserved_record` to ``path`` and return the row."""
    row = build_preserved_record(**kw)
    _append(path, row)
    return row


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("record_id"), str):
                rows.append(row)
    return rows


def pending_record(path: Path, thread_id: str) -> dict[str, Any] | None:
    """The thread's latest ``retreat`` row, unless a ``delivered`` row names it already."""
    latest: dict[str, Any] | None = None
    delivered: set[str] = set()
    for row in _rows(path):
        if row.get("thread_id") != thread_id:
            continue
        if row.get("kind") == RETREAT_KIND:
            latest = row
        elif row.get("kind") == DELIVERED_KIND:
            delivered.add(row["record_id"])
    if latest is None or latest["record_id"] in delivered:
        return None
    return latest


def mark_delivered(
    path: Path, record: dict[str, Any], *, session_role: str, at: datetime | None = None
) -> None:
    _append(
        path,
        {
            "kind": DELIVERED_KIND,
            "record_id": record["record_id"],
            "thread_id": record.get("thread_id"),
            "at": _now_iso(at),
            "role": session_role,
        },
    )


def _outcome_line(record: dict[str, Any]) -> str:
    outcome = record.get("outcome")
    if outcome == "retreated":
        pushed = (
            "pushed to origin"
            if record.get("pushed")
            else "NOT pushed: the branch exists only in this clone"
        )
        return f"retreated to a wip/ branch ({pushed})"
    if outcome == "discarded":
        return "discarded after exporting a patch (the retreat could not run)"
    if outcome == "discard_incomplete":
        return (
            f"discard incomplete (marker {record.get('marker') or '-'} still present; "
            f"the clone stays parked)"
        )
    return f"{outcome} ({record.get('detail') or 'no detail'})"


# Outcomes after which the clone is known to be clean again (the cleanup completed).
CLEANED_OUTCOMES = frozenset({"retreated", "discarded"})


def _opening_state(record: dict[str, Any]) -> str:
    """What the notice may assert about the clone: only a completed cleanup says the work is gone.

    For ``failed`` / ``discard_incomplete`` (or any outcome this module does not know) the
    cleanup was only attempted, so the clone may still be on the wip/ branch or still hold the
    changes: the notice says so and sends the implementer to inspect the clone first.
    """
    if record.get("outcome") in CLEANED_OUTCOMES:
        return (
            "The wrapper cleaned it up after that launch, so your work from then is not in the "
            "working tree; it is saved where the lines below say."
        )
    return (
        "The wrapper attempted to clean it up after that launch, but the cleanup did not "
        "complete, so the clone may still be on another branch or still hold that work. "
        "Inspect the current clone state (`git status`, `git branch --show-current`, "
        "`git log -1`) before recovering anything below."
    )


def _render_preserved(record: dict[str, Any]) -> str:
    """The notice for a ``clone-recover --preserve`` row: the commits are on a local ``wip/`` ref
    of this clone (not pushed), named with its sha."""
    ref = record.get("preserved_ref")
    sha = record.get("preserved_sha")
    if record.get("outcome") == PRESERVED_OUTCOME:
        opening = (
            "A previous launch on this thread ended with the shared clone off the default branch, "
            "holding commits that were not on the remote. The wrapper kept them on a local ref "
            "and switched the clone back to the default branch."
        )
    else:
        opening = (
            "A previous launch on this thread ended with the shared clone off the default branch, "
            "holding commits that were not on the remote. The wrapper kept them on a local ref, "
            "but switching back to the default branch did not complete, so the clone may still "
            "be on that branch. Inspect the current clone state (`git status`, "
            "`git branch --show-current`, `git log -1`) first."
        )
    lines = [
        f"{NOTICE_TAG} {opening}",
        f"- when: {record.get('at')}",
        f"- repo: {record.get('repo_dir')}",
        f"- outcome: {record.get('outcome')} ({record.get('detail') or 'no detail'})",
    ]
    if record.get("branch"):
        lines.append(
            f"- original branch: `{record['branch']}` (deleted locally; name is free again)"
        )
    lines.append(
        f"- commits: in this clone's local ref `{ref}` at `{sha}` "
        "(NOT pushed: it exists only in this clone)"
    )
    lines.append(
        f"Check that ref before redoing the work: `git switch -c <your branch> {ref}` or "
        "cherry-pick from it."
    )
    return "\n".join(lines)


def render_notice(record: dict[str, Any]) -> str:
    """The prompt block for one ``retreat`` row. Information for the turn, not a rule."""
    if record.get("preserved_ref"):
        return _render_preserved(record)
    lines = [
        f"{NOTICE_TAG} A previous launch on this thread left the shared clone dirty. "
        f"{_opening_state(record)}",
        f"- when: {record.get('at')}  head msg: {record.get('head_msg')}  "
        f"stop reason: {record.get('stop_reason')}",
        f"- repo: {record.get('repo_dir')}",
        f"- outcome: {_outcome_line(record)}",
    ]
    branch = record.get("branch")
    if branch:
        lines.append(
            f"- branch: `{branch}` - {record.get('files')} files, "
            f"+{record.get('insertions')}/-{record.get('deletions')} lines"
        )
    if record.get("patch"):
        lines.append(f"- patch: {record['patch']}")
    if record.get("untracked"):
        lines.append(f"- untracked list: {record['untracked']}")
    if branch:
        lines.append(
            "Check that branch before redoing the work: it holds the uncommitted changes as one "
            "wrapper commit you can merge or cherry-pick into your feature branch."
        )
    elif record.get("patch"):
        lines.append(
            "Check that patch before redoing the work: `git apply` it on a branch to recover the "
            "changes; untracked files are listed but were not saved."
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class PendingNotice:
    record: dict[str, Any]
    text: str


def load_pending_notice(path: Path | None, thread_id: str) -> PendingNotice | None:
    """:func:`pending_record` + :func:`render_notice`; a read failure is a WARNING and ``None``."""
    if path is None:
        return None
    try:
        record = pending_record(path, thread_id)
    except Exception:
        logger.warning("clone-retreat log: reading %s failed", path, exc_info=True)
        return None
    if record is None:
        return None
    return PendingNotice(record=record, text=render_notice(record))


__all__ = [
    "CLEANED_OUTCOMES",
    "CLONE_RETREAT_LOG_FILENAME",
    "DELIVERED_KIND",
    "NOTICE_TAG",
    "NO_OP_OUTCOMES",
    "PRESERVED_OUTCOME",
    "RETREAT_KIND",
    "PendingNotice",
    "build_preserved_record",
    "build_record",
    "load_pending_notice",
    "mark_delivered",
    "pending_record",
    "record_preserved",
    "record_result",
    "render_notice",
]
