"""Quarantine export and GC (T-ephemeral-impl-clone-slot-per-thread A5, D-6).

The design is msg-8047 D-6 as revised by Bohr msg-9901 (replacing msg-9899 §3), msg-9903 §1-§3
and msg-9905; Einstein's go is msg-9906. Two halves:

**Export (at quarantine time).** Right before a slot is renamed into ``_quarantine/<repo>/`` —
by the borrow-time check (``clone_slot_prep``) or by the turn end (``clone_slot_finish``) —
:func:`export_slot` writes, under ``<state>/slot-exports/<repo>/``, one stem per quarantine:

* ``<stem>.patch`` — ``git diff --binary HEAD`` (index and work-tree changes against HEAD);
* ``<stem>.untracked.tar`` — the CONTENT of every untracked, not ignored file (``tarfile``);
* ``<stem>.bundle`` — only when there are commits no remote ref has: ``git bundle create`` of
  HEAD, the local branches, the tags and the stash, ``--not --remotes``, then ``git bundle verify``;
* ``<stem>.manifest.json`` — written by :func:`write_manifest` once the rename has succeeded, with
  the quarantine path and time, the HEAD sha, each file's sha256 and size, the bundle verify
  result, the local-only commits, every untracked file with its sha256, and ``complete``.

``complete`` is false when a file could not be read, the untracked files exceed
:data:`UNTRACKED_CAP` (256 MiB), the exports together exceed :data:`TOTAL_CAP` (1 GiB, msg-9903
§3), the bundle could not be created or verified, HEAD could not be read, or the slot has
something the export cannot carry (a gitlink / submodule, an embedded repository). Only checks
that actually ran are recorded: there is no ``restore_checked`` (msg-9905 Obj-3-1); the restore
path is verified by the tests. A failing export never stops or parks anything: the quarantine
goes on exactly as before (msg-9901 §1).

**GC (once per sweep tick, slot mode only).** :class:`QuarantineGc` deletes a quarantined slot
only when ALL of these hold (msg-9901 §2, msg-9903 §1): a manifest names exactly that path and
says ``complete``; every file it lists exists with the recorded sha256; re-reading the quarantined
slot right before deleting gives the same HEAD, the same ``git diff --binary HEAD`` sha256, the
same local-only commits, and the same untracked files with the same sha256; no lease names the
path. This is inside Takahito's approval in msg-8047 D-6 ("patch export 済み"), and narrower:
a rescue ref alone never makes anything deletable (msg-9901 Obj-1-2).

What is due: among the deletable candidates of one repo (oldest first by ``quarantined_at``),
those older than :data:`MAX_AGE` (7 days) or beyond the newest :data:`MAX_PER_REPO` (10). A
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


_DIFF = ("diff", "--binary", "--no-color", "--no-ext-diff", "HEAD")
_UNTRACKED = ("ls-files", "--others", "--exclude-standard", "-z")


def _local_args(cwd: Path, run: Callable[[Sequence[str]], tuple[int, bytes, bytes]]) -> list[str]:
    """The positive refs whose commits must survive: HEAD, branches, tags, and the stash."""
    args = ["HEAD", "--branches", "--tags"]
    code, _out, _err = run(("rev-parse", "-q", "--verify", "refs/stash"))
    if code == 0:
        args.append("refs/stash")
    return args


def _sha256_file(
    path: Path, deadline: float | None = None, mono: Callable[[], float] = time.monotonic
) -> str:
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


def _has_gitlink(slot: Path, run: Callable[[Sequence[str]], tuple[int, bytes, bytes]]) -> bool:
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


@dataclass
class ExportResult:
    """What :func:`export_slot` wrote. ``manifest`` is the body :func:`write_manifest` completes."""

    stem: Path
    patch: Path | None = None
    untracked_tar: Path | None = None
    bundle: Path | None = None
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return bool(self.manifest.get("complete"))


def export_slot(
    slot: Path,
    exports_root: Path,
    repo: str,
    name: str,
    *,
    untracked_cap: int = UNTRACKED_CAP,
    total_cap: int = TOTAL_CAP,
    timeout: float = EXPORT_GIT_TIMEOUT_S,
) -> ExportResult:
    """Export a slot that is about to be quarantined. Never raises: a failure is ``complete`` false.

    ``name`` makes the stem readable (``<slot>-<who>-<stamp>``); a random suffix keeps two
    quarantines of the same second apart.
    """
    out_dir = exports_root / repo
    stem = out_dir / f"{name}-{uuid.uuid4().hex[:8]}"
    res = ExportResult(stem=stem)
    m: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "repo": repo,
        "head": None,
        "patch": None,
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
        _export(slot, out_dir, stem, res, m, why, untracked_cap, total_cap, timeout)
    except Exception as exc:  # never stop the quarantine (msg-9901 §1)
        why.append(f"export_error: {type(exc).__name__}: {exc}")
    m["complete"] = not why
    return res


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
) -> None:
    def run(args: Sequence[str]) -> tuple[int, bytes, bytes]:
        return _git_bytes(slot, args, timeout)

    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    code, out, _ = run(("rev-parse", "HEAD"))
    if code != 0:
        why.append("head_unreadable")
    else:
        m["head"] = out.decode().strip()
    if _has_gitlink(slot, run):
        why.append("submodule")

    code, patch, err = run(_DIFF)
    if code != 0:
        why.append(f"diff_failed: {err.decode(errors='replace').strip()[:200]}")
    else:
        total += len(patch)
        if total > total_cap:
            why.append("total_cap")
        else:
            p = Path(f"{stem}.patch")
            p.write_bytes(patch)
            res.patch = p
            m["patch"] = {
                "file": p.name,
                "sha256": hashlib.sha256(patch).hexdigest(),
                "bytes": len(patch),
            }

    code, listing, _ = run(_UNTRACKED)
    if code != 0:
        why.append("untracked_list_failed")
        names: list[str] = []
    else:
        names = sorted(os.fsdecode(n) for n in listing.split(b"\0") if n)
    if any(n.endswith("/") for n in names):
        why.append("embedded_repo")
    files = [n for n in names if not n.endswith("/")]
    sizes = 0
    entries: list[dict[str, str]] = []
    for n in files:
        fp = slot / n
        try:
            sizes += fp.lstat().st_size
            entries.append({"path": n, "sha256": _sha256_file(fp)})
        except OSError:
            why.append(f"unreadable: {n}")
    m["untracked"] = entries
    if sizes > untracked_cap:
        why.append("untracked_cap")
    elif not any(w.startswith("unreadable") for w in why):
        if total + sizes > total_cap:
            why.append("total_cap")
        else:
            tp = Path(f"{stem}.untracked.tar")
            with tarfile.open(tp, "w") as tar:
                for e in entries:
                    tar.add(str(slot / e["path"]), arcname=e["path"], recursive=False)
            res.untracked_tar = tp
            size = tp.stat().st_size
            total += size
            m["untracked_tar"] = {"file": tp.name, "sha256": _sha256_file(tp), "bytes": size}

    pos = _local_args(slot, run)
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
        "sha256": _sha256_file(bp),
        "bytes": size,
        "verify": "ok" if vcode == 0 else "failed",
    }
    res.bundle = bp
    if vcode != 0:
        why.append("bundle_verify_failed")
    if total > total_cap:
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
        """Delete ``d`` file by file. False when the deadline stopped it or a removal failed."""
        try:
            for root, dirs, files in os.walk(d, topdown=False):
                for name in files:
                    if self._mono() > deadline:
                        return False
                    fp = Path(root) / name
                    try:
                        self._remove(fp)
                    except PermissionError:
                        os.chmod(fp, stat.S_IWRITE)  # git objects are read-only on Windows
                        self._remove(fp)
                for name in dirs:
                    dp = Path(root) / name
                    if dp.is_symlink():
                        self._remove(dp)
                    else:
                        os.rmdir(dp)
            os.rmdir(d)
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
        due: list[_Item] = []
        for items in by_repo.values():
            items.sort(key=lambda i: (i.at, str(i.path)))
            over = len(items) - self.max_per_repo
            for n, it in enumerate(items):
                assert it.at is not None
                if n < over or now - it.at >= self.max_age:
                    due.append(it)
        due.sort(key=lambda i: (i.at, str(i.path)))
        started = 0
        for it in due:
            if started >= self.max_items or self._mono() > deadline:
                res.remaining_due += 1
                continue
            started += 1
            self._verify_and_delete(it, state, res, deadline)

    def _verify_and_delete(
        self, it: _Item, state: dict[str, dict[str, Any]], res: GcResult, deadline: float
    ) -> None:
        key = _norm(it.path)
        rec = state.setdefault(key, {})
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
        if _norm(it.path) in self.leased:
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

    def run(args: Sequence[str]) -> bytes:
        try:
            code, out, err = _git_bytes(path, args, left())
        except subprocess.TimeoutExpired as exc:
            raise _TimeoutError(" ".join(args)) from exc
        except OSError as exc:
            raise _MismatchError(f"unverified: git {args[0]}: {exc}") from exc
        if code != 0:
            raise _MismatchError(
                f"unverified: git {' '.join(args)}: {err.decode(errors='replace')[:200]}"
            )
        return out

    for key in ("patch", "untracked_tar", "bundle"):
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
    patch = run(_DIFF)
    if hashlib.sha256(patch).hexdigest() != (m.get("patch") or {}).get("sha256"):
        raise _MismatchError("changed: tracked files (git diff --binary HEAD)")
    pos = ["HEAD", "--branches", "--tags"]
    try:
        code, _, _ = _git_bytes(path, ("rev-parse", "-q", "--verify", "refs/stash"), left())
    except subprocess.TimeoutExpired as exc:
        raise _TimeoutError("stash") from exc
    if code == 0:
        pos.append("refs/stash")
    commits = sorted(r for r in run(("rev-list", *pos, "--not", "--remotes")).decode().split() if r)
    if commits != sorted(m.get("local_commits") or []):
        raise _MismatchError("changed: local-only commits")
    names = sorted(os.fsdecode(n) for n in run(_UNTRACKED).split(b"\0") if n)
    saved = {str(e["path"]): str(e["sha256"]) for e in m.get("untracked") or []}
    if names != sorted(saved):
        raise _MismatchError("changed: untracked file list")
    for n in names:
        try:
            if _sha256_file(path / n, deadline, mono) != saved[n]:
                raise _MismatchError(f"changed: untracked {n}")
        except OSError as exc:
            raise _MismatchError(f"unverified: untracked {n}: {exc}") from exc
