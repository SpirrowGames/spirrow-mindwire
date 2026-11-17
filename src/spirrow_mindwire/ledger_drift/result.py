"""The result file and the freshness contract the digest reads it under (msg-9526 Obj-2-2).

One run writes one file, ``<logs_dir>/ledger_drift/<project>.json``, atomically (temp file +
``os.replace``). It carries ``generated_at`` (UTC), ``window`` (``since`` / ``until``) and, per
section, ``complete`` with the reasons when it is not.

A run that never started, crashed, or failed to save leaves ``generated_at`` where it was. So the
reader never trusts the file for being there: :func:`judge` returns ``missing`` (no file),
``unreadable`` (not a result file), ``stale`` (older than ``stale_after``) or ``fresh``. A stale
result's counts are reference values only and are never presented as the current state.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from .detect import WINDOWED_SECTIONS, Section, SectionResult

SCHEMA = "ledger_drift/v1"


class Freshness(StrEnum):
    MISSING = "missing"
    UNREADABLE = "unreadable"
    STALE = "stale"
    FRESH = "fresh"


def result_path(logs_dir: Path, project: str) -> Path:
    if not project or any(c in project for c in "/\\:") or project in (".", ".."):
        raise ValueError(f"not a project id usable as a file name: {project!r}")
    return logs_dir / "ledger_drift" / f"{project}.json"


def build(
    *,
    project: str,
    generated_at: datetime,
    since: datetime,
    until: datetime,
    sections: Mapping[Section, SectionResult],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "project": project,
        "generated_at": _iso(generated_at),
        "window": {
            "since": _iso(since),
            "until": _iso(until),
            # Only these sections are bounded by the window (msg-9526 Obj-2-1).
            "applies_to": sorted(s.value for s in WINDOWED_SECTIONS),
        },
        "sections": {s.value: sections[s].as_dict() for s in Section},
    }


def write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Write ``payload`` so a reader sees the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    data = json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=1)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass(frozen=True)
class Judged:
    freshness: Freshness
    generated_at: datetime | None = None
    age: timedelta | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def incomplete_sections(self) -> dict[str, list[str]]:
        sections = self.payload.get("sections")
        if not isinstance(sections, dict):
            return {}
        return {
            name: [str(r) for r in body.get("incomplete_reasons", [])]
            for name, body in sections.items()
            if isinstance(body, dict) and body.get("complete") is not True
        }


def judge(path: Path, *, now: datetime, stale_after: timedelta) -> Judged:
    """How far the file at ``path`` may be believed at ``now``."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Judged(Freshness.MISSING)
    except OSError as exc:
        return Judged(Freshness.UNREADABLE, error=repr(exc))
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            raise ValueError("not a ledger_drift result")
        generated = datetime.fromisoformat(str(payload["generated_at"]))
        if generated.tzinfo is None:
            raise ValueError("generated_at has no timezone")
    except (ValueError, KeyError, TypeError) as exc:
        return Judged(Freshness.UNREADABLE, error=repr(exc))
    age = now - generated
    state = Freshness.STALE if age > stale_after else Freshness.FRESH
    return Judged(state, generated_at=generated, age=age, payload=payload)


def digest_heading(judged: Judged) -> str:
    """The heading line the digest prints for this project's section."""
    if judged.freshness is Freshness.MISSING:
        return "台帳のずれ: 未生成"
    if judged.freshness is Freshness.UNREADABLE:
        return f"台帳のずれ: 読めない結果 ({judged.error})"
    assert judged.generated_at is not None and judged.age is not None
    if judged.freshness is Freshness.STALE:
        return (
            f"台帳のずれ: 古い結果 ({_iso(judged.generated_at)} 時点, "
            f"{_human_age(judged.age)} 前。件数は参考値)"
        )
    return f"台帳のずれ ({_iso(judged.generated_at)} 時点)"


def digest_lines(judged: Judged) -> list[str]:
    """Heading, then one line per section: its count, or ``不完全: <reasons>``."""
    lines = [digest_heading(judged)]
    sections = judged.payload.get("sections")
    if not isinstance(sections, dict):
        return lines
    window = judged.payload.get("window") or {}
    for name in (s.value for s in Section):
        body = sections.get(name)
        if not isinstance(body, dict):
            continue
        note = f" (対象外: {window.get('since')} より前)" if name in _WINDOWED else ""
        if body.get("complete") is True:
            lines.append(f"  {_TITLES[name]}: {body.get('count')}{note}")
        else:
            why = "; ".join(str(r) for r in body.get("incomplete_reasons", []))
            lines.append(f"  {_TITLES[name]}: 不完全: {why}")
    return lines


_WINDOWED = frozenset(s.value for s in WINDOWED_SECTIONS)
_TITLES = {
    Section.NOT_STARTED.value: "(a) merge 済み PR があるのに not_started",
    Section.PR_RECORD.value: (
        "(a') PR 記録の確認候補 (merge 済みの PR 番号が notes に出てこない。完了は示さない)"
    ),
    Section.UNLINKED_PRS.value: "対応不明の PR",
    Section.CLOSE_CANDIDATES.value: (
        "(c) close 候補 (関連タスクの完了は, スレッドの役目が終わったことを保証しない)"
    ),
    Section.STALE_BLOCKED_BY.value: "(d) completed を指す blocked_by",
}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _human_age(age: timedelta) -> str:
    minutes = int(age.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} 分"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} 時間"
    return f"{hours // 24} 日"
