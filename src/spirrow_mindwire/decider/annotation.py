"""Jev annotation for a ``NEXT: human`` that reaches the human (T-decider-conductor-hook 2e-2).

Spec: Bohr msg-5141 §2e-2 (endorsed Einstein msg-5142 / msg-5144), carried into this PR by
msg-6626 §3. Under ``[decider.tierc] mode = "annotate"`` (and ``"bounce"``, which acts on top of
it), a role's ``NEXT: human`` that the gate let through and that actually stops at the human gets
**one display line** in the digest::

    Jev: should_ask_human=0.12 (未検証、AUC 未確立) — R3

(the live line keeps the full-width parentheses of msg-5141; see :func:`annotation_line`), and
rows with a lower ``p`` are listed lower. Delivery, stopping and routing are unchanged (D20): the
line is written after ``_route`` / the gate have already decided, and nothing reads it back
into a decision.

**Write side.** The Conductor appends one :class:`TierCAnnotation` per annotated stop to
``<data_dir>/state/tierc_annotations.jsonl`` (:func:`append_annotation`; the path is
:func:`spirrow_mindwire.config.resolve_tierc_annotations_path`). Only an *actionable*
tierc-v2 verdict is annotated (msg-4184: acting and displaying code read ``actionable_verdict``
only), whatever its kind — ``CONFIRMED`` / ``UNSURE`` / ``LIKELY_NOT`` all show their ``p``. A
write failure is a WARNING and changes nothing: the row is simply shown without the line. The
file is bounded: an append that leaves it over :data:`COMPACT_AT_BYTES` rewrites it to its newest
:data:`KEEP_LINES` lines (:func:`compact_annotations`).

**Read side.** ``scripts/parked_humans.py --annotations <path>`` joins the file on
``(project, thread_id, head_msg_id)`` and hands the sweep the rendered line
(:func:`annotation_line`), so the wording is spelled once, here, and the PowerShell digest only
places it. Last row per key wins. A missing file is "nothing annotated"; an unreadable line is
skipped and counted.

**Reader of the payload.** Takahito, through the daily digest. This module never posts to a
chatroom thread, so no chatroom fallback surface is involved.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from spirrow_mindwire.decider.result import DecisionResult
from spirrow_mindwire.decider.verdict import TierCV2Verdict

logger = logging.getLogger(__name__)

COMPACT_AT_BYTES = 1024 * 1024
"""After an append leaves the annotations file larger than this, it is compacted (#465 PR-gate
advisory on 224d068: the file was append-only and is read whole on every sweep)."""

KEEP_LINES = 2000
"""How many of the newest lines a compaction keeps. The digest needs the annotation of a head
parked on a human *now*; 2000 escalations is months of the loop's traffic. If a still-parked
head's line were ever the one dropped, its row shows without the Jev line — the same fail-open
state as a missing file."""

ANNOTATING_TIERC_MODES: frozenset[str] = frozenset({"annotate", "bounce"})
"""``[decider.tierc].mode`` values that write annotations. ``bounce`` acts on ``LIKELY_NOT`` by
bouncing; every head it lets through is still shown with its ``p``."""

NO_RULE_TEXT = "該当ルールなし"
"""The ``<該当ルール>`` slot when Jev's result named no matched rule."""


@dataclass(frozen=True)
class TierCAnnotation:
    """One annotated ``NEXT: human`` head. ``msg_id`` is the head the human sees."""

    project: str
    thread_id: str
    msg_id: str
    ask_score: float
    matched_rule: str | None
    questions_version: str
    decision_id: str | None
    logged_at: str

    def key(self) -> tuple[str, str, str]:
        return (self.project, self.thread_id, self.msg_id)


def annotation_from_result(
    dr: DecisionResult | None,
    *,
    project: str,
    thread_id: str,
    msg_id: str,
    now: datetime,
) -> TierCAnnotation | None:
    """The annotation for ``dr``, or ``None`` when there is nothing to show.

    ``None`` unless the result carries an actionable tierc-v2 verdict (``EVALUATED`` ∧
    ``IN_GATE``): a null / malformed / transport-error outcome or a v1 verdict has no
    ``should_ask_human`` to display, and a guessed one would be worse than none."""
    if dr is None:
        return None
    av = dr.actionable_verdict
    if not isinstance(av, TierCV2Verdict):
        return None
    return TierCAnnotation(
        project=project,
        thread_id=thread_id,
        msg_id=msg_id,
        ask_score=float(av.ask_score),
        matched_rule=dr.matched_rule,
        questions_version=av.questions_version,
        decision_id=dr.decision_id,
        logged_at=now.isoformat(),
    )


def append_annotation(path: Path, annotation: TierCAnnotation) -> None:
    """Append one JSON line (UTF-8, LF). Raises on I/O failure; the caller decides fail-open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(annotation), ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
    try:
        if path.stat().st_size > COMPACT_AT_BYTES:
            compact_annotations(path, KEEP_LINES)
    except OSError as exc:
        # The append above already succeeded; a failed compaction only leaves the file longer.
        logger.warning("tierc annotations: compaction of %s failed (%s); left as is", path, exc)


def compact_annotations(path: Path, keep_lines: int = KEEP_LINES) -> None:
    """Rewrite ``path`` to its newest ``keep_lines`` lines, by atomic replace.

    Keeping a suffix preserves the reader's "last row per key wins" (:func:`read_annotations`).
    Raises ``OSError``; :func:`append_annotation` turns that into a WARNING.

    The replacement is prepared in a temp file **unique to this call** (``tempfile.mkstemp`` in
    the same directory, created with ``O_EXCL``), never a fixed name: several Conductors share
    this file, and two compactions writing one fixed ``.tmp`` would interleave and swap a
    corrupted file into place (#465 PR-gate on c7a8343). Each compaction swaps in a whole file
    of its own; a temp file that is not swapped in is removed."""
    with path.open(encoding="utf-8", newline="") as f:
        lines = f.readlines()
    if len(lines) <= keep_lines:
        return
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".compact.tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.writelines(lines[-keep_lines:])
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _parse(raw: Any) -> TierCAnnotation | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        score = raw["ask_score"]
        if isinstance(score, bool) or not isinstance(score, int | float):
            return None
        score = float(score)
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            return None
        rule = raw.get("matched_rule")
        decision_id = raw.get("decision_id")
        fields = (raw["project"], raw["thread_id"], raw["msg_id"], raw["questions_version"])
        if not all(isinstance(v, str) and v for v in fields):
            return None
        if rule is not None and not isinstance(rule, str):
            return None
        if decision_id is not None and not isinstance(decision_id, str):
            return None
        return TierCAnnotation(
            project=raw["project"],
            thread_id=raw["thread_id"],
            msg_id=raw["msg_id"],
            ask_score=score,
            matched_rule=rule or None,
            questions_version=raw["questions_version"],
            decision_id=decision_id,
            logged_at=str(raw.get("logged_at") or ""),
        )
    except KeyError:
        return None


def read_annotations(path: Path) -> tuple[dict[tuple[str, str, str], TierCAnnotation], int]:
    """``({(project, thread_id, msg_id): annotation}, skipped_lines)``. Last row per key wins.

    A missing file is ``({}, 0)``. Any other read error raises ``OSError`` (the caller shows the
    rows without lines and says so). A line that is not a well-formed annotation is skipped and
    counted, never guessed at."""
    if not path.exists():
        return {}, 0
    out: dict[tuple[str, str, str], TierCAnnotation] = {}
    skipped = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                ann = _parse(json.loads(line))
            except ValueError:
                ann = None
            if ann is None:
                skipped += 1
                continue
            out[ann.key()] = ann
    return out, skipped


def annotation_line(annotation: TierCAnnotation) -> str:
    """The one display line (msg-5141 §2e-2), spelled once for every surface."""
    rule = annotation.matched_rule or NO_RULE_TEXT
    p = f"{annotation.ask_score:.2f}"
    return f"Jev: should_ask_human={p}（未検証、AUC 未確立）— {rule}"  # noqa: RUF001 (msg-5141 wording)


__all__ = [
    "ANNOTATING_TIERC_MODES",
    "COMPACT_AT_BYTES",
    "KEEP_LINES",
    "NO_RULE_TEXT",
    "TierCAnnotation",
    "annotation_from_result",
    "annotation_line",
    "append_annotation",
    "compact_annotations",
    "read_annotations",
]
