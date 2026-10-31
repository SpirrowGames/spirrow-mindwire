"""T-operator-task-lane D0 (Bohr msg-7408, approved by Einstein msg-7409): a well-formed
``NEXT: operator`` head must be SKIPped by the sweep on EVERY tick, not just the tick that
fetched its body.

Measured (conductor log, 2026-10-21..24): msg-7364 / msg-7365 / msg-7369 were launched 49-53
times each on an unchanged head, every launch ending ``operator_work_to_human rounds=0``. The
same head alternated in the log between ``head_skip SKIP (reason=stop-token, token=operator)``
and ``head_skip DEFER (delay=3600s)``. Cause: on a head-cache hit ``_decide_all`` synthesises the
body ``NEXT: <cached token>``. ``operator`` is the one CONDITIONAL member of ``STOP_TOKENS`` —
Stage 1 skips it only when the whole body is the accepted 3-line form — and the synthesised
one-line body never is, so every cache-hit tick fell through to Stage 2 and launched at CAP.

The three real bodies are pinned under ``tests/data/head_skip_operator_heads/``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.conductor.handoff import OPERATOR_TOKEN
from spirrow_mindwire.conductor.head_skip import (
    BODY_DEPENDENT_STOP_TOKENS,
    HEAD_CACHE_TTL,
    STOP_TOKENS,
    Record,
    can_reuse_cached_parse,
    commit_observation,
    stage1_skips,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "head_skip_decide.py"
_FIXTURE_DIR = Path(__file__).resolve().parent / "data" / "head_skip_operator_heads"


def _load_cli_module() -> Any:
    spec = importlib.util.spec_from_file_location("_head_skip_operator_test_module", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CLI = _load_cli_module()

_T0 = datetime(2026, 10, 24, 5, 0, 0, tzinfo=UTC)


def _fixtures() -> list[dict[str, Any]]:
    out = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(_FIXTURE_DIR.glob("*.json"))]
    assert {f["msg_id"] for f in out} == {"msg-7364", "msg-7365", "msg-7369"}
    return out


_FIXTURES = _fixtures()


class _CountingMcp:
    def __init__(self, thread_id: str, msg_id: str, body: str) -> None:
        self._thread_id = thread_id
        self._msg_id = msg_id
        self._body = body
        self.calls = 0

    async def call_tool(self, name: str, params: dict[str, object]) -> object:
        assert name == "chatroom_get_thread"
        assert params["thread_id"] == self._thread_id
        self.calls += 1
        return {
            "messages": [{"msg_id": self._msg_id, "content": self._body}],
            "thread": {"thread_id": self._thread_id, "status": "awaiting_reply"},
        }


def _tick(
    monkeypatch: pytest.MonkeyPatch,
    fake: _CountingMcp,
    candidates: list[dict[str, Any]],
    state: dict[str, Record],
    now: datetime,
) -> tuple[dict[str, Record], list[dict[str, Any]]]:
    monkeypatch.setattr(_CLI, "StreamableHttpChatroomMcp", lambda *_a, **_k: fake)
    result: tuple[dict[str, Record], list[dict[str, Any]]] = asyncio.run(
        _CLI._decide_all(
            project="spirrow-mindwire",
            candidates=candidates,
            state=state,
            now=now,
            mode="decide",
            url=None,
        )
    )
    return result


def _launched_record(msg_id: str) -> Record:
    """The measured shape: launched many times at CAP on this same operator head."""
    return Record(
        last_launch_at=_T0 - timedelta(hours=2),
        nomination_at_launch=OPERATOR_TOKEN,
        control_at_launch="run",
        head_msg_id_at_launch=msg_id,
        launch_attempts=53,
        launches_same_head=53,
    )


@pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda f: f["msg_id"])
def test_real_operator_body_is_the_parked_form(fixture: dict[str, Any]) -> None:
    # The premise of D0: these bodies are well-formed, so the boundary owner says SKIP.
    assert stage1_skips(fixture["body"]) is True


@pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda f: f["msg_id"])
@pytest.mark.parametrize("seed", ["no-record", "launched-at-cap"])
def test_sweep_skips_a_parked_operator_head_on_every_tick(
    monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any], seed: str
) -> None:
    """The sweep's verdict equals ``stage1_skips(body)`` on the fetch tick AND every tick after.

    Before the fix, ticks 2..n within HEAD_CACHE_TTL were cache hits, served ``NEXT: operator``,
    and returned DEFER / LAUNCH instead of SKIP.
    """
    tid, mid, body = fixture["thread_id"], fixture["msg_id"], fixture["body"]
    fake = _CountingMcp(tid, mid, body)
    cand = [{"thread_id": tid, "head_msg_id": mid, "control_state": "run"}]
    state: dict[str, Record] = {} if seed == "no-record" else {tid: _launched_record(mid)}
    expected = stage1_skips(body)
    # 3-minute ticks across more than one CAP window, so a backoff-eligible tick is included.
    for i in range(25):
        state, verdicts = _tick(monkeypatch, fake, cand, state, _T0 + timedelta(minutes=3 * i))
        v = verdicts[0]
        assert (v["decision"] == "skip") is expected, (i, v)
        assert v["reason"] == "stop-token", (i, v)
        assert "commit_launch_payload" not in v
    # Every tick re-read the body: an operator head is never served from the token cache.
    assert fake.calls == 25


def test_malformed_operator_head_still_launches_after_a_cache_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixing the cache must not park a MALFORMED operator head: the conductor posts its
    stand-down notice, so it has to keep launching (head_skip.py STOP_TOKENS comment)."""
    body = "please do the thing\n\nNEXT: operator"
    assert stage1_skips(body) is False
    fake = _CountingMcp("T-m", "msg-1", body)
    cand = [{"thread_id": "T-m", "head_msg_id": "msg-1", "control_state": "run"}]
    state, first = _tick(monkeypatch, fake, cand, {}, _T0)
    assert first[0]["decision"] == "launch"
    _, second = _tick(monkeypatch, fake, cand, state, _T0 + timedelta(minutes=3))
    assert second[0]["decision"] != "skip"


def test_body_dependent_tokens_are_the_conditional_stop_members() -> None:
    assert frozenset({OPERATOR_TOKEN}) == BODY_DEPENDENT_STOP_TOKENS
    assert BODY_DEPENDENT_STOP_TOKENS <= STOP_TOKENS
    # Every stop token NOT listed here must be decidable from the synthesised one-line body,
    # otherwise the cache would mis-serve it the same way.
    for token in STOP_TOKENS - BODY_DEPENDENT_STOP_TOKENS:
        assert stage1_skips(f"NEXT: {token}") is True, token
    for token in BODY_DEPENDENT_STOP_TOKENS:
        assert stage1_skips(f"NEXT: {token}") is False, token


def test_cache_predicate_refuses_a_cached_operator_token_but_serves_others() -> None:
    later = _T0 + timedelta(minutes=5)
    assert later - _T0 < HEAD_CACHE_TTL
    op = commit_observation(
        now=_T0, head_msg_id="msg-1", token=OPERATOR_TOKEN, record=None, thread_status=""
    )
    assert can_reuse_cached_parse(op, "msg-1", later) is False
    human = commit_observation(
        now=_T0, head_msg_id="msg-1", token="human", record=None, thread_status=""
    )
    assert can_reuse_cached_parse(human, "msg-1", later) is True
