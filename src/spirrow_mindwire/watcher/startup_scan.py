"""Startup full-scan for state-based recovery (Feature 2 sub-PR 1).

When the watcher starts, it walks ``threads/`` and re-injects
:class:`ThreadEvent` for any thread that still needs work. The
``meta.yaml.status`` field drives the decision:

- ``active`` / ``retrying``: enqueue a synthetic event so the
  dispatcher picks the thread up. The dispatcher's existing
  per-thread lock + dedup machinery handles concurrency with the live
  observer; if the latest message is already from claude-code, the
  same logic that handles the live path short-circuits via
  :meth:`ThreadDispatcher._run_thread`.
- ``terminated`` / ``resolved`` / ``archived``: skip (terminal states
  must not be auto-revived; operator manual transition is the only
  re-entry path, see ``docs/feature-2-design.md`` §3.6).

See ``docs/feature-2-design.md`` §2.1.

**Race-gap summary metric** (Feature 3-A sub-PR 3, D3-1 option-a — chatroom
``T-feat3-d3-single-writer-crack`` msg-131): once claude.ai writes via
``mindwire-mcp-server`` (single-writer crack, sub-PR 3), the dispatcher
and the write server can race on the same thread's ``next_seq``
(msg-127 §1 D2-6 accepted Phase 1 MVP race). This module's startup walk
is the natural place to *observe* the structural fallout cheaply: see
:func:`_detect_race_gap`. The scan emits one structured summary log
line (``startup_scan race-gap summary: ...``) so downstream log
aggregation can compute the cross-startup gap rate without any
in-process persistence — that rate is the quantitative input for the
sub-PR 4 (2-phase-commit re-design) trigger judgement. The
race-rate→trigger *threshold* itself is intentionally deferred to the
sub-PR 4 propose (msg-131 §5): pinning a speculative threshold before
real dogfooding observation would make the judgement circular.

This is purely observational — it never changes requeue behaviour.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import yaml

from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.lifecycle import REQUEUE_STATES
from spirrow_mindwire.schema import Message

from .events import ThreadEvent
from .loader import load_messages, load_thread_meta

logger = logging.getLogger(__name__)


def _detect_race_gap(messages: list[Message]) -> str | None:
    """Detect a structural anomaly in the seq sequence (D3-1 option-a).

    Two signatures of a cross-process write race
    (dispatcher=claude-code vs mindwire-mcp-server=claude.ai both
    computing the same ``next_seq``):

    - **duplicate seq**: two message files share a seq. Different
      ``from`` suffixes (``-from-cc`` vs ``-from-cai``) mean both files
      survive on disk — the canonical structural trace of the race.
    - **seq hole**: the sequence is non-contiguous. A lost write can
      leave a gap.

    ``messages`` is sorted by seq (loader contract). Returns a short
    reason string if an anomaly is found, else ``None``.

    Both checks are single-pass O(n): duplicates via :class:`Counter`,
    holes via an adjacent-pair scan that reports compact
    ``(lo, hi)`` ranges. Neither materialises ``range(min, max)``, so a
    corrupted huge seq cannot blow up startup memory/time (PR #47
    Copilot review).

    BLIND SPOT (= D3-1 option-a known trade-off, naysayer Q4 case (b),
    msg-131 §2): if both racing writers used the *same* filename (same
    seq **and** same ``from`` suffix), ``os.replace`` later-wins leaves
    exactly one file and no structural trace — this function cannot see
    it. Detecting that needs runtime conflict detection (option-b/option-c), which is
    deferred (a option-b/option-c-switch revisit is triggered if dogfooding reports
    "meta consistent but message content unexpected"; see
    docs/feature-3-design.md §2.4).
    """
    if not messages:
        return None
    seqs = [m.seq for m in messages]
    dups = sorted(s for s, c in Counter(seqs).items() if c > 1)
    if dups:
        return f"duplicate_seq={dups}"
    # seqs is sorted (loader contract) and duplicate-free here. Report
    # holes as compact (lo, hi) ranges via an adjacent-pair scan — never
    # enumerate every missing int, so a corrupted huge seq stays cheap.
    holes = [(prev + 1, cur - 1) for prev, cur in pairwise(seqs) if cur > prev + 1]
    if holes:
        return f"seq_hole missing_ranges={holes}"
    return None


def startup_full_scan(
    base_dir: Path,
    queue: asyncio.Queue[ThreadEvent],
    *,
    now: datetime | None = None,
) -> int:
    """Walk ``threads/`` and enqueue events for re-queueable thread states.

    For each ``threads/<ULID>/`` directory:

    1. Skip if the directory name isn't a valid ULID (e.g.
       ``.staging-<ULID>/``).
    2. Skip if ``meta.yaml`` fails to parse / validate (logged at WARNING).
    3. If ``meta.status`` is in :data:`spirrow_mindwire.lifecycle.REQUEUE_STATES`
       and the thread has at least one message, enqueue a synthetic
       :class:`ThreadEvent` for the latest message.
    4. Otherwise log at INFO (terminal-state skip) or WARNING (no
       messages but non-terminal status) and move on.

    Args:
        base_dir: ``settings.paths.data_dir``.
        queue: the asyncio queue the live observer also feeds.
        now: optional override for ``detected_at`` (test-only injection;
            defaults to ``datetime.now(UTC)``).

    Returns:
        Count of events enqueued.
    """
    threads_root = base_dir / "threads"
    if not threads_root.is_dir():
        return 0

    detected_at = now if now is not None else datetime.now(UTC)
    enqueued = 0
    # D3-1 option-a race-gap observation (msg-131 §2). Counted only over
    # requeue-path threads with messages on disk — the race
    # (dispatcher vs mindwire-mcp-server) only happens on
    # active/retrying threads, so terminal threads are out of scope by
    # construction (no scope expansion).
    gap_scanned = 0
    gap_anomalies: list[str] = []

    for thread_dir in sorted(threads_root.iterdir()):
        if not thread_dir.is_dir():
            continue
        if thread_dir.name.startswith("."):
            # ``.staging-<ULID>/`` etc. — incomplete / hidden, never a final thread.
            continue

        try:
            layout = ThreadDirLayout(base_dir=base_dir, thread_id=thread_dir.name)
        except ValueError:
            logger.warning("startup_scan: skipping non-ULID dir %s", thread_dir.name)
            continue

        try:
            meta = load_thread_meta(layout)
        except (FileNotFoundError, OSError, ValueError, yaml.YAMLError):
            logger.warning(
                "startup_scan: failed to load meta.yaml for %s",
                thread_dir.name,
                exc_info=True,
            )
            continue

        if meta.status not in REQUEUE_STATES:
            logger.info(
                "startup_scan: skipping %s (status=%s, terminal)",
                thread_dir.name,
                meta.status,
            )
            continue

        try:
            messages = load_messages(layout)
        except (FileNotFoundError, OSError, ValueError, yaml.YAMLError):
            logger.warning(
                "startup_scan: failed to load messages for %s",
                thread_dir.name,
                exc_info=True,
            )
            continue

        if not messages:
            logger.warning(
                "startup_scan: %s has no messages but status=%s; skipping",
                thread_dir.name,
                meta.status,
            )
            continue

        # D3-1 option-a: observe (never gate) the structural race signature.
        gap_scanned += 1
        gap_reason = _detect_race_gap(messages)
        if gap_reason is not None:
            gap_anomalies.append(f"{thread_dir.name}:{gap_reason}")
            logger.warning(
                "startup_scan: race-gap anomaly on %s (%s); requeueing normally "
                "(observation only, not a gate)",
                thread_dir.name,
                gap_reason,
            )

        latest = messages[-1]
        evt = ThreadEvent(
            thread_id=thread_dir.name,
            seq=latest.seq,
            path=layout.message_path(latest.seq, latest.from_),
            detected_at=detected_at,
        )
        queue.put_nowait(evt)
        enqueued += 1
        logger.info(
            "startup_scan: enqueued %s (status=%s, seq=%d)",
            thread_dir.name,
            meta.status,
            latest.seq,
        )

    # D3-1 option-a: one structured summary line per scan. Downstream log
    # aggregation sums these across startups to derive the cross-startup
    # gap rate (= sub-PR 4 trigger input) — no in-process persistence,
    # which keeps this minimal (msg-131 §2 / ClaudeCode review §1.4).
    # The anomalies list is capped so a corrupt/adversarial data dir with
    # many anomalous threads can't produce an unbounded log line (PR #47
    # Copilot review); full per-thread detail is in the WARNING logs above.
    gap_rate = (len(gap_anomalies) / gap_scanned) if gap_scanned else 0.0
    _max_shown = 20
    shown = gap_anomalies[:_max_shown]
    extra = len(gap_anomalies) - len(shown)
    anomalies_repr = f"{shown}" + (f" ...(+{extra} more)" if extra else "")
    logger.info(
        "startup_scan race-gap summary: scanned=%d gap_detected=%d gap_rate=%.3f anomalies=%s",
        gap_scanned,
        len(gap_anomalies),
        gap_rate,
        anomalies_repr,
    )

    return enqueued


__all__ = ["startup_full_scan"]
