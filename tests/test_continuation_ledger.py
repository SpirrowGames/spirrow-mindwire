"""T-implementer-self-continuation PR-2: per-project ``continue_cap`` and the D-3 ledger.

Design: Bohr msg-7971 (scope), revised msg-7973 (Obj-1-1 cap type, Obj-1-2 ledger record unit);
naysayer go from Einstein on the reply to msg-7973.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_conductor_continuation import (
    _ENTRY,
    _check,
    _conductor,
    _make_dirty,
    _relays,
    _report,
    build_pushed_clone,
)
from test_conductor_core import _FakeChatroomMcp, _ScriptedDispatcher
from test_loop_runner import (
    _CONDUCTOR_ROSTER,
    _exec_caps,
    _FakeChatroom,
    _FakeMcp,
    _naysayer_caps,
    _proposer_caps,
    _StubAdapter,
)

from spirrow_mindwire.cli import main as cli_main
from spirrow_mindwire.clone_guard import DirtyCloneError
from spirrow_mindwire.conductor.continuation import render_relay
from spirrow_mindwire.conductor.continuation_ledger import (
    AttemptOutcome,
    LedgerKind,
    read_ledger,
    record_admitted,
    record_attempt,
    report_text,
    summarize,
)
from spirrow_mindwire.conductor.core import CONDUCTOR_RELAY_AUTHOR, Conductor
from spirrow_mindwire.config import (
    ConductorConfig,
    MindwireSettings,
    PathsConfig,
    Stage3LoopConfig,
    resolve_continuation_ledger_path,
)
from spirrow_mindwire.loop_runner import build_conductor
from spirrow_mindwire.value_objects import Role

# ------------------------------------------------------------------ #13 continue_cap config


@pytest.fixture
def pushed_clone(tmp_path: Path) -> Path:
    return build_pushed_clone(tmp_path)


def test_cap_default_when_project_has_no_key() -> None:
    assert ConductorConfig().continue_cap_for("spirrow-mindwire") is None


def test_cap_override_for_project() -> None:
    cfg = ConductorConfig(continue_cap={"spirrow-verimend": 2})
    assert cfg.continue_cap_for("spirrow-verimend") == 2
    assert cfg.continue_cap_for("spirrow-mindwire") is None  # another project's key is not ours


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "3"])
def test_cap_invalid_value_refuses_config(bad: object) -> None:
    """msg-7973 Obj-1-1: positive integers only; nothing coerced, nothing falls back to 3."""
    with pytest.raises(ValidationError, match="positive integer"):
        ConductorConfig.model_validate({"continue_cap": {"p": bad}})


@pytest.mark.parametrize("bad", [2.0, False])
def test_cap_integral_float_and_false_are_refused_too(bad: object) -> None:
    with pytest.raises(ValidationError, match="positive integer"):
        ConductorConfig.model_validate({"continue_cap": {"p": bad}})


def _settings(
    tmp_path: Path, *, project: str, caps: dict[str, int] | None = None
) -> MindwireSettings:
    return MindwireSettings(
        paths=PathsConfig(data_dir=tmp_path / "data"),
        loop=Stage3LoopConfig(repo_dir=tmp_path, project=project),
        conductor=ConductorConfig(
            task_thread_id="T-cond",
            roster=_CONDUCTOR_ROSTER,
            naysayer_identity="Einstein",
            continue_cap=caps or {},
        ),
    )


def _build(settings: MindwireSettings) -> Conductor:
    return build_conductor(
        settings,
        mcp=_FakeMcp(_FakeChatroom()),
        proposer=_StubAdapter("p", _proposer_caps()),
        implementer=_StubAdapter("i", _exec_caps()),
        naysayer=_StubAdapter("n", _naysayer_caps()),
    ).conductor


@pytest.mark.parametrize(
    ("project", "caps", "expected"),
    [
        ("spirrow-mindwire", None, 3),
        ("spirrow-verimend", {"spirrow-verimend": 2}, 2),
        ("spirrow-mindwire", {"spirrow-verimend": 2}, 3),
    ],
)
def test_build_conductor_wires_project_cap_and_ledger(
    tmp_path: Path, project: str, caps: dict[str, int] | None, expected: int
) -> None:
    settings = _settings(tmp_path, project=project, caps=caps)
    cond = _build(settings)
    assert cond._continue_cap == expected
    assert cond._continuation_ledger_path == resolve_continuation_ledger_path(settings)
    assert cond._continuation_ledger_path == (
        tmp_path / "data" / "state" / "continuation_ledger.jsonl"
    )


# ---------------------------------------------------------- #20 the ledger, via the conductor


def _ledgered(
    mcp: _FakeChatroomMcp,
    disp: _ScriptedDispatcher,
    ledger: Path,
    **kw: object,
) -> Conductor:
    cond = _conductor(mcp, disp, **kw)  # type: ignore[arg-type]
    cond._continuation_ledger_path = ledger
    return cond


def _relay_head(mcp: _FakeChatroomMcp) -> None:
    """Entry, CONTINUE report, then the relay the conductor posted for it, left at the head."""
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    mcp.seed(
        author=CONDUCTOR_RELAY_AUTHOR,
        content=render_relay(n=1, cap=3, continue_body="step 2 of 3", implementer="Heisenberg"),
    )


def _kinds(ledger: Path) -> list[tuple[str, str]]:
    return [(r["kind"], r.get("outcome", "")) for r in read_ledger(ledger).rows]


@pytest.mark.anyio
async def test_report_path_records_admitted_with_relay_key(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})

    await _ledgered(mcp, disp, ledger).run()

    rows = read_ledger(ledger).rows
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == LedgerKind.ADMITTED
    assert len(_relays(mcp)) == 1
    assert row["relay_msg_id"] == disp.dispatches[0][1] == "m3"  # the relay it was dispatched on
    assert row["report_msg_id"] == "m2"
    assert (row["n"], row["cap"]) == (1, 3)
    assert row["thread"] and row["project"]


@pytest.mark.anyio
async def test_same_relay_recovered_twice_is_one_admitted(tmp_path: Path) -> None:
    """msg-7973 Obj-1-2: the report path admits relay m3; two restarts pick m3 up again from the
    head. Still exactly one ``continue_admitted`` row."""
    ledger = tmp_path / "l.jsonl"
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})
    await _ledgered(mcp, disp, ledger).run()

    for _ in range(2):
        again = _FakeChatroomMcp()
        _relay_head(again)
        redisp = _ScriptedDispatcher(again, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})
        await _ledgered(again, redisp, ledger).run()
        assert redisp.dispatches[0] == (Role.IMPLEMENTER, "m3")

    assert _kinds(ledger) == [(LedgerKind.ADMITTED, "")]


@pytest.mark.anyio
async def test_dirty_report_is_an_attempt_not_admitted(tmp_path: Path, pushed_clone: Path) -> None:
    _make_dirty(pushed_clone, "unpushed")
    ledger = tmp_path / "l.jsonl"
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    disp = _ScriptedDispatcher(mcp, {})

    with pytest.raises(DirtyCloneError):
        await _ledgered(mcp, disp, ledger, clone_check=_check(pushed_clone)).run()

    rows = read_ledger(ledger).rows
    assert _kinds(ledger) == [(LedgerKind.ATTEMPT, AttemptOutcome.DIRTY)]
    assert rows[0]["msg_id"] == "m2"
    assert rows[0]["clone"] == "unpushed"


@pytest.mark.anyio
async def test_recovered_relay_on_dirty_clone_is_relay_recovered_dirty(
    tmp_path: Path, pushed_clone: Path
) -> None:
    _make_dirty(pushed_clone, "uncommitted")
    ledger = tmp_path / "l.jsonl"
    mcp = _FakeChatroomMcp()
    _relay_head(mcp)
    disp = _ScriptedDispatcher(mcp, {})

    with pytest.raises(DirtyCloneError):
        await _ledgered(mcp, disp, ledger, clone_check=_check(pushed_clone)).run()

    assert _kinds(ledger) == [(LedgerKind.ATTEMPT, AttemptOutcome.RELAY_RECOVERED_DIRTY)]
    assert read_ledger(ledger).rows[0]["msg_id"] == "m3"


@pytest.mark.anyio
async def test_cap_is_an_attempt_not_admitted(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    for n in (1, 2, 3):
        mcp.seed(author="Heisenberg", content=_report(f"part {n + 1}"))
        mcp.seed(
            author=CONDUCTOR_RELAY_AUTHOR,
            content=render_relay(
                n=n, cap=3, continue_body=f"part {n + 1}", implementer="Heisenberg"
            ),
        )
    mcp.seed(author="Heisenberg", content=_report("part 5"))
    disp = _ScriptedDispatcher(mcp, {Role.PROPOSER: ["re-split\n\nNEXT: human"]})

    await _ledgered(mcp, disp, ledger).run()

    assert "continue_cap (4/3)" in _relays(mcp)[0]["content"]
    rows = read_ledger(ledger).rows
    assert _kinds(ledger) == [(LedgerKind.ATTEMPT, AttemptOutcome.CAP)]
    assert (rows[0]["n"], rows[0]["cap"], rows[0]["msg_id"]) == (4, 3, "m8")


@pytest.mark.anyio
async def test_unwritable_ledger_does_not_change_routing(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    mcp = _FakeChatroomMcp()
    mcp.seed(author="Einstein", content=_ENTRY)
    mcp.seed(author="Heisenberg", content=_report())
    disp = _ScriptedDispatcher(mcp, {Role.IMPLEMENTER: ["done\n\nNEXT: Bohr"]})

    await _ledgered(mcp, disp, blocker / "l.jsonl").run()

    assert disp.dispatches[0] == (Role.IMPLEMENTER, "m3")


# ------------------------------------------------------------------ the weekly view


_NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _seed_ledger(ledger: Path) -> None:
    recent = _NOW - timedelta(days=1)
    for i, n in enumerate((1, 2, 3)):
        record_admitted(
            ledger,
            project="p",
            thread="T-a",
            relay_msg_id=f"r{i}",
            report_msg_id=f"q{i}",
            n=n,
            cap=3,
            at=recent,
        )
    # a duplicate of r0 is not written
    assert not record_admitted(
        ledger, project="p", thread="T-a", relay_msg_id="r0", report_msg_id="q0", n=1, cap=3
    )
    record_attempt(
        ledger,
        project="p",
        thread="T-a",
        msg_id="q9",
        outcome=AttemptOutcome.CAP,
        n=4,
        cap=3,
        at=recent,
    )
    record_attempt(
        ledger,
        project="p",
        thread="T-b",
        msg_id="x1",
        outcome=AttemptOutcome.DIRTY,
        n=1,
        cap=3,
        clone="unpushed",
        at=recent,
    )
    record_attempt(
        ledger,
        project="p",
        thread="T-b",
        msg_id="x2",
        outcome=AttemptOutcome.RELAY_RECOVERED_DIRTY,
        n=1,
        cap=3,
        at=recent,
    )
    # outside the 7-day window
    record_admitted(
        ledger,
        project="p",
        thread="T-old",
        relay_msg_id="o1",
        report_msg_id="o0",
        n=1,
        cap=3,
        at=_NOW - timedelta(days=30),
    )


def test_summary_matches_ledger_counts(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    _seed_ledger(ledger)
    rows = read_ledger(ledger).rows
    since = _NOW - timedelta(days=7)
    in_window = [r for r in rows if datetime.fromisoformat(r["at"]) >= since]

    summaries, undated = summarize(rows, since=since)

    assert undated == 0
    assert [(s.project, s.thread) for s in summaries] == [("p", "T-a"), ("p", "T-b")]
    for s in summaries:
        mine = [r for r in in_window if r["thread"] == s.thread]
        assert s.admitted == sum(r["kind"] == LedgerKind.ADMITTED for r in mine)
        assert s.cap_hits == sum(r.get("outcome") == AttemptOutcome.CAP for r in mine)
        assert s.dirty == sum(r.get("outcome") == AttemptOutcome.DIRTY for r in mine)
        assert s.relay_recovered_dirty == sum(
            r.get("outcome") == AttemptOutcome.RELAY_RECOVERED_DIRTY for r in mine
        )
    a, b = summaries
    assert (a.admitted, a.max_n, a.cap_hits) == (3, 3, 1)
    assert (b.dirty, b.relay_recovered_dirty, b.admitted) == (1, 1, 0)


def test_report_text_lines_and_malformed_count(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    _seed_ledger(ledger)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({"kind": "continue_admitted"}) + "\n")  # no key fields

    text = report_text(ledger, days=7, now=_NOW)

    lines = text.splitlines()
    assert lines[0].startswith("continue-report since=")
    assert "threads=2 malformed=2 undated=0" in lines[0]
    assert lines[1] == "p/T-a admitted=3 max_n=3 cap_hits=1 dirty=0 relay_recovered_dirty=0"
    assert lines[2] == "p/T-b admitted=0 max_n=0 cap_hits=0 dirty=1 relay_recovered_dirty=1"
    assert lines[3] == "total admitted=3 cap_hits=1 dirty=1 relay_recovered_dirty=1"


def test_missing_ledger_is_empty_report(tmp_path: Path) -> None:
    text = report_text(tmp_path / "absent.jsonl", days=7, now=_NOW)
    assert "threads=0 malformed=0" in text
    assert text.splitlines()[-1] == "total admitted=0 cap_hits=0 dirty=0 relay_recovered_dirty=0"


def test_cli_continue_report_reads_the_configured_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    config = tmp_path / "mindwire.toml"
    config.write_text(
        f'schema_version = 1\n\n[paths]\ndata_dir = "{data.as_posix()}"\n', encoding="utf-8"
    )
    record_admitted(
        data / "state" / "continuation_ledger.jsonl",
        project="p",
        thread="T-a",
        relay_msg_id="r1",
        report_msg_id="q1",
        n=1,
        cap=3,
    )

    with pytest.raises(SystemExit) as ei:
        cli_main(["continue-report", "--config", str(config)])

    assert ei.value.code == 0
    assert "p/T-a admitted=1 max_n=1" in capsys.readouterr().out


# --- PR #521 gate round 1 -------------------------------------------------------------------


@pytest.mark.parametrize("outcome", [[], {}, ["dirty"], {"dirty": 1}, 1, None])
def test_non_string_outcome_is_malformed_not_a_crash(tmp_path: Path, outcome: object) -> None:
    """Obj-1-1: an unhashable (or otherwise non-string) outcome marks the row malformed."""
    ledger = tmp_path / "l.jsonl"
    bad = {"kind": "continue_attempt", "project": "p", "thread": "T-a", "msg_id": "m-1"}
    bad["outcome"] = outcome  # type: ignore[assignment]
    ledger.write_text(json.dumps(bad) + "\n", encoding="utf-8")

    assert read_ledger(ledger).malformed == 1
    assert "threads=0 malformed=1" in report_text(ledger, days=7, now=_NOW)
    # ... and the conductor's next write still works, since every append reads first.
    assert record_attempt(
        ledger, project="p", thread="T-a", msg_id="m-1", outcome=AttemptOutcome.DIRTY, n=1, cap=3
    )
    read = read_ledger(ledger)
    assert (len(read.rows), read.malformed) == (1, 1)


@pytest.mark.parametrize(
    "fragment",
    [
        '{"kind": "continue_admitted", "project": "p", "thr',
        # cut off inside a multi-byte character (the ledger is written with ensure_ascii=False)
        '{"kind": "continue_admitted", "thread": "T-例'.encode()[:-1],
    ],
)
def test_append_after_truncated_tail_keeps_the_new_row(
    tmp_path: Path, fragment: str | bytes
) -> None:
    """Obj-1-2: a truncated last line stays one malformed line; the next row is still readable."""
    ledger = tmp_path / "l.jsonl"
    record_admitted(
        ledger, project="p", thread="T-a", relay_msg_id="r-1", report_msg_id="m-1", n=1, cap=3
    )
    with ledger.open("ab") as fh:
        fh.write(fragment if isinstance(fragment, bytes) else fragment.encode())

    assert record_admitted(
        ledger, project="p", thread="T-a", relay_msg_id="r-2", report_msg_id="m-2", n=2, cap=3
    )

    read = read_ledger(ledger)
    assert read.malformed == 1
    assert [r["relay_msg_id"] for r in read.rows] == ["r-1", "r-2"]
    assert ledger.read_bytes().endswith(b"\n")
