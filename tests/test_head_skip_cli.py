"""Tests for the head-skip CLI wrapper (``scripts/head_skip_decide.py``).

The module-level predicate is tested exhaustively in ``test_head_skip.py``; this file focuses
on the CLI's TWO-PHASE PROTOCOL (Tier B naysayer round 5, PR #140):

- ``decide`` mode: batch-evaluate every candidate, emit verdicts. Refresh only the
  observation-side of the state file. NEVER touch the launch baseline for any candidate,
  even for those whose verdict is LAUNCH. Emit a ``commit_launch_payload`` on each LAUNCH
  verdict for the sweep to feed back to phase 2.
- ``commit-launch`` mode: apply the launch baseline for exactly ONE thread (the one the sweep
  actually starts). Any other LAUNCH-verdict candidates from the same decide batch are left
  with launch baseline untouched — no phantom-launch, no exponential-starvation loop.

The MCP fetch path is monkey-patched to isolate the tests from network I/O.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spirrow_mindwire.conductor.head_skip import (
    BASE,
    Record,
    record_from_json,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "head_skip_decide.py"


def _load_cli_module() -> object:
    """Load the standalone CLI script as a module (it is not on the import path)."""
    spec = importlib.util.spec_from_file_location("_head_skip_cli_test_module", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_cli_module()


_T0 = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)


class _FakeMcp:
    """Stand-in for :class:`StreamableHttpChatroomMcp` — returns canned bodies per thread."""

    def __init__(self, bodies: dict[str, tuple[str, str] | None]) -> None:
        # bodies[thread_id] = (head_msg_id, head_body); None to simulate a failed fetch.
        self._bodies = bodies

    async def call_tool(self, name: str, params: dict[str, object]) -> object:
        assert name == "chatroom_get_thread"
        thread_id = str(params["thread_id"])
        result = self._bodies.get(thread_id)
        if result is None:
            # Simulate a failed fetch by returning a shape _fetch_head_body treats as empty.
            return {"messages": []}
        msg_id, body = result
        return {"messages": [{"msg_id": msg_id, "content": body}]}


def _patch_mcp(monkeypatch: pytest.MonkeyPatch, bodies: dict[str, tuple[str, str] | None]) -> None:
    """Redirect the CLI's MCP constructor to the fake, so no network I/O happens."""
    fake = _FakeMcp(bodies)
    monkeypatch.setattr(
        _MODULE,
        "StreamableHttpChatroomMcp",
        lambda *_args, **_kwargs: fake,
    )


def _read_state(state_path: Path) -> dict[str, Record]:
    """Read the state file back as a Record map (test-side inverse of ``_save_state``)."""
    if not state_path.exists():
        return {}
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    out: dict[str, Record] = {}
    for tid, rec_dict in raw.items():
        rec = record_from_json(rec_dict)
        if rec is not None:
            out[str(tid)] = rec
    return out


# --- decide phase: does NOT touch launch baseline for any candidate -----------------------------


def test_decide_does_not_commit_launch_state_for_any_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 candidates all LAUNCH-eligible; decide MUST leave the launch baseline untouched.

    This is the Tier B naysayer round-5 core fix. Under the OLD design (batch-decide-commit-
    all), every one of these 3 candidates would have ``last_launch_at`` and
    ``launch_attempts`` written even though the sweep only ever starts one — the other two
    would be "phantom-launched" and eat backoff on subsequent ticks. Under the new two-phase
    design, decide writes only observation-side state; the launch baseline stays untouched
    until ``commit-launch`` is invoked for the one thread the sweep actually starts.
    """
    # Note: no state_path here — this test uses ``_decide_all`` directly on an in-memory dict,
    # so no file I/O is needed. The persistence side is exercised by the end-to-end test below.
    del tmp_path
    # All three threads fetch as LAUNCH-eligible (fresh, no prior record).
    _patch_mcp(
        monkeypatch,
        {
            "T-a": ("msg-a1", "content\n\nNEXT: Bohr"),
            "T-b": ("msg-b1", "content\n\nNEXT: Einstein"),
            "T-c": ("msg-c1", "content\n\nNEXT: Heisenberg"),
        },
    )
    candidates = [
        {"thread_id": "T-a", "head_msg_id": "msg-a1", "control_state": "run"},
        {"thread_id": "T-b", "head_msg_id": "msg-b1", "control_state": "run"},
        {"thread_id": "T-c", "head_msg_id": "msg-c1", "control_state": "run"},
    ]

    new_state, verdicts = asyncio.run(
        _MODULE._decide_all(  # type: ignore[attr-defined]
            project="test",
            candidates=candidates,
            state={},
            now=_T0,
            mode="decide",
            url=None,
        )
    )
    # All three verdicts are LAUNCH...
    assert [v["decision"] for v in verdicts] == ["launch", "launch", "launch"]
    # ...and each carries a self-describing commit_launch_payload for the sweep.
    for v in verdicts:
        assert "commit_launch_payload" in v
        assert v["commit_launch_payload"]["thread_id"] == v["thread_id"]
        assert v["commit_launch_payload"]["head_fetched"] is True

    # But NONE of them has a launch baseline in the state — this is the load-bearing property.
    for tid in ("T-a", "T-b", "T-c"):
        rec = new_state[tid]
        # Observation IS refreshed (we successfully fetched all three bodies).
        assert rec.last_observed_head_msg_id.startswith("msg-")
        assert rec.last_observed_nomination != ""
        assert rec.head_observed_at == _T0
        # Launch baseline: UNCHANGED (defaults) — no launch has actually happened yet.
        assert rec.last_launch_at is None
        assert rec.nomination_at_launch == ""
        assert rec.control_at_launch == ""
        assert rec.head_msg_id_at_launch == ""
        assert rec.launch_attempts == 0


def test_decide_refreshes_observation_even_for_launch_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LAUNCH candidate whose sweep never actually starts still benefits from the observation
    refresh — subsequent ticks can cache-hit and skip the re-fetch."""
    _patch_mcp(monkeypatch, {"T-a": ("msg-a1", "content\n\nNEXT: Bohr")})
    candidates = [{"thread_id": "T-a", "head_msg_id": "msg-a1", "control_state": "run"}]
    new_state, _ = asyncio.run(
        _MODULE._decide_all(  # type: ignore[attr-defined]
            project="test",
            candidates=candidates,
            state={},
            now=_T0,
            mode="decide",
            url=None,
        )
    )
    rec = new_state["T-a"]
    # Observation IS populated (this is what makes the cache-hit on the next tick).
    assert rec.last_observed_head_msg_id == "msg-a1"
    assert rec.last_observed_nomination == "bohr"
    # But the launch baseline stays empty.
    assert rec.last_launch_at is None


# --- commit-launch phase: applies launch baseline for ONE thread only ---------------------------


def test_commit_launch_applies_baseline_for_one_thread_only(tmp_path: Path) -> None:
    """Feeding a commit_launch_payload to commit-launch writes ONE thread's launch baseline.

    Regression guard: commit-launch must not touch any other thread's state. The state file
    may hold observations for other threads from a prior decide phase; those must survive.
    """
    state_path = tmp_path / "head_skip.json"
    # Pre-populate the state with observations for two threads (as decide would have done).
    pre_state = {
        "T-a": {
            "last_launch_at": None,
            "nomination_at_launch": "",
            "control_at_launch": "",
            "head_msg_id_at_launch": "",
            "launch_attempts": 0,
            "head_observed_at": _T0.isoformat(),
            "last_observed_head_msg_id": "msg-a1",
            "last_observed_nomination": "bohr",
        },
        "T-b": {
            "last_launch_at": None,
            "nomination_at_launch": "",
            "control_at_launch": "",
            "head_msg_id_at_launch": "",
            "launch_attempts": 0,
            "head_observed_at": _T0.isoformat(),
            "last_observed_head_msg_id": "msg-b1",
            "last_observed_nomination": "einstein",
        },
    }
    state_path.write_text(json.dumps(pre_state), encoding="utf-8")

    # Commit-launch for T-a only.
    payload = {
        "thread_id": "T-a",
        "head_msg_id": "msg-a1",
        "token": "bohr",
        "token_raw": "Bohr",
        "attempts_after": 1,
        "control_state": "run",
        "head_fetched": True,
    }
    commit_time = _T0 + timedelta(seconds=30)  # sweep may take a bit
    new_record = _MODULE._apply_commit_launch(  # type: ignore[attr-defined]
        state_path=state_path,
        payload=payload,
        now=commit_time,
    )

    # Returned record has the launch baseline set for T-a.
    assert new_record.last_launch_at == commit_time
    assert new_record.nomination_at_launch == "bohr"
    assert new_record.launch_attempts == 1

    # State file: T-a got its launch baseline, T-b was untouched.
    final = _read_state(state_path)
    assert final["T-a"].last_launch_at == commit_time
    assert final["T-a"].launch_attempts == 1
    # T-b's observation is still there, launch baseline still empty (never launched).
    assert final["T-b"].last_launch_at is None
    assert final["T-b"].last_observed_head_msg_id == "msg-b1"
    assert final["T-b"].last_observed_nomination == "einstein"
    assert final["T-b"].launch_attempts == 0


def test_full_two_phase_cycle_matches_naysayer_round_5_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: 3 concurrently-eligible candidates. Sweep commits only 1. Others stay
    eligible next tick — NO phantom-launch, NO exponential-starvation loop.

    This is the naysayer round-5 scenario exactly. Before the fix: T-b and T-c would land in
    the state file with launch baselines set, next tick they would DEFER (BASE=15min), then
    LAUNCH again, sweep picks one, others go to 2*BASE, and so on. After the fix: only the
    sweep-selected T-a gets a launch commit; T-b / T-c re-evaluate freshly on the next tick.
    """
    state_path = tmp_path / "head_skip.json"
    _patch_mcp(
        monkeypatch,
        {
            "T-a": ("msg-a1", "content\n\nNEXT: Bohr"),
            "T-b": ("msg-b1", "content\n\nNEXT: Einstein"),
            "T-c": ("msg-c1", "content\n\nNEXT: Heisenberg"),
        },
    )
    candidates = [
        {"thread_id": "T-a", "head_msg_id": "msg-a1", "control_state": "run"},
        {"thread_id": "T-b", "head_msg_id": "msg-b1", "control_state": "run"},
        {"thread_id": "T-c", "head_msg_id": "msg-c1", "control_state": "run"},
    ]

    # Phase 1: decide.
    new_state, verdicts = asyncio.run(
        _MODULE._decide_all(  # type: ignore[attr-defined]
            project="test",
            candidates=candidates,
            state={},
            now=_T0,
            mode="decide",
            url=None,
        )
    )
    # Persist as the CLI would.
    from spirrow_mindwire.conductor.head_skip import record_to_json

    state_path.write_text(
        json.dumps({tid: record_to_json(rec) for tid, rec in new_state.items()}),
        encoding="utf-8",
    )

    # Sweep decides to commit-launch T-a and starts the conductor. It never gets to T-b / T-c.
    payload_a = verdicts[0]["commit_launch_payload"]
    _MODULE._apply_commit_launch(  # type: ignore[attr-defined]
        state_path=state_path,
        payload=payload_a,
        now=_T0 + timedelta(seconds=30),
    )

    final = _read_state(state_path)

    # T-a: launch committed.
    assert final["T-a"].last_launch_at is not None
    assert final["T-a"].launch_attempts == 1

    # T-b, T-c: launch baseline NOT set (no phantom launch), observation IS set (next-tick
    # re-evaluation will cache-hit and reach LAUNCH again immediately, not DEFER).
    for tid in ("T-b", "T-c"):
        assert final[tid].last_launch_at is None
        assert final[tid].launch_attempts == 0
        assert final[tid].last_observed_head_msg_id.startswith("msg-")
        assert final[tid].last_observed_nomination != ""

    # Second tick: re-evaluate. T-b and T-c should STILL be LAUNCH (freshly eligible).
    # The optimisation here is that the cached observations mean no re-fetch is needed.
    now_tick2 = _T0 + timedelta(minutes=5)
    _patch_mcp(monkeypatch, {})  # NO bodies — must be cache-hits, else the test detects a fetch
    _new_state_2, verdicts_2 = asyncio.run(
        _MODULE._decide_all(  # type: ignore[attr-defined]
            project="test",
            candidates=candidates,
            state=final,
            now=now_tick2,
            mode="decide",
            url=None,
        )
    )
    # T-b / T-c: cache-hit -> synth body -> decide -> LAUNCH (no-prior-record, since
    # launch baseline is still empty).
    verdicts_by_tid = {v["thread_id"]: v for v in verdicts_2}
    assert verdicts_by_tid["T-b"]["decision"] == "launch"
    assert verdicts_by_tid["T-b"]["head_fetched"] is False  # cache-hit
    assert verdicts_by_tid["T-c"]["decision"] == "launch"
    assert verdicts_by_tid["T-c"]["head_fetched"] is False

    # T-a: cache-hit -> synth `NEXT: bohr` -> decide sees the launched baseline.
    # last_launch_at was 30s past _T0; now is 5min past _T0. Delta = 4min 30s < BASE(15min).
    # Not progressed (token=`bohr` = baseline `bohr` = observation `bohr`). Delay=BASE. DEFER.
    assert verdicts_by_tid["T-a"]["decision"] == "defer"


def test_commit_launch_handles_never_seen_thread(tmp_path: Path) -> None:
    """commit-launch on a thread with no prior record works (initial launch case)."""
    state_path = tmp_path / "head_skip.json"
    state_path.write_text("{}", encoding="utf-8")

    payload = {
        "thread_id": "T-new",
        "head_msg_id": "msg-1",
        "token": "bohr",
        "token_raw": "Bohr",
        "attempts_after": 1,
        "control_state": "run",
        "head_fetched": True,
    }
    new_record = _MODULE._apply_commit_launch(  # type: ignore[attr-defined]
        state_path=state_path,
        payload=payload,
        now=_T0,
    )
    assert new_record.last_launch_at == _T0
    assert new_record.nomination_at_launch == "bohr"
    assert new_record.last_observed_nomination == "bohr"  # head_fetched=True populates it


def test_commit_launch_head_fetched_false_preserves_prior_observation(tmp_path: Path) -> None:
    """A fail-open commit-launch (head_fetched=False) must NOT overwrite the prior observation.

    Combined round-2 anti-poison rule with round-5 two-phase design: commit-launch must honor
    the head_fetched flag from the payload. This is the CLI-level analogue of round 2 test #18.
    """
    state_path = tmp_path / "head_skip.json"
    pre_state = {
        "T-a": {
            "last_launch_at": _T0.isoformat(),
            "nomination_at_launch": "bohr",
            "control_at_launch": "run",
            "head_msg_id_at_launch": "msg-1",
            "launch_attempts": 1,
            "head_observed_at": _T0.isoformat(),
            "last_observed_head_msg_id": "msg-1",
            "last_observed_nomination": "bohr",
        },
    }
    state_path.write_text(json.dumps(pre_state), encoding="utf-8")

    # Fail-open commit-launch payload (fetch was unsuccessful, verdict token empty).
    payload = {
        "thread_id": "T-a",
        "head_msg_id": "msg-99",  # probe-reported id
        "token": "",  # empty because fetch failed
        "token_raw": None,
        "attempts_after": 2,
        "control_state": "run",
        "head_fetched": False,
    }
    commit_time = _T0 + timedelta(minutes=BASE.total_seconds() // 60)  # after BASE elapsed
    new_record = _MODULE._apply_commit_launch(  # type: ignore[attr-defined]
        state_path=state_path,
        payload=payload,
        now=commit_time,
    )
    # Launch baseline advances (real attempt).
    assert new_record.last_launch_at == commit_time
    assert new_record.launch_attempts == 2
    # But observation is PRESERVED from prior (round-2 anti-poison).
    assert new_record.last_observed_head_msg_id == "msg-1"
    assert new_record.last_observed_nomination == "bohr"
