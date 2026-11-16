"""The local-branch state an implementer turn started from, saved so the CONTINUE check can tell
"this turn's unpushed work" from "some other thread's stale branch".

Thread T-check-pushed-misreads-narrow-fetch-refspec-and-stale-branches, design v2.2 (Bohr
msg-9354, endorsed by Einstein in the go that followed it). ``CloneGuard.check_pushed`` used to
refuse every continuation on a clone while *any* local branch held unpushed commits, so one
months-old branch stopped every thread that used the clone (defect 2, Fermi msg-9285). The
boundary between "before this turn" and "this turn" is a ref state, never a time: committer dates
can be set, preserved by a rebase, or old on a branch newly pointed at an old commit (Einstein
msg-9349 Obj-1).

**Record (D-2' 1).** Before every implementer dispatch the dispatcher reads ``refs/heads`` →
sha (:meth:`~spirrow_mindwire.clone_guard.CloneGuard.ref_snapshot`) and :meth:`record` s it under
``(project, thread_id, trigger message id, repo_dir)``. The adapter starts only after the write
succeeded; a failure refuses the dispatch (exit 8), so a turn that ran always has a snapshot. A
key that already has a snapshot is never overwritten: a relaunch on the same trigger keeps the
first launch's state as its boundary, so a failed attempt's unpushed commit is not absorbed into
the baseline (msg-9351 Obj-1).

**Look up (D-2' 2).** :meth:`baseline_for` takes the trigger message the conductor identified for
the CONTINUE turn and returns that exact snapshot. It never substitutes another launch's snapshot:
refs can move back and forth (A→B→A), so an older baseline is not a safe stand-in (msg-9353
Obj-1). Missing snapshot, no identified trigger, an unreadable file, or a snapshot recorded on a
message between the trigger and the CONTINUE (the identification disagrees with what the
dispatcher saw) all return ``None``: judge every branch, as before this module existed.

Storage: ``<state_dir>/turn_ref_snapshots/<project>/<thread_id>.json``, the last
:data:`RETAIN_PER_THREAD` launches per thread, written atomically (temp file + ``os.replace``).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

SNAPSHOT_DIRNAME = "turn_ref_snapshots"
RETAIN_PER_THREAD = 8
SCHEMA_VERSION = 1


class SnapshotStoreError(Exception):
    """The snapshot could not be recorded; the implementer dispatch must not start."""


@dataclass(frozen=True)
class ContinuationScope:
    """What the conductor knows about the CONTINUE turn whose clone is being judged.

    ``trigger_msg_id`` is the message the conductor identified as the one that launched the turn
    that declared CONTINUE (``None`` when it found none). ``between_msg_ids`` are the messages
    after that trigger and before the CONTINUE report; a snapshot keyed on any of them means the
    identification disagrees with what the dispatcher recorded.
    """

    project: str
    thread_id: str
    trigger_msg_id: str | None
    between_msg_ids: tuple[str, ...] = field(default=())


def _norm_repo(repo_dir: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(repo_dir)))


def _component(value: str) -> str:
    # Percent-encoding is injective, so two distinct ids never share a file.
    return quote(value, safe="") or "_"


class TurnRefSnapshotStore:
    """Per-thread JSON files of ``{refname: sha}`` keyed by trigger message id and repo_dir."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, project: str, thread_id: str) -> Path:
        return self._root / _component(project) / f"{_component(thread_id)}.json"

    # -- write ----------------------------------------------------------------------------------

    def record(
        self,
        *,
        project: str,
        thread_id: str,
        trigger_msg_id: str,
        repo_dir: Path | str,
        refs: Mapping[str, str],
        now: datetime | None = None,
    ) -> bool:
        """Save ``refs`` for this launch; ``False`` = a snapshot for the key already existed.

        Raises :class:`SnapshotStoreError` when the snapshot is not on disk afterwards for any
        reason, including an existing file that cannot be read: rewriting it from scratch could
        drop this key's first snapshot and move the boundary later.
        """
        if not trigger_msg_id:
            raise SnapshotStoreError("no trigger message id to key the snapshot on")
        path = self.path_for(project, thread_id)
        repo = _norm_repo(repo_dir)
        try:
            entries = self._read_entries(path) if path.exists() else []
        except (OSError, ValueError) as exc:
            raise SnapshotStoreError(f"cannot read {path}: {exc}") from exc
        if any(e["trigger_msg_id"] == trigger_msg_id and e["repo_dir"] == repo for e in entries):
            return False
        entries.append(
            {
                "trigger_msg_id": trigger_msg_id,
                "repo_dir": repo,
                "recorded_at": (now or datetime.now(UTC)).isoformat(),
                "refs": dict(sorted(refs.items())),
            }
        )
        entries = entries[-RETAIN_PER_THREAD:]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"version": SCHEMA_VERSION, "entries": entries}, fh, indent=1)
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        except OSError as exc:
            raise SnapshotStoreError(f"cannot write {path}: {exc}") from exc
        return True

    # -- read -----------------------------------------------------------------------------------

    def baseline_for(
        self, scope: ContinuationScope, repo_dir: Path | str
    ) -> Mapping[str, str] | None:
        """The launch snapshot to diff against; ``None`` = judge every branch (see module doc)."""
        if scope.trigger_msg_id is None:
            logger.warning(
                "turn ref snapshot: no implementer trigger found for %s/%s; judging all branches",
                scope.project,
                scope.thread_id,
            )
            return None
        path = self.path_for(scope.project, scope.thread_id)
        try:
            entries = self._read_entries(path) if path.exists() else []
        except (OSError, ValueError) as exc:
            logger.warning(
                "turn ref snapshot: cannot read %s (%s); judging all branches", path, exc
            )
            return None
        repo = _norm_repo(repo_dir)
        mine = {e["trigger_msg_id"]: e["refs"] for e in entries if e["repo_dir"] == repo}
        clash = [m for m in scope.between_msg_ids if m in mine]
        if clash:
            logger.warning(
                "turn ref snapshot: snapshot on %s, after the identified trigger %s and before the "
                "CONTINUE (%s/%s); identification disagrees, judging all branches",
                ",".join(clash),
                scope.trigger_msg_id,
                scope.project,
                scope.thread_id,
            )
            return None
        refs = mine.get(scope.trigger_msg_id)
        if refs is None:
            logger.warning(
                "turn ref snapshot: none for trigger %s (%s/%s); judging all branches",
                scope.trigger_msg_id,
                scope.project,
                scope.thread_id,
            )
            return None
        return dict(refs)

    @staticmethod
    def _read_entries(path: Path) -> list[dict[str, Any]]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            raise ValueError("unexpected snapshot file shape or version")
        raw = data.get("entries")
        if not isinstance(raw, list):
            raise ValueError("entries is not a list")
        out: list[dict[str, Any]] = []
        for e in raw:
            if (
                not isinstance(e, dict)
                or not isinstance(e.get("trigger_msg_id"), str)
                or not isinstance(e.get("repo_dir"), str)
                or not isinstance(e.get("refs"), dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in e["refs"].items())
            ):
                raise ValueError("malformed snapshot entry")
            out.append(e)
        return out


__all__ = [
    "RETAIN_PER_THREAD",
    "SNAPSHOT_DIRNAME",
    "ContinuationScope",
    "SnapshotStoreError",
    "TurnRefSnapshotStore",
]
