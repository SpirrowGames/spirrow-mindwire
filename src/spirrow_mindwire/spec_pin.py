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

import asyncio
import contextlib
import logging
import os
import tempfile
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
    returned a non-``None`` value) but the writer cannot build a resolved pin
    from it — either the spec file is not on disk, or (Phase 1) no git reader
    is wired to compute the resolved-form fields (branch / commit / blob_sha).
    Bohr msg-3991 objection 3: silently degrading to bootstrap in either case
    is fail-open — it buries a mapping fault under the annunciator that
    ``ABSENT`` / ``BOOTSTRAP`` was designed to expose. Instead, the dispatcher
    aborts loudly so the operator can see the fault (the SPEC-id the mapping
    named, the path it pointed at, and the concrete cause).

    The ``cause`` string is copied verbatim into the exception message; it MUST
    read as a full sentence that names the actual cause of the abort, because
    it is the only free-text explanation an operator sees. The message
    deliberately does not editorialise on whether the file exists — the
    caller's ``cause`` is the single source of truth for what went wrong
    (measured PR-review #335 finding: a fixed "is unreadable" prefix
    contradicted the actual cause on the Phase 1 no-git-reader path where the
    spec file existed and was readable).
    """

    def __init__(self, *, spec_id: str, expected_path: Path, cause: str, thread_id: str) -> None:
        self.spec_id = spec_id
        self.expected_path = expected_path
        self.cause = cause
        self.thread_id = thread_id
        super().__init__(
            f"cannot write resolved `.mindwire/pin` for thread {thread_id!r}: "
            f"mapping names spec_id={spec_id!r} (expected at "
            f"{expected_path.as_posix()}) but {cause}. "
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
    """Write bytes to *path* atomically, safe under concurrent writers.

    Two-step atomic replace: (1) create a per-writer unique temporary
    file in ``path.parent``, write to it in full, then (2) ``os.replace``
    it over ``path``. The rename is atomic on POSIX and on NTFS for
    same-directory renames.

    Uniqueness of the tmp file (via :func:`tempfile.mkstemp` with a
    process- and call-unique suffix) is load-bearing under concurrent
    writers (PR-review #335 round-3 finding): a hardcoded
    ``pin.tmp`` name lets two processes racing to write open the SAME
    tmp path — process A then renames tmp to ``path`` while process B
    is still writing to it, and the reader sees a torn or interleaved
    file. Per-writer tmp names uncouple the writers so each ``replace``
    installs whichever writer's own COMPLETE tmp file happened to
    finish last, and the reader sees at worst one writer's whole pin.

    Failure cleanup: if writing the tmp file raises, the leftover tmp
    is removed. ``.mindwire/`` is ``.gitignore``d (SPEC-2026-08-11
    D-5), so a crash between mkstemp and unlink leaves the tmp inside
    the untracked pin directory; a subsequent successful write will
    not reuse the name (mkstemp is unique per call).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".pin.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        # os.replace is atomic on both POSIX and NTFS for same-dir renames.
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up the tmp file on any failure so a crashed writer does
        # not leak files into the pin directory. Ignore secondary
        # errors from the cleanup itself — the primary exception is
        # re-raised.
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


@dataclass(frozen=True)
class SpecPinWriter:
    """The pin writer both dispatchers call before an implementer / naysayer dispatch.

    Two independent directories, kept SEPARATE so the two dispatchers can
    hold them right (PR-review #335 round-2 finding: conflating the two
    breaks the Phase 0/1 path where the pin lives inside the SDK's
    per-thread scratch dir and no spec source exists there at all):

    - ``pin_target_dir``: the directory whose ``./.mindwire/pin`` is the
      output file. This is the SDK invoke's cwd — where the agent reads
      the pin from on entry. For the Stage 3 T13
      :class:`~spirrow_mindwire.dispatcher.core.Dispatcher` this is the
      target git checkout; for the Phase 0/1
      :class:`~spirrow_mindwire.watcher.dispatcher.ThreadDispatcher` it
      is ``layout.thread_dir`` (per-thread scratch).
    - ``spec_source_root``: the directory whose ``./spec/design/`` holds
      spec files for resolved-pin construction. ``None`` means the caller
      does not support resolved-pin construction on this writer — a
      mapping that returns a non-``None`` ``spec_id`` on such a writer
      aborts with :class:`PinDispatchAbortError` naming the missing
      source-root as the cause (fail-loud per msg-3991 objection 3,
      never silent bootstrap degradation).

    ``mapping`` (default :data:`EMPTY_MAPPING`) is the thread → SPEC-id
    table. Phase 1 leaves it empty; every dispatch writes bootstrap.

    ``write_before_dispatch(role, thread_id)`` is the single call site:

    - If ``role`` is not in :data:`_PIN_REQUIRED_ROLES` (proposer today),
      the call is a no-op — D-32's mandate targets implementer / naysayer
      dispatch.
    - If the mapping returns ``None`` for ``thread_id``, a bootstrap pin
      is written.
    - If the mapping returns a ``spec_id``, resolved-pin construction is
      required. In this codebase it always aborts loudly with
      :class:`PinDispatchAbortError` — either because no
      ``spec_source_root`` was configured, because the mapped file is
      not on disk under that root, or because Phase 1 wires no git reader
      to compute the resolved-form fields. All three aborts are
      operational faults per msg-3991 objection 3; degrading to bootstrap
      would bury any one of them under the BOOTSTRAP annunciator.

    Concurrency note: two concurrent dispatches on the same
    ``pin_target_dir`` would race the file. Within-process races are
    already prevented by higher layers — the T13 dispatcher serialises
    per-session (I9 FIFO), and the Phase 0/1 ThreadDispatcher uses a
    per-thread asyncio lock — so on the "one-role-per-repo-clone"
    production topology no two writers coexist in a single interpreter.
    Cross-process races (two Stage 3 loops on the same clone) are a
    configuration error the deployment layer prevents, but the writer
    remains defensive against them: :func:`_atomic_write` uses a
    per-writer unique tmp filename (:func:`tempfile.mkstemp`) so a
    racing pair of processes cannot corrupt each other's write, and a
    reader still observes at worst one writer's whole complete pin
    (PR-review #335 round-3: a hardcoded ``pin.tmp`` name silently
    voided this guarantee — the fix is the tmp filename, not the
    rename step).
    """

    pin_target_dir: Path
    spec_source_root: Path | None = None
    mapping: SpecPinMapping = EMPTY_MAPPING

    async def write_before_dispatch_async(self, role: Role, thread_id: str) -> None:
        """Async wrapper around :meth:`write_before_dispatch` for asyncio callers.

        Both dispatchers that call this writer live inside ``async def``
        coroutines (the T13
        :meth:`~spirrow_mindwire.dispatcher.core.Dispatcher.dispatch` and
        the Phase 0/1
        :meth:`~spirrow_mindwire.watcher.dispatcher.ThreadDispatcher._run_thread`).
        The write itself is small-file blocking I/O (``mkstemp`` + a
        few-hundred-byte YAML write + ``os.replace``), and running it
        inline on the event loop would block every other task the loop
        holds for as long as the disk takes to accept the write. PR-review
        #335 round-4 raised this as ADVISORY, and the human decision
        before merge was: defer it.

        The deferral uses :func:`asyncio.to_thread` (Python 3.9+ high-level
        wrapper over the running loop's default executor). This preserves
        two guarantees the callers rely on:

        - **Fail-loud propagation.** ``asyncio.to_thread`` re-raises the
          sync exception in the awaiting coroutine — a
          :class:`PinDispatchAbortError` still stops the dispatch on the
          call site's own frame (msg-3991 objection 3).
        - **Ordering.** ``await`` on the coroutine returned here does not
          return until the sync work has completed, so callers may safely
          rely on the pin being on disk when the next line runs (T13
          ``deliver_event`` / Phase 0/1 ``invoke_claude_code`` — the
          "直前に" invariant of §2.1 D-32).

        The synchronous :meth:`write_before_dispatch` remains public for
        tests and for the (rare) synchronous caller a successor spec may
        introduce; do not remove it without amending both surfaces at once.
        """
        await asyncio.to_thread(self.write_before_dispatch, role, thread_id)

    def write_before_dispatch(self, role: Role, thread_id: str) -> None:
        """Write ``.mindwire/pin`` before an implementer / naysayer dispatch.

        No-op for roles outside :data:`_PIN_REQUIRED_ROLES`; a full pin
        write (bootstrap) or a fail-loud abort for the required roles.

        This is the **synchronous** entry point. Async callers on the event
        loop should await :meth:`write_before_dispatch_async` instead so the
        blocking file I/O runs on the default thread-pool executor rather
        than stalling the loop (PR-review #335 round-4 advisory).
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
        # spec_id present — mapping wants a resolved pin. Three possible
        # aborts, each with a distinct `cause` that reads accurately as a
        # standalone sentence (:class:`PinDispatchAbortError` docstring).
        # All three are operational faults per msg-3991 objection 3 —
        # degrading to bootstrap would bury any of them under BOOTSTRAP.
        if self.spec_source_root is None:
            # (a) The caller (typically a per-thread scratch-dir writer)
            #     did not wire a spec source at all. This is the shape
            #     that the Phase 0/1 ThreadDispatcher takes — a mapping
            #     returning a non-None spec_id there is a configuration
            #     error, not a request the writer can honour.
            raise PinDispatchAbortError(
                spec_id=spec_id,
                expected_path=Path("<no spec_source_root configured>"),
                cause=(
                    "this writer was configured without a spec_source_root, "
                    "so resolved-pin construction is not supported on it "
                    "(the mapping returned a spec_id but the writer has no "
                    "source tree to resolve it against)"
                ),
                thread_id=thread_id,
            )
        expected_path = self.spec_source_root / "spec" / "design" / f"{spec_id}.md"
        if not expected_path.is_file():
            # (b) The spec source exists but the mapped file is missing.
            raise PinDispatchAbortError(
                spec_id=spec_id,
                expected_path=expected_path,
                cause=(
                    "the mapped spec file is not on disk "
                    "(resolved-pin construction requires the landed spec)"
                ),
                thread_id=thread_id,
            )
        # (c) Phase 1 stops here: resolved-pin construction needs git
        #     shell (branch, commit, blob_sha) which is not wired at the
        #     composition root today. When a successor spec introduces a
        #     mapping table that actually points at a spec, it will also
        #     wire the git reader.
        raise PinDispatchAbortError(
            spec_id=spec_id,
            expected_path=expected_path,
            cause=(
                "this build wires no git reader for resolved-pin construction "
                "(Phase 1 mapping = bootstrap only; the file exists on disk)"
            ),
            thread_id=thread_id,
        )

    def _write(self, pin: Mapping[str, object]) -> None:
        target = self.pin_target_dir / _PIN_RELATIVE_PATH
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
