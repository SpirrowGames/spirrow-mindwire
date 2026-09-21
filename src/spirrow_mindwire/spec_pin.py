"""`.mindwire/pin` writer — SPEC-2026-09-20-pin-hardening-and-id-audit I-3.

The dispatcher (both the T13 :class:`~spirrow_mindwire.dispatcher.core.Dispatcher`
and the Phase 0/1 :class:`~spirrow_mindwire.watcher.dispatcher.ThreadDispatcher`)
writes ``.mindwire/pin`` into the working tree BEFORE every implementer /
naysayer dispatch (SPEC-2026-09-20 §2.1 D-32, §3-B, §3-C).

Design in one paragraph. Absent-pin means "the dispatcher did not do its job"
under the SPEC-2026-09-20 body of ``OBL-SPEC-PIN``; every dispatch must
therefore carry a pin. A pin is written in one of two shapes: **resolved**
(§3-A, when the dispatcher knows the SPEC-id for the thread) or
**bootstrap** (§3-B, when it does not — the sanctioned proceed-on-body
code). Which shape is written is decided by the mapping table
(:class:`SpecPinMapping`): if the thread is mapped to a spec_id and that
spec file is readable, write resolved; if the thread is unmapped, write
bootstrap; if the thread IS mapped but the spec file cannot be read, that
is an operational fault — the dispatcher raises :class:`PinDispatchAbortError`
rather than silently degrading to bootstrap (Bohr msg-3991 objection 3:
"mapping fault を bootstrap の annunciator で埋葬してしまう" is fail-open
and forbidden).

The canonical bootstrap pin values follow §3-C verbatim:

  ``schema_version: 1``
  ``mode: bootstrap``
  ``pinned_at: <ISO-8601 UTC "Z">``
  ``pinned_by: dispatcher``
  ``reason: "..."``

`pinned_by: dispatcher` is the canonical bootstrap value per §3-C; a
non-code role identifier is not applied here because the entity writing
the pin is the dispatcher (msg-3991 rejection of Einstein Objection 1:
the identity role registry names loop-role authors, and `pinned_by` is
the pin-file-writer identity — a different domain).

The 7 resolved-form fields (``spec_id`` / ``thread`` / ``repo`` /
``branch`` / ``path`` / ``blob_sha`` / ``commit``) are explicitly NOT
present on a bootstrap pin (§3-A: mixing any of them fires
``PROHIBITED_FIELD`` on the reader side). The writer does not set
``mode: resolved`` explicitly in a resolved-form pin either — §3-A
declares it as ``optional (default: resolved)``, and omitting it
minimises byte-drift with pre-SPEC-2026-09-20 pins that never had a
``mode`` field.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import yaml

from .value_objects import Role

logger = logging.getLogger(__name__)

_PIN_RELATIVE_PATH = Path(".mindwire") / "pin"

# Roles whose dispatch requires a `.mindwire/pin` write per SPEC-2026-09-20
# §2.1 D-32. The proposer runs in the same repo, but the spec's target of
# the OBL-SPEC-PIN body is the implementer / naysayer face (see role fields
# in spec/process/obligations.yaml); D-32 speaks only to "implementer /
# naysayer dispatch". Widening this set is a spec change, not an
# implementation choice — new roles that need a pin get added when a
# successor spec says so.
_PIN_REQUIRED_ROLES: frozenset[Role] = frozenset({Role.IMPLEMENTER, Role.NAYSAYER})

# The canonical dispatcher identity written into `pinned_by` for bootstrap
# pins (SPEC-2026-09-20 §3-C canonical example). See module docstring for
# why this value is `dispatcher` rather than one of the 7 registry roles.
DISPATCHER_PINNED_BY: str = "dispatcher"


class SpecPinError(Exception):
    """Base class for spec-pin writer errors."""


class PinDispatchAbortError(SpecPinError):
    """The dispatch cannot proceed because a resolved pin cannot be constructed.

    Raised when a thread has a mapped spec_id (``SpecPinMapping.spec_id_for``
    returned a non-``None`` value) but the spec file is missing or unreadable.
    Bohr msg-3991 objection 3: silently degrading to bootstrap in this case is
    fail-open — it buries a mapping fault under the annunciator that
    ``ABSENT`` / ``BOOTSTRAP`` was designed to expose. Instead, the dispatcher
    aborts loudly so the operator can see the fault (the missing SPEC-id, the
    expected path, and the cause).
    """

    def __init__(self, *, spec_id: str, expected_path: Path, cause: str, thread_id: str) -> None:
        self.spec_id = spec_id
        self.expected_path = expected_path
        self.cause = cause
        self.thread_id = thread_id
        super().__init__(
            f"cannot write resolved `.mindwire/pin` for thread {thread_id!r}: "
            f"mapping names spec_id={spec_id!r} but "
            f"{expected_path.as_posix()} is unreadable ({cause}). "
            "Refusing to degrade to bootstrap (mapping fault must not be buried "
            "under the BOOTSTRAP annunciator — SPEC-2026-09-20 §3-B, Bohr msg-3991)."
        )


class SpecPinMapping(Protocol):
    """Thread-id → SPEC-id mapping consulted by :class:`SpecPinWriter`.

    Returns ``None`` when the mapping has no entry for a thread (the
    dispatcher then writes a bootstrap pin — §3-B step 3.5 branch 1).
    Returns a ``spec_id`` string when a spec is in force for the thread
    (the dispatcher writes a resolved pin — §3-A).

    Bohr msg-3991 disposition of Objection 3 draws the line:

    - mapping absent (this method returns ``None``) → bootstrap
    - mapping present + spec file readable → resolved
    - mapping present + spec file unreadable → :class:`PinDispatchAbortError`

    Phase 1 (this PR): no production mapping is wired — every dispatch
    writes bootstrap. The protocol exists so a successor spec that mints
    a real mapping (dispatcher-side thread→spec table) can be added
    without touching the pin-writer's shape.
    """

    def spec_id_for(self, thread_id: str) -> str | None: ...


class _EmptyMapping:
    """The Phase-1 default mapping — every thread returns ``None`` (bootstrap).

    Satisfies :class:`SpecPinMapping` structurally; no code depends on the
    class name. Kept as a concrete class rather than a lambda so the
    default is inspectable in tracebacks.
    """

    def spec_id_for(self, thread_id: str) -> str | None:
        del thread_id
        return None


EMPTY_MAPPING: SpecPinMapping = _EmptyMapping()


def _utc_now_iso_z() -> str:
    """ISO-8601 UTC with a literal ``Z`` suffix — the §3-C canonical form."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_bootstrap_pin(
    *,
    thread_id: str,
    pinned_by: str = DISPATCHER_PINNED_BY,
    pinned_at: str | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    """Build the pin dict for a bootstrap-mode pin (§3-B, §3-C).

    The result serialises to YAML that mirrors §3-C's canonical bootstrap
    example: ``schema_version: 1`` / ``mode: bootstrap`` / ``pinned_at`` /
    ``pinned_by`` / optional ``reason``. The 7 resolved-form fields are
    NOT present — including any of them would fire ``PROHIBITED_FIELD``
    at the reader (§3-A).
    """
    pin: dict[str, object] = {
        "schema_version": 1,
        "mode": "bootstrap",
        "pinned_at": pinned_at or _utc_now_iso_z(),
        "pinned_by": pinned_by,
    }
    if reason is None:
        reason = f"no spec-pin mapping for thread {thread_id} at dispatch time"
    pin["reason"] = reason
    return pin


def build_resolved_pin(
    *,
    spec_id: str,
    thread_id: str,
    repo: str,
    branch: str,
    path: str,
    blob_sha: str,
    commit: str,
    pinned_at: str | None = None,
    pinned_by: str = DISPATCHER_PINNED_BY,
) -> dict[str, object]:
    """Build the pin dict for a resolved-mode pin (§3-A, §3-C).

    ``mode`` is deliberately not written: §3-A declares it optional with
    default ``resolved``, and omitting it keeps the pin byte-identical to
    pre-SPEC-2026-09-20 resolved pins the reader also accepts.
    """
    return {
        "schema_version": 1,
        "spec_id": spec_id,
        "thread": thread_id,
        "repo": repo,
        "branch": branch,
        "path": path,
        "blob_sha": blob_sha,
        "commit": commit,
        "pinned_at": pinned_at or _utc_now_iso_z(),
        "pinned_by": pinned_by,
    }


def pin_yaml_bytes(pin: Mapping[str, object]) -> bytes:
    """Render a pin dict to the bytes we write to `.mindwire/pin`.

    ``sort_keys=False`` preserves the caller's key order (the field order
    mirrors the §3-C example, which is what a human reader will compare
    against). ``default_flow_style=False`` forces block-style YAML — the
    reader's `_resolve_pin` uses `yaml.safe_load` and accepts either, but
    block style is what §3-C shows.
    """
    text = yaml.safe_dump(dict(pin), sort_keys=False, default_flow_style=False)
    return text.encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    """Write bytes to *path* atomically (write to `.tmp`, then rename).

    The rename is atomic on POSIX and on NTFS for same-directory renames,
    so a reader observing the pin file sees either the previous complete
    content or the new complete content — never a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


@dataclass(frozen=True)
class SpecPinWriter:
    """The pin writer both dispatchers call before an implementer / naysayer dispatch.

    Constructed with a target repo root (where ``.mindwire/pin`` lives) and,
    optionally, a :class:`SpecPinMapping` (defaults to :data:`EMPTY_MAPPING`,
    which reports no mapping for every thread and forces a bootstrap pin).

    ``write_before_dispatch(role, thread_id)`` is the single call site:

    - If ``role`` is not in :data:`_PIN_REQUIRED_ROLES` (proposer today),
      the call is a no-op — D-32's mandate targets implementer / naysayer
      dispatch.
    - If the mapping returns ``None`` for ``thread_id``, a bootstrap pin
      is written.
    - If the mapping returns a ``spec_id`` and the spec file is readable,
      a resolved pin is written (with real git blob_sha / HEAD sha).
    - If the mapping returns a ``spec_id`` and the spec file is NOT
      readable, :class:`PinDispatchAbortError` is raised (Bohr msg-3991
      objection 3 — no silent degradation).

    Concurrency note: two concurrent dispatches on the same repo_root
    would race the file. The T13 :class:`~spirrow_mindwire.dispatcher.
    core.Dispatcher` serialises per-session (I9 FIFO), and the Phase 0/1
    :class:`~spirrow_mindwire.watcher.dispatcher.ThreadDispatcher` uses
    a per-thread asyncio lock, so within-process races are impossible on
    the "one-role-per-repo-clone" production topology (each Stage 3 loop
    clone runs at most one adapter of a given role at a time). Cross-
    process races (two Stage 3 loops on the same clone) are a
    configuration error the deployment layer prevents — this writer's
    ``_atomic_write`` still guarantees the file is never observed
    half-written, which is what the reader depends on.
    """

    repo_root: Path
    mapping: SpecPinMapping = EMPTY_MAPPING
    # Optional git shell function for resolved-pin construction. Left None
    # in Phase 1 (no production mapping wires a real resolved pin). When a
    # successor spec adds mapping infrastructure, it will pass a real
    # git-reader callable; the shape stays a Protocol seam so this module
    # never grows a hard git dependency.

    def write_before_dispatch(self, role: Role, thread_id: str) -> None:
        """Write ``.mindwire/pin`` before an implementer / naysayer dispatch.

        No-op for roles outside :data:`_PIN_REQUIRED_ROLES`; a full pin
        write (bootstrap or resolved) for the required roles.
        """
        if role not in _PIN_REQUIRED_ROLES:
            return
        spec_id = self.mapping.spec_id_for(thread_id)
        if spec_id is None:
            pin = build_bootstrap_pin(thread_id=thread_id)
            self._write(pin)
            logger.debug(
                "wrote bootstrap `.mindwire/pin` for thread %s (role=%s)",
                thread_id,
                role.value,
            )
            return
        # spec_id present — mapping wants a resolved pin. If the spec file
        # is unreachable, abort loudly (msg-3991 objection 3). Resolved-pin
        # construction requires a git reader; Phase 1 does not wire one, so
        # a mapping that returns a spec_id today is a configuration error
        # (no code path constructs that mapping). The abort message names
        # the SPEC-id + path so an operator can see what was missing.
        expected_path = self.repo_root / "spec" / "design" / f"{spec_id}.md"
        if not expected_path.is_file():
            raise PinDispatchAbortError(
                spec_id=spec_id,
                expected_path=expected_path,
                cause=(
                    "spec file not found under spec/design/ "
                    "(resolved-pin construction requires the landed spec)"
                ),
                thread_id=thread_id,
            )
        # Phase 1 stops here: resolved-pin construction needs git shell
        # (branch, commit, blob_sha) which is not wired at the composition
        # root today. When a successor spec introduces a mapping table
        # that actually points at a spec, it will also wire the git
        # reader; until then, a spec_id-returning mapping is unreachable
        # from the composition root and this branch is defensive.
        raise PinDispatchAbortError(
            spec_id=spec_id,
            expected_path=expected_path,
            cause=(
                "resolved-pin construction requires a git reader that "
                "this build does not wire (Phase 1 mapping = bootstrap only)"
            ),
            thread_id=thread_id,
        )

    def _write(self, pin: Mapping[str, object]) -> None:
        target = self.repo_root / _PIN_RELATIVE_PATH
        _atomic_write(target, pin_yaml_bytes(pin))


__all__ = [
    "DISPATCHER_PINNED_BY",
    "EMPTY_MAPPING",
    "PinDispatchAbortError",
    "SpecPinError",
    "SpecPinMapping",
    "SpecPinWriter",
    "build_bootstrap_pin",
    "build_resolved_pin",
    "pin_yaml_bytes",
]
