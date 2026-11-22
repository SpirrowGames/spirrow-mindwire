"""T-tier-c-six-articles-security-boundary part 4 — the ``security`` section of the measurement.

Spec: Bohr msg-9912 B → msg-9914 (Obj-2/3/4) → msg-9916 (Obj-2-2, release attribution), endorsed
by Einstein msg-9917. Outcomes come from the decisions log only; ``formal_recheck`` is the rerun,
kept apart. Every value is a count, a hold id or a ``thread#msg_id`` ref.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from test_conductor_core import _ROSTER as ROSTER

SINCE = datetime(2026, 11, 22, tzinfo=UTC)
UNTIL = SINCE + timedelta(days=14)
HUMAN = "human"
BASIS = "SECURITY-BASIS: widen gh token scope repo->org"


def _measure() -> Any:
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "tierc_gate_measure_security", root / "scripts" / "tierc_gate_measure.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tierc_gate_measure_security"] = mod
    spec.loader.exec_module(mod)
    return mod


MEASURE = _measure()


def _at(day: float) -> datetime:
    return SINCE + timedelta(days=day)


def _row(kind: str, msg: str, day: float, **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "thread": "T-a",
        "author": "Bohr",
        "msg_id": msg,
        "ts": _at(day).isoformat(),
        **extra,
    }


def _held(msg: str, day: float, hold_id: str, sha: str = "s1", **extra: Any) -> dict[str, Any]:
    return _row(
        "BOUNCED",
        msg,
        day,
        reason="security-basis-missing",
        label="security",
        retry_uuid=hold_id,
        security_hold=True,
        hold_kind="structural",
        body_sha256=sha,
        **extra,
    )


def _msg(i: int, author: str, content: str, day: float) -> Any:
    return MEASURE.builder.RawMessage(
        msg_id=f"msg-{i}",
        author=author,
        content=content,
        timestamp=_at(day),
        role=None,
        next_participant=None,
    )


def _section(
    rows: list[dict[str, Any]],
    messages: list[Any] | None = None,
    *,
    replay_from: datetime | None = None,
) -> dict[str, Any]:
    threads = [] if messages is None else [("p", "T-a", messages)]
    return MEASURE.security_section(  # type: ignore[no-any-return]
        rows,
        threads,
        since=SINCE,
        until=UNTIL,
        human_identity=HUMAN,
        replay_from=replay_from,
    )


def test_one_of_each_simple_row() -> None:
    rows = [
        _row("ESCALATED", "msg-1", 1, label="security"),
        _held("msg-2", 2, "h1"),
        _row("BOUNCED", "msg-3", 3, reason="security-hold", label="security", retry_uuid="u3"),
        _row("BOUNCED", "msg-4", 4, reason="security-hold-no-verdict", label="goal"),
        _row("HOLD_RELEASED", "msg-5", 5, holds_released=["h1"]),
        _row("RETRY_ADMIT", "msg-6", 6, label="security"),
    ]
    out = _section(rows, [_msg(5, HUMAN, "HOLD-RELEASE: h1", 4.5)])
    assert out["heads"] == {
        "delivered": 1,
        "bounced": 2,
        "bounced_by_reason": {"security-basis-missing": 1, "security-hold": 1},
        "no_outcome": 1,
    }
    assert out["heads_msg_ids"]["delivered"] == ["T-a#msg-1"]
    assert out["heads_msg_ids"]["no_outcome"] == ["T-a#msg-6"]
    assert out["hold_bounces"] == {"security-hold": 1, "security-hold-no-verdict": 1}
    assert out["holds_opened"] == 1
    assert out["holds_opened_ids"] == ["h1"]
    assert out["holds_released"] == {"human": 1, "delivery": 0, "unknown": 0}
    json.dumps(out)  # counts and ids only: serialisable as written


def test_a_head_that_passes_the_formal_check_but_bounces_on_a_hold_counts_as_bounced() -> None:
    body = f"body\n\nTIER-C: security\n{BASIS}\nNEXT: human"
    message = _msg(7, "Bohr", body, 2)
    formal = MEASURE.classify([("p", "T-a", [message])], dict(ROSTER), since=SINCE, until=UNTIL)
    assert formal["formal_recheck"]["admitted_msg_ids"] == ["p/T-a#msg-7"]
    rows = [
        _held("msg-1", 1, "h1"),
        _row("BOUNCED", "msg-7", 2, reason="security-hold", label="security", retry_uuid="u7"),
    ]
    out = _section(rows, [message])
    assert out["heads"]["delivered"] == 0
    assert out["heads_msg_ids"]["bounced_by_reason"]["security-hold"] == ["T-a#msg-7"]
    assert "formal_recheck" not in out  # the rerun is never mixed into the actual counts


def test_a_refusal_merged_into_an_open_identical_hold_opens_no_new_hold() -> None:
    rows = [_held("msg-1", 1, "h1", sha="same"), _held("msg-2", 2, "h2", sha="same")]
    out = _section(rows)
    assert out["holds_opened_ids"] == ["h1"]
    assert out["heads"]["bounced"] == 2  # two heads bounced, one hold


def test_a_hold_opened_before_the_window_and_released_inside_it() -> None:
    rows = [
        _held("msg-1", -3, "h0"),
        _row("ESCALATED", "msg-2", 1, label="security", holds_released=["h0"]),
    ]
    out = _section(rows, [])
    assert out["holds_opened"] == 0
    assert out["heads_msg_ids"]["bounced_by_reason"] == {}  # the pre-window bounce is not counted
    assert out["holds_released_ids"] == {"human": [], "delivery": ["h0"], "unknown": []}


def test_replay_from_drops_earlier_rows_from_the_replay() -> None:
    rows = [
        _held("msg-1", -3, "h0"),
        _row("ESCALATED", "msg-2", 1, label="security", holds_released=["h0"]),
    ]
    out = _section(rows, [], replay_from=_at(-1))
    assert out["holds_released"] == {"human": 0, "delivery": 0, "unknown": 0}
    assert out["release_stamps_unmatched"] == 1
    assert out["window"]["replay_from"] == _at(-1).isoformat()


def test_a_human_release_stamped_on_an_escalated_row_counts_as_human() -> None:
    rows = [
        _held("msg-1", 1, "h1"),
        _row("ESCALATED", "msg-3", 3, label="goal", holds_released=["h1"]),
    ]
    out = _section(rows, [_msg(2, "Human", "ok\nHOLD-RELEASE: h1\n", 2)])
    assert out["holds_released_ids"]["human"] == ["h1"]


def test_the_same_line_by_someone_else_is_unknown() -> None:
    rows = [_held("msg-1", 1, "h1"), _row("HOLD_RELEASED", "msg-3", 3, holds_released=["h1"])]
    out = _section(rows, [_msg(2, "Bohr", "HOLD-RELEASE: h1", 2)])
    assert out["holds_released"] == {"human": 0, "delivery": 0, "unknown": 1}


def test_a_human_line_posted_after_the_stamped_row_is_not_human() -> None:
    rows = [
        _held("msg-1", 1, "h1"),
        _row("HOLD_RELEASED", "msg-3", 3, holds_released=["h1"]),
    ]
    out = _section(rows, [_msg(4, HUMAN, "HOLD-RELEASE: h1", 4)])
    assert out["holds_released"]["unknown"] == 1


def test_an_unread_thread_makes_a_delivery_stamp_unknown() -> None:
    rows = [
        _held("msg-1", 1, "h1"),
        _row("ESCALATED", "msg-2", 2, label="security", holds_released=["h1"]),
    ]
    out = _section(rows, None)
    assert out["holds_released"] == {"human": 0, "delivery": 0, "unknown": 1}


def test_a_delivery_after_the_window_does_not_change_a_bounce_inside_it() -> None:
    bounce = _row("BOUNCED", "msg-1", 13.9, reason="security-hold", label="security")
    before = _section([bounce])
    late = _row("ESCALATED", "msg-1", 14.1, label="security")
    after = _section([bounce, late])
    assert before == after
    assert after["heads"]["bounced_by_reason"] == {"security-hold": 1}
    assert after["heads"]["delivered"] == 0


def test_within_the_window_escalated_wins_over_a_bounce_of_the_same_head() -> None:
    rows = [
        _row("BOUNCED", "msg-1", 1, reason="jev-likely-not", label="security"),
        _row("ESCALATED", "msg-1", 2, label="security"),
    ]
    out = _section(rows)
    assert out["heads"]["delivered"] == 1
    assert out["heads"]["bounced"] == 0


def test_a_row_without_a_time_is_counted_nowhere() -> None:
    row = _row("ESCALATED", "msg-1", 1, label="security")
    del row["ts"]
    out = _section([row])
    assert out["rows_without_ts"] == 1
    assert out["heads"]["delivered"] == 0


def test_an_unreadable_log_is_unmeasured_not_zero(tmp_path: Path) -> None:
    missing = MEASURE.load_decisions_log(tmp_path / "nope.jsonl")
    assert isinstance(missing, str) and missing.startswith("unmeasured: ")
    folder = tmp_path / "dir.jsonl"
    folder.mkdir()
    assert MEASURE.load_decisions_log(folder).startswith("unmeasured: ")
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(_held("msg-1", 1, "h1")) + "\n", encoding="utf-8")
    assert MEASURE.load_decisions_log(log) == [_held("msg-1", 1, "h1")]
