"""Tests for :mod:`spirrow_mindwire.tier_c_decisions_log`.

Two things this suite pins:

1. The JSONL row shape — every kind serialises with ``kind`` at the top
   level plus the payload flattened one level, so ``jq '.kind'`` and
   ``jq '.reason'`` both work. This is the on-disk contract the
   ``/decisions`` dashboard reads; any drift would silently break
   aggregation.

2. The RETRY store behaviour — a BOUNCED row makes an UUID unresolved
   for its author; a subsequent RETRY_ADMIT with the same UUID resolves
   it. This is the rule the admission gate depends on to prevent
   infinite retry loops (msg-3648 v1 §2).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from spirrow_mindwire.tier_c_admission_gate import (
    LogEntry,
    LogKind,
    decide_admission,
)
from spirrow_mindwire.tier_c_decisions_log import (
    append_log_entries,
    append_log_entry,
    build_retry_lookup,
)

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def _read_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


class TestAppend:
    def test_row_shape_is_flat(self, tmp_path: Path) -> None:
        entry = LogEntry(
            kind=LogKind.BOUNCED,
            payload={"ts": NOW.isoformat(), "author": "alice", "reason": "no-label"},
        )
        log = tmp_path / "decisions.jsonl"
        append_log_entry(log, entry, thread="T-x", msg_id="msg-1")
        rows = _read_rows(log)
        assert rows == [
            {
                "author": "alice",
                "kind": "BOUNCED",
                "msg_id": "msg-1",
                "reason": "no-label",
                "thread": "T-x",
                "ts": NOW.isoformat(),
            }
        ]

    def test_thread_and_msg_id_optional(self, tmp_path: Path) -> None:
        entry = LogEntry(
            kind=LogKind.ADMIT_UNSURE,
            payload={"ts": NOW.isoformat(), "author": "alice", "label": "unsure:goal?"},
        )
        log = tmp_path / "decisions.jsonl"
        append_log_entry(log, entry)
        row = _read_rows(log)[0]
        assert "thread" not in row
        assert "msg_id" not in row

    def test_append_creates_parent_dirs(self, tmp_path: Path) -> None:
        entry = LogEntry(
            kind=LogKind.BOUNCED,
            payload={"ts": NOW.isoformat(), "author": "alice"},
        )
        log = tmp_path / "state" / "logs" / "decisions.jsonl"
        append_log_entry(log, entry)
        assert log.exists()

    def test_append_entries_writes_all_in_order(self, tmp_path: Path) -> None:
        entries = [
            LogEntry(
                kind=LogKind.LABEL_MIGRATION,
                payload={"ts": NOW.isoformat(), "author": "alice", "from": "scope", "to": "goal"},
            ),
            LogEntry(
                kind=LogKind.RETRY_ADMIT,
                payload={"ts": NOW.isoformat(), "author": "alice", "retry_uuid": "u1"},
            ),
        ]
        log = tmp_path / "decisions.jsonl"
        append_log_entries(log, entries)
        kinds = [row["kind"] for row in _read_rows(log)]
        assert kinds == ["LABEL_MIGRATION", "RETRY_ADMIT"]


class TestRetryLookup:
    def test_empty_log_returns_false(self, tmp_path: Path) -> None:
        lookup = build_retry_lookup(tmp_path / "decisions.jsonl")
        assert lookup("any-uuid", "alice") is False

    def test_bounced_uuid_matches_same_author(self, tmp_path: Path) -> None:
        log = tmp_path / "decisions.jsonl"
        append_log_entry(
            log,
            LogEntry(
                kind=LogKind.BOUNCED,
                payload={
                    "ts": NOW.isoformat(),
                    "author": "alice",
                    "retry_uuid": "u1",
                    "reason": "no-label",
                },
            ),
        )
        lookup = build_retry_lookup(log)
        assert lookup("u1", "alice") is True
        # Wrong author does not match — bounces are scoped to their author.
        assert lookup("u1", "bob") is False

    def test_retry_admit_resolves_the_bounce(self, tmp_path: Path) -> None:
        log = tmp_path / "decisions.jsonl"
        append_log_entries(
            log,
            [
                LogEntry(
                    kind=LogKind.BOUNCED,
                    payload={
                        "ts": NOW.isoformat(),
                        "author": "alice",
                        "retry_uuid": "u1",
                        "reason": "no-label",
                    },
                ),
                LogEntry(
                    kind=LogKind.RETRY_ADMIT,
                    payload={
                        "ts": NOW.isoformat(),
                        "author": "alice",
                        "retry_uuid": "u1",
                        "reason": "label_corrected",
                    },
                ),
            ],
        )
        lookup = build_retry_lookup(log)
        assert lookup("u1", "alice") is False

    def test_unrelated_uuid_does_not_match(self, tmp_path: Path) -> None:
        log = tmp_path / "decisions.jsonl"
        append_log_entry(
            log,
            LogEntry(
                kind=LogKind.BOUNCED,
                payload={
                    "ts": NOW.isoformat(),
                    "author": "alice",
                    "retry_uuid": "u1",
                    "reason": "no-label",
                },
            ),
        )
        lookup = build_retry_lookup(log)
        assert lookup("u-other", "alice") is False

    def test_reused_uuid_after_admit_reopens_state(self, tmp_path: Path) -> None:
        """Regression pin for PR-gate objection #322-1 (BLOCKING).

        A UUID may be reused across independent bounce/admit cycles
        (short-token spaces collide; sequence counters wrap). The
        lookup must reflect the LAST observation, not the first
        ``RETRY_ADMIT`` it stumbles onto. Before the fix, forward
        iteration returned ``False`` early on the resolved cycle and
        never reached the fresh ``BOUNCED`` — trapping the author in
        an infinite loop.
        """
        log = tmp_path / "decisions.jsonl"
        append_log_entries(
            log,
            [
                # First cycle: bounce, then admit — resolved.
                LogEntry(
                    kind=LogKind.BOUNCED,
                    payload={
                        "ts": NOW.isoformat(),
                        "author": "alice",
                        "retry_uuid": "u-reused",
                        "reason": "no-label",
                    },
                ),
                LogEntry(
                    kind=LogKind.RETRY_ADMIT,
                    payload={
                        "ts": NOW.isoformat(),
                        "author": "alice",
                        "retry_uuid": "u-reused",
                        "reason": "label_corrected",
                    },
                ),
                # Second cycle: same UUID reused for a fresh bounce.
                LogEntry(
                    kind=LogKind.BOUNCED,
                    payload={
                        "ts": NOW.isoformat(),
                        "author": "alice",
                        "retry_uuid": "u-reused",
                        "reason": "unknown-label",
                    },
                ),
            ],
        )
        # The LAST event for this UUID is a fresh BOUNCED ∴ unresolved.
        assert build_retry_lookup(log)("u-reused", "alice") is True

        # And a second RETRY_ADMIT closes the second cycle again.
        append_log_entry(
            log,
            LogEntry(
                kind=LogKind.RETRY_ADMIT,
                payload={
                    "ts": NOW.isoformat(),
                    "author": "alice",
                    "retry_uuid": "u-reused",
                    "reason": "label_corrected",
                },
            ),
        )
        assert build_retry_lookup(log)("u-reused", "alice") is False


class TestGateEndToEndAgainstLog:
    """Small integration: run the admission gate against a real JSONL
    RETRY store to confirm the two modules compose correctly.

    This is the shape the caller / infrastructure will actually use:
    * append a BOUNCED row when the gate first rejected the author.
    * on retry, build a fresh lookup from the log path and pass it in.
    * append the returned entries.
    """

    def test_bounce_then_retry_admits_on_the_second_call(self, tmp_path: Path) -> None:
        log = tmp_path / "decisions.jsonl"

        # First call: no label → BOUNCE. Persist the bounce.
        no_lookup = build_retry_lookup(log)
        first = decide_admission(
            body="NEXT: human\n",
            author="alice",
            retry_lookup=no_lookup,
            now=NOW,
        )
        assert first.verdict.value == "bounce"
        # Simulate the transport stamping the retry_uuid on the bounce row.
        bounced_entry = first.log_entries[0]
        payload = dict(bounced_entry.payload)
        payload["retry_uuid"] = "u1"
        append_log_entry(log, LogEntry(kind=bounced_entry.kind, payload=payload))

        # Second call: the author sends RETRY: u1 with a corrected label.
        retry_lookup = build_retry_lookup(log)
        second = decide_admission(
            body="RETRY: u1\nTIER-C: goal\nNEXT: human\n",
            author="alice",
            retry_lookup=retry_lookup,
            now=NOW,
        )
        assert second.verdict.value == "admit"
        assert second.rule == "R0-RETRY:label_corrected"

        # Persist the retry admit — the uuid is now resolved.
        append_log_entries(log, second.log_entries)
        assert build_retry_lookup(log)("u1", "alice") is False
