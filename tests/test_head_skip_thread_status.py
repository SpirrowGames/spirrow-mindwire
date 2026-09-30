"""Stage 0 of the head-skip admission: a finished thread is SKIPPED before any role session starts.

T-sweep-admission-ignores-thread-status (Bohr msg-4613 §1/§3, Einstein follow-up, operator
msg-4749). Measured failure: a resolved thread left in ``sweep.json`` was LAUNCHed, the naysayer
spent a full inference (22,970 input tokens), and magickit refused the post only afterwards with
``ChatroomThreadResolvedError``. These tests pin:

1. ``decide(thread_status="resolved")`` → SKIP ``thread-resolved`` whatever the head / token /
   record say (Stage 0 precedes Stage 1).
2. ``""`` and unknown statuses reproduce the pre-Stage-0 verdict exactly (fail-open).
3. The cache-hit path: ``last_observed_status`` drives Stage 0 with no fetch; a legacy state
   record without the field reads as ``""``.
4. ``_decide_all`` against the MEASURED response shape — status nested at
   ``result["thread"]["status"]``, no top-level ``status`` (msg-4749) — emits no
   ``commit_launch_payload`` for a resolved thread. Malformed shapes fail open.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.chatroom.status import FINISHED_STATUSES, is_terminal_status
from spirrow_mindwire.conductor.head_skip import (
    Decision,
    Record,
    commit_launch,
    commit_observation,
    commit_terminal,
    decide,
    record_from_json,
    record_to_json,
)
from spirrow_mindwire.pr_review_sweep import phase0

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "head_skip_decide.py"


def _load_cli_module() -> Any:
    spec = importlib.util.spec_from_file_location("_head_skip_status_test_module", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CLI = _load_cli_module()

_T0 = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)

_HEADS = [
    "work\n\nNEXT: Bohr",
    "work\n\nNEXT: human",
    "work\n\nNEXT: none",
    "no next line at all",
    "",
]

_RECORDS: list[Record | None] = [
    None,
    Record(
        last_launch_at=_T0 - timedelta(seconds=10),
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-1",
        launch_attempts=3,
    ),
    Record(
        last_launch_at=_T0 - timedelta(hours=5),
        nomination_at_launch="bohr",
        control_at_launch="run",
        head_msg_id_at_launch="msg-1",
        launch_attempts=1,
        terminal_stop_reason="no_progress_to_human",
        terminal_head_msg_id="msg-2",
    ),
]


# --- shared closed set ---------------------------------------------------------------------------


def test_finished_statuses_is_the_pinned_closed_set() -> None:
    # Widening this set widens what the sweep silently skips; it needs a deliberate test edit.
    assert frozenset({"resolved", "superseded"}) == FINISHED_STATUSES


@pytest.mark.parametrize("status", ["", "active", "awaiting_reply", "parked", "Resolved", "zzz"])
def test_non_finished_statuses_are_not_terminal(status: str) -> None:
    assert not is_terminal_status(status)


def test_phase0_reads_the_same_set_object() -> None:
    # One set, two readers — not two copies of one rule.
    assert phase0.FINISHED_STATUSES is FINISHED_STATUSES
    assert phase0.intake_exclusion_reason("resolved") == phase0.OUT_OF_SCOPE_FINISHED
    assert phase0.intake_exclusion_reason("superseded") == phase0.OUT_OF_SCOPE_FINISHED
    assert phase0.intake_exclusion_reason("zzz") == phase0.OUT_OF_SCOPE_UNRECOGNISED
    assert phase0.intake_exclusion_reason("active") is None


# --- 1. Stage 0 precedes everything --------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(FINISHED_STATUSES))
@pytest.mark.parametrize("body", _HEADS)
@pytest.mark.parametrize("record", _RECORDS)
def test_finished_status_skips_regardless_of_head_and_record(
    status: str, body: str, record: Record | None
) -> None:
    v = decide(
        now=_T0,
        head_msg_id="msg-2",
        head_body=body,
        control_state="run",
        record=record,
        thread_status=status,
    )
    assert v.decision is Decision.SKIP
    # The reason names the actual status, so the wrapper log cannot mislabel it (advisory 2).
    assert v.reason == f"thread-{status}"
    expected_attempts = record.launch_attempts if record else 0
    assert v.attempts_before == v.attempts_after == expected_attempts


# --- 2. fail-open regression ---------------------------------------------------------------------


@pytest.mark.parametrize("status", ["", "active", "awaiting_reply", "parked", "unheard-of"])
@pytest.mark.parametrize("body", _HEADS)
@pytest.mark.parametrize("record", _RECORDS)
def test_unknown_or_live_status_matches_the_pre_stage0_verdict(
    status: str, body: str, record: Record | None
) -> None:
    kwargs: dict[str, Any] = {
        "now": _T0,
        "head_msg_id": "msg-2",
        "head_body": body,
        "control_state": "run",
        "record": record,
    }
    assert decide(**kwargs, thread_status=status) == decide(**kwargs)


# --- 3. cache-hit path + persistence -------------------------------------------------------------


def test_observed_status_round_trips_and_legacy_record_reads_empty() -> None:
    rec = commit_observation(now=_T0, head_msg_id="msg-9", token="bohr", record=None,
                             thread_status="resolved")  # fmt: skip
    assert rec.last_observed_status == "resolved"
    assert record_from_json(record_to_json(rec)) == rec

    legacy = record_to_json(rec)
    del legacy["last_observed_status"]
    old = record_from_json(legacy)
    assert old is not None
    assert old.last_observed_status == ""


def test_commit_terminal_carries_observed_status() -> None:
    rec = commit_observation(now=_T0, head_msg_id="msg-9", token="bohr", record=None,
                             thread_status="resolved")  # fmt: skip
    after = commit_terminal(reason="no_progress_to_human", head_msg_id="msg-9", record=rec)
    assert after.last_observed_status == "resolved"


def test_commit_launch_head_fetched_false_carries_prior_status() -> None:
    prior = Record(last_observed_status="active", last_observed_head_msg_id="msg-1")
    launch = decide(now=_T0, head_msg_id="msg-1", head_body="", control_state="run", record=None)
    rec = commit_launch(
        now=_T0,
        head_msg_id="msg-1",
        verdict=launch,
        control_state="run",
        head_fetched=False,
        prior_record=prior,
        thread_status="",
    )
    assert rec.last_observed_status == "active"


class _ShapedMcp:
    """Fake MCP returning a caller-supplied raw response per thread, counting fetches."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def call_tool(self, name: str, params: dict[str, object]) -> object:
        assert name == "chatroom_get_thread"
        assert params.get("mode") == "full"
        tid = str(params["thread_id"])
        self.calls.append(tid)
        return self._responses[tid]


def _measured_shape(msg_id: str, body: str, status: str | None) -> dict[str, Any]:
    """The production ``get_thread(mode="full")`` shape measured in msg-4749.

    Top-level keys: ``digest`` / ``messages`` / ``mode`` / ``thread``; ``status`` lives in
    ``thread`` alongside the other thread metadata, and NOT at the top level.
    """
    thread: dict[str, Any] = {
        "affects_threads": [],
        "created_at": "2026-09-20T00:00:00Z",
        "created_by_msg": "msg-1",
        "last_activity_at": "2026-09-23T11:07:04Z",
        "last_msg_id": msg_id,
        "msg_count": 2,
        "owner": "operator",
        "project": "spirrow-playproof",
        "resolved_by_msg": msg_id if status == "resolved" else None,
        "tags": [],
        "thread_id": "T-x",
        "title": "x",
    }
    if status is not None:
        thread["status"] = status
    return {
        "digest": None,
        "messages": [{"msg_id": msg_id, "content": body}],
        "mode": "full",
        "thread": thread,
    }


def _run(
    monkeypatch: pytest.MonkeyPatch,
    fake: _ShapedMcp,
    candidates: list[dict[str, Any]],
    state: dict[str, Record],
    now: datetime = _T0,
) -> tuple[dict[str, Record], list[dict[str, Any]]]:
    monkeypatch.setattr(_CLI, "StreamableHttpChatroomMcp", lambda *_a, **_k: fake)
    result: tuple[dict[str, Record], list[dict[str, Any]]] = asyncio.run(
        _CLI._decide_all(
            project="spirrow-playproof",
            candidates=candidates,
            state=state,
            now=now,
            mode="decide",
            url=None,
        )
    )
    return result


# --- 4. _decide_all against the measured payload shape -------------------------------------------


def test_resolved_thread_gets_no_commit_launch_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _ShapedMcp(
        {
            "T-resolved": _measured_shape("msg-1084", "done\n\nNEXT: Einstein", "resolved"),
            "T-live": _measured_shape("msg-7", "work\n\nNEXT: Einstein", "active"),
        }
    )
    cands = [
        {"thread_id": "T-resolved", "head_msg_id": "msg-1084", "control_state": "run"},
        {"thread_id": "T-live", "head_msg_id": "msg-7", "control_state": "run"},
    ]
    new_state, verdicts = _run(monkeypatch, fake, cands, {})
    by_id = {v["thread_id"]: v for v in verdicts}

    resolved = by_id["T-resolved"]
    assert resolved["decision"] == "skip"
    assert resolved["reason"] == "thread-resolved"
    assert resolved["thread_status"] == "resolved"
    # Structural guarantee: the wrapper has nothing to feed commit-launch, so it cannot spawn.
    assert "commit_launch_payload" not in resolved
    assert new_state["T-resolved"].last_observed_status == "resolved"

    live = by_id["T-live"]
    assert live["decision"] == "launch"
    assert live["thread_status"] == "active"
    assert live["commit_launch_payload"]["thread_status"] == "active"


def test_top_level_status_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A top-level ``status`` (the ``list_threads`` item shape) is not where get_thread puts it.

    Guards the msg-4749 trap from the other side: if the extractor regressed to
    ``result.get("status")``, this response would SKIP; reading only ``thread.status`` it
    fails open to LAUNCH.
    """
    response = _measured_shape("msg-5", "work\n\nNEXT: Bohr", None)
    response["status"] = "resolved"
    fake = _ShapedMcp({"T-a": response})
    _, verdicts = _run(
        monkeypatch, fake, [{"thread_id": "T-a", "head_msg_id": "msg-5", "control_state": "run"}],
        {},
    )  # fmt: skip
    assert verdicts[0]["decision"] == "launch"
    assert verdicts[0]["thread_status"] == ""


@pytest.mark.parametrize(
    "thread_value",
    ["__missing__", None, "resolved", ["resolved"], {}, {"status": None}, {"status": ""}],
)
def test_malformed_thread_object_fails_open(
    monkeypatch: pytest.MonkeyPatch, thread_value: Any
) -> None:
    """Einstein blocking 1: a missing / malformed ``thread`` must not raise and must not skip."""
    response: dict[str, Any] = {
        "messages": [{"msg_id": "msg-5", "content": "work\n\nNEXT: Bohr"}],
        "mode": "full",
    }
    if thread_value != "__missing__":
        response["thread"] = thread_value
    fake = _ShapedMcp({"T-a": response})
    _, verdicts = _run(
        monkeypatch, fake, [{"thread_id": "T-a", "head_msg_id": "msg-5", "control_state": "run"}],
        {},
    )  # fmt: skip
    assert verdicts[0]["decision"] == "launch"
    assert verdicts[0]["thread_status"] == ""


def test_cache_hit_skips_resolved_without_fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _ShapedMcp({"T-r": _measured_shape("msg-1084", "x\n\nNEXT: Einstein", "resolved")})
    cand = [{"thread_id": "T-r", "head_msg_id": "msg-1084", "control_state": "run"}]
    state, first = _run(monkeypatch, fake, cand, {})
    assert first[0]["decision"] == "skip"
    assert fake.calls == ["T-r"]

    # Next tick, same head, within TTL: served from cache, no fetch, still SKIP on status.
    _, second = _run(monkeypatch, fake, cand, state, now=_T0 + timedelta(minutes=5))
    assert fake.calls == ["T-r"]  # no second fetch
    assert second[0]["head_fetched"] is False
    assert second[0]["decision"] == "skip"
    assert second[0]["reason"] == "thread-resolved"
    assert "commit_launch_payload" not in second[0]


class _FailingMcp:
    """Every fetch returns a shape ``_fetch_head_body`` treats as a failure."""

    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, name: str, params: dict[str, object]) -> object:
        self.calls += 1
        return {"messages": []}


def _observed(head: str, status: str) -> Record:
    return commit_observation(now=_T0, head_msg_id=head, token="einstein", record=None,
                              thread_status=status)  # fmt: skip


def test_failed_refetch_on_same_head_keeps_the_observed_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #364 gate: TTL expired → re-fetch → fetch fails. The status seen on this same head
    must still gate Stage 0, and the cached observation must not be overwritten."""
    state = {"T-r": _observed("msg-1084", "resolved")}
    fake = _FailingMcp()
    later = _T0 + timedelta(hours=2)  # past HEAD_CACHE_TTL → cache miss → fetch
    new_state, verdicts = _run(
        monkeypatch, fake,  # type: ignore[arg-type]
        [{"thread_id": "T-r", "head_msg_id": "msg-1084", "control_state": "run"}],
        state, now=later,
    )  # fmt: skip
    assert fake.calls == 1
    v = verdicts[0]
    assert v["head_fetched"] is False
    assert v["decision"] == "skip"
    assert v["reason"] == "thread-resolved"
    assert "commit_launch_payload" not in v
    assert new_state["T-r"].last_observed_status == "resolved"


def test_failed_fetch_on_a_moved_head_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A head that moved took a message after we looked — the old 'resolved' is stale."""
    state = {"T-r": _observed("msg-1084", "resolved")}
    _, verdicts = _run(
        monkeypatch, _FailingMcp(),  # type: ignore[arg-type]
        [{"thread_id": "T-r", "head_msg_id": "msg-1090", "control_state": "run"}],
        state,
    )  # fmt: skip
    assert verdicts[0]["decision"] == "launch"
    assert verdicts[0]["thread_status"] == ""


def test_failed_fetch_with_no_record_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _, verdicts = _run(
        monkeypatch, _FailingMcp(),  # type: ignore[arg-type]
        [{"thread_id": "T-r", "head_msg_id": "msg-1", "control_state": "run"}],
        {},
    )  # fmt: skip
    assert verdicts[0]["decision"] == "launch"
