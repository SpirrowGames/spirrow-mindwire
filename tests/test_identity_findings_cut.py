"""Tests for the PR-C per-identity cut in ``scripts/identity_findings.py``.

T-role-null-must-become-impossible, msg-5676 §1/§4 + msg-5678 §2 + msg-5680 §2-§3:

* each registered participant gets one of violated / evidenced / silent, cut at its own
  store ``created_at`` (``undetermined`` when the cut itself cannot be computed);
* start condition 1 = no participant violated, condition 2 = all five critical-path
  identities evidenced, other participants may be silent;
* ``human``'s post-cut null posts are split by the delegation record.

These pin the fail-closed edges: a JST store value read as UTC, an undated null post, a
failed lookup. Each of them would otherwise let a post-registration null pass as history.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.identity import load_legitimate_roles

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "identity_findings.py"


def _load_cli_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_identity_findings_cut_test_module", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_cli_module()
_JST = _MODULE._parse_tz_offset("+09:00")


def _found(ic: str, created_at: str = "2026-09-30T16:52:37.196996") -> dict[str, Any]:
    return {
        "status": "found",
        "identity": {"independence_class": ic, "allowed_roles": [], "created_at": created_at},
    }


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


# created_at 2026-09-30T16:52:37 JST == 07:52:37Z.
_BEFORE = _t("2026-09-30T07:00:00Z")
_AFTER = _t("2026-09-30T08:00:00Z")


def test_participant_with_post_cut_null_is_violated() -> None:
    posts = [(_BEFORE, None, False), (_AFTER, "naysayer", False), (_AFTER, None, False)]
    row = _MODULE._cut_row("naysayer-pr-review", _found("independent"), posts, _JST)
    assert row["state"] == "violated"
    assert row["pre_cut"] == {"posts": 1, "null_role": 1}
    assert row["post_cut"] == {"posts": 2, "null_role": 1}
    assert row["cut"] == "2026-09-30T07:52:37.196996Z"
    assert row["last_post_cut_null_at"] == "2026-09-30T08:00:00Z"


def test_last_post_cut_null_at_is_none_without_post_cut_nulls() -> None:
    posts = [(_BEFORE, None, False), (_AFTER, "naysayer", False)]
    row = _MODULE._cut_row("naysayer-pr-review", _found("independent"), posts, _JST)
    assert row["last_post_cut_null_at"] is None


def test_pre_cut_nulls_are_history_and_do_not_violate() -> None:
    posts = [(_BEFORE, None, False), (_AFTER, "naysayer", False)]
    row = _MODULE._cut_row("naysayer-pr-review", _found("independent"), posts, _JST)
    assert row["state"] == "evidenced"


def test_participant_with_no_post_cut_posts_is_silent() -> None:
    row = _MODULE._cut_row("Fermi", _found("human"), [(_BEFORE, None, False)], _JST)
    assert row["state"] == "silent"


def test_naive_store_time_is_read_in_the_given_offset_not_utc() -> None:
    """Read as UTC, the cut moves 9 hours late and the 08:00Z null becomes 'history'."""
    posts = [(_AFTER, None, False)]
    as_jst = _MODULE._cut_row("x", _found("independent"), posts, _JST)
    as_utc = _MODULE._cut_row("x", _found("independent"), posts, _MODULE._parse_tz_offset("Z"))
    assert as_jst["state"] == "violated"
    assert as_utc["state"] == "silent"


def test_undated_null_post_violates_and_undated_post_is_not_evidence() -> None:
    violated = _MODULE._cut_row("x", _found("main-chain"), [(None, None, False)], _JST)
    assert violated["state"] == "violated"
    assert violated["undated"] == {"posts": 1, "null_role": 1}
    silent = _MODULE._cut_row("x", _found("main-chain"), [(None, "proposer", False)], _JST)
    assert silent["state"] == "silent"


def test_machine_identity_has_no_state() -> None:
    row = _MODULE._cut_row("pr-gate-relay", _found("machine"), [(_AFTER, None, False)], _JST)
    assert row["participant"] is False
    assert row["state"] is None


def test_unregistered_author_gets_no_cut_and_no_state() -> None:
    row = _MODULE._cut_row(
        "operator", {"status": "not_found"}, [(_AFTER, None, False), (None, None, False)], _JST
    )
    assert row["cut"] is None
    assert row["cut_reason"] == "unregistered"
    assert row["state"] is None
    assert row["all_posts"] == {"posts": 2, "null_role": 2}


@pytest.mark.parametrize(
    ("lookup", "reason"),
    [
        (None, "store_status:lookup_failed"),
        ({"status": "contract_violation"}, "store_status:contract_violation"),
        (_found("independent", created_at="not-a-date"), "created_at_unparseable"),
    ],
)
def test_measurement_failure_is_undetermined_not_clean(
    lookup: dict[str, Any] | None, reason: str
) -> None:
    row = _MODULE._cut_row("Bohr", lookup, [(_AFTER, "proposer", False)], _JST)
    assert row["cut_reason"] == reason
    assert row["state"] == "undetermined"


def test_human_gets_delegation_breakdown_and_others_do_not() -> None:
    posts = [
        (_AFTER, None, True),
        (_AFTER, None, False),
        (_AFTER, None, True),
        (_AFTER, "human", True),
        (_BEFORE, None, True),
    ]
    human = _MODULE._cut_row("human", _found("human"), posts, _JST)
    assert human["delegation"] == {
        "post_cut_null_role": 3,
        "delegated": 2,
        "not_delegated": 1,
        "post_cut_with_role_delegated": 1,
    }
    assert "delegation" not in _MODULE._cut_row("Bohr", _found("main-chain"), posts, _JST)


def test_delegation_marker_is_the_record_not_a_paraphrase() -> None:
    assert _MODULE._is_delegated({"content": "**代行の記録**: operator が human 名義で投稿"})
    assert not _MODULE._is_delegated({"content": "operator が代行した"})
    assert not _MODULE._is_delegated({})


def _row(name: str, state: str | None) -> dict[str, Any]:
    return {"identity_name": name, "state": state}


_ALL_CRITICAL_EVIDENCED = [_row(n, "evidenced") for n in _MODULE._CRITICAL_PATH]


def test_phase2_start_passes_with_critical_evidenced_and_others_silent() -> None:
    rows = [*_ALL_CRITICAL_EVIDENCED, _row("Fermi", "silent"), _row("operator", None)]
    verdict = _MODULE._phase2_start(rows)
    assert verdict["pass"] is True


def test_phase2_start_condition_1_fails_on_any_violated_participant() -> None:
    verdict = _MODULE._phase2_start([*_ALL_CRITICAL_EVIDENCED, _row("Fermi", "violated")])
    assert verdict["condition_1_no_violated"] == {
        "pass": False,
        "violated": ["Fermi"],
        "undetermined": [],
    }
    assert verdict["condition_2_critical_path_evidenced"]["pass"] is True
    assert verdict["pass"] is False


def test_phase2_start_condition_1_fails_on_undetermined() -> None:
    verdict = _MODULE._phase2_start([*_ALL_CRITICAL_EVIDENCED, _row("x", "undetermined")])
    assert verdict["condition_1_no_violated"]["pass"] is False
    assert verdict["pass"] is False


def test_phase2_start_condition_2_names_each_unevidenced_critical_identity() -> None:
    rows = [r for r in _ALL_CRITICAL_EVIDENCED if r["identity_name"] not in {"human", "Bohr"}]
    rows.append(_row("Bohr", "silent"))
    rows.append(_row("human", "undetermined"))
    verdict = _MODULE._phase2_start(rows)
    assert verdict["condition_2_critical_path_evidenced"] == {
        "pass": False,
        "not_evidenced": [
            {"identity_name": "Bohr", "state": "silent"},
            {"identity_name": "human", "state": "undetermined"},
        ],
    }
    assert verdict["condition_1_no_violated"]["undetermined"] == ["human"]
    assert verdict["pass"] is False


def test_phase2_start_raises_when_a_critical_path_row_is_missing() -> None:
    rows = [r for r in _ALL_CRITICAL_EVIDENCED if r["identity_name"] != "human"]
    with pytest.raises(ValueError, match="human"):
        _MODULE._phase2_start(rows)


def test_critical_path_is_the_list_fixed_in_msg_5680() -> None:
    assert _MODULE._CRITICAL_PATH == (
        "naysayer-pr-review",
        "Bohr",
        "Einstein",
        "Heisenberg",
        "human",
    )


@pytest.mark.parametrize("bad", ["", "JST", "+9", "2026-01-01", None])
def test_parse_tz_offset_rejects_junk(bad: Any) -> None:
    with pytest.raises(ValueError):
        _MODULE._parse_tz_offset(bad)


def test_main_rejects_bad_store_tz_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("network touched")

    monkeypatch.setattr(_MODULE, "StreamableHttpChatroomMcp", _boom)
    monkeypatch.setattr(sys, "argv", ["identity_findings.py", "--store-naive-tz", "JST"])
    assert _MODULE.main() == 2


class _CorpusMcp:
    """One thread: a Bohr post before --since but after Bohr's cut, and an operator post."""

    def __init__(self) -> None:
        self.lookups: list[str] = []

    async def call_tool(self, name: str, params: dict[str, Any]) -> Any:
        if name == "chatroom_list_threads":
            if int(params.get("offset") or 0) > 0:
                return {"items": [], "total": 1}
            return {"items": [{"thread_id": "T-a"}], "total": 1}
        if name == "get_identity":
            who = params["identity_name"]
            self.lookups.append(who)
            if who == "Bohr":
                return _found("main-chain", created_at="2026-05-29T17:07:47.497866")
            return {"status": "not_found", "identity_name": who}
        assert name == "chatroom_get_thread"
        return {
            "messages": [
                {
                    "msg_id": "msg-1",
                    "author": "Bohr",
                    "role": None,
                    "timestamp": "2026-08-01T00:00:00Z",
                },
                {
                    "msg_id": "msg-2",
                    "author": "operator",
                    "role": None,
                    "timestamp": "2026-09-01T00:00:00Z",
                },
            ]
        }


def test_measure_cut_ignores_since_and_covers_the_critical_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _CorpusMcp()
    monkeypatch.setattr(_MODULE, "StreamableHttpChatroomMcp", lambda *_a, **_kw: fake)
    path = _REPO_ROOT / "spec" / "identity" / "legitimate_roles.yaml"
    since = "2026-08-25T00:00:00Z"
    result: dict[str, Any] = asyncio.run(
        _MODULE._measure(
            projects=("spirrow-mindwire",),
            since_iso=since,
            since_cutoff=_MODULE._parse_cutoff(since),
            since_msg_id=None,
            url=None,
            classification=load_legitimate_roles(path),
            classification_path=path,
        )
    )
    rows = {r["identity_name"]: r for r in result["identity_cut"]}
    # Bohr's 08-01 null post is before --since (so outside "authors"), yet after Bohr's cut.
    assert "Bohr" not in {a["raw_name"] for a in result["authors"]}
    assert rows["Bohr"]["state"] == "violated"
    assert rows["operator"]["cut_reason"] == "unregistered"
    # Every critical-path identity is looked up, even with no posts in the corpus.
    assert set(_MODULE._CRITICAL_PATH) <= set(rows)
    assert result["scope"]["store_naive_tz"] == "+09:00"
    assert result["phase2_start"]["pass"] is False
    assert result["totals"]["phase2_start_pass"] is False
    assert datetime.fromisoformat(rows["Bohr"]["cut"].replace("Z", "+00:00")) == datetime(
        2026, 5, 29, 8, 7, 47, 497866, tzinfo=UTC
    )
