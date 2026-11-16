"""Open PRs per project, for the digest liveness view (T-digest-lacks-project-liveness-view).

The liveness table's SHIP state ("no open thread, but work waits to ship") needs each project's open
PRs (msg-2015 §2: "出荷待ちがあるか — open PR"). Nothing on the sweep path emits that per project
today: the 1c auto-gate pass lists open PRs, but it returns early on a capped day and skips held
projects, so its output cannot stand in for "every open PR" without lying on exactly those days.

This script reads what already exists and adds nothing new to GitHub's side:

* the repo of each project comes from ``sweep.json`` ``clones`` through
  :func:`spirrow_mindwire.repo_projects.build_repo_project_map` (the table 1c reads);
* the PRs come from :meth:`spirrow_mindwire.github.client.GitHubClient.list_open_prs` (the listing
  the stall ledger reads), whose failure is loud and whole-listing.

It is read-only (one GET listing per repo) and the wrapper runs it only on the tick that sends the
digest, so it costs one listing per repo per day.

Output (stdout, one JSON line, ASCII-only)::

    {"projects": {"spirrow-mindwire": {"prs": [{"number": 544, "created_at": "..."}],
                                        "error": null},
                  "spirrow-x": {"prs": null, "error": "no clones entry"}}}

``prs: null`` with an ``error`` means "not measured"; it is never written as ``[]``, which would
read as "measured, none open" and could turn a project into DONE (the same rule the listing's
``--liveness`` output follows). A repo that two ``clones`` projects share cannot say which of them
a PR belongs to, so every project sharing it is reported unmeasured rather than guessed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from spirrow_mindwire.config import DEFAULT_DATA_DIR
from spirrow_mindwire.github.client import OpenPrListing, OpenPrListingError
from spirrow_mindwire.repo_projects import RepoProjectMap, build_repo_project_map, repo_key


class OpenPrSource(Protocol):
    async def list_open_prs(self, owner: str, repo: str) -> OpenPrListing: ...


async def collect(gh: OpenPrSource, repo_map: RepoProjectMap) -> dict[str, dict[str, Any]]:
    """Every ``clones`` project's open PRs, or its reason for being unmeasured."""
    out: dict[str, dict[str, Any]] = {}
    for project, error in repo_map.errors.items():
        out[project] = {"prs": None, "error": error}
    listed: dict[str, dict[str, Any]] = {}
    for target in repo_map.targets:
        key = repo_key(target.owner, target.repo)
        sharers = repo_map.candidates.get(key, frozenset())
        if len(sharers) > 1:
            out[target.project] = {
                "prs": None,
                "error": f"repo {target.slug} is shared by {len(sharers)} projects; "
                "its PRs cannot be attributed",
            }
            continue
        if key not in listed:
            try:
                listing = await gh.list_open_prs(target.owner, target.repo)
            except OpenPrListingError as exc:
                listed[key] = {"prs": None, "error": f"{exc.outcome}: {exc}"}
            else:
                listed[key] = {
                    "prs": [
                        {"number": pr.ref.number, "created_at": pr.created_at.isoformat()}
                        for pr in listing.prs
                    ],
                    # A row the listing could not read is not silently a closed PR.
                    "error": None
                    if listing.unrecognized == 0
                    else f"{listing.unrecognized} listing row(s) unreadable",
                }
                if listing.unrecognized:
                    listed[key]["prs"] = None
        out[target.project] = listed[key]
    return out


def _load_clones(path: Path) -> Mapping[str, Any]:
    sweep = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(sweep, dict):
        raise ValueError("sweep.json must be an object")
    clones = sweep.get("clones")
    return clones if isinstance(clones, dict) else {}


async def _run(clones: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    from spirrow_mindwire.github.client import GitHubClient

    async with GitHubClient() as gh:
        return await collect(gh, build_repo_project_map(clones))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sweep-config",
        default=str(DEFAULT_DATA_DIR / "config" / "sweep.json"),
        help="sweep.json path",
    )
    args = parser.parse_args()
    try:
        clones = _load_clones(Path(args.sweep_config))
    except (OSError, ValueError) as exc:
        print(f"open_prs_by_project: {exc}", file=sys.stderr)
        return 2
    try:
        projects = asyncio.run(_run(clones))
    except Exception as exc:  # pragma: no cover - defence in depth
        print(f"open_prs_by_project: run failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"projects": projects}, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
