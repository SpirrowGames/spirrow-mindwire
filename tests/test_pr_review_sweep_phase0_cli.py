"""CLI wiring for the Phase 0 sweep — and the machine check behind "it writes nothing".

Loaded the same way ``tests/test_identity_findings_cli.py`` loads its script: by path,
because ``scripts/`` is not an importable package.
"""

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
from spirrow_mindwire.pr_review_sweep.config import ProjectEntry

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pr_review_sweep_phase0.py"
_TERMINAL = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def _load_cli_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pr_review_sweep_phase0_cli", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_cli_module()


class _RecordingMcp:
    """Records every tool call so the tests can assert on the query shapes."""

    def __init__(self, *, threads: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
        self._threads = threads
        self._events = events
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name == "chatroom_list_threads":
            if int(arguments.get("offset") or 0) > 0:
                return {"items": [], "total": len(self._threads)}
            return {"items": self._threads, "total": len(self._threads)}
        if name == "chatroom_list_events":
            if int(arguments.get("offset") or 0) > 0:
                return {"items": [], "total": 0}
            since = arguments.get("since")
            items = self._events
            if isinstance(since, str):
                cutoff = datetime.fromisoformat(since)
                items = [
                    e for e in self._events if datetime.fromisoformat(str(e["timestamp"])) >= cutoff
                ]
            return {"items": items, "total": len(items)}
        if name == "chatroom_get_thread":
            return {"messages": [{"msg_id": "msg-1", "next_participant": "Bohr"}]}
        raise AssertionError(f"unexpected tool {name}")


class _FakeGitHub:
    def __init__(self, state: PrState) -> None:
        self._state = state
        self.asked: list[PrRef] = []

    async def fetch_pr_state(self, pr: PrRef) -> PrState:
        self.asked.append(pr)
        return self._state


def _event(offset_seconds: int) -> dict[str, Any]:
    return {"timestamp": (_TERMINAL + timedelta(seconds=offset_seconds)).isoformat()}


# --------------------------------------------------------------- the write-zero check


@pytest.mark.parametrize(
    "tool",
    ["chatroom_post_message", "chatroom_close_thread", "chatroom_open_thread", "checkpoint"],
)
def test_the_read_only_wrapper_refuses_every_write_tool(tool: str) -> None:
    """Phase 0's write-zero property is enforced here, not merely documented.

    msg-2155 R-11 adopted CQS after Bohr's own "the close is desirable either way, so
    doing it during the measurement is fine" was withdrawn as a gate-defeating
    argument. This test is that withdrawal made mechanical.
    """
    mcp = _MODULE.ReadOnlyMcp(_RecordingMcp(threads=[], events=[]))
    with pytest.raises(_MODULE.Phase0WriteAttemptedError, match="read-only"):
        asyncio.run(mcp.call_tool(tool, {}))


def test_the_allowlist_is_exactly_the_three_reads() -> None:
    expected = {"chatroom_list_threads", "chatroom_get_thread", "chatroom_list_events"}
    assert set(_MODULE.READ_ONLY_TOOLS) == expected


def test_allowed_reads_pass_through() -> None:
    inner = _RecordingMcp(threads=[{"thread_id": "T-a"}], events=[])
    mcp = _MODULE.ReadOnlyMcp(inner)
    result = asyncio.run(mcp.call_tool("chatroom_list_threads", {"project": "p"}))
    assert result["items"] == [{"thread_id": "T-a"}]


# --------------------------------------------------------------------- query shapes


def _sweep(
    threads: list[dict[str, Any]], events: list[dict[str, Any]], state: PrState
) -> tuple[_RecordingMcp, list[Any]]:
    inner = _RecordingMcp(threads=threads, events=events)
    entry = ProjectEntry(project="p", owner="o", repo="r")
    pairs = asyncio.run(
        _MODULE._sweep_project(
            _MODULE.ReadOnlyMcp(inner), _FakeGitHub(state), entry, timedelta(seconds=300)
        )
    )
    return inner, pairs


def _closed_state() -> PrState:
    return PrState(
        ref=PrRef("o", "r", 7), resolution=PrResolution.CLOSED, closed_at=_TERMINAL, merged=True
    )


def test_two_event_queries_are_issued_one_windowed_one_not() -> None:
    """msg-2166 R-25. If these ever collapse into one call, the margin validates itself."""
    inner, _ = _sweep(
        [{"thread_id": "T-pr-review-p-7", "status": "active"}], [_event(60)], _closed_state()
    )
    event_calls = [args for name, args in inner.calls if name == "chatroom_list_events"]
    windowed = [a for a in event_calls if "since" in a]
    unwindowed = [a for a in event_calls if "since" not in a]
    assert windowed and unwindowed
    assert windowed[0]["since"] == (_TERMINAL - timedelta(seconds=300)).isoformat()
    assert all(a["action"] == "post_message" for a in event_calls)


def test_the_unwindowed_query_sees_what_the_windowed_one_cannot() -> None:
    """The point of the split: an offset outside the margin still reaches the report."""
    _, pairs = _sweep(
        [{"thread_id": "T-pr-review-p-7", "status": "active"}],
        [_event(-86400)],
        _closed_state(),
    )
    facts, _pr = pairs[0]
    assert facts.classification_events == ()
    assert facts.measurement_events != ()


def test_non_pr_review_threads_are_ignored() -> None:
    inner, pairs = _sweep(
        [
            {"thread_id": "T-some-design-thread", "status": "active"},
            {"thread_id": "T-pr-review-q-7", "status": "active"},
        ],
        [],
        _closed_state(),
    )
    assert pairs == []
    assert not [name for name, _ in inner.calls if name == "chatroom_list_events"]


def test_no_thread_body_is_fetched_when_limb_two_cannot_change_the_answer() -> None:
    """(ii) is only consulted when the thread is in a liveness status and (i) holds."""
    inner, _ = _sweep(
        [{"thread_id": "T-pr-review-p-7", "status": "resolved"}], [_event(60)], _closed_state()
    )
    assert not [name for name, _ in inner.calls if name == "chatroom_get_thread"]


def test_the_thread_body_is_fetched_when_limb_two_matters() -> None:
    inner, pairs = _sweep(
        [{"thread_id": "T-pr-review-p-7", "status": "active"}], [_event(60)], _closed_state()
    )
    assert [name for name, _ in inner.calls if name == "chatroom_get_thread"]
    assert pairs[0][0].last_next_participant == "Bohr"


def test_an_unresolvable_pr_skips_both_event_queries() -> None:
    """No terminal time exists, so there is no window to ask about and nothing to measure."""
    inner, pairs = _sweep(
        [{"thread_id": "T-pr-review-p-7", "status": "active"}],
        [_event(60)],
        PrState(ref=PrRef("o", "r", 7), resolution=PrResolution.UNRESOLVABLE),
    )
    assert not [name for name, _ in inner.calls if name == "chatroom_list_events"]
    assert pairs[0][0].classification_events == ()


def test_no_identity_is_ever_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    """msg-2177 R-43 ②: Phase 0 runs under no author identity because it never writes."""
    inner, _ = _sweep(
        [{"thread_id": "T-pr-review-p-7", "status": "active"}], [_event(60)], _closed_state()
    )
    for _name, args in inner.calls:
        assert "identity_name" not in args
        assert "author" not in args


def test_unparseable_event_timestamps_are_skipped_not_fatal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    inner = _RecordingMcp(threads=[], events=[{"timestamp": "not-a-time"}, _event(60)])
    stamps = asyncio.run(
        _MODULE._post_message_times(_MODULE.ReadOnlyMcp(inner), "p", "T-pr-review-p-7", None)
    )
    assert len(stamps) == 1
    assert "unparseable" in capsys.readouterr().err


# ------------------------------------------------------------------------ main()


def test_main_refuses_a_bad_config_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"schema_version": 9, "projects": []}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["prog", "--config", str(path)])
    assert _MODULE.main() == 2
    assert "schema_version" in capsys.readouterr().err


def test_main_emits_ascii_only_json_on_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "cfg.json"
    path.write_text(
        json.dumps(
            {"schema_version": 1, "projects": [{"project": "p", "owner": "o", "repo": "r"}]}
        ),
        encoding="utf-8",
    )

    async def _fake_run(*_args: Any, **_kw: Any) -> dict[str, Any]:
        return {"verdict": "no_go", "note": "日本語"}

    monkeypatch.setattr(_MODULE, "_run", _fake_run)
    monkeypatch.setattr(sys, "argv", ["prog", "--config", str(path)])
    assert _MODULE.main() == 0
    out = capsys.readouterr().out
    assert out.isascii()
    assert json.loads(out)["verdict"] == "no_go"


def test_the_default_margin_is_the_provisional_one() -> None:
    from spirrow_mindwire.pr_review_sweep.phase0 import PROVISIONAL_MARGIN_SECONDS

    assert PROVISIONAL_MARGIN_SECONDS == 300
    assert _MODULE.PROVISIONAL_MARGIN_SECONDS == 300
