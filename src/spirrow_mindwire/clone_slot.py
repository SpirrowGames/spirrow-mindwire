"""Pooled clone slots: the path layout (T-ephemeral-impl-clone-slot-per-thread, A1).

The human ruling of 2026-11-17 (msg-9385, option A) replaced the per-thread fixed path of D-1
with a finite pool lent one turn at a time. The revised D-1 (Bohr msg-9386 §D-1 改訂 1) fixes
the layout this module names:

* slot ``<slots_root>/<repo>/slot-NN/`` with ``NN`` = ``01`` .. ``slots_per_repo``
  (two digits, more when ``slots_per_repo`` exceeds 99),
* bare mirror ``<slots_root>/<repo>.git``,
* quarantine ``<slots_root>/_quarantine/<repo>/``.

A slot belongs to no thread: which turn holds it is the lease record (``state/slot-leases.json``,
A2), not the path. The earlier per-thread layout (``<hash12>[-N]`` with an owner file) is gone
with the per-thread path it served. Pure path arithmetic only: no git command runs here and
nothing is created or deleted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SLOT_PREFIX = "slot-"
_SLOT_NAME = re.compile(r"^slot-(\d{2,})$")

# Park codes named by the spec. The owner-file codes of the per-thread layout are gone with it.
PARK_PRESERVE_FAILED = "clone_slot_preserve_failed"
PARK_PROVISION_FAILED = "clone_slot_provision_failed"
PARK_UNRECOVERABLE = "clone_slot_unrecoverable"
PARK_QUARANTINE_UNCONFIRMED = "clone_slot_quarantine_unconfirmed"
PARK_INDEX_LOCK_STALE = "clone_slot_index_lock_stale"  # Bohr msg-8206
PARK_SESSION_UNKNOWN = "clone_slot_session_unknown"  # Bohr msg-8234 判断 2
# msg-9386 D-1 改訂 7: the slot path has no trusted entry; only the turn that would use it parks.
PARK_WORKSPACE_UNTRUSTED = "workspace-untrusted"
# Written by the sweep wrapper (not SlotPrep) when `mindwire slot-prep` exits 1 or with a code it
# does not define: parked on that tick, no counter anywhere (Bohr msg-8236, Obj-18-1).
PARK_PREP_CRASHED = "clone_slot_prep_crashed"
PARK_GIT_DIVERGED = "git.diverged"
PARK_GIT_PUSH_FAILED = "git.push_failed"


def slot_name(n: int) -> str:
    """``slot-NN`` for the 1-based pool index ``n`` (``slot-01`` .. ``slot-99``, then 3 digits)."""
    if n < 1:
        raise ValueError(f"slot index is 1-based, got {n}")
    return f"{SLOT_PREFIX}{n:02d}"


def slot_index(name: str) -> int | None:
    """The pool index of a ``slot-NN`` directory name, or ``None`` for anything else."""
    m = _SLOT_NAME.match(name)
    if m is None or int(m.group(1)) < 1 or m.group(1) != f"{int(m.group(1)):02d}":
        return None
    return int(m.group(1))


def repo_root(slots_root: Path, repo: str) -> Path:
    return slots_root / repo


def slot_dir(slots_root: Path, repo: str, n: int) -> Path:
    return repo_root(slots_root, repo) / slot_name(n)


def slot_dirs(slots_root: Path, repo: str, slots_per_repo: int) -> list[Path]:
    """Every slot of ``repo``'s pool, in index order (``slot-01`` first)."""
    if slots_per_repo < 1:
        raise ValueError(f"slots_per_repo must be >= 1, got {slots_per_repo}")
    return [slot_dir(slots_root, repo, n) for n in range(1, slots_per_repo + 1)]


def mirror_path(slots_root: Path, repo: str) -> Path:
    return slots_root / f"{repo}.git"


def quarantine_root(slots_root: Path, repo: str) -> Path:
    return slots_root / "_quarantine" / repo


@dataclass(frozen=True)
class SlotRef:
    """One slot of one repo's pool: what a lease (A2) names."""

    slots_root: Path
    repo: str
    index: int

    @property
    def name(self) -> str:
        return slot_name(self.index)

    @property
    def path(self) -> Path:
        return slot_dir(self.slots_root, self.repo, self.index)
