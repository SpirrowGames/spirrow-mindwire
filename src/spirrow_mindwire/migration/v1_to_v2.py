"""ThreadMeta ``schema_version`` 1 → 2 migration (Feature 3-A skeleton bump).

Rewrites ``<data_dir>/threads/*/meta.yaml`` so the on-disk
``schema_version`` literal flips from 1 to 2. No structural changes
accompany the bump — see ``docs/feature-3-design.md`` §3.1 and the
sub-PR 1 design propose (chatroom ``T-feat3-d1-schema-skeleton``
msg-120 §2 D1-1 = A).

Behaviour:

- **Per-file atomicity**: uses :func:`atomic_write_text` so the watcher
  / readers never see a partial file (``*.tmp`` → ``os.replace``).
- **Idempotent**: meta.yaml already at v2 is skipped, so the script can
  be re-run safely after a partial run.
- **Pre-flight validation**: the v2 payload is parsed through
  :class:`ThreadMeta` *before* the file is written, so a successful
  exit guarantees every migrated file loads with the current schema.
- **Per-thread isolation**: a failure on one thread does not abort the
  run; outcomes accumulate into :class:`MigrationReport` and the CLI
  exits non-zero if any failed.

The module is intentionally narrow — it touches only ``meta.yaml`` and
nothing else (``messages/`` and ``events.jsonl`` keep their own
``schema_version`` literals, see the docstring on
``spirrow_mindwire.schema._common.SCHEMA_VERSION``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from spirrow_mindwire.config import load_settings
from spirrow_mindwire.filesystem import atomic_write_text
from spirrow_mindwire.schema import ThreadMeta

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThreadOutcome:
    """One thread's result from a migration pass."""

    thread_id: str
    outcome: str
    """One of: ``migrated`` / ``skipped_already_v2`` /
    ``skipped_unknown_version`` / ``failed``. String rather than enum so
    structured-log consumers don't have to import a Python type."""
    detail: str | None = None


@dataclass(frozen=True)
class MigrationReport:
    """Summary of one ``migrate_data_dir`` invocation."""

    migrated: tuple[ThreadOutcome, ...] = ()
    skipped_already_v2: tuple[ThreadOutcome, ...] = ()
    skipped_unknown_version: tuple[ThreadOutcome, ...] = ()
    failed: tuple[ThreadOutcome, ...] = ()

    @property
    def total_scanned(self) -> int:
        """Number of thread dirs whose ``meta.yaml`` was classified.

        Counts the four outcome buckets, so it excludes thread dirs that
        were never opened: staging dirs (``.staging-<ULID>/``), empty
        thread dirs (no ``meta.yaml``), and any non-dir entry under
        ``threads/``. In other words, ``total_scanned`` is "threads the
        migrator had an opinion about", not "directories enumerated".
        """
        return (
            len(self.migrated)
            + len(self.skipped_already_v2)
            + len(self.skipped_unknown_version)
            + len(self.failed)
        )

    @property
    def has_failures(self) -> bool:
        return len(self.failed) > 0


def migrate_data_dir(data_dir: Path, *, dry_run: bool = False) -> MigrationReport:
    """Migrate every ``meta.yaml`` under ``<data_dir>/threads/*/`` from v1 to v2.

    ``dry_run=True`` performs the scan, validation, and planning but
    writes no files; the returned report still classifies each thread as
    if a real run had happened (a ``v1`` file that would have been
    rewritten lands in ``migrated`` with ``detail="dry-run"``).

    **Field-preservation guarantee**: the rewrite only changes the
    ``schema_version`` literal (1 → 2). Other fields are copied verbatim
    from the v1 payload; no missing field is injected, no extra key is
    added. A v1 file that omits a field required by the current
    :class:`ThreadMeta` schema therefore lands in ``failed`` via the
    pre-flight validation (= ``ThreadMeta.model_validate(new_payload)``)
    rather than being silently completed — by design, since this is a
    skeleton bump that must not move structure.
    """

    threads_root = data_dir / "threads"
    if not threads_root.is_dir():
        logger.info("migration: no threads/ directory at %s; nothing to do", threads_root)
        return MigrationReport()

    migrated: list[ThreadOutcome] = []
    skipped_already_v2: list[ThreadOutcome] = []
    skipped_unknown_version: list[ThreadOutcome] = []
    failed: list[ThreadOutcome] = []

    for thread_dir in sorted(threads_root.iterdir()):
        if not thread_dir.is_dir():
            continue
        if thread_dir.name.startswith("."):
            # Skip staging dirs (`.staging-<ULID>/`) and any hidden dirs.
            continue
        meta_path = thread_dir / "meta.yaml"
        if not meta_path.is_file():
            # Some operator artefact (an empty thread dir, etc.); leave alone.
            continue

        outcome = _migrate_one(thread_dir.name, meta_path, dry_run=dry_run)
        if outcome.outcome == "migrated":
            migrated.append(outcome)
        elif outcome.outcome == "skipped_already_v2":
            skipped_already_v2.append(outcome)
        elif outcome.outcome == "skipped_unknown_version":
            skipped_unknown_version.append(outcome)
        else:
            failed.append(outcome)

    return MigrationReport(
        migrated=tuple(migrated),
        skipped_already_v2=tuple(skipped_already_v2),
        skipped_unknown_version=tuple(skipped_unknown_version),
        failed=tuple(failed),
    )


def _migrate_one(thread_id: str, meta_path: Path, *, dry_run: bool) -> ThreadOutcome:
    """Inspect one ``meta.yaml`` and (unless dry-run) rewrite it in place."""

    try:
        raw_text = meta_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error("migration: failed to read %s: %s", meta_path, e)
        return ThreadOutcome(thread_id, "failed", f"read error: {e}")

    try:
        payload = yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        logger.error("migration: failed to parse YAML %s: %s", meta_path, e)
        return ThreadOutcome(thread_id, "failed", f"yaml parse error: {e}")

    if not isinstance(payload, dict):
        logger.error("migration: meta.yaml at %s is not a mapping", meta_path)
        return ThreadOutcome(
            thread_id,
            "failed",
            f"meta.yaml is not a mapping ({type(payload).__name__})",
        )

    current_version = payload.get("schema_version")
    if current_version == 2:
        logger.info("migration: skip %s (already v2)", thread_id)
        return ThreadOutcome(thread_id, "skipped_already_v2")

    if current_version != 1:
        logger.warning(
            "migration: skip %s (unknown schema_version=%r, expected 1 or 2)",
            thread_id,
            current_version,
        )
        return ThreadOutcome(
            thread_id,
            "skipped_unknown_version",
            f"schema_version={current_version!r}",
        )

    # v1 → v2: only the literal value changes (skeleton bump).
    new_payload: dict[str, Any] = dict(payload)
    new_payload["schema_version"] = 2

    # Pre-flight validation: catches structural issues before any write
    # so an irrecoverably-broken meta.yaml can't be silently overwritten.
    try:
        ThreadMeta.model_validate(new_payload)
    except ValidationError as e:
        logger.error("migration: v2 validation failed for %s: %s", thread_id, e)
        return ThreadOutcome(thread_id, "failed", f"v2 validation failed: {e}")

    if dry_run:
        logger.info("migration: dry-run plan migrate %s (1 -> 2)", thread_id)
        return ThreadOutcome(thread_id, "migrated", "dry-run")

    new_text = yaml.safe_dump(new_payload, default_flow_style=False, sort_keys=False)
    try:
        atomic_write_text(meta_path, new_text)
    except OSError as e:
        logger.error("migration: write failed for %s: %s", meta_path, e)
        return ThreadOutcome(thread_id, "failed", f"write error: {e}")

    logger.info("migration: migrated %s (1 -> 2)", thread_id)
    return ThreadOutcome(thread_id, "migrated")


def main() -> None:
    """Entry point for the ``mindwire-migrate-v1-to-v2`` console script."""

    parser = argparse.ArgumentParser(
        prog="mindwire-migrate-v1-to-v2",
        description=(
            "Migrate `<data_dir>/threads/*/meta.yaml` from schema_version 1 "
            "to 2 (Feature 3-A skeleton bump). Idempotent and atomic."
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            "Override data_dir (default: read via load_settings(), which "
            "honours mindwire.toml and MINDWIRE_PATHS__DATA_DIR)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan, validate, and report; do not write any files.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.data_dir is not None:
        data_dir: Path = args.data_dir
    else:
        settings = load_settings()
        data_dir = settings.paths.data_dir

    logger.info("migration: scanning %s (dry_run=%s)", data_dir, args.dry_run)
    report = migrate_data_dir(data_dir, dry_run=args.dry_run)
    logger.info(
        "migration: done — migrated=%d, already_v2=%d, unknown_version=%d, failed=%d",
        len(report.migrated),
        len(report.skipped_already_v2),
        len(report.skipped_unknown_version),
        len(report.failed),
    )
    if report.has_failures:
        for outcome in report.failed:
            logger.error("migration: failed thread=%s detail=%s", outcome.thread_id, outcome.detail)
        sys.exit(1)


__all__ = ["MigrationReport", "ThreadOutcome", "main", "migrate_data_dir"]
