"""CLI wiring for Phase 2: off by default means no close client and no state write."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from spirrow_mindwire.github.client import PrRef, PrResolution, PrState
from spirrow_mindwire.magickit.read_only import ReadOnlyViolationError
from spirrow_mindwire.pr_review_sweep.config import SweepConfig, parse_sweep_config
from spirrow_mindwire.pr_review_sweep.phase2 import CLOSE_TOOLS, AttemptRecord, State

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pr_review_sweep_phase2.py"
_P = "spirrow-mindwire"
_CLOSED_AT = datetime(2026, 9, 1, tzinfo=UTC)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pr_review_sweep_phase2_cli", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load()


def _config() -> SweepConfig:
    return parse_sweep_config(
        {
            "schema_version": 1,
            "projects": [{"project": _P, "owner": "SpirrowGames", "repo": _P}],
        }
    )


class _Inner:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.status: dict[str, str] = {}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(name)
        if int(arguments.get("offset") or 0) > 0:
            return {"items": [], "total": 0}
        if name == "chatroom_list_threads":
            items = [
                {"thread_id": f"T-pr-review-{_P}-1", "status": "active"},
                {"thread_id": f"T-pr-review-{_P}-2", "status": "active"},
            ]
            return {"items": items, "total": len(items)}
        if name == "chatroom_list_events":
            return {"items": [], "total": 0}
        if name == "chatroom_close_thread":
            self.status[arguments["thread_id"]] = "resolved"
            return {"ok": True}
        if name == "chatroom_get_thread":
            return {"thread": {"status": self.status.get(arguments["thread_id"], "active")}}
        raise AssertionError(name)


class _GitHub:
    async def __aenter__(self) -> _GitHub:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def fetch_pr_state(self, pr: PrRef) -> PrState:
        if pr.number == 1:
            return PrState(
                ref=pr, resolution=PrResolution.CLOSED, closed_at=_CLOSED_AT, merged=True
            )
        return PrState(ref=pr, resolution=PrResolution.OPEN)


@pytest.fixture
def inner(monkeypatch: pytest.MonkeyPatch) -> _Inner:
    fake = _Inner()
    monkeypatch.setattr(_MODULE, "StreamableHttpChatroomMcp", lambda url: fake)
    monkeypatch.setattr(_MODULE, "GitHubClient", _GitHub)
    return fake


def _run(enabled: bool, state_dir: Path) -> dict[str, Any]:
    settings = _MODULE.settings_from_args(
        _MODULE.build_parser().parse_args(["--enable"] if enabled else [])
    )
    result: dict[str, Any] = asyncio.run(_MODULE._run(_config(), None, settings, state_dir))
    return result


def test_parser_default_is_off() -> None:
    assert _MODULE.settings_from_args(_MODULE.build_parser().parse_args([])).enabled is False


def test_disabled_run_writes_nothing(inner: _Inner, tmp_path: Path) -> None:
    out = _run(False, tmp_path)
    assert "chatroom_close_thread" not in inner.calls
    assert "chatroom_get_thread" not in inner.calls
    assert out["wrote_anything"] is False
    assert [w["thread_id"] for w in out["would_attempt"]] == [f"T-pr-review-{_P}-1"]
    assert list(tmp_path.iterdir()) == []


def test_enabled_run_closes_and_writes_state(inner: _Inner, tmp_path: Path) -> None:
    stale: State = {
        (_P, f"T-pr-review-{_P}-2"): AttemptRecord(datetime.now(UTC) - timedelta(days=1), 1)
    }
    (tmp_path / f"{_P}.json").write_text(_MODULE.dump_state(_P, stale), encoding="utf-8")
    out = _run(True, tmp_path)
    assert inner.calls.count("chatroom_close_thread") == 1
    assert [c["thread_id"] for c in out["closed"]] == [f"T-pr-review-{_P}-1"]
    saved = json.loads((tmp_path / f"{_P}.json").read_text(encoding="utf-8"))
    # #1 closed, #2 is OPEN now -> both are gone from the state file.
    assert saved["ledgers"] == {}


def test_corrupt_state_file_is_reported_not_fatal(inner: _Inner, tmp_path: Path) -> None:
    (tmp_path / f"{_P}.json").write_text("{nope", encoding="utf-8")
    out = _run(False, tmp_path)
    assert out["warnings"] and "not valid JSON" in out["warnings"][0]
    assert [w["thread_id"] for w in out["would_attempt"]] == [f"T-pr-review-{_P}-1"]


def test_close_client_allowlist_is_close_plus_readback() -> None:
    assert frozenset({"chatroom_close_thread", "chatroom_get_thread"}) == CLOSE_TOOLS


def test_close_client_refuses_other_writes() -> None:
    wrapped = _MODULE.ReadOnlyMcp(_Inner(), CLOSE_TOOLS, label="Phase 2")
    with pytest.raises(ReadOnlyViolationError):
        asyncio.run(wrapped.call_tool("chatroom_post_message", {"thread_id": "x"}))


def test_bad_setting_exits_2(tmp_path: Path) -> None:
    assert _MODULE.main(["--breaker", "0", "--config", str(tmp_path / "x.json")]) == 2
