"""``mindwire codex-band-count`` — requests billed to Gemini by the codex limit raise.

Reads the Lexora ledger and counts rows whose ``trace_id`` carries the band marker
(:func:`~.n3_routing.is_codex_limit_band_trace_id`). One review or design-time turn is one trace
id, so distinct trace ids count requests, not passes. Shadow rows are left out of the count and
the cost: they record Lexora's comparison request, not the route that answered. Failed rows are
kept, because a failed attempt was still routed here.

A read that fills ``limit`` may have been cut off, so it prints ``truncated=true`` and the count
is a lower bound. A failed read exits non-zero and prints no ``reviews=`` line, so a script cannot
read a failure as zero. That includes a stalled ledger: the client timeout only bounds each network
operation, so a server that keeps trickling bytes would never trip it, and ``run`` also caps the
whole read at ``_READ_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any

from ..lexora.client import LexoraClient, LexoraError, lexora_url
from .n3_routing import is_codex_limit_band_trace_id
from .preflight import SHADOW_ENDPOINT

_READ_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class BandCount:
    reviews: int
    rows: int
    cost_usd: float
    rows_read: int
    oldest: str | None
    truncated: bool

    def render(self) -> str:
        return (
            f"reviews={self.reviews} rows={self.rows} cost_usd={self.cost_usd:.4f} "
            f"rows_read={self.rows_read} oldest={self.oldest} "
            f"truncated={'true' if self.truncated else 'false'}"
        )


def count_band(rows: list[dict[str, Any]], *, limit: int) -> BandCount:
    """Count the marked rows in one ledger read. Pure."""
    marked = [
        row
        for row in rows
        if is_codex_limit_band_trace_id(row.get("trace_id"))
        and row.get("endpoint") != SHADOW_ENDPOINT
    ]
    stamps = [str(row["timestamp"]) for row in rows if row.get("timestamp")]
    return BandCount(
        reviews=len({row["trace_id"] for row in marked}),
        rows=len(marked),
        cost_usd=sum(float(row.get("cost_usd") or 0.0) for row in marked),
        rows_read=len(rows),
        oldest=min(stamps) if stamps else None,
        truncated=len(rows) >= limit,
    )


async def _read(limit: int) -> list[dict[str, Any]]:
    async with LexoraClient(lexora_url(), timeout_seconds=_READ_TIMEOUT_SECONDS) as client:
        return await client.stats_costs_recent(limit=limit)


async def _read_within_deadline(limit: int) -> list[dict[str, Any]]:
    return await asyncio.wait_for(_read(limit), _READ_TIMEOUT_SECONDS)


def run(limit: int) -> int:
    """CLI body. Prints one result line on stdout and returns the exit code."""
    try:
        rows = asyncio.run(_read_within_deadline(limit))
    except LexoraError as exc:
        print(f"codex-band-count: error: {exc}", file=sys.stderr)
        return 1
    except TimeoutError:
        print(
            f"codex-band-count: error: ledger read exceeded {_READ_TIMEOUT_SECONDS}s",
            file=sys.stderr,
        )
        return 1
    print(count_band(rows, limit=limit).render())
    return 0
