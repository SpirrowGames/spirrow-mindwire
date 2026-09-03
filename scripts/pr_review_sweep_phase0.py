"""Phase 0 of the PR-review sweep — measure how many pr-review threads outlived their PRs.

Read-only. This script closes nothing, posts nothing, and calls no ledger. It exists to
produce one number — the size of ``a_union_b`` — and the go/no-go it implies (``>= 5``
proceeds to Phase 1, ``< 5`` parks with the whole short list attached for a human).

    uv run python scripts/pr_review_sweep_phase0.py \
        --config ~/spirrow-mindwire-data/config/pr_review_sweep.json

The write-zero property is **enforced, not promised**: every chatroom call goes through
:class:`ReadOnlyMcp`, which raises on any tool outside a three-entry read allowlist. A
future edit that reaches for ``chatroom_post_message`` fails loudly at the call instead
of quietly turning a measurement into a partial rollout — the exact confusion msg-2155
R-11 adopted CQS to prevent.

Phase 0 also runs under **no author identity** (msg-2177 R-43 ②). None of the three
tools it may call takes one, and there is nothing for it to sign, because it never
writes. Do not add an ``identity_name`` here to make some future inbox query convenient.

Two chatroom event queries are issued per candidate thread and MUST NOT be conflated
(msg-2166 R-25) — see :mod:`spirrow_mindwire.pr_review_sweep.phase0` for why:

* windowed at ``terminal_at - margin``  → decides S1 (i)
* unwindowed                            → the raw offset distribution and the margin
                                          sensitivity table

Output is a single JSON object on stdout (ASCII-only, per the repo's D-33 rule for
machine-read stdout on a cp932 console). Errors and progress go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from spirrow_mindwire.config import DEFAULT_DATA_DIR
from spirrow_mindwire.github.client import GitHubClient, PrRef, PrResolution, PrState
from spirrow_mindwire.magickit.client import MagickitMcpError, StreamableHttpChatroomMcp
from spirrow_mindwire.pr_review_sweep.config import (
    ProjectEntry,
    SweepConfig,
    SweepConfigError,
    load_sweep_config,
)
from spirrow_mindwire.pr_review_sweep.phase0 import (
    PROVISIONAL_MARGIN_SECONDS,
    ThreadFacts,
    build_report,
    report_to_json,
)

_reconfigure_err = getattr(sys.stderr, "reconfigure", None)
if _reconfigure_err is not None:
    _reconfigure_err(errors="backslashreplace")

#: The complete set of chatroom tools Phase 0 may call. Every one is a read.
READ_ONLY_TOOLS = frozenset(
    {"chatroom_list_threads", "chatroom_get_thread", "chatroom_list_events"}
)

_PAGE = 200
_EVENT_PAGE = 500


class Phase0WriteAttemptedError(RuntimeError):
    """A tool outside :data:`READ_ONLY_TOOLS` was requested. Phase 0 does not write."""


class ToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


class PrStateReader(Protocol):
    async def fetch_pr_state(self, pr: PrRef) -> PrState: ...


class ReadOnlyMcp:
    """Allowlist wrapper: the machine check behind this script's write-zero claim."""

    def __init__(self, inner: ToolCaller) -> None:
        self._inner = inner

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in READ_ONLY_TOOLS:
            raise Phase0WriteAttemptedError(
                f"Phase 0 is read-only; refusing to call {name!r}. "
                f"Allowed: {sorted(READ_ONLY_TOOLS)}"
            )
        return await self._inner.call_tool(name, arguments)


def _as_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


async def _list_threads(mcp: ReadOnlyMcp, project: str) -> list[dict[str, Any]]:
    """Every thread in a project, paged. Same stop condition as ``identity_findings.py``."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _as_dict(
            await mcp.call_tool(
                "chatroom_list_threads",
                {"project": project, "limit": _PAGE, "offset": offset},
            )
        )
        if page is None:
            break
        items = page.get("items")
        if not isinstance(items, list) or not items:
            break
        out.extend(item for item in items if isinstance(item, dict))
        offset += len(items)
        total = page.get("total")
        if isinstance(total, int) and offset >= total:
            break
    return out


async def _post_message_times(
    mcp: ReadOnlyMcp, project: str, thread_id: str, since: datetime | None
) -> tuple[datetime, ...]:
    """``post_message`` audit timestamps for a thread, optionally windowed.

    ``since`` is the audit log's INCLUSIVE lower bound, which is why the pure predicate
    compares with ``>=``. Passing ``None`` issues the unwindowed measurement query;
    msg-2166 R-25 forbids reusing one call's result for the other's purpose, so this
    helper is called twice with different arguments rather than once and filtered.
    """
    args: dict[str, Any] = {
        "project": project,
        "thread_id": thread_id,
        "action": "post_message",
        "limit": _EVENT_PAGE,
    }
    if since is not None:
        args["since"] = since.isoformat()

    stamps: list[datetime] = []
    offset = 0
    while True:
        page = _as_dict(await mcp.call_tool("chatroom_list_events", {**args, "offset": offset}))
        if page is None:
            break
        items = page.get("items")
        if not isinstance(items, list) or not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            raw = item.get("timestamp")
            if not isinstance(raw, str) or not raw:
                continue
            try:
                stamps.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                print(
                    f"pr_review_sweep_phase0: {thread_id}: unparseable event timestamp {raw!r}",
                    file=sys.stderr,
                )
        offset += len(items)
        total = page.get("total")
        if isinstance(total, int) and offset >= total:
            break
    return tuple(stamps)


async def _last_next_participant(mcp: ReadOnlyMcp, project: str, thread_id: str) -> str | None:
    """``next_participant`` of the thread's last message (S1 (ii)'s structured limb)."""
    result = _as_dict(
        await mcp.call_tool(
            "chatroom_get_thread",
            {"project": project, "thread_id": thread_id, "mode": "full"},
        )
    )
    if result is None:
        return None
    messages = result.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    last = messages[-1]
    if not isinstance(last, dict):
        return None
    value = last.get("next_participant")
    return value if isinstance(value, str) and value.strip() else None


async def _sweep_project(
    mcp: ReadOnlyMcp,
    github: PrStateReader,
    entry: ProjectEntry,
    margin: timedelta,
) -> list[tuple[ThreadFacts, PrState]]:
    """Resolve every ``T-pr-review-<project>-N`` thread in one project."""
    pairs: list[tuple[ThreadFacts, PrState]] = []
    for thread in await _list_threads(mcp, entry.project):
        thread_id = str(thread.get("thread_id") or "")
        number = entry.pr_number_for_thread(thread_id)
        if number is None:
            continue
        status = str(thread.get("status") or "")
        pr = await github.fetch_pr_state(PrRef(entry.owner, entry.repo, number))

        if pr.resolution is not PrResolution.CLOSED or pr.closed_at is None:
            # S0 already decides these; no terminal time exists, so neither event
            # query is meaningful. Hand the classifier bare facts.
            pairs.append((ThreadFacts(thread_id=thread_id, status=status), pr))
            continue

        classification_events = await _post_message_times(
            mcp, entry.project, thread_id, pr.closed_at - margin
        )
        measurement_events = await _post_message_times(mcp, entry.project, thread_id, None)
        # Only fetched when S1 (ii) can still change the answer: the thread is in a
        # liveness status and (i) already holds. Every other path ignores the value,
        # and thread bodies are the most expensive read here.
        next_participant = None
        if status in {"active", "awaiting_reply"} and classification_events:
            next_participant = await _last_next_participant(mcp, entry.project, thread_id)

        pairs.append(
            (
                ThreadFacts(
                    thread_id=thread_id,
                    status=status,
                    last_next_participant=next_participant,
                    classification_events=classification_events,
                    measurement_events=measurement_events,
                ),
                pr,
            )
        )
    return pairs


async def _run(config: SweepConfig, url: str | None, margin_seconds: int) -> dict[str, Any]:
    margin = timedelta(seconds=margin_seconds)
    mcp = ReadOnlyMcp(StreamableHttpChatroomMcp(url))
    pairs: list[tuple[ThreadFacts, PrState]] = []
    async with GitHubClient() as github:
        for entry in config.entries:
            pairs.extend(await _sweep_project(mcp, github, entry, margin))
    payload = report_to_json(build_report(pairs, margin_seconds=margin_seconds))
    payload["projects"] = [e.project for e in config.entries]
    return payload


def default_config_path() -> Path:
    return DEFAULT_DATA_DIR / "config" / "pr_review_sweep.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=None,
        help=f"sweep config path (default: {default_config_path()})",
    )
    parser.add_argument(
        "--margin-seconds",
        type=int,
        default=PROVISIONAL_MARGIN_SECONDS,
        help=(
            "clock-skew margin for S1 (i). PROVISIONAL: it has no measured basis. Read "
            "the sensitivity table in the output before trusting the verdict."
        ),
    )
    parser.add_argument("--url", default=None, help="magickit MCP URL (default: env/in-code)")
    args = parser.parse_args()

    path = Path(args.config) if args.config else default_config_path()
    try:
        config = load_sweep_config(path)
    except SweepConfigError as exc:
        print(f"pr_review_sweep_phase0: {exc}", file=sys.stderr)
        return 2

    try:
        payload = asyncio.run(_run(config, args.url, args.margin_seconds))
    except (MagickitMcpError, Phase0WriteAttemptedError) as exc:
        print(f"pr_review_sweep_phase0: sweep failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
