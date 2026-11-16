"""scripts/open_prs_by_project.py — the liveness view's open-PR source.

Pins the fail direction: a project that could not be measured is ``prs: null`` with a reason,
never ``[]`` (which would read as "measured, none open" and could make it DONE).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from spirrow_mindwire.github.client import OpenPr, OpenPrListing, OpenPrListingError, PrRef
from spirrow_mindwire.repo_projects import build_repo_project_map

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "open_prs_by_project.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("open_prs_by_project_cli", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load()
_AT = datetime(2026, 11, 1, tzinfo=UTC)


def _pr(owner: str, repo: str, n: int) -> OpenPr:
    return OpenPr(ref=PrRef(owner, repo, n), draft=False, head_sha="x", created_at=_AT)


class _Gh:
    def __init__(self, listings: dict[str, object]) -> None:
        self.listings = listings
        self.calls: list[str] = []

    async def list_open_prs(self, owner: str, repo: str) -> OpenPrListing:
        self.calls.append(f"{owner}/{repo}")
        got = self.listings[f"{owner}/{repo}"]
        if isinstance(got, Exception):
            raise got
        assert isinstance(got, OpenPrListing)
        return got


def _map(clones: dict[str, object], origins: dict[str, str]):  # type: ignore[no-untyped-def]
    return build_repo_project_map(clones, origin_url=lambda clone: origins[clone])


def test_each_project_gets_its_own_repos_prs() -> None:
    repo_map = _map(
        {"a": "/a", "b": "/b"},
        {"/a": "https://github.com/o/ra.git", "/b": "https://github.com/o/rb.git"},
    )
    gh = _Gh(
        {
            "o/ra": OpenPrListing(prs=(_pr("o", "ra", 7),), examined=1, unrecognized=0),
            "o/rb": OpenPrListing(prs=(), examined=0, unrecognized=0),
        }
    )
    out = asyncio.run(_MODULE.collect(gh, repo_map))
    assert out["a"] == {"prs": [{"number": 7, "created_at": _AT.isoformat()}], "error": None}
    assert out["b"] == {"prs": [], "error": None}


def test_a_failed_listing_is_null_not_empty() -> None:
    repo_map = _map({"a": "/a"}, {"/a": "https://github.com/o/ra.git"})
    gh = _Gh({"o/ra": OpenPrListingError("boom", outcome="http_error")})
    out = asyncio.run(_MODULE.collect(gh, repo_map))
    assert out["a"]["prs"] is None
    assert "http_error" in out["a"]["error"]


def test_an_unreadable_row_makes_the_project_unmeasured() -> None:
    repo_map = _map({"a": "/a"}, {"/a": "https://github.com/o/ra.git"})
    gh = _Gh({"o/ra": OpenPrListing(prs=(), examined=1, unrecognized=1)})
    out = asyncio.run(_MODULE.collect(gh, repo_map))
    assert out["a"]["prs"] is None and "unreadable" in out["a"]["error"]


def test_a_shared_repo_is_not_attributed_and_not_listed() -> None:
    repo_map = _map(
        {"a": "/a", "b": "/b"},
        {"/a": "https://github.com/o/r.git", "/b": "https://github.com/o/r.git"},
    )
    gh = _Gh({})
    out = asyncio.run(_MODULE.collect(gh, repo_map))
    assert out["a"]["prs"] is None and out["b"]["prs"] is None
    assert "shared" in out["a"]["error"]
    assert gh.calls == []


def test_a_clone_without_a_path_is_reported() -> None:
    repo_map = _map({"a": ""}, {})
    out = asyncio.run(_MODULE.collect(_Gh({}), repo_map))
    assert out["a"] == {"prs": None, "error": "no clones entry"}
