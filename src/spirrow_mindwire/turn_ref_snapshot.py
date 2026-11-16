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
succeeded; a failure refuses the dispatch (exit 12, see below), so a turn that ran always has a
snapshot. A
key that already has a snapshot is never overwritten: a relaunch on the same trigger keeps the
first launch's state as its boundary, so a failed attempt's unpushed commit is not absorbed into
the baseline (msg-9351 Obj-1). That holds past retention too: a key dropped by the
:data:`RETAIN_PER_THREAD` bound is remembered as a tombstone (ids only, never pruned), and a
relaunch on a tombstoned key records nothing, so its continuation judges every branch instead of
diffing against a replacement baseline that already contains the first attempt's unpushed work
(PR #581 gate round 1, Obj-1).

**Look up (D-2' 2).** :meth:`baseline_for` takes the trigger message the conductor identified for
the CONTINUE turn and returns that exact snapshot. It never substitutes another launch's snapshot:
refs can move back and forth (A→B→A), so an older baseline is not a safe stand-in (msg-9353
Obj-1). Missing snapshot, no identified trigger, an unreadable file, or a snapshot recorded on a
message between the trigger and the CONTINUE (the identification disagrees with what the
dispatcher saw) all return ``None``: judge every branch, as before this module existed.

Storage: ``<state_dir>/turn_ref_snapshots/<project>/<thread_id>.json``, the last
:data:`RETAIN_PER_THREAD` launches per thread, plus the ``evicted`` tombstone list of
``[trigger_msg_id, repo_dir]`` keys (optional on read, so a file written before tombstones
existed still loads), written atomically (temp file + ``os.replace``).

**Unreadable file (T-ref-snapshot-unreadable-file-blocks-thread-forever, design v2 Bohr msg-9433,
go Einstein in the message that followed it).** A file that parses wrong (bad JSON, unknown
version, malformed shape) used to refuse every later launch of its thread until a person deleted
it. :meth:`record` now recovers it in two steps, neither of which deletes or overwrites anything
but the original path:

1. a byte-for-byte copy of the unreadable file is created exclusively (``open(..., "xb")`` +
   fsync) as ``<thread>.json.unreadable-<UTC yyyymmddThhmmssZ>[-<n>]``; a name that exists gets
   the next ``-<n>`` (at most :data:`UNREADABLE_COPY_MAX_TRIES` names);
2. the original path is atomically replaced (temp file + ``os.replace``) by a **degraded** file,
   ``{"version": DEGRADED_SCHEMA_VERSION, "degraded": true, "reset_at", "unreadable_copy"}``.

Wherever a crash stops this, the original path holds either the original unreadable file (so the
next launch recovers again) or the finished degraded file; it is never missing, so a lost history
is never mistaken for "first launch, make a baseline". A degraded thread records nothing from then
on and :meth:`baseline_for` returns ``None`` for every key: every CONTINUE of the thread judges
every branch, as before PR #581, whatever order old and new triggers are retried in (Einstein
msg-9432 Obj-1). The degraded version is one above :data:`SCHEMA_VERSION`, so code that predates
it refuses the file instead of recording a baseline on top of it. The recovery is logged as a
``notice:`` line at INFO, not as a warning.

A file that cannot be read (``OSError``), or a copy / replace / write that fails, raises
:class:`RefSnapshotUnrecordedError`: a fault of the state directory, not of the clone. The
dispatcher lets it through, ``loop_runner.main`` exits :data:`REF_SNAPSHOT_UNRECORDED_EXIT_CODE`
(never 8), and the sweep wrapper skips only this candidate — no repo parking, no quarantine, the
launch reverted — and retries it next tick.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

logger = logging.getLogger(__name__)

SNAPSHOT_DIRNAME = "turn_ref_snapshots"
RETAIN_PER_THREAD = 8
SCHEMA_VERSION = 1
# A recovered (degraded) file carries this version, so code that knows only SCHEMA_VERSION refuses
# it rather than recording a baseline on top of the lost history.
DEGRADED_SCHEMA_VERSION = SCHEMA_VERSION + 1
UNREADABLE_COPY_INFIX = ".unreadable-"
UNREADABLE_COPY_MAX_TRIES = 100

# The next free daemon exit code: 1 crash, 2 environment terminal, 3 stand-down, 4 stall, 5/6/7
# wall-clock budget, 8 dirty clone, 9 git-sync deferred, 10 secrets stop, 11 preflight deferred.
# The sweep wrapper mirrors it as $RefSnapshotUnrecordedExitCode (a pytest pins the two).
REF_SNAPSHOT_UNRECORDED_EXIT_CODE = 12
REF_SNAPSHOT_UNRECORDED_REASON = "ref_snapshot_unrecorded"
# stdout sentinel of the one JSON row ``loop_runner.main`` prints before exiting 12. The wrapper
# reads it only to enrich its notification; the no-park / no-quarantine decision rides on the
# exit code alone.
REF_SNAPSHOT_UNRECORDED_PAYLOAD_PREFIX = "MINDWIRE_REF_SNAPSHOT_UNRECORDED_PAYLOAD "
REF_SNAPSHOT_UNRECORDED_REMEDY = (
    "state ディレクトリの空き容量と権限を確認してください。ファイルを手で直す必要はありません。"
    "読めないファイルは自動で退避されます。"
)

UnrecordedKind = Literal["read", "write", "rename"]


class SnapshotStoreError(Exception):
    """The snapshot could not be recorded; the implementer dispatch must not start."""


class RefSnapshotUnrecordedError(SnapshotStoreError):
    """The state file could not be read, recovered or written (design v2 D-2).

    ``kind`` is ``read`` (an ``OSError`` reading the existing file), ``rename`` (the exclusive
    copy of an unreadable file, or the replace that puts the degraded file in its place, failed)
    or ``write`` (saving the new snapshot failed). A state-directory fault, not the clone's: it
    exits :data:`REF_SNAPSHOT_UNRECORDED_EXIT_CODE`, never 8, and only this candidate is skipped.
    The implementer is not started either way.
    """

    def __init__(
        self, *, kind: UnrecordedKind, path: Path, project: str, thread_id: str, error: str
    ) -> None:
        self.kind: UnrecordedKind = kind
        self.path = path
        self.project = project
        self.thread_id = thread_id
        self.error = error
        super().__init__(
            f"{REF_SNAPSHOT_UNRECORDED_REASON} ({kind}) {os.path.abspath(path)}: {error}"
        )

    def payload(self) -> dict[str, object]:
        return {
            "reason": REF_SNAPSHOT_UNRECORDED_REASON,
            "kind": self.kind,
            "path": os.path.abspath(self.path),
            "project": self.project,
            "thread_id": self.thread_id,
            "error": self.error,
            "remedy": REF_SNAPSHOT_UNRECORDED_REMEDY,
        }


def emit_ref_snapshot_unrecorded_payload(exc: RefSnapshotUnrecordedError) -> None:
    """Print the one ``MINDWIRE_REF_SNAPSHOT_UNRECORDED_PAYLOAD <json>`` row for the wrapper."""
    row = json.dumps(exc.payload(), ensure_ascii=False)
    sys.stdout.write(f"{REF_SNAPSHOT_UNRECORDED_PAYLOAD_PREFIX}{row}\n")
    sys.stdout.flush()


@dataclass(frozen=True)
class _Loaded:
    """A parsed snapshot file; ``degraded`` = recovered earlier, every key reads as unrecorded."""

    entries: list[dict[str, Any]]
    evicted: list[list[str]]
    degraded: bool = False


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
        """Save ``refs`` for this launch; ``False`` = nothing written for the key.

        ``False`` covers two cases: the key already has a snapshot (kept, it is the first
        launch's boundary), or the key was evicted earlier (tombstoned: a fresh snapshot now would
        absorb the earlier attempt's unpushed work, so none is made and the continuation judges
        every branch). Either way the dispatch may start.

        A third ``False``: the thread's file is degraded (recovered from an unreadable file, now
        or earlier; see the module doc), so nothing is recorded and every continuation judges
        every branch. An existing file that does not parse is recovered here and never rewritten
        from scratch: that could drop this key's first snapshot and move the boundary later.

        Raises :class:`RefSnapshotUnrecordedError` when the file cannot be read (``OSError``),
        recovered, or written; the dispatch must not start.
        """
        if not trigger_msg_id:
            raise SnapshotStoreError("no trigger message id to key the snapshot on")
        path = self.path_for(project, thread_id)
        repo = _norm_repo(repo_dir)
        when = now or datetime.now(UTC)
        loaded = _Loaded(entries=[], evicted=[])
        raw: bytes | None
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raw = None
        except OSError as exc:
            raise RefSnapshotUnrecordedError(
                kind="read", path=path, project=project, thread_id=thread_id, error=str(exc)
            ) from exc
        if raw is not None:
            try:
                loaded = self._parse(raw)
            except ValueError as exc:
                self._recover(path, raw, project=project, thread_id=thread_id, now=when, why=exc)
                return False
        if loaded.degraded:
            logger.info(
                "turn ref snapshot: %s/%s is degraded (recovered from an unreadable file); "
                "trigger %s not recorded, its continuation judges all branches",
                project,
                thread_id,
                trigger_msg_id,
            )
            return False
        entries, evicted = loaded.entries, loaded.evicted
        if any(e["trigger_msg_id"] == trigger_msg_id and e["repo_dir"] == repo for e in entries):
            return False
        if [trigger_msg_id, repo] in evicted:
            logger.warning(
                "turn ref snapshot: trigger %s (%s/%s) was evicted earlier; not re-recording, its "
                "continuation judges all branches",
                trigger_msg_id,
                project,
                thread_id,
            )
            return False
        entries.append(
            {
                "trigger_msg_id": trigger_msg_id,
                "repo_dir": repo,
                "recorded_at": when.isoformat(),
                "refs": dict(sorted(refs.items())),
            }
        )
        dropped = entries[:-RETAIN_PER_THREAD]
        entries = entries[-RETAIN_PER_THREAD:]
        evicted = evicted + [[e["trigger_msg_id"], e["repo_dir"]] for e in dropped]
        try:
            self._write_atomic(
                path, {"version": SCHEMA_VERSION, "entries": entries, "evicted": evicted}
            )
        except OSError as exc:
            raise RefSnapshotUnrecordedError(
                kind="write", path=path, project=project, thread_id=thread_id, error=str(exc)
            ) from exc
        return True

    # -- recovery (design v2 D-1) ---------------------------------------------------------------

    def _recover(
        self,
        path: Path,
        raw: bytes,
        *,
        project: str,
        thread_id: str,
        now: datetime,
        why: ValueError,
    ) -> None:
        """Copy the unreadable file aside, then replace it with a degraded file (module doc)."""
        try:
            copy = self._write_unreadable_copy(path, raw, now)
            self._write_atomic(
                path,
                {
                    "version": DEGRADED_SCHEMA_VERSION,
                    "degraded": True,
                    "reset_at": now.isoformat(),
                    "unreadable_copy": copy.name,
                },
            )
        except OSError as exc:
            raise RefSnapshotUnrecordedError(
                kind="rename", path=path, project=project, thread_id=thread_id, error=str(exc)
            ) from exc
        logger.info(
            "notice: turn ref snapshot %s was unreadable (%s); kept as %s — %s/%s now judges all "
            "branches on every CONTINUE",
            os.path.abspath(path),
            why,
            os.path.abspath(copy),
            project,
            thread_id,
        )

    @staticmethod
    def _write_unreadable_copy(path: Path, raw: bytes, now: datetime) -> Path:
        """Create ``<name>.unreadable-<stamp>[-<n>]`` exclusively; never replaces an existing."""
        stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = f"{path.name}{UNREADABLE_COPY_INFIX}{stamp}"
        for n in range(UNREADABLE_COPY_MAX_TRIES):
            copy = path.with_name(base if n == 0 else f"{base}-{n}")
            try:
                fh = open(copy, "xb")  # noqa: SIM115 - closed in the finally below, after fsync
            except FileExistsError:
                continue
            try:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fh.close()
            return copy
        raise FileExistsError(
            f"{UNREADABLE_COPY_MAX_TRIES} names taken for the unreadable copy of {path.name}"
        )

    @staticmethod
    def _write_atomic(path: Path, data: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

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
            loaded = (
                self._parse(path.read_bytes()) if path.exists() else _Loaded(entries=[], evicted=[])
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "turn ref snapshot: cannot read %s (%s); judging all branches", path, exc
            )
            return None
        if loaded.degraded:
            logger.info(
                "turn ref snapshot: %s/%s is degraded (recovered from an unreadable file); "
                "judging all branches",
                scope.project,
                scope.thread_id,
            )
            return None
        entries = loaded.entries
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
    def _parse(blob: bytes) -> _Loaded:
        """Parse a file's bytes; ``ValueError`` = unreadable (bad JSON / version / shape)."""
        data = json.loads(blob.decode("utf-8"))  # decode and JSON errors are both ValueError
        if isinstance(data, dict) and data.get("version") == DEGRADED_SCHEMA_VERSION:
            if data.get("degraded") is not True:
                raise ValueError("degraded-version file without degraded: true")
            return _Loaded(entries=[], evicted=[], degraded=True)
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
        raw_evicted = data.get("evicted", [])
        if not isinstance(raw_evicted, list) or not all(
            isinstance(k, list) and len(k) == 2 and all(isinstance(x, str) for x in k)
            for k in raw_evicted
        ):
            raise ValueError("malformed evicted list")
        return _Loaded(entries=out, evicted=[list(k) for k in raw_evicted])


__all__ = [
    "DEGRADED_SCHEMA_VERSION",
    "REF_SNAPSHOT_UNRECORDED_EXIT_CODE",
    "REF_SNAPSHOT_UNRECORDED_PAYLOAD_PREFIX",
    "REF_SNAPSHOT_UNRECORDED_REASON",
    "RETAIN_PER_THREAD",
    "SNAPSHOT_DIRNAME",
    "ContinuationScope",
    "RefSnapshotUnrecordedError",
    "SnapshotStoreError",
    "TurnRefSnapshotStore",
    "emit_ref_snapshot_unrecorded_payload",
]
