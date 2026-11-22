"""Quarantine export and GC (T-ephemeral-impl-clone-slot-per-thread A5, D-6).

The design is msg-8047 D-6 as revised by Bohr msg-9901 (replacing msg-9899 §3), msg-9903 §1-§3
and msg-9905; Einstein's go is msg-9906. Two halves:

**Export (at quarantine time).** Right before a slot is renamed into ``_quarantine/<repo>/`` —
by the borrow-time check (``clone_slot_prep``) or by the turn end (``clone_slot_finish``) —
:func:`export_slot` writes, under ``<state>/slot-exports/<repo>/``, one stem per quarantine:

* ``<stem>.patch`` — ``git diff --binary HEAD`` (the work tree against HEAD);
* ``<stem>.index.patch`` — ``git diff --cached --binary HEAD`` (the index against HEAD), so a
  staged version the work tree no longer has is kept too (#600 gate Obj-1). Restore: check out
  HEAD, ``git apply --cached`` the index patch, ``git apply`` the patch, extract the tar;
* ``<stem>.untracked.tar`` — the CONTENT of every untracked, not ignored file (``tarfile``); the
  manifest records each entry's type (file / symlink) and execute bit next to its sha256;
* ``<stem>.bundle`` — only when there are commits no remote ref has: ``git bundle create`` of
  HEAD, the local branches, the tags and every stash entry (the whole reflog), ``--not
  --remotes``, then ``git bundle verify``;
* ``<stem>.manifest.json`` — written by :func:`write_manifest` once the rename has succeeded, with
  the quarantine path and time, the HEAD sha, each file's sha256 and size, the bundle verify
  result, the local-only commits, every untracked file with its sha256, and ``complete``.

``complete`` is false when a file could not be read, the untracked files exceed
:data:`UNTRACKED_CAP` (256 MiB), the exports together exceed :data:`TOTAL_CAP` (1 GiB, msg-9903
§3), the bundle could not be created or verified, HEAD could not be read, the whole export ran
past its deadline (``export_timeout``: one deadline for all of it, sizes checked from ``lstat``
before any content is read, #600 gate Obj-4), or the slot has something the export cannot carry
(a gitlink / submodule, an embedded repository, an untracked special file, unmerged index
entries, assume-unchanged / skip-worktree entries). Only checks
that actually ran are recorded: there is no ``restore_checked`` (msg-9905 Obj-3-1); the restore
path is verified by the tests. A failing export never stops or parks anything: the quarantine
goes on exactly as before (msg-9901 §1).

**GC (once per sweep tick, slot mode only).** :class:`QuarantineGc` deletes a quarantined slot
only when ALL of these hold (msg-9901 §2, msg-9903 §1): a manifest names exactly that path and
says ``complete``; every file it lists exists with the recorded sha256; re-reading the quarantined
slot right before deleting gives the same HEAD, the same ``git diff --binary HEAD`` sha256, the
same ``git diff --cached --binary HEAD`` sha256, no index hazard, the same local-only commits,
and the same untracked entries with the same type, execute bit and sha256; no lease names the
path. This is inside Takahito's approval in msg-8047 D-6 ("patch export 済み"), and narrower:
a rescue ref alone never makes anything deletable (msg-9901 Obj-1-2).

What is due: a candidate older than :data:`MAX_AGE` (7 days), or, among the candidates of one
repo whose verification COMPLETED within :data:`ELIGIBLE_TTL` (oldest first by
``quarantined_at``), those beyond the newest :data:`MAX_PER_REPO` (10). A candidate not yet
verified is verified first without deleting anything (#600 gate Obj-2), inside the same per-tick
bound; a due one is verified again right before deletion. A
quarantine that does not qualify (no manifest, as for every one made before A5; ``complete``
false; changed since export; unverifiable) is kept, is not counted in the 10, and is named in one
notice. Deleting: rename to ``_quarantine/_deleting/<repo>/<dir>`` first, then delete file by
file. Whatever is under ``_deleting/`` is never a restore candidate, only work to finish.

Bounds (msg-9903 §2): no new verification or deletion starts after :data:`TICK_BUDGET_S` (20 s)
and at most :data:`MAX_ITEMS` (3) quarantines are verified per tick; hashing goes 1 MiB at a time
and git gets the remaining time as its timeout, so a verification stops mid-way at the deadline
(nothing was deleted, it starts over next tick); deletion checks the clock after every file and
resumes under ``_deleting/`` next tick. A rename or deletion that fails :data:`STUCK_AFTER` (3)
times in a row is ``gc_stuck``; a verification that times out 3 times in a row is
``gc_verify_timeout`` (kept whatever its age or count, msg-9905 Obj-3-2); a verification that
completes resets that count. Both are named in one notice.

Notice reader and fallback surface (OBL-CHATROOM-PRODUCER-READER-SURFACE): nothing here posts to
a thread. ``mindwire quarantine-gc`` prints the notices in its json; the sweep wrapper sends them
through its operator notification (Discord webhook, deduplicated by key + signature, so each is
sent once). The reader is the operator who must look at a kept quarantine. The durable fallback
is the ``quarantine_gc`` row in ``clone-retreats.jsonl`` and ``state/quarantine-gc.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .clone_retreat import git_env

__all__ = [
    "EXPORTS_DIR",
    "GC_STATE_FILE",
    "ExportResult",
    "GcResult",
    "QuarantineGc",
    "export_deadline",
    "export_slot",
    "write_manifest",
]

EXPORTS_DIR = "slot-exports"
GC_STATE_FILE = "quarantine-gc.json"
DELETING_DIR = "_deleting"
MANIFEST_SCHEMA = 1
UNTRACKED_CAP = 256 * 1024 * 1024
TOTAL_CAP = 1024 * 1024 * 1024
CHUNK = 1024 * 1024
MAX_AGE = timedelta(days=7)
MAX_PER_REPO = 10
TICK_BUDGET_S = 20.0
MAX_ITEMS = 3
STUCK_AFTER = 3
EXPORT_GIT_TIMEOUT_S = 300.0
# #600 gate Obj-4: one export's whole budget, and the time it leaves inside the wrapper's bound.
EXPORT_BUDGET_S = 120.0
EXPORT_RESERVE_S = 60.0
# The wrapper's bound on `mindwire slot-finish` ($SlotFinishTimeoutSeconds in
# deploy/run-conductor-scheduled.ps1; a test keeps the two equal).
FINISH_ALLOWANCE_S = 300.0
# #600 gate Obj-2: a completed verification makes a quarantine count towards MAX_PER_REPO
# for this long; after that it is verified again before it counts.
ELIGIBLE_TTL = timedelta(hours=24)

STATUS_STUCK = "gc_stuck"
STATUS_VERIFY_TIMEOUT = "gc_verify_timeout"
STATUS_CHANGED = "gc_changed"
STATUS_UNVERIFIED = "gc_unverified"
KEEP_NO_MANIFEST = "no_manifest"
KEEP_INCOMPLETE = "export_incomplete"


class _TimeoutError(Exception):
    """The tick's deadline passed in the middle of a step."""


class _MismatchError(Exception):
    """The quarantine is not what its manifest says, or could not be read."""


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _git_bytes(cwd: Path, args: Sequence[str], timeout: float) -> tuple[int, bytes, bytes]:
    env = git_env()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        check=False,
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    return proc.returncode, proc.stdout or b"", proc.stderr or b""


# #600 gate round 2 Obj-1: ``--no-textconv`` as well as ``--no-ext-diff``. A configured
# ``diff.<driver>.textconv`` would otherwise put converted text in the patch, which export and
# verification would agree on while it cannot restore the original bytes.
_DIFF = ("diff", "--binary", "--no-color", "--no-ext-diff", "--no-textconv", "HEAD")
# #600 gate Obj-1: the index is exported on its own. ``_DIFF`` is HEAD -> work tree only, so a
# staged version that the work tree no longer has (stage B, restore the file to A) is in neither.
# Restore: ``git apply --cached <index patch>`` then ``git apply <patch>`` on a checkout of HEAD.
_DIFF_INDEX = (
    "diff",
    "--cached",
    "--binary",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "HEAD",
)
_UNTRACKED = ("ls-files", "--others", "--exclude-standard", "-z")

_Run = Callable[[Sequence[str]], tuple[int, bytes, bytes]]


def _stash_entries(run: _Run) -> list[str]:
    """Every stash entry, newest first (#600 gate round 2 Obj-2). Older entries live only in the
    ``refs/stash`` reflog and are generally not ancestors of the newest, so ``refs/stash`` alone
    would drop them. Raises :class:`_MismatchError` when the reflog cannot be read."""
    code, _out, _err = run(("rev-parse", "-q", "--verify", "refs/stash"))
    if code != 0:
        return []
    code, out, err = run(("reflog", "show", "--format=%H", "refs/stash", "--"))
    if code != 0:
        raise _MismatchError(f"unverified: stash reflog: {err.decode(errors='replace')[:200]}")
    return [r for r in out.decode().split() if r]


def _local_args(cwd: Path, run: _Run) -> tuple[list[str], list[str]]:
    """``(positive revisions, stash entries)``. The revisions whose commits must survive are
    HEAD, branches, tags, and EVERY stash entry: ``refs/stash`` as a ref (so a bundle always
    has one to name, even when nothing else is local) plus each older entry by sha. Restore the
    stash with ``git stash store <sha>`` from the manifest's ``stash`` list, oldest first."""
    stash = _stash_entries(run)
    args = ["HEAD", "--branches", "--tags"]
    if stash:
        args += ["refs/stash", *stash[1:]]
    return args, stash


def _tar_estimate(kinds: Sequence[tuple[str, str, bool]], extra: dict[str, int]) -> int:
    """An upper estimate of the tar's size BEFORE it is written (#600 gate round 2 Obj-4).

    Per entry: a PAX extended header (tarfile writes one for every entry, at least for the
    fractional mtime) of one header block plus the records padded to 512 -- the path when it
    is long, a symlink's target, and room for mtime/uid/gid/names -- then the ustar header and
    the contents padded to 512. Then the two end blocks and the record rounding. ``extra`` is
    the content size of a file, the target length of a symlink. The written size is checked
    again afterwards, so an estimate that is low only costs disk, never completeness.
    """
    total = 1024
    for n, kind, _exe in kinds:
        size = extra.get(n, 0)
        link = size if kind == "symlink" else 0
        body = 0 if kind == "symlink" else size
        pax = len(os.fsencode(n)) + link + 256
        total += 512 + -(-pax // 512) * 512 + 512 + -(-body // 512) * 512
    return -(-total // tarfile.RECORDSIZE) * tarfile.RECORDSIZE


def _sha256_file(
    path: Path, deadline: float | None = None, mono: Callable[[], float] = time.monotonic
) -> str:
    """Content hash (a symlink: its target string). The entry TYPE is compared separately."""
    h = hashlib.sha256()
    if path.is_symlink():
        h.update(os.fsencode(os.readlink(path)))
        return h.hexdigest()
    with path.open("rb") as fh:
        while True:
            if deadline is not None and mono() > deadline:
                raise _TimeoutError(str(path))
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _entry_kind(st: os.stat_result) -> tuple[str, bool]:
    """``(type, exec)`` of an untracked entry (#600 gate Obj-3). ``type`` is ``file``,
    ``symlink`` or ``special``; ``exec`` is the owner execute bit of a regular file."""
    if stat.S_ISLNK(st.st_mode):
        return "symlink", False
    if stat.S_ISREG(st.st_mode):
        return "file", bool(st.st_mode & stat.S_IXUSR)
    return "special", False


def _has_gitlink(slot: Path, run: _Run) -> bool:
    if (slot / ".gitmodules").exists():
        return True
    for args in (("ls-files", "-s", "-z"), ("ls-tree", "-r", "-z", "HEAD")):
        code, out, _ = run(args)
        if code != 0:
            return True  # unreadable: treat as something the export cannot carry
        for entry in out.split(b"\0"):
            if entry.startswith(b"160000 "):
                return True
    return False


def _index_hazards(run: _Run) -> list[str]:
    """Index states the two patches cannot carry (#600 gate Obj-1): unmerged stages, and
    assume-unchanged / skip-worktree entries, whose work-tree changes ``git diff`` hides."""
    found: list[str] = []
    code, out, _ = run(("ls-files", "-u", "-z"))
    if code != 0:
        return ["index_unreadable"]
    if out.strip(b"\0"):
        found.append("unmerged_index")
    code, out, _ = run(("ls-files", "-v", "-z"))
    if code != 0:
        return [*found, "index_unreadable"]
    for entry in out.split(b"\0"):
        tag = entry[:1]
        if tag.islower():
            found.append("assume_unchanged")
            break
        if tag == b"S":
            found.append("skip_worktree")
            break
    return found


@dataclass
class ExportResult:
    """What :func:`export_slot` wrote. ``manifest`` is the body :func:`write_manifest` completes."""

    stem: Path
    patch: Path | None = None
    index_patch: Path | None = None
    untracked_tar: Path | None = None
    bundle: Path | None = None
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return bool(self.manifest.get("complete"))


def export_deadline(started: float, allowance_s: float, now: float) -> float:
    """The absolute (monotonic) deadline of one export (#600 gate Obj-4): at most
    :data:`EXPORT_BUDGET_S` from ``now``, and never later than ``allowance_s`` after the process
    ``started`` minus :data:`EXPORT_RESERVE_S`, which is left for the rename, the manifest, the
    ledger row and the lease bookkeeping that must still run inside the wrapper's bound."""
    return min(now + EXPORT_BUDGET_S, started + allowance_s - EXPORT_RESERVE_S)


def export_slot(
    slot: Path,
    exports_root: Path,
    repo: str,
    name: str,
    *,
    untracked_cap: int = UNTRACKED_CAP,
    total_cap: int = TOTAL_CAP,
    timeout: float = EXPORT_GIT_TIMEOUT_S,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ExportResult:
    """Export a slot that is about to be quarantined. Never raises: a failure is ``complete`` false.

    ``deadline`` (monotonic) bounds the WHOLE export, not one git call (#600 gate Obj-4): every
    git call gets the time left as its timeout, file contents are read in tar-sized blocks with
    the clock checked in between, and the size caps are checked from ``lstat`` before any content
    is read. Past the deadline the export stops with ``export_timeout`` and the caller goes on to
    the rename. ``None`` = :data:`EXPORT_BUDGET_S` from now.

    ``name`` makes the stem readable (``<slot>-<who>-<stamp>``); a random suffix keeps two
    quarantines of the same second apart.
    """
    if deadline is None:
        deadline = monotonic() + EXPORT_BUDGET_S
    out_dir = exports_root / repo
    stem = out_dir / f"{name}-{uuid.uuid4().hex[:8]}"
    res = ExportResult(stem=stem)
    m: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "repo": repo,
        "head": None,
        "patch": None,
        "index_patch": None,
        "untracked_tar": None,
        "bundle": None,
        "local_commits": [],
        "untracked": [],
        "complete": False,
        "incomplete_reasons": [],
    }
    res.manifest = m
    why: list[str] = m["incomplete_reasons"]
    try:
        _export(
            slot, out_dir, stem, res, m, why, untracked_cap, total_cap, timeout, deadline, monotonic
        )
    except _TimeoutError as exc:
        why.append(f"export_timeout: {exc}")
    except Exception as exc:  # never stop the quarantine (msg-9901 §1)
        why.append(f"export_error: {type(exc).__name__}: {exc}")
    m["complete"] = not why
    return res


class _DeadlineReader:
    """A file reader for ``tarfile.addfile`` that hashes what it reads and stops at the deadline."""

    def __init__(self, fh: Any, deadline: float, mono: Callable[[], float]) -> None:
        self._fh = fh
        self._deadline = deadline
        self._mono = mono
        self.sha = hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        if self._mono() > self._deadline:
            raise _TimeoutError("untracked contents")
        data: bytes = self._fh.read(n)
        self.sha.update(data)
        return data


def _export(
    slot: Path,
    out_dir: Path,
    stem: Path,
    res: ExportResult,
    m: dict[str, Any],
    why: list[str],
    untracked_cap: int,
    total_cap: int,
    timeout: float,
    deadline: float,
    mono: Callable[[], float],
) -> None:
    def run(args: Sequence[str]) -> tuple[int, bytes, bytes]:
        left = deadline - mono()
        if left <= 0:
            raise _TimeoutError("git " + args[0])
        try:
            return _git_bytes(slot, args, min(timeout, left))
        except subprocess.TimeoutExpired as exc:
            raise _TimeoutError("git " + args[0]) from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    code, out, _ = run(("rev-parse", "HEAD"))
    if code != 0:
        why.append("head_unreadable")
    else:
        m["head"] = out.decode().strip()
    if _has_gitlink(slot, run):
        why.append("submodule")
    why.extend(_index_hazards(run))

    # Untracked entries: list, lstat and cap BEFORE reading any content (#600 gate Obj-4).
    code, listing, _ = run(_UNTRACKED)
    if code != 0:
        why.append("untracked_list_failed")
        names: list[str] = []
    else:
        names = sorted(os.fsdecode(n) for n in listing.split(b"\0") if n)
    if any(n.endswith("/") for n in names):
        why.append("embedded_repo")
    kinds: list[tuple[str, str, bool]] = []
    sizes = 0
    each: dict[str, int] = {}
    for n in (n for n in names if not n.endswith("/")):
        try:
            st = (slot / n).lstat()
        except OSError:
            why.append(f"unreadable: {n}")
            continue
        kind, exe = _entry_kind(st)
        if kind == "special":
            why.append(f"special_file: {n}")
            continue
        sizes += st.st_size
        if kind == "symlink":
            each[n] = len(os.fsencode(os.readlink(slot / n)))  # the link, not any content
        else:
            each[n] = st.st_size
        kinds.append((n, kind, exe))
    if sizes > untracked_cap:
        why.append("untracked_cap")

    for key, args, suffix in (
        ("patch", _DIFF, ".patch"),
        ("index_patch", _DIFF_INDEX, ".index.patch"),
    ):
        code, patch, err = run(args)
        if code != 0:
            why.append(f"{key}_failed: {err.decode(errors='replace').strip()[:200]}")
            continue
        total += len(patch)
        if total > total_cap:
            why.append("total_cap")
            continue
        p = Path(f"{stem}{suffix}")
        p.write_bytes(patch)
        setattr(res, key, p)
        m[key] = {"file": p.name, "sha256": hashlib.sha256(patch).hexdigest(), "bytes": len(patch)}

    blocked = "untracked_cap" in why or any(
        w.startswith(("unreadable", "special_file")) for w in why
    )
    if not blocked:
        if total + _tar_estimate(kinds, each) > total_cap:
            why.append("total_cap")
        else:
            entries: list[dict[str, Any]] = []
            tp = Path(f"{stem}.untracked.tar")
            with tarfile.open(tp, "w") as tar:
                for n, kind, exe in kinds:
                    fp = slot / n
                    info = tar.gettarinfo(str(fp), arcname=n)
                    if kind == "symlink":
                        tar.addfile(info)
                        sha = _sha256_file(fp)
                    else:
                        with fp.open("rb") as fh:
                            reader = _DeadlineReader(fh, deadline, mono)
                            tar.addfile(info, reader)
                            sha = reader.sha.hexdigest()
                    entries.append({"path": n, "type": kind, "exec": exe, "sha256": sha})
            m["untracked"] = entries
            res.untracked_tar = tp
            size = tp.stat().st_size
            total += size
            m["untracked_tar"] = {
                "file": tp.name,
                "sha256": _sha256_file(tp, deadline, mono),
                "bytes": size,
            }
            if total > total_cap:  # the WRITTEN size, before any early return below
                why.append("total_cap")

    pos, m["stash"] = _local_args(slot, run)
    code, revs, _ = run(("rev-list", *pos, "--not", "--remotes"))
    if code != 0:
        why.append("rev_list_failed")
        return
    commits = sorted(r for r in revs.decode().split() if r)
    m["local_commits"] = commits
    if not commits:
        return
    bp = Path(f"{stem}.bundle")
    code, _, err = run(("bundle", "create", str(bp), *pos, "--not", "--remotes"))
    if code != 0 or not bp.is_file():
        why.append(f"bundle_failed: {err.decode(errors='replace').strip()[:200]}")
        return
    size = bp.stat().st_size
    total += size
    vcode, _, _ = run(("bundle", "verify", str(bp)))
    m["bundle"] = {
        "file": bp.name,
        "sha256": _sha256_file(bp, deadline, mono),
        "bytes": size,
        "verify": "ok" if vcode == 0 else "failed",
    }
    res.bundle = bp
    if vcode != 0:
        why.append("bundle_verify_failed")
    if total > total_cap and "total_cap" not in why:
        why.append("total_cap")


def write_manifest(res: ExportResult, quarantine_path: Path, at: datetime) -> Path | None:
    """Write ``<stem>.manifest.json`` for a quarantine that now lives at ``quarantine_path``.
    Returns its path, or ``None`` when it could not be written (the quarantine is then kept)."""
    m = dict(res.manifest)
    m["quarantine_path"] = str(quarantine_path)
    m["quarantined_at"] = at.astimezone(UTC).isoformat()
    path = Path(f"{res.stem}.manifest.json")
    tmp = Path(f"{path}.tmp")
    try:
        tmp.write_text(
            json.dumps(m, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        return None
    return path


# --------------------------------------------------------------------------------------- GC


@dataclass
class GcResult:
    deleted: list[str] = field(default_factory=list)
    started_deleting: list[str] = field(default_factory=list)
    verified: int = 0
    remaining_due: int = 0
    deleting_pending: int = 0
    seconds: float = 0.0
    notices: list[dict[str, str]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "deleted": self.deleted,
            "started_deleting": self.started_deleting,
            "verified": self.verified,
            "remaining_due": self.remaining_due,
            "deleting_pending": self.deleting_pending,
            "seconds": round(self.seconds, 3),
            "notices": self.notices,
            "events": self.events,
        }


@dataclass
class _Item:
    repo: str
    path: Path
    manifest: dict[str, Any] | None
    manifest_path: Path | None
    at: datetime | None


def _parse_at(value: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


class QuarantineGc:
    """One GC pass over ``<slots_root>/_quarantine/``."""

    def __init__(
        self,
        *,
        slots_root: Path,
        state_dir: Path,
        leased_paths: Iterable[Path] = (),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        budget_s: float = TICK_BUDGET_S,
        max_items: int = MAX_ITEMS,
        max_age: timedelta = MAX_AGE,
        max_per_repo: int = MAX_PER_REPO,
        rename: Callable[[Path, Path], None] = os.rename,
        remove: Callable[[Path], None] = os.remove,
    ) -> None:
        self.root = slots_root / "_quarantine"
        self.state_dir = state_dir
        self.exports = state_dir / EXPORTS_DIR
        self.state_path = state_dir / GC_STATE_FILE
        self.leased = {_norm(p) for p in leased_paths}
        self._clock = clock
        self._mono = monotonic
        self.budget_s = budget_s
        self.max_items = max_items
        self.max_age = max_age
        self.max_per_repo = max_per_repo
        self._rename = rename
        self._remove = remove

    # -- state -----------------------------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, state: dict[str, dict[str, Any]]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(f"{self.state_path}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=1), "utf-8")
        os.replace(tmp, self.state_path)

    # -- discovery -------------------------------------------------------------------------

    def _manifests(self) -> dict[str, tuple[dict[str, Any], Path]]:
        found: dict[str, tuple[dict[str, Any], Path]] = {}
        if not self.exports.is_dir():
            return found
        for mp in self.exports.glob("*/*.manifest.json"):
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(m, dict) and m.get("quarantine_path"):
                found[_norm(Path(str(m["quarantine_path"])))] = (m, mp)
        return found

    def _items(self) -> list[_Item]:
        manifests = self._manifests()
        items: list[_Item] = []
        if not self.root.is_dir():
            return items
        for repo_dir in sorted(self.root.iterdir()):
            if not repo_dir.is_dir() or repo_dir.name.startswith("_"):
                continue  # _deleting/ is never a restore candidate
            for q in sorted(repo_dir.iterdir()):
                if not q.is_dir():
                    continue
                hit = manifests.get(_norm(q))
                m, mp = hit if hit is not None else (None, None)
                at = _parse_at(m.get("quarantined_at")) if m is not None else None
                items.append(_Item(repo_dir.name, q, m, mp, at))
        return items

    # -- the pass --------------------------------------------------------------------------

    def run(self) -> GcResult:
        start = self._mono()
        deadline = start + self.budget_s
        res = GcResult()
        state = self._load()
        try:
            self._finish_deleting(state, res, deadline)
            self._collect(state, res, deadline)
        finally:
            res.seconds = self._mono() - start
            self._save(state)
        return res

    def _notice(self, res: GcResult, reason: str, path: Path, detail: str) -> None:
        res.notices.append(
            {
                "key": f"__quarantine_gc__/{_norm(path)}",
                "signature": f"{reason}:{path}",
                "reason": reason,
                "path": str(path),
                "detail": detail,
            }
        )

    def _finish_deleting(
        self, state: dict[str, dict[str, Any]], res: GcResult, deadline: float
    ) -> None:
        base = self.root / DELETING_DIR
        if not base.is_dir():
            return
        for repo_dir in sorted(base.iterdir()):
            for d in sorted(repo_dir.iterdir()) if repo_dir.is_dir() else []:
                rec = state.setdefault(_norm(d), {})
                if rec.get("status") == STATUS_STUCK:
                    self._notice(res, STATUS_STUCK, d, "deletion failed repeatedly")
                    continue
                if self._mono() > deadline:
                    res.deleting_pending += 1
                    continue
                done = self._delete_tree(d, rec, res, deadline)
                if done:
                    state.pop(_norm(d), None)
                    res.deleted.append(str(d))
                else:
                    res.deleting_pending += 1

    def _delete_tree(self, d: Path, rec: dict[str, Any], res: GcResult, deadline: float) -> bool:
        """Delete ``d`` entry by entry. False when the deadline stopped it or a removal failed.

        #600 gate round 2 Obj-3: an incremental depth-first walk (``os.scandir``, one directory
        open at a time) with the clock checked before every entry and before every ``rmdir``,
        so neither discovering a large tree nor removing empty directories runs past the
        deadline. What is left stays under ``_deleting/`` for the next tick.
        """
        try:
            stack = [d]
            while stack:
                if self._mono() > deadline:
                    return False
                cur = stack[-1]
                descend: Path | None = None
                with os.scandir(cur) as it:
                    for e in it:
                        if self._mono() > deadline:
                            return False
                        fp = Path(e.path)
                        if e.is_symlink():
                            self._remove(fp)  # never followed
                        elif getattr(e, "is_junction", lambda: False)():  # 3.12+
                            os.rmdir(fp)  # a Windows junction: removes the link only
                        elif e.is_dir(follow_symlinks=False):
                            descend = fp
                            break
                        else:
                            try:
                                self._remove(fp)
                            except PermissionError:
                                os.chmod(fp, stat.S_IWRITE)  # git objects: read-only on Windows
                                self._remove(fp)
                if descend is not None:
                    stack.append(descend)
                    continue
                if self._mono() > deadline:
                    return False
                os.rmdir(cur)
                stack.pop()
        except OSError as exc:
            rec["delete_failures"] = int(rec.get("delete_failures", 0)) + 1
            res.events.append(f"delete failed {d}: {exc}")
            if rec["delete_failures"] >= STUCK_AFTER:
                rec["status"] = STATUS_STUCK
                self._notice(
                    res, STATUS_STUCK, d, f"deletion failed {rec['delete_failures']} times: {exc}"
                )
            return False
        return True

    def _collect(self, state: dict[str, dict[str, Any]], res: GcResult, deadline: float) -> None:
        now = self._clock()
        by_repo: dict[str, list[_Item]] = {}
        for it in self._items():
            rec = state.get(_norm(it.path), {})
            status = rec.get("status")
            if status in (STATUS_STUCK, STATUS_VERIFY_TIMEOUT, STATUS_CHANGED, STATUS_UNVERIFIED):
                self._notice(res, str(status), it.path, str(rec.get("detail", "")))
                continue
            if it.manifest is None:
                self._notice(res, KEEP_NO_MANIFEST, it.path, "no export manifest names this path")
                continue
            if not it.manifest.get("complete") or it.at is None:
                reasons = ", ".join(map(str, it.manifest.get("incomplete_reasons") or [])) or "-"
                self._notice(res, KEEP_INCOMPLETE, it.path, f"export incomplete: {reasons}")
                continue
            if _norm(it.path) in self.leased:
                continue
            by_repo.setdefault(it.repo, []).append(it)
        # #600 gate Obj-2: only a quarantine whose verification COMPLETED (within ELIGIBLE_TTL)
        # counts towards MAX_PER_REPO. One that passed only the manifest metadata is verified
        # first ("pre-verify", nothing deleted), so ten broken exports never make a valid older
        # one due. Age makes a quarantine due by itself; it is verified right before deletion.
        due: list[_Item] = []
        unknown: list[_Item] = []
        for items in by_repo.values():
            items.sort(key=lambda i: (i.at, str(i.path)))
            counted = [i for i in items if self._eligible(state, i, now)]
            over = set(map(id, counted[: max(0, len(counted) - self.max_per_repo)]))
            for it in items:
                assert it.at is not None
                if id(it) in over or now - it.at >= self.max_age:
                    due.append(it)
                elif not self._eligible(state, it, now):
                    unknown.append(it)
        due.sort(key=lambda i: (i.at, str(i.path)))
        unknown.sort(key=lambda i: (i.at, str(i.path)))
        started = 0
        for it, delete in [(i, True) for i in due] + [(i, False) for i in unknown]:
            if started >= self.max_items or self._mono() > deadline:
                if delete:
                    res.remaining_due += 1
                continue
            started += 1
            self._verify_and_delete(it, state, res, deadline, delete=delete)

    def _eligible(self, state: dict[str, dict[str, Any]], it: _Item, now: datetime) -> bool:
        at = _parse_at(state.get(_norm(it.path), {}).get("eligible_at"))
        return at is not None and now - at < ELIGIBLE_TTL

    def _verify_and_delete(
        self,
        it: _Item,
        state: dict[str, dict[str, Any]],
        res: GcResult,
        deadline: float,
        *,
        delete: bool = True,
    ) -> None:
        key = _norm(it.path)
        rec = state.setdefault(key, {})
        rec.pop("eligible_at", None)  # set again only by a verification that completes now
        try:
            verify(it.path, it.manifest or {}, it.manifest_path, deadline, self._mono)
        except _TimeoutError as exc:
            rec["verify_timeouts"] = int(rec.get("verify_timeouts", 0)) + 1
            res.events.append(f"verify timeout {it.path} ({rec['verify_timeouts']}): {exc}")
            if rec["verify_timeouts"] >= STUCK_AFTER:
                rec["status"] = STATUS_VERIFY_TIMEOUT
                rec["detail"] = f"verification timed out {rec['verify_timeouts']} ticks in a row"
                self._notice(res, STATUS_VERIFY_TIMEOUT, it.path, rec["detail"])
            return
        except _MismatchError as exc:
            res.verified += 1
            changed = str(exc).startswith("changed:")
            rec["status"] = STATUS_CHANGED if changed else STATUS_UNVERIFIED
            rec["detail"] = str(exc)
            self._notice(res, rec["status"], it.path, rec["detail"])
            return
        res.verified += 1
        rec["verify_timeouts"] = 0
        rec["eligible_at"] = self._clock().astimezone(UTC).isoformat()
        if not delete or _norm(it.path) in self.leased:
            return
        target = self.root / DELETING_DIR / it.repo / it.path.name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise OSError(f"{target} exists")
            self._rename(it.path, target)
        except OSError as exc:
            rec["delete_failures"] = int(rec.get("delete_failures", 0)) + 1
            res.events.append(f"rename to {DELETING_DIR} failed {it.path}: {exc}")
            if rec["delete_failures"] >= STUCK_AFTER:
                rec["status"] = STATUS_STUCK
                rec["detail"] = (
                    f"rename to {DELETING_DIR} failed {rec['delete_failures']} times: {exc}"
                )
                self._notice(res, STATUS_STUCK, it.path, rec["detail"])
            return
        state.pop(key, None)
        res.started_deleting.append(str(it.path))
        drec = state.setdefault(_norm(target), {})
        if self._mono() > deadline:
            res.deleting_pending += 1
            return
        if self._delete_tree(target, drec, res, deadline):
            state.pop(_norm(target), None)
            res.deleted.append(str(target))
        else:
            res.deleting_pending += 1


def verify(
    path: Path,
    m: dict[str, Any],
    manifest_path: Path | None,
    deadline: float,
    mono: Callable[[], float] = time.monotonic,
) -> None:
    """Raise unless the quarantine at ``path`` is exactly what manifest ``m`` saved (msg-9903 §1).

    :class:`_TimeoutError` when the deadline passed; :class:`_MismatchError` otherwise
    (``changed:`` when the quarantine itself differs from the export, ``unverified:`` when
    something could not be read or an export file is missing or altered).
    """
    if manifest_path is None:
        raise _MismatchError("unverified: no manifest")
    base = manifest_path.parent

    def left() -> float:
        r = deadline - mono()
        if r <= 0:
            raise _TimeoutError("deadline")
        return r

    def run_raw(args: Sequence[str]) -> tuple[int, bytes, bytes]:
        try:
            return _git_bytes(path, args, left())
        except subprocess.TimeoutExpired as exc:
            raise _TimeoutError(" ".join(args)) from exc
        except OSError as exc:
            raise _MismatchError(f"unverified: git {args[0]}: {exc}") from exc

    def run(args: Sequence[str]) -> bytes:
        code, out, err = run_raw(args)
        if code != 0:
            raise _MismatchError(
                f"unverified: git {' '.join(args)}: {err.decode(errors='replace')[:200]}"
            )
        return out

    for key in ("patch", "index_patch", "untracked_tar", "bundle"):
        entry = m.get(key)
        if key != "bundle" and entry is None:
            raise _MismatchError(f"unverified: no {key} export")
        if entry is None:
            continue
        fp = base / str(entry["file"])
        try:
            if _sha256_file(fp, deadline, mono) != entry["sha256"]:
                raise _MismatchError(f"unverified: {fp.name} sha256 differs")
        except OSError as exc:
            raise _MismatchError(f"unverified: {fp.name}: {exc}") from exc
    if m.get("local_commits") and m.get("bundle") is None:
        raise _MismatchError("unverified: local commits without a bundle")
    if run(("rev-parse", "HEAD")).decode().strip() != m.get("head"):
        raise _MismatchError("changed: HEAD")
    hazards = _index_hazards(run_raw)
    if hazards:
        raise _MismatchError(f"changed: index ({', '.join(hazards)})")
    for key, args, what in (
        ("patch", _DIFF, "tracked files (git diff --binary HEAD)"),
        ("index_patch", _DIFF_INDEX, "index (git diff --cached --binary HEAD)"),
    ):
        if hashlib.sha256(run(args)).hexdigest() != (m.get(key) or {}).get("sha256"):
            raise _MismatchError(f"changed: {what}")
    pos, stash = _local_args(path, run_raw)  # the export's revision set (round 2 Obj-2)
    if stash != list(m.get("stash") or []):
        raise _MismatchError("changed: stash entries")
    commits = sorted(r for r in run(("rev-list", *pos, "--not", "--remotes")).decode().split() if r)
    if commits != sorted(m.get("local_commits") or []):
        raise _MismatchError("changed: local-only commits")
    names = sorted(os.fsdecode(n) for n in run(_UNTRACKED).split(b"\0") if n)
    saved: dict[str, dict[str, Any]] = {}
    for e in m.get("untracked") or []:
        if not isinstance(e, dict) or e.get("type") not in ("file", "symlink"):
            raise _MismatchError("unverified: untracked entry without a type")
        saved[str(e["path"])] = e
    if names != sorted(saved):
        raise _MismatchError("changed: untracked file list")
    for n in names:
        e = saved[n]
        try:
            # #600 gate Obj-3: type and execute bit first, then the content.
            if _entry_kind((path / n).lstat()) != (e["type"], bool(e.get("exec"))):
                raise _MismatchError(f"changed: untracked {n} (type or mode)")
            if _sha256_file(path / n, deadline, mono) != e["sha256"]:
                raise _MismatchError(f"changed: untracked {n}")
        except OSError as exc:
            raise _MismatchError(f"unverified: untracked {n}: {exc}") from exc
